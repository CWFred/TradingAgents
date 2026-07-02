"""Uses explicit `at=` params instead of freezegun — no new test dep."""
from datetime import date, datetime, timezone
import pytest
from ops.scheduler.market_calendar import MarketCalendar

# Fixed reference points chosen from a real NYSE calendar:
#   2026-07-02 (Thursday): trading day
#   2026-07-03 (Friday): market closed for July 4 observed
#   2026-07-04 (Saturday): weekend
#   2026-07-06 (Monday): trading day


def test_is_open_now_regular_hours_true():
    cal = MarketCalendar()
    at = datetime(2026, 7, 2, 14, 30, tzinfo=timezone.utc)   # 10:30 ET
    assert cal.is_open_now(at=at) is True


def test_is_open_now_pre_market_false():
    cal = MarketCalendar()
    at = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)   # 08:00 ET
    assert cal.is_open_now(at=at) is False


def test_is_open_now_weekend_false():
    cal = MarketCalendar()
    at = datetime(2026, 7, 4, 14, 30, tzinfo=timezone.utc)   # Saturday
    assert cal.is_open_now(at=at) is False


def test_is_trading_day_true():
    cal = MarketCalendar()
    assert cal.is_trading_day(date(2026, 7, 2)) is True
    assert cal.is_trading_day(date(2026, 7, 6)) is True


def test_is_trading_day_weekend_false():
    cal = MarketCalendar()
    assert cal.is_trading_day(date(2026, 7, 4)) is False


def test_next_open_from_saturday_is_next_monday_or_holiday_skipped():
    cal = MarketCalendar()
    at = datetime(2026, 7, 4, 12, tzinfo=timezone.utc)
    nxt = cal.next_open(at=at)
    assert nxt > at
    assert cal.is_trading_day(nxt.astimezone(timezone.utc).date())


def test_previous_close_is_before_at():
    cal = MarketCalendar()
    at = datetime(2026, 7, 2, 14, tzinfo=timezone.utc)
    prev = cal.previous_close(at=at)
    assert prev < at
