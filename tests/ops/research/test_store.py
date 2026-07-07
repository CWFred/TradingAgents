"""Unit tests for the screen store / deep-research queue."""

from datetime import date
from decimal import Decimal

import pytest

from ops.research.screener import Bar, ScreenResult
from ops.research.store import ScreenStore

pytestmark = pytest.mark.unit

ASOF = date(2026, 7, 1)


def _result(symbol, passed=True):
    bar = Bar(name="fcf_yield", passed=True, detail="FCF yield 8.0% vs 6%")
    return ScreenResult(
        symbol=symbol, asof=ASOF, passed=passed, cheap=passed, quality=passed,
        valuation_bars=(bar,), quality_bars=(bar,), triggers=(),
        market_cap=Decimal("1000"), ev_ebit=Decimal("10.5"),
    )


@pytest.fixture
def store(tmp_path):
    return ScreenStore(tmp_path / "screen.sqlite")


def test_record_run_stores_only_passed_results_as_hits(store):
    run_id = store.record_run(
        asof=ASOF, universe_size=100,
        results=[_result("AAA"), _result("BBB", passed=False)],
    )
    hits = store.pending_hits()
    assert [h["symbol"] for h in hits] == ["AAA"]
    assert hits[0]["run_id"] == run_id
    assert hits[0]["payload"]["market_cap"] == "1000"


def test_pending_symbol_not_duplicated_across_runs(store):
    store.record_run(asof=ASOF, universe_size=100, results=[_result("AAA")])
    store.record_run(asof=date(2026, 7, 8), universe_size=100, results=[_result("AAA")])
    assert len(store.pending_hits()) == 1


def test_mark_researched_removes_from_queue_and_allows_requeue(store):
    store.record_run(asof=ASOF, universe_size=100, results=[_result("AAA")])
    hit = store.pending_hits()[0]
    store.mark_researched(hit["id"])
    assert store.pending_hits() == []
    # Once researched, a later screen pass may queue the name again.
    store.record_run(asof=date(2026, 10, 1), universe_size=100, results=[_result("AAA")])
    assert len(store.pending_hits()) == 1


def test_mark_expired(store):
    store.record_run(asof=ASOF, universe_size=100, results=[_result("AAA")])
    store.mark_expired(store.pending_hits()[0]["id"])
    assert store.pending_hits() == []


def test_mark_failed_and_requeue(store):
    store.record_run(asof=ASOF, universe_size=5, results=[_result("AAA")])
    hit = store.pending_hits()[0]
    store.mark_failed(hit["id"])
    assert store.pending_hits() == []
    # A later run may queue the symbol again.
    store.record_run(asof=date(2026, 7, 8), universe_size=5, results=[_result("AAA")])
    assert [h["symbol"] for h in store.pending_hits()] == ["AAA"]


def test_last_run_summary(store):
    assert store.last_run() is None
    run_id = store.record_run(
        asof=ASOF, universe_size=100,
        results=[_result("AAA"), _result("BBB", passed=False)],
    )
    run = store.last_run()
    assert run["run_id"] == run_id
    assert run["universe_size"] == 100
    assert run["passed_count"] == 1
    assert run["asof"] == "2026-07-01"


def test_record_run_persists_coverage(store):
    coverage = {"fcf_yield": {"computed": 5, "missing": 1}}
    store.record_run(asof=ASOF, universe_size=6, results=[_result("AAA")], coverage=coverage)
    run = store.last_run()
    assert run["coverage"] == coverage
