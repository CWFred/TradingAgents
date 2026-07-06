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

from datetime import date, datetime, timedelta, timezone
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


def _is_trading_day(d: date) -> bool:
    # Mon=0..Fri=4. Holidays are not handled — same approximation as
    # ops/universe/earnings.py; a holiday merely shortens a window by a day.
    return d.weekday() < 5


def trading_days_back(asof: date, n: int) -> date:
    """The date n trading days strictly before `asof`."""
    d = asof
    counted = 0
    while counted < n:
        d -= timedelta(days=1)
        if _is_trading_day(d):
            counted += 1
    return d


def trading_days_between(start: date, end: date) -> int:
    """Trading days strictly after `start`, up to and including `end`."""
    if end <= start:
        return 0
    d, count = start, 0
    while d < end:
        d += timedelta(days=1)
        if _is_trading_day(d):
            count += 1
    return count
