"""ops/trading_time.py: ET-calendar day/week boundaries returned as
tz-aware UTC instants (M7 — see docs/superpowers/plans/2026-07-02-ops-review-remaining-fixes.md).

Reference points (real NYSE calendar, EDT = UTC-4 in July):
  2026-07-06 (Monday)    -- trading day
  2026-07-05 (Sunday)    -- previous trading week
"""
from datetime import datetime, timezone

import pytest

from ops.trading_time import TRADING_TZ, trading_day_start, trading_week_start


def test_trading_tz_is_america_new_york():
    assert str(TRADING_TZ) == "America/New_York"


def test_trading_day_start_returns_utc_midnight_et_as_utc_instant():
    # 2026-07-06 10:30 ET (mid-day) -> ET midnight of the same day, in UTC.
    now = datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc)  # 10:30 ET (EDT, UTC-4)
    start = trading_day_start(now)
    assert start == datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)  # 00:00 ET
    assert start.tzinfo is not None


def test_trading_day_start_event_at_2100_et_counts_as_today_for_2200_et_check():
    """Spec case (M7): an event at 21:00 ET on 2026-07-06 must count as
    'today' relative to a 22:00 ET check the same trading day."""
    event_utc = datetime(2026, 7, 6, 21, 0, tzinfo=TRADING_TZ).astimezone(timezone.utc)
    check_utc = datetime(2026, 7, 6, 22, 0, tzinfo=TRADING_TZ).astimezone(timezone.utc)
    start = trading_day_start(check_utc)
    assert event_utc >= start


def test_trading_day_start_early_event_stays_today_across_the_utc_midnight_rollover():
    """Real regression case: under the old UTC-midnight boundary, an event
    from earlier in the ET trading day (before the 8pm-ET/UTC-midnight
    crossover) fell OUT of 'today' once a later same-day ET check crossed
    into the next UTC calendar date. That must not happen anymore."""
    # Event: Monday 2026-07-06 09:00 ET (13:00 UTC).
    event_utc = datetime(2026, 7, 6, 9, 0, tzinfo=TRADING_TZ).astimezone(timezone.utc)
    # Check: same Monday, 21:00 ET -> already past UTC midnight (Tuesday 01:00 UTC).
    check_utc = datetime(2026, 7, 6, 21, 0, tzinfo=TRADING_TZ).astimezone(timezone.utc)
    start = trading_day_start(check_utc)
    assert event_utc >= start, "same ET trading day event must still count as today"


def test_trading_day_start_previous_day_event_excluded():
    now = datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc)  # 10:30 ET Monday
    prior_day = datetime(2026, 7, 5, 20, 0, tzinfo=timezone.utc)  # Sunday
    start = trading_day_start(now)
    assert prior_day < start


def test_trading_week_start_returns_monday_midnight_et_as_utc_instant():
    # Thursday 2026-07-02 -> Monday 2026-06-29 00:00 ET (04:00 UTC).
    now = datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
    start = trading_week_start(now)
    assert start == datetime(2026, 6, 29, 4, 0, tzinfo=timezone.utc)


def test_trading_week_start_sunday_evening_belongs_to_previous_week():
    """Spec case (M7): Sunday 20:00 ET belongs to the PREVIOUS trading week,
    not the UTC-Monday-derived one. Sunday 2026-07-05 20:00 ET is exactly
    Monday 2026-07-06 00:00 UTC -- under the old UTC-Monday convention this
    event lands inside 'since Monday'; under ET calendar semantics it must
    not."""
    sunday_evening_et = datetime(2026, 7, 5, 20, 0, tzinfo=TRADING_TZ)
    sunday_evening_utc = sunday_evening_et.astimezone(timezone.utc)
    assert sunday_evening_utc == datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)

    # A check on the new trading week, e.g. Monday 10:00 ET.
    check_utc = datetime(2026, 7, 6, 10, 0, tzinfo=TRADING_TZ).astimezone(timezone.utc)
    week_start = trading_week_start(check_utc)
    assert sunday_evening_utc < week_start, (
        "Sunday-evening ET event must fall before this week's ET start"
    )


def test_trading_week_start_naive_datetime_rejected():
    with pytest.raises(ValueError, match="naive"):
        trading_day_start(datetime(2026, 7, 6, 10, 0))
    with pytest.raises(ValueError, match="naive"):
        trading_week_start(datetime(2026, 7, 6, 10, 0))


def test_trading_day_start_dst_boundary_sanity():
    """DST ends 2026-11-01 (fall back, EST = UTC-5 begins). A trading day
    boundary either side of the transition must reflect the correct offset
    -- ZoneInfo handles this; we assert on the resulting UTC instant to
    catch any hand-rolled-offset regression."""
    before_dst_end = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)  # Mon, EDT (UTC-4)
    after_dst_end = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)    # Mon, EST (UTC-5)
    assert trading_day_start(before_dst_end) == datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc)
    assert trading_day_start(after_dst_end) == datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)


from datetime import date

from ops.trading_time import trading_days_back, trading_days_between


def test_trading_days_between_same_week():
    # Mon 2026-07-06 -> Fri 2026-07-10: Tue, Wed, Thu, Fri = 4
    assert trading_days_between(date(2026, 7, 6), date(2026, 7, 10)) == 4


def test_trading_days_between_spans_weekend():
    # Fri 2026-07-10 -> Mon 2026-07-13: just Monday = 1
    assert trading_days_between(date(2026, 7, 10), date(2026, 7, 13)) == 1


def test_trading_days_between_zero_for_same_or_reversed():
    assert trading_days_between(date(2026, 7, 6), date(2026, 7, 6)) == 0
    assert trading_days_between(date(2026, 7, 10), date(2026, 7, 6)) == 0


def test_trading_days_back_skips_weekend():
    # 2 trading days before Mon 2026-07-13 = Thu 2026-07-09
    assert trading_days_back(date(2026, 7, 13), 2) == date(2026, 7, 9)
