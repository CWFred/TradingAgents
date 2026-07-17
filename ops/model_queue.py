"""Live-first coordinator for all background DS4 memo work.

The individual sleeve databases remain the durable sources of truth.  This
module supplies scheduling policy only: live queues drain before explicitly
enqueued backtests, all work is sequential, and market/paper-trade hours are a
hard blackout.  That avoids a second payload database and preserves each
sleeve's existing retry/idempotency behavior.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from ops.work_pause import pause_state

ET = ZoneInfo("America/New_York")
DEFAULT_EVENING_START = time(16, 45)


@dataclass(frozen=True)
class QueueWindow:
    allowed: bool
    deadline: datetime | None
    reason: str


@dataclass(frozen=True)
class QueueTickResult:
    state: str
    backtest_attempted: int = 0


def background_window(
    now: datetime,
    *,
    morning_deadline_hour: int = 8,
    evening_start: time = DEFAULT_EVENING_START,
) -> QueueWindow:
    """Return the safe model-work window around paper-trading activity.

    Weekdays are available before the configured morning deadline and after
    the 16:35 sleeve/overview train plus a ten-minute buffer.  Weekends run
    continuously until Monday morning.  The current job may finish its active
    name at the deadline; callers check the boundary between names.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("queue clock must be timezone-aware")
    if not 0 <= morning_deadline_hour < 9:
        raise ValueError("morning deadline must be in 0..8")
    local = now.astimezone(ET)
    morning = local.replace(
        hour=morning_deadline_hour, minute=0, second=0, microsecond=0,
    )
    weekday = local.weekday()
    if weekday >= 5:
        deadline = morning
        while deadline.weekday() >= 5 or deadline <= local:
            deadline += timedelta(days=1)
        return QueueWindow(True, deadline, "weekend backfill")
    if local < morning:
        return QueueWindow(True, morning, "pre-market backfill")
    evening = local.replace(
        hour=evening_start.hour, minute=evening_start.minute,
        second=0, microsecond=0,
    )
    if local >= evening:
        deadline = morning + timedelta(days=1)
        while deadline.weekday() >= 5:
            deadline += timedelta(days=1)
        return QueueWindow(True, deadline, "after-hours backfill")
    return QueueWindow(False, None, "paper-trading blackout")


def coordinate_queue_tick(
    *,
    pause_path: str,
    now: datetime,
    morning_deadline_hour: int,
    run_live: Callable[[], None],
    live_pending: Callable[[], bool],
    run_backtest: Callable[[], int],
) -> QueueTickResult:
    """Drain live work first, then at most one backtest batch."""
    if pause_state(pause_path, now=now, cleanup_expired=True).paused:
        return QueueTickResult("paused")
    window = background_window(
        now, morning_deadline_hour=morning_deadline_hour,
    )
    if not window.allowed:
        return QueueTickResult("blackout")
    run_live()
    if pause_state(pause_path, cleanup_expired=True).paused:
        return QueueTickResult("paused")
    if live_pending():
        return QueueTickResult("live-pending")
    attempted = run_backtest()
    return QueueTickResult(
        "backtest" if attempted else "idle",
        backtest_attempted=attempted,
    )
