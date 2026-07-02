"""NYSE calendar adapter over pandas_market_calendars.

Every method accepts an optional `at`/`d` for testability; production
callers pass `None` to mean "now, UTC".
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

import pandas_market_calendars as mcal


class MarketCalendar:
    def __init__(self) -> None:
        self._cal = mcal.get_calendar("NYSE")

    def is_open_now(self, at: datetime | None = None) -> bool:
        when = at if at is not None else datetime.now(timezone.utc)
        d = when.astimezone(timezone.utc).date()
        if not self.is_trading_day(d):
            return False
        sched = self._cal.schedule(start_date=d, end_date=d)
        if sched.empty:
            return False
        open_ = sched.iloc[0]["market_open"].to_pydatetime()
        close_ = sched.iloc[0]["market_close"].to_pydatetime()
        return open_ <= when <= close_

    def is_trading_day(self, d: date) -> bool:
        return self._trading_day_cached(d.isoformat())

    @lru_cache(maxsize=1024)
    def _trading_day_cached(self, iso: str) -> bool:
        d = date.fromisoformat(iso)
        sched = self._cal.schedule(start_date=d, end_date=d)
        return not sched.empty

    def previous_close(self, at: datetime | None = None) -> datetime:
        when = at if at is not None else datetime.now(timezone.utc)
        start = (when - timedelta(days=7)).date()
        end = when.date()
        sched = self._cal.schedule(start_date=start, end_date=end)
        for row in reversed(list(sched.iterrows())):
            close_ = row[1]["market_close"].to_pydatetime()
            if close_ < when:
                return close_
        raise RuntimeError(f"no previous close within 7 days of {when}")

    def next_open(self, at: datetime | None = None) -> datetime:
        when = at if at is not None else datetime.now(timezone.utc)
        start = when.date()
        end = (when + timedelta(days=7)).date()
        sched = self._cal.schedule(start_date=start, end_date=end)
        for _, row in sched.iterrows():
            open_ = row["market_open"].to_pydatetime()
            if open_ > when:
                return open_
        raise RuntimeError(f"no next open within 7 days of {when}")
