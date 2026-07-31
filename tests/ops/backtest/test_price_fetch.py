import time
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ops.backtest.price_fetch import _with_deadline, yfinance_bar_fetcher


def _frame():
    idx = pd.to_datetime(["2025-06-02", "2025-06-03"])
    return pd.DataFrame(
        {
            "Open": [10.0, 20.0],
            "High": [11.0, 21.0],
            "Low": [9.0, 19.0],
            "Close": [10.0, 20.0],
            "Adj Close": [5.0, 20.0],
            "Volume": [100, 200],
            "Dividends": [0.0, 0.5],
            "Stock Splits": [0.0, 2.0],
        },
        index=idx,
    )


def test_maps_yfinance_frame_to_pricebars():
    bars = yfinance_bar_fetcher(
        "ACMR",
        date(2025, 6, 2),
        date(2025, 6, 3),
        history_fn=lambda s, a, b: _frame(),
    )
    assert [b.session for b in bars] == [date(2025, 6, 2), date(2025, 6, 3)]
    b0 = bars[0]
    assert b0.symbol == "ACMR" and b0.provider == "yfinance"
    assert b0.close == Decimal("10") and b0.adjusted_close == Decimal("5")
    # adj ratio 5/10 applied to OHLC:
    assert b0.adjusted_open == Decimal("5") and b0.adjusted_high == Decimal("5.5")
    assert b0.split_ratio == Decimal("1")  # 0 -> 1
    assert bars[1].dividend == Decimal("0.5")
    assert bars[1].split_ratio == Decimal("2")


def test_skips_rows_with_zero_or_nan_close():
    idx = pd.to_datetime(["2025-06-02", "2025-06-03", "2025-06-04"])
    frame = pd.DataFrame(
        {
            "Open": [10.0, 20.0, 30.0],
            "High": [11.0, 21.0, 31.0],
            "Low": [9.0, 19.0, 29.0],
            "Close": [10.0, 0.0, float("nan")],
            "Adj Close": [10.0, 0.0, float("nan")],
            "Volume": [100, 200, 300],
            "Dividends": [0.0, 0.0, 0.0],
            "Stock Splits": [0.0, 0.0, 0.0],
        },
        index=idx,
    )
    bars = yfinance_bar_fetcher(
        "ACMR",
        date(2025, 6, 2),
        date(2025, 6, 4),
        history_fn=lambda s, a, b: frame,
    )
    assert [b.session for b in bars] == [date(2025, 6, 2)]


def test_missing_actions_columns_default_to_no_action():
    idx = pd.to_datetime(["2025-06-02"])
    frame = pd.DataFrame(
        {
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.0],
            "Adj Close": [10.0],
            "Volume": [100],
        },
        index=idx,
    )
    bars = yfinance_bar_fetcher(
        "ACMR",
        date(2025, 6, 2),
        date(2025, 6, 2),
        history_fn=lambda s, a, b: frame,
    )
    assert bars[0].dividend == Decimal("0")
    assert bars[0].split_ratio == Decimal("1")


def test_bars_feed_price_cache_upsert(tmp_path):
    from ops.backtest.prices import PriceCache

    bars = yfinance_bar_fetcher(
        "ACMR",
        date(2025, 6, 2),
        date(2025, 6, 3),
        history_fn=lambda s, a, b: _frame(),
    )
    cache = PriceCache(tmp_path / "px.db")
    assert cache.upsert_bars(bars) == 2


class TestWithDeadline:
    def test_returns_result_when_fast_enough(self):
        assert _with_deadline(lambda: 42, seconds=1.0) == 42

    def test_raises_timeout_error_when_fn_exceeds_deadline(self):
        def slow():
            time.sleep(0.5)
            return "too late"

        with pytest.raises(TimeoutError):
            _with_deadline(slow, seconds=0.05)

    def test_propagates_exception_raised_by_fn(self):
        def boom():
            raise ValueError("kaboom")

        with pytest.raises(ValueError, match="kaboom"):
            _with_deadline(boom, seconds=1.0)
