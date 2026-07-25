"""Backfill driver: window derivation, benchmark, per-symbol isolation.

Everything here is network-free: a fabricated ``fetcher`` returns known bars
(and can raise for a chosen symbol) and a fixed ``today`` pins the window.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ops.backtest.models import CaseSource
from ops.backtest.models import BacktestCase
from ops.backtest.price_backfill import BackfillSummary, backfill_prices
from ops.backtest.price_fetch import YfBar
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
            fetcher=fetcher, today=date(2026, 1, 15),
        )

    assert isinstance(summary, BackfillSummary)
    # 2 distinct case symbols + SPY benchmark.
    assert summary.symbols == 3
    assert summary.failures == ()
    assert summary.bars == 3
    # Benchmark fetched exactly once, over the global union window start.
    assert "SPY" in fetcher.calls
    assert fetcher.calls["SPY"][0] == date(2025, 6, 10) - timedelta(days=10)
    # Per-symbol window opens 10 calendar days before that symbol's earliest asof.
    assert fetcher.calls["AAA"][0] == date(2025, 6, 10) - timedelta(days=10)
    assert fetcher.calls["BBB"][0] == date(2025, 6, 20) - timedelta(days=10)

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
            fetcher=fetcher, today=date(2026, 1, 15),
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
            fetcher=fetcher, today=today,
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
