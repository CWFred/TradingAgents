"""Memo-lite: non-gating overnight authoring + mechanical resolution."""

from datetime import date
from decimal import Decimal

import pytest

from ops.insider.memo_lite import (
    MemoLiteDraft, author_pending_memos, resolve_on_exit,
)
from ops.insider.store import SignalStore
from ops.research.prices import PriceContext
from tradingagents.dataflows.form4 import InsiderTransaction
from tradingagents.memos.schema import ReturnScenario
from tradingagents.memos.store import MemoStore

pytestmark = pytest.mark.unit

D = Decimal
ASOF = date(2026, 7, 13)


def _buy(name, *, when=date(2026, 7, 1), accession=None):
    return InsiderTransaction(
        insider_name=name, insider_title="Director", is_director=True,
        is_officer=False, is_ten_pct_owner=False, transaction_date=when,
        code="P", shares=D("1000"), price=D("20"), acquired=True,
        ten_b5_1=False, accession=accession or f"0001-26-{name}", filed_date=when,
    )


class FakeLLM:
    def __init__(self, result):
        self._result = result

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.fixture
def stores(tmp_path):
    signal_store = SignalStore(tmp_path / "signals.sqlite")
    memo_store = MemoStore(tmp_path / "insider_memos.sqlite")
    return signal_store, memo_store


def _draft():
    return MemoLiteDraft(
        thesis="Two directors bought size after the selloff.",
        must_be_true=["the buys are discretionary conviction, not window dressing"],
        scenarios=[ReturnScenario(probability=0.6, return_pct=0.3,
                                  description="drift up")],
    )


def _prices(symbol):
    return PriceContext(closes={ASOF: D("20")})


def test_authoring_writes_open_memo_and_sets_entry(stores):
    signal_store, memo_store = stores
    signal_store.record_transactions("AAA", [_buy("A"), _buy("B")])
    signal_store.record_entry("AAA", asof=ASOF)
    written = author_pending_memos(
        signal_store=signal_store, memo_store=memo_store,
        thesis_llm=FakeLLM(_draft()), thesis_model_spec="test:model",
        price_fetcher=_prices,
    )
    assert written == 1
    assert signal_store.entries_without_memo() == []
    (memo,) = memo_store.open_memos()
    assert memo.thesis_type == "event"
    assert memo.event_block.event_type == "insider_cluster"
    assert memo.vetting is None
    assert memo.conviction_tier == "starter"       # 2 buyers
    assert {e.source_ref for e in memo.evidence} == {"0001-26-A", "0001-26-B"}
    assert memo.falsifiers[0].metric == "drawdown_from_cost_pct"
    assert memo.falsifiers[0].operator == ">="
    assert memo.entry_price_ref == 20.0
    assert memo.authored_by_model == "test:model"


def test_llm_failure_leaves_entry_queued(stores):
    signal_store, memo_store = stores
    signal_store.record_transactions("AAA", [_buy("A"), _buy("B")])
    signal_store.record_entry("AAA", asof=ASOF)
    written = author_pending_memos(
        signal_store=signal_store, memo_store=memo_store,
        thesis_llm=FakeLLM(RuntimeError("ds4 fell over")),
        price_fetcher=_prices,
    )
    assert written == 0
    assert memo_store.list() == []
    assert signal_store.entries_without_memo() == [{"symbol": "AAA", "asof": ASOF}]


def _resolve(memo_store, memo_id, *, reason, exit_price, benchmark_fetcher=None):
    resolve_on_exit(
        memo_store=memo_store, memo_id=memo_id, entry_price=D("20"),
        exit_price=exit_price, entry_date=ASOF, exit_date=date(2026, 9, 1),
        reason=reason, benchmark_fetcher=benchmark_fetcher,
    )
    return memo_store.get(memo_id)


def _authored_memo(stores):
    signal_store, memo_store = stores
    signal_store.record_transactions("AAA", [_buy("A"), _buy("B")])
    signal_store.record_entry("AAA", asof=ASOF)
    author_pending_memos(
        signal_store=signal_store, memo_store=memo_store,
        thesis_llm=FakeLLM(_draft()), price_fetcher=_prices,
    )
    return memo_store.open_memos()[0]


def test_target_exit_resolves_right_made_money(stores):
    _, memo_store = stores
    memo = _authored_memo(stores)
    bench = lambda s: PriceContext(closes={ASOF: D("100"), date(2026, 9, 1): D("105")})
    got = _resolve(memo_store, memo.memo_id, reason="target",
                   exit_price=D("28"), benchmark_fetcher=bench)
    assert got.status == "resolved"
    r = got.resolution
    assert r.outcome_label == "thesis_right_made_money"
    assert r.realized_return_pct == pytest.approx(0.4)
    assert r.benchmark_return_pct == pytest.approx(0.05)
    assert r.falsifiers_tripped == []
    assert r.holding_days == 50


def test_stop_exit_resolves_wrong_lost_money_and_trips_falsifier(stores):
    _, memo_store = stores
    memo = _authored_memo(stores)
    got = _resolve(memo_store, memo.memo_id, reason="stop", exit_price=D("16"))
    assert got.resolution.outcome_label == "thesis_wrong_lost_money"
    assert got.resolution.falsifiers_tripped == [0]


def test_benchmark_failure_degrades_to_zero(stores):
    _, memo_store = stores
    memo = _authored_memo(stores)

    def boom(symbol):
        raise RuntimeError("yfinance down")

    got = _resolve(memo_store, memo.memo_id, reason="time",
                   exit_price=D("21"), benchmark_fetcher=boom)
    assert got.resolution.benchmark_return_pct == 0.0
    assert "benchmark fetch failed" in got.resolution.narrative
    assert got.resolution.outcome_label == "thesis_right_made_money"
