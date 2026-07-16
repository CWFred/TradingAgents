"""Gate-aware forecast of the next real ds4 work.

Pure read-only computation: cron facts from ops.scheduler.times, day gates
from the momentum journal, queue depths via mode=ro SQL (never the store
classes — instantiating them runs CREATE TABLE writes). Purposes describe
what the run WILL do, not just when the scheduler fires — a tick that
would no-op through its gates is not "work" and is skipped."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ops import events
from ops.dashboard.snapshot import ro_conn
from ops.journal import Journal
from ops.scheduler import times
from ops.scheduler.orchestrator import MAX_DAILY_CYCLE_ATTEMPTS

ET = ZoneInfo("America/New_York")


def _count(path: str, sql: str, params: tuple = ()) -> int:
    """ro count; a missing store is an empty queue, not an error."""
    try:
        with closing(ro_conn(path)) as conn:
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row is not None else 0
    except sqlite3.OperationalError:
        return 0


def _last_screen_run_at(path: str) -> datetime | None:
    try:
        with closing(ro_conn(path)) as conn:
            row = conn.execute(
                "SELECT created_at FROM screen_runs"
                " ORDER BY created_at DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    dt = datetime.fromisoformat(row[0])
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _next_half_hour(now_et: datetime) -> datetime:
    base = now_et.replace(second=0, microsecond=0)
    if base.minute < 30:
        return base.replace(minute=30)
    return (base + timedelta(hours=1)).replace(minute=0)


def _next_cycle_tick(now_et: datetime, calendar, *, skip_today: bool) -> datetime:
    """Next :00/:30 tick within TICK_HOUR_START..TICK_HOUR_END on a trading
    day, strictly after now (or from tomorrow when skip_today)."""
    day = now_et.date()
    for _ in range(14):  # two weeks covers any holiday cluster
        if calendar.is_trading_day(day) and not (skip_today and day == now_et.date()):
            for hour in range(times.TICK_HOUR_START, times.TICK_HOUR_END + 1):
                for minute in times.TICK_MINUTES:
                    tick = datetime(day.year, day.month, day.day, hour, minute,
                                    tzinfo=ET)
                    if tick > now_et:
                        return tick
        day += timedelta(days=1)
    raise RuntimeError("no trading day within 14 days")


def _cycle_entry(config, now_et: datetime, calendar) -> dict[str, Any]:
    try:
        with Journal(config.journal_path, readonly=True) as j:
            done_today = j.has_event_today(
                events.KIND_DAILY_CYCLE_COMPLETED, now=now_et)
            from ops.trading_time import trading_day_start
            attempts = j.count_events(
                events.KIND_DAILY_CYCLE_RUN, since=trading_day_start(now_et))
    except sqlite3.OperationalError:
        # Missing journal = no gates recorded: forecast the standard cycle.
        done_today, attempts = False, 0
    skip_today = done_today or attempts >= MAX_DAILY_CYCLE_ATTEMPTS
    at = _next_cycle_tick(now_et, calendar, skip_today=skip_today)
    if not skip_today and attempts > 0:
        purpose = (f"retry daily cycle, attempt {attempts + 1}"
                   f" of {MAX_DAILY_CYCLE_ATTEMPTS}")
    else:
        purpose = (f"daily cycle: leaderboard, exits, up to "
                   f"{config.daily_analysis_budget} analyses")
    return {"at": at.astimezone(timezone.utc), "job": "daily_cycle",
            "purpose": purpose}


def _overnight_entry(config, now_et: datetime) -> dict[str, Any]:
    deadline_h = config.research_drain_deadline_hour
    in_window = now_et.hour < deadline_h or now_et.weekday() >= 5
    if in_window:
        at = _next_half_hour(now_et)
    else:
        at = (now_et + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)

    pending_sql = "SELECT COUNT(*) FROM screen_hits WHERE status = 'pending'"
    vet_sql = "SELECT COUNT(*) FROM memos WHERE status = 'pending_vetting'"
    hits = (_count(config.screen_store_path, pending_sql)
            + _count(config.short_screen_store_path, pending_sql))
    memos = (_count(config.memo_store_path, vet_sql)
             + _count(config.short_memo_store_path, vet_sql))
    insider = _count(config.insider_signal_store_path,
                     "SELECT COUNT(*) FROM sleeve_entries WHERE memo_id = ''")

    last_screen = _last_screen_run_at(config.screen_store_path)
    interval = timedelta(days=config.research_screen_interval_days)
    screen_due = last_screen is None or (at.astimezone(timezone.utc)
                                         - last_screen) >= interval

    parts: list[str] = []
    if screen_due:
        parts.append("screen due")
    if hits:
        parts.append(f"{hits} hit(s) to research")
    if memos:
        parts.append(f"{memos} memo(s) to vet")
    if insider:
        parts.append(f"{insider} insider memo(s) to author")
    if parts:
        purpose = " · ".join(parts)
    else:
        due_date = (last_screen + interval).astimezone(ET).date()
        purpose = (f"likely idle: queues empty, screen not due until "
                   f"{due_date.isoformat()}")
    return {"at": at.astimezone(timezone.utc), "job": "overnight",
            "purpose": purpose}


def next_work(config, *, now: datetime, calendar=None) -> list[dict[str, Any]]:
    if calendar is None:
        from ops.scheduler.market_calendar import MarketCalendar
        calendar = MarketCalendar()
    now_et = now.astimezone(ET)
    entries = [
        _cycle_entry(config, now_et, calendar),
        _overnight_entry(config, now_et),
    ]
    entries.sort(key=lambda e: e["at"])
    return entries
