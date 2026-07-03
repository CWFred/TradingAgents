"""Trading-calendar day/week boundary helpers (M7).

Market hours and cron triggers run in America/New_York, but every stored
timestamp in the journal stays UTC. Computing "start of today" / "start of
this week" by zeroing the hour of a UTC-aware datetime rolls the boundary at
UTC midnight -- 8pm ET (EDT) / 7pm ET (EST) -- not at ET midnight, which
mis-buckets late-evening ET events into the wrong trading day/week and can
let a Sunday-evening ET event leak into "this week"'s idempotency window.

This module is the single place that converts an ET-calendar boundary into a
tz-aware UTC instant. Every day/week boundary computation in ops/ must call
these two functions instead of rolling its own; stored timestamps and
comparisons remain UTC throughout.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TRADING_TZ = ZoneInfo("America/New_York")


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("naive datetimes are not allowed in trading_time")


def trading_day_start(now: datetime) -> datetime:
    """Start of `now`'s ET-calendar trading day, as a tz-aware UTC instant.

    `now` must be tz-aware. Converts to ET, zeroes the time-of-day, then
    converts back to UTC so DST transitions are handled by ZoneInfo rather
    than a hand-rolled offset.
    """
    _require_aware(now)
    local = now.astimezone(TRADING_TZ)
    local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(timezone.utc)


def trading_week_start(now: datetime) -> datetime:
    """Start of `now`'s ET-calendar trading week (Monday 00:00 ET), as a
    tz-aware UTC instant.
    """
    _require_aware(now)
    local = now.astimezone(TRADING_TZ)
    monday_local = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday_local.astimezone(timezone.utc)
