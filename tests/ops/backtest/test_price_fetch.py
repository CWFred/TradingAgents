import concurrent.futures
import time
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ops.backtest import price_fetch
from ops.backtest.price_fetch import (
    HISTORY_DEADLINE_SECONDS,
    _with_deadline,
    history_deadline_seconds,
    yfinance_bar_fetcher,
)


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

    def test_wraps_the_entire_retry_chain_not_one_budget_per_attempt(self):
        """A regression guard for the composition-inversion bug: a caller
        that retries internally (e.g. ``call_paced``) must be timed as ONE
        chain, not get a fresh clock on every attempt."""
        attempts = 0

        def flaky_retry_chain():
            nonlocal attempts
            for _ in range(50):
                attempts += 1
                time.sleep(0.02)
                # never succeeds within the budget; simulates a paced
                # retry loop that keeps trying past the deadline.
            return "unreachable"

        with pytest.raises(TimeoutError):
            _with_deadline(flaky_retry_chain, seconds=0.1)
        # Several attempts fired inside the single deadline window (proves
        # the deadline wraps the WHOLE chain, not a per-attempt window —
        # per-attempt would never time out since each sleep is well under
        # any per-call budget).
        assert attempts > 1

    def test_shared_executor_is_a_module_level_singleton(self, monkeypatch):
        """Bounds stranded-thread risk: _with_deadline must never construct
        a fresh ThreadPoolExecutor per call (the fd-leak mechanism from the
        prior incident) — it reuses one shared, bounded pool."""
        created = []
        real_cls = concurrent.futures.ThreadPoolExecutor

        class SpyExecutor(real_cls):
            def __init__(self, *args, **kwargs):
                created.append((args, kwargs))
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", SpyExecutor)

        executor_before = price_fetch._DEADLINE_EXECUTOR
        _with_deadline(lambda: 1, seconds=1.0)
        with pytest.raises(TimeoutError):
            _with_deadline(lambda: time.sleep(0.2), seconds=0.01)
        _with_deadline(lambda: 2, seconds=1.0)

        assert created == []  # no new executor constructed by any of the 3 calls
        assert price_fetch._DEADLINE_EXECUTOR is executor_before


class TestHistoryDeadlineSeconds:
    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("OPS_YF_DEADLINE_S", raising=False)
        assert history_deadline_seconds() == HISTORY_DEADLINE_SECONDS

    def test_env_override_respected(self, monkeypatch):
        monkeypatch.setenv("OPS_YF_DEADLINE_S", "12.5")
        assert history_deadline_seconds() == 12.5
