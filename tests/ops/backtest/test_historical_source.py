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
from ops.backtest.fetch_cache import FetchCache
from ops.backtest.historical_source import ReconstructionScreenerFetcher
from ops.research.prices import PriceContext


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


def test_near_miss_controls_emitted_and_flagged():
    def fake_screen(universe, *, asof, facts_fetcher, triggers_finder,
                    price_context_fetcher):
        return (
            (_FakeInputs("PASS1", triggers=("t",)), _FakeResult(passed=True)),
            # cheap+quality but zero triggers -> near miss (failed=trigger)
            (_FakeInputs("NM1"), _FakeResult(passed=False)),
            # fails two conditions -> NOT a near miss
            (_FakeInputs("FAR"), _FakeResult(passed=False, cheap=False, quality=False)),
        )
    fetcher = ReconstructionScreenerFetcher(
        universe=("PASS1", "NM1", "FAR"),
        facts_fetcher=lambda s: {}, price_context_fetcher=lambda s: {},
        triggers_finder=lambda s, *, asof: [], screen=fake_screen,
        include_near_misses=True,
    )
    out = fetcher(date(2025, 6, 16))
    kinds = {c.symbol: c.trigger["kind"] for c in out}
    assert kinds == {"PASS1": "historical_screener_replay",
                     "NM1": "near_miss_control"}
    nm = next(c for c in out if c.symbol == "NM1")
    assert nm.trigger["failed"] == "trigger"
    assert nm.source_ref == "nearmiss:2025-06-16:NM1"


# --- disk-backed caching: trigger sources / facts / price context ----------
#
# These exercise the cached path, which is only active when no explicit
# ``triggers_finder`` is supplied (that stays the uncached back-compat path
# used by every test above). ``fake_screen_calling_triggers`` mimics
# ``screen_inputs_and_results`` calling ``triggers_finder(symbol, asof=asof)``
# per universe symbol, which is what makes the trigger_sources cache
# assertions meaningful.

def _fake_screen_calling_triggers(universe, *, asof, facts_fetcher, triggers_finder,
                                   price_context_fetcher):
    pairs = []
    for sym in universe:
        facts_fetcher(sym)
        price_context_fetcher(sym)
        triggers = triggers_finder(sym, asof=asof)
        pairs.append((_FakeInputs(symbol=sym, triggers=tuple(triggers)), _FakeResult(passed=True)))
    return tuple(pairs)


def _price_ctx_for(sym):
    return PriceContext(
        closes={date(2025, 1, 1): Decimal("10"), date(2025, 6, 1): Decimal("12")},
        splits={},
    )


def test_cached_path_fetches_trigger_sources_once_per_symbol_across_asofs(tmp_path):
    calls = {"sources": 0}

    def fake_trigger_sources_fetcher(symbol):
        calls["sources"] += 1
        return {
            "symbol": symbol, "edgar_filings": [], "insider_transactions": [],
            "insider_transactions_truncated": False,
        }

    cache = FetchCache(tmp_path / "fetch_cache.sqlite")
    fetcher = ReconstructionScreenerFetcher(
        universe=("AAA", "BBB"),
        facts_fetcher=lambda s: {"symbol": s},
        price_context_fetcher=_price_ctx_for,
        screen=_fake_screen_calling_triggers,
        fetch_cache=cache,
        trigger_sources_fetcher=fake_trigger_sources_fetcher,
    )

    fetcher(date(2025, 6, 16))
    fetcher(date(2025, 6, 30))
    fetcher(date(2025, 7, 14))

    assert calls["sources"] == 2  # AAA + BBB, once each, not x3 dates


def test_second_fetcher_instance_sharing_cache_does_zero_source_fetches(tmp_path):
    def fake_trigger_sources_fetcher(symbol):
        return {
            "symbol": symbol, "edgar_filings": [], "insider_transactions": [],
            "insider_transactions_truncated": False,
        }

    cache_path = tmp_path / "fetch_cache.sqlite"
    first = ReconstructionScreenerFetcher(
        universe=("AAA", "BBB"),
        facts_fetcher=lambda s: {"symbol": s},
        price_context_fetcher=_price_ctx_for,
        screen=_fake_screen_calling_triggers,
        fetch_cache=FetchCache(cache_path),
        trigger_sources_fetcher=fake_trigger_sources_fetcher,
    )
    first(date(2025, 6, 16))

    calls = {"sources": 0, "facts": 0, "price": 0}

    def counting_sources_fetcher(symbol):
        calls["sources"] += 1
        return fake_trigger_sources_fetcher(symbol)

    def counting_facts(symbol):
        calls["facts"] += 1
        return {"symbol": symbol}

    def counting_price(symbol):
        calls["price"] += 1
        return _price_ctx_for(symbol)

    second = ReconstructionScreenerFetcher(
        universe=("AAA", "BBB"),
        facts_fetcher=counting_facts,
        price_context_fetcher=counting_price,
        screen=_fake_screen_calling_triggers,
        fetch_cache=FetchCache(cache_path),
        trigger_sources_fetcher=counting_sources_fetcher,
    )
    second(date(2025, 6, 16))

    assert calls == {"sources": 0, "facts": 0, "price": 0}


def test_screening_results_identical_with_and_without_cache(tmp_path):
    def trigger_sources_fetcher(symbol):
        return {
            "symbol": symbol,
            "edgar_filings": [],
            "insider_transactions": [],
            "insider_transactions_truncated": False,
        }

    uncached = ReconstructionScreenerFetcher(
        universe=("AAA", "BBB"),
        facts_fetcher=lambda s: {"symbol": s},
        price_context_fetcher=_price_ctx_for,
        screen=_fake_screen_calling_triggers,
        fetch_cache=None,
        trigger_sources_fetcher=trigger_sources_fetcher,
    )
    cached = ReconstructionScreenerFetcher(
        universe=("AAA", "BBB"),
        facts_fetcher=lambda s: {"symbol": s},
        price_context_fetcher=_price_ctx_for,
        screen=_fake_screen_calling_triggers,
        fetch_cache=FetchCache(tmp_path / "fetch_cache.sqlite"),
        trigger_sources_fetcher=trigger_sources_fetcher,
    )

    asof = date(2025, 6, 16)
    out_uncached = uncached(asof)
    out_cached = cached(asof)

    assert [(c.symbol, c.trigger, c.score, c.source_ref) for c in out_uncached] == (
        [(c.symbol, c.trigger, c.score, c.source_ref) for c in out_cached]
    )
