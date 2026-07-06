"""Unit tests for PriceContext (no yfinance)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from ops.research.prices import PriceContext

pytestmark = pytest.mark.unit


def _ctx():
    # 100 consecutive weekdays ending Tuesday 2026-06-30. Values encode
    # recency: the NEWEST day was inserted first, so newer date = smaller
    # value (newest = 10.00, next 10.01, ...).
    closes = {}
    d = date(2026, 6, 30)
    while len(closes) < 100:
        if d.weekday() < 5:
            closes[d] = Decimal("10") + Decimal(len(closes)) / 100
        d -= timedelta(days=1)
    return PriceContext(closes=closes)


def test_recent_closes_returns_last_n_trading_days_oldest_first():
    ctx = _ctx()
    closes = ctx.recent_closes(asof=date(2026, 6, 30), days=60)
    assert len(closes) == 60
    # Oldest-first: values strictly descend toward the newest close (10.00).
    assert closes == sorted(closes, reverse=True)
    assert closes[-1] == Decimal("10.00")


def test_recent_closes_excludes_dates_after_asof():
    ctx = _ctx()
    closes = ctx.recent_closes(asof=date(2026, 6, 15), days=60)
    assert len(closes) == 60
    # 2026-06-15 is the 12th-newest trading day in the fixture (index 11),
    # so with everything after asof excluded the newest value is 10.11.
    assert closes[-1] == Decimal("10.11")


def test_close_on_or_before_picks_prior_trading_day():
    ctx = _ctx()
    # 2026-06-28 is a Sunday; the prior trading day is Friday 2026-06-26.
    assert ctx.close_on_or_before(date(2026, 6, 28)) == ctx.closes[date(2026, 6, 26)]


def test_close_on_or_before_respects_max_gap():
    ctx = _ctx()
    oldest = min(ctx.closes)
    assert ctx.close_on_or_before(oldest - timedelta(days=30)) is None


def test_unadjusted_close_reverses_forward_split():
    # 10:1 forward split on 2026-06-01. Yahoo shows pre-split closes divided
    # by 10; as-traded price was 10x the adjusted figure.
    ctx = PriceContext(
        closes={date(2026, 5, 15): Decimal("10"), date(2026, 6, 15): Decimal("95")},
        splits={date(2026, 6, 1): Decimal("10")},
    )
    assert ctx.unadjusted_close_on_or_before(date(2026, 5, 15)) == Decimal("100")
    # After the split there is nothing to undo.
    assert ctx.unadjusted_close_on_or_before(date(2026, 6, 15)) == Decimal("95")


def test_unadjusted_close_reverses_reverse_split():
    # 1:10 reverse split (ratio 0.1): busted small cap trading at $1 becomes
    # $10; Yahoo scales history UP; as-traded was the low price.
    ctx = PriceContext(
        closes={date(2026, 5, 15): Decimal("10")},
        splits={date(2026, 6, 1): Decimal("0.1")},
    )
    assert ctx.unadjusted_close_on_or_before(date(2026, 5, 15)) == Decimal("1.0")


def test_unadjusted_equals_adjusted_without_splits():
    ctx = PriceContext(closes={date(2026, 5, 15): Decimal("42")})
    assert ctx.unadjusted_close_on_or_before(date(2026, 5, 15)) == Decimal("42")


def test_multiple_future_splits_compound():
    ctx = PriceContext(
        closes={date(2024, 12, 31): Decimal("5")},
        splits={date(2025, 6, 1): Decimal("2"), date(2026, 6, 1): Decimal("3")},
    )
    assert ctx.unadjusted_close_on_or_before(date(2024, 12, 31)) == Decimal("30")


def test_era_end_anchors_split_factor_to_that_era():
    # 10:1 split on 2026-03-15, AFTER the fiscal year end 2025-12-31 but
    # before asof: today's $50 close is worth $500 in FY-2025 share basis.
    ctx = PriceContext(
        closes={date(2026, 7, 3): Decimal("50")},
        splits={date(2026, 3, 15): Decimal("10")},
    )
    assert ctx.unadjusted_close_on_or_before(
        date(2026, 7, 3), era_end=date(2025, 12, 31)) == Decimal("500")
    # An era ending after the split has nothing to undo.
    assert ctx.unadjusted_close_on_or_before(
        date(2026, 7, 3), era_end=date(2026, 4, 1)) == Decimal("50")
