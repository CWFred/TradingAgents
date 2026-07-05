"""Market-close daily summary: computes a one-line + full-body summary from
the journal + broker and records a single daily_summary event per day."""
from __future__ import annotations

from datetime import datetime, timezone

from ops import events
from ops.trading_time import TRADING_TZ, trading_day_start


def emit_daily_summary(journal, broker, calendar=None, *, now: datetime | None = None) -> bool:
    when = now if now is not None else datetime.now(timezone.utc)
    # Holiday gate: the 16:05 ET cron fires mon-fri regardless of NYSE
    # holidays. is_trading_day is the right calendar question here —
    # is_open_now is ALWAYS False at 16:05 (the summary runs after close).
    if calendar is not None and not calendar.is_trading_day(
        when.astimezone(TRADING_TZ).date()
    ):
        return False
    if journal.has_event_today(events.KIND_DAILY_SUMMARY, now=when):
        return False

    equity = broker.get_equity()
    positions = [p for p in broker.get_positions() if p.symbol.upper() != "SPOT"]
    # (a) Pass since=trading_day_start to avoid stale baseline from previous day.
    day_start = trading_day_start(when)
    start = journal.get_latest_equity_snapshot(kind="open_day", since=day_start)
    day_pnl = (equity - start.equity) if start is not None else None

    day_str = when.date().isoformat()
    # (b) Filter on filled_at instead of at (journal write time).
    fills_today = [
        f for f in journal.read_fills()
        if f["filled_at"] >= day_start
    ]

    pnl_txt = f"${day_pnl}" if day_pnl is not None else "n/a"
    headline = f"{day_str}: equity ${equity}, P&L {pnl_txt}, {len(fills_today)} fill(s)"
    lines = [
        headline,
        "",
        "Open positions:",
        *[f"  {p.symbol}: qty {p.quantity} entry ${p.avg_entry_price}"
          for p in positions],
    ]
    journal.record_event(
        events.KIND_DAILY_SUMMARY,
        events.daily_summary_payload(
            headline=headline,
            body="\n".join(lines),
            equity=equity,
            n_fills_today=len(fills_today),
        ),
    )
    return True
