"""Backfill driver: window derivation, benchmark, per-symbol isolation.

Everything here is network-free: a fabricated ``fetcher`` returns known bars
(and can raise for a chosen symbol) and a fixed ``today`` pins the window.
"""
import time
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ops.backtest.models import BacktestCase
from ops.backtest.price_backfill import (
    BackfillSummary,
    backfill_prices,
    backfill_symbol_windows,
)
from ops.backtest.price_fetch import YfBar, _with_deadline
from ops.backtest.prices import PriceCache
from ops.backtest.store import BacktestStore
from ops.config import OpsConfig

pytestmark = pytest.mark.unit

_SLEEVE = "research"


def _seed_cases(store, symbols_asofs):
    for symbol, asof in symbols_asofs:
        store.insert_case(
            BacktestCase.create(sleeve=_SLEEVE, symbol=symbol, asof=asof)
        )


def _bar(symbol: str, day: date) -> YfBar:
    price = Decimal("100")
    return YfBar(
        symbol=symbol.strip().upper(), session=day,
        open=price, high=price + 1, low=price - 1, close=price,
        adjusted_open=price, adjusted_high=price + 1,
        adjusted_low=price - 1, adjusted_close=price,
        volume=Decimal("1000"), dividend=Decimal("0"), split_ratio=Decimal("1"),
    )


def _no_batch(symbols, start, end):
    """Batch fetcher fake that never has anything: forces per-symbol fallback.

    Keeps these window/isolation-focused tests exercising the injected
    single-symbol ``fetcher`` (via ``_RecordingFetcher``) even when two
    symbols in a case happen to share an identical uncovered window and
    would otherwise be grouped into a real batch call.
    """
    return {}


class _RecordingFetcher:
    """Returns one bar at the window start; records the requested windows."""

    def __init__(self, *, raises_for: set[str] | None = None):
        self.calls: dict[str, tuple[date, date]] = {}
        self._raises_for = {s.upper() for s in (raises_for or set())}

    def __call__(self, symbol, start, end):
        sym = symbol.strip().upper()
        self.calls[sym] = (start, end)
        if sym in self._raises_for:
            raise RuntimeError(f"boom for {sym}")
        return (_bar(sym, start),)


def test_backfills_case_symbols_and_benchmark_once(tmp_path):
    prices_path = tmp_path / "prices.sqlite"
    cfg = OpsConfig(backtest_store_path=str(prices_path))
    fetcher = _RecordingFetcher()

    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        _seed_cases(store, [
            ("AAA", date(2025, 6, 10)),
            ("BBB", date(2025, 6, 20)),
        ])
        summary = backfill_prices(
            cfg, store, sleeve=_SLEEVE,
            start=date(2025, 6, 1), end=date(2025, 6, 30),
            fetcher=fetcher, batch_fetcher=_no_batch, today=date(2026, 1, 15),
        )

    assert isinstance(summary, BackfillSummary)
    # 2 distinct case symbols + SPY benchmark.
    assert summary.symbols == 3
    assert summary.failures == ()
    assert summary.bars == 3
    # Benchmark fetched exactly once, over the global union window start.
    assert "SPY" in fetcher.calls
    assert fetcher.calls["SPY"][0] == date(2025, 6, 10) - timedelta(days=400)
    # Per-symbol window opens 400 calendar days before that symbol's earliest asof.
    assert fetcher.calls["AAA"][0] == date(2025, 6, 10) - timedelta(days=400)
    assert fetcher.calls["BBB"][0] == date(2025, 6, 20) - timedelta(days=400)

    cache = PriceCache(str(prices_path))
    assert cache.bars("AAA")
    assert cache.bars("BBB")
    assert cache.bars("SPY")


def test_one_failing_symbol_is_isolated(tmp_path):
    prices_path = tmp_path / "prices.sqlite"
    cfg = OpsConfig(backtest_store_path=str(prices_path))
    fetcher = _RecordingFetcher(raises_for={"BBB"})

    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        _seed_cases(store, [
            ("AAA", date(2025, 6, 10)),
            ("BBB", date(2025, 6, 20)),
        ])
        summary = backfill_prices(
            cfg, store, sleeve=_SLEEVE,
            start=date(2025, 6, 1), end=date(2025, 6, 30),
            fetcher=fetcher, batch_fetcher=_no_batch, today=date(2026, 1, 15),
        )

    # Still attempted all three; one recorded as a failure.
    assert summary.symbols == 3
    assert len(summary.failures) == 1
    failed_symbol, message = summary.failures[0]
    assert failed_symbol == "BBB"
    assert "boom" in message
    # The good names still persist.
    assert summary.bars == 2
    cache = PriceCache(str(prices_path))
    assert cache.bars("AAA")
    assert cache.bars("SPY")
    assert cache.bars("BBB") == []


def test_deadline_timeout_is_isolated_as_symbol_failure(tmp_path):
    """A hung history call must surface as a per-symbol failure, not a hang.

    ``_with_deadline`` is the composable seam (Task 7); this confirms it
    plugs straight into the existing per-symbol isolation with no special
    casing needed in the driver.
    """
    prices_path = tmp_path / "prices.sqlite"
    cfg = OpsConfig(backtest_store_path=str(prices_path))

    def hung_fetcher(symbol, start, end):
        return _with_deadline(
            lambda: (time.sleep(0.5), (_bar(symbol, start),))[1],
            seconds=0.02,
        )

    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        _seed_cases(store, [("AAA", date(2025, 6, 10))])
        summary = backfill_prices(
            cfg, store, sleeve=_SLEEVE,
            start=date(2025, 6, 1), end=date(2025, 6, 30),
            fetcher=hung_fetcher, batch_fetcher=_no_batch, today=date(2026, 1, 15),
        )

    # Both the case symbol and the benchmark hang; both are isolated as
    # failures rather than propagating and aborting the run.
    assert summary.symbols == 2
    assert len(summary.failures) == 2
    failed_symbols = {symbol for symbol, _message in summary.failures}
    assert failed_symbols == {"AAA", "SPY"}
    assert summary.bars == 0


def test_end_clamped_to_today(tmp_path):
    prices_path = tmp_path / "prices.sqlite"
    cfg = OpsConfig(backtest_store_path=str(prices_path))
    fetcher = _RecordingFetcher()
    today = date(2025, 6, 25)

    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        _seed_cases(store, [("AAA", date(2025, 6, 10))])
        backfill_prices(
            cfg, store, sleeve=_SLEEVE,
            start=date(2025, 6, 1), end=date(2025, 6, 30),
            fetcher=fetcher, batch_fetcher=_no_batch, today=today,
        )

    # horizon end (asof + ~126 sessions) is far past ``today``, so it clamps.
    assert fetcher.calls["AAA"][1] == today


def test_no_cases_returns_empty_summary(tmp_path):
    prices_path = tmp_path / "prices.sqlite"
    cfg = OpsConfig(backtest_store_path=str(prices_path))
    fetcher = _RecordingFetcher()

    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        summary = backfill_prices(
            cfg, store, sleeve=_SLEEVE,
            start=date(2025, 6, 1), end=date(2025, 6, 30),
            fetcher=fetcher, today=date(2026, 1, 15),
        )

    assert summary == BackfillSummary(symbols=0, bars=0, failures=())
    assert fetcher.calls == {}


def test_fully_covered_symbol_skips_fetch(tmp_path):
    """A symbol whose desired window is already fully cached costs zero
    network calls; it is counted as skipped, not attempted."""
    prices_path = tmp_path / "prices.sqlite"
    cfg = OpsConfig(backtest_store_path=str(prices_path))
    cache = PriceCache(str(prices_path))
    asof = date(2025, 6, 10)
    today = date(2025, 6, 25)
    w0 = asof - timedelta(days=400)
    # Coverage only needs MIN/MAX; seed the two boundary bars.
    cache.upsert_bars([_bar("AAA", w0), _bar("AAA", today)])

    fetcher = _RecordingFetcher()
    summary = backfill_symbol_windows(
        cfg, [("AAA", asof)], fetcher=fetcher, today=today,
    )

    assert summary.skipped == 1
    assert summary.symbols == 0
    assert summary.bars == 0
    assert fetcher.calls == {}


def test_partial_coverage_fetches_only_the_gap(tmp_path):
    """Only the uncovered trailing sub-range is fetched, not the whole window."""
    prices_path = tmp_path / "prices.sqlite"
    cfg = OpsConfig(backtest_store_path=str(prices_path))
    cache = PriceCache(str(prices_path))
    asof = date(2025, 6, 10)
    today = date(2025, 6, 25)
    w0 = asof - timedelta(days=400)
    covered_hi = today - timedelta(days=5)
    cache.upsert_bars([_bar("AAA", w0), _bar("AAA", covered_hi)])

    fetcher = _RecordingFetcher()
    summary = backfill_symbol_windows(
        cfg, [("AAA", asof)], fetcher=fetcher, today=today,
    )

    assert summary.skipped == 0
    assert summary.symbols == 1
    assert fetcher.calls == {"AAA": (covered_hi + timedelta(days=1), today)}
    assert summary.bars == 1


def test_batch_fetch_falls_back_per_symbol_for_stragglers(tmp_path):
    """Symbols sharing an identical uncovered window get one batch call; a
    straggler missing from the batch result falls back individually without
    affecting the symbol that WAS present in the batch."""
    import pandas as pd

    prices_path = tmp_path / "prices.sqlite"
    cfg = OpsConfig(backtest_store_path=str(prices_path))
    asof = date(2025, 6, 10)
    today = date(2025, 6, 25)

    idx = pd.to_datetime(["2025-06-02"])
    good_frame = pd.DataFrame(
        {
            "Open": [10.0], "High": [11.0], "Low": [9.0], "Close": [10.0],
            "Adj Close": [10.0], "Volume": [100],
            "Dividends": [0.0], "Stock Splits": [0.0],
        },
        index=idx,
    )

    batch_calls: list[tuple[tuple[str, ...], date, date]] = []

    def fake_batch_fetcher(symbols, start, end):
        batch_calls.append((tuple(symbols), start, end))
        # AAA comes back in the batch; BBB is a straggler (absent).
        return {"AAA": good_frame}

    fetcher = _RecordingFetcher()

    summary = backfill_symbol_windows(
        cfg, [("AAA", asof), ("BBB", asof)],
        fetcher=fetcher, batch_fetcher=fake_batch_fetcher, today=today,
    )

    assert len(batch_calls) == 1
    assert set(batch_calls[0][0]) == {"AAA", "BBB"}
    assert "BBB" in fetcher.calls
    assert "AAA" not in fetcher.calls
    assert summary.symbols == 2
    assert summary.failures == ()
    assert summary.bars == 2
