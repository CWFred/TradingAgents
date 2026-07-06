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
