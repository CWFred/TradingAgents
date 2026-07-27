"""Reconstruction screener fetcher: passing-name candidates + per-symbol cache.

The screen callable is injected, so no network is touched. The fake screen
mimics the real ``screen_inputs_and_results`` contract by calling the injected
facts/price fetchers once per universe symbol -- that is what makes the caching
assertion meaningful: the fetcher wraps them in per-symbol caches, so a two-date
sweep fetches each symbol's data once, not once per date.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ops.backtest.cases import RECONSTRUCTION_SOURCE_MODE
from ops.backtest.historical_source import ReconstructionScreenerFetcher


@dataclass(frozen=True)
class _FakeInputs:
    symbol: str
    triggers: tuple = ()


@dataclass(frozen=True)
class _FakeResult:
    passed: bool
    cheap: bool = True
    quality: bool = True


def _fake_universe(symbols):
    return tuple(symbols)


def _fake_ctx(symbol):
    return {"symbol": symbol, "closes": {}}


def _pairs_where_pass(universe, asof, *, passing, facts_fetcher, price_context_fetcher):
    """Build (inputs, result) pairs, calling the injected fetchers per symbol
    exactly as the real screen does, so the cache wrappers get exercised."""
    pairs = []
    for sym in universe:
        facts_fetcher(sym)
        price_context_fetcher(sym)
        pairs.append((_FakeInputs(symbol=sym), _FakeResult(passed=sym in passing)))
    return tuple(pairs)


def test_fetcher_returns_passing_candidates_and_caches_per_symbol(monkeypatch):
    calls = {"facts": 0, "price": 0}

    def fake_facts(sym):
        calls["facts"] += 1
        return {"symbol": sym}

    def fake_price_ctx(sym):
        calls["price"] += 1
        return _fake_ctx(sym)

    def fake_screen(universe, *, asof, facts_fetcher, triggers_finder,
                    price_context_fetcher):
        return _pairs_where_pass(
            universe, asof, passing={"AAA"},
            facts_fetcher=facts_fetcher,
            price_context_fetcher=price_context_fetcher,
        )

    fetcher = ReconstructionScreenerFetcher(
        universe=_fake_universe(["AAA", "BBB"]),
        facts_fetcher=fake_facts, price_context_fetcher=fake_price_ctx,
        triggers_finder=lambda s, *, asof: [],
        screen=fake_screen,
    )
    a = fetcher(date(2025, 6, 16))
    b = fetcher(date(2025, 6, 30))

    assert [c.symbol for c in a] == ["AAA"]
    assert a[0].trigger["kind"] == "historical_screener_replay"
    assert a[0].trigger["asof"] == "2025-06-16"
    assert a[0].asof == date(2025, 6, 16)
    assert a[0].score == Decimal(1)
    # Full live-shaped payload: the entire ScreenResult dump plus the
    # symbol/asof keys the brain's screen summary requires (2026-07-27
    # incident: the old 3-key payload KeyError'd every regenerated memo).
    assert a[0].screen_payload["passed"] is True
    assert a[0].screen_payload["cheap"] is True
    assert a[0].screen_payload["quality"] is True
    assert a[0].screen_payload["symbol"] == "AAA"
    assert a[0].screen_payload["asof"] == "2025-06-16"
    assert a[0].source_ref == "reconstruction:2025-06-16:AAA"
    # per-symbol data fetched once total, reused across both asofs:
    assert calls["facts"] == 2 and calls["price"] == 2  # AAA + BBB, not x2 dates
    assert b[0].asof == date(2025, 6, 30)
    assert fetcher.source_mode == RECONSTRUCTION_SOURCE_MODE


def test_score_counts_triggers(monkeypatch):
    def fake_screen(universe, *, asof, facts_fetcher, triggers_finder,
                    price_context_fetcher):
        return (
            (_FakeInputs(symbol="AAA", triggers=("t1", "t2", "t3")),
             _FakeResult(passed=True)),
        )

    fetcher = ReconstructionScreenerFetcher(
        universe=("AAA",),
        facts_fetcher=lambda s: {}, price_context_fetcher=lambda s: {},
        triggers_finder=lambda s, *, asof: [],
        screen=fake_screen,
    )
    out = fetcher(date(2025, 6, 16))
    assert out[0].score == Decimal(3)


def test_skips_failing_names(monkeypatch):
    def fake_screen(universe, *, asof, facts_fetcher, triggers_finder,
                    price_context_fetcher):
        return (
            (_FakeInputs(symbol="AAA"), _FakeResult(passed=False)),
            (_FakeInputs(symbol="BBB"), _FakeResult(passed=True, cheap=False, quality=True)),
        )

    fetcher = ReconstructionScreenerFetcher(
        universe=("AAA", "BBB"),
        facts_fetcher=lambda s: {}, price_context_fetcher=lambda s: {},
        triggers_finder=lambda s, *, asof: [],
        screen=fake_screen,
    )
    out = fetcher(date(2025, 6, 16))
    assert [c.symbol for c in out] == ["BBB"]
    assert out[0].screen_payload["cheap"] is False
