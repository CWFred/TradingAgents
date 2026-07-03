"""Market-close daily summary: computes a one-line + full-body summary from
the journal + broker and records a single daily_summary event per day."""
from __future__ import annotations

from datetime import datetime, timezone

from ops.trading_time import trading_day_start


def emit_daily_summary(journal, broker, *, now: datetime | None = None) -> bool:
    when = now if now is not None else datetime.now(timezone.utc)
    if journal.has_event_today("daily_summary", now=when):
        return False

    equity = broker.get_equity()
    positions = [p for p in broker.get_positions() if p.symbol.upper() != "SPOT"]
    start = journal.get_latest_equity_snapshot(kind="open_day")
    day_pnl = (equity - start.equity) if start is not None else None

    day_str = when.date().isoformat()
    # ET trading-day boundary, matching has_event_today's idempotency guard
    # (M7): a UTC-calendar-date comparison mis-buckets fills recorded in the
    # UTC-evening/ET-morning gap into the wrong trading day.
    day_start = trading_day_start(when)
    fills_today = [
        f for f in journal.read_fills()
        if f["at"] >= day_start
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
    payload = {
        "headline": headline,
        "body": "\n".join(lines),
        "equity": str(equity),
        "n_fills_today": len(fills_today),
    }
    journal.record_event("daily_summary", payload)
    return True
