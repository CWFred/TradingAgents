"""author_pending_memos emits item breadcrumbs per attempted entry."""
from datetime import date
from decimal import Decimal

import pytest

from ops import events
from ops.activity import ActivityReporter
from ops.insider.memo_lite import MemoLiteDraft, author_pending_memos
from ops.insider.store import SignalStore
from ops.journal import Journal
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
def journal(tmp_path):
    j = Journal(str(tmp_path / "j.db"))
    yield j
    j.close()


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


def test_author_pending_emits_item_per_entry(journal, stores):
    signal_store, memo_store = stores
    signal_store.record_transactions(
        "AAA", [_buy("A", accession="AAA-1"), _buy("B", accession="AAA-2")])
    signal_store.record_entry("AAA", asof=ASOF)
    signal_store.record_transactions(
        "BBB", [_buy("A", accession="BBB-1"), _buy("B", accession="BBB-2")])
    signal_store.record_entry("BBB", asof=ASOF)

    author_pending_memos(
        signal_store=signal_store, memo_store=memo_store,
        thesis_llm=FakeLLM(_draft()), reporter=ActivityReporter(journal),
        price_fetcher=_prices,
    )
    starts = [e["payload"] for e in journal.read_events()
              if e["kind"] == events.KIND_ACTIVITY_STARTED]
    assert all(s["stage"] == "authoring_memo" and s["job"] == "overnight"
               for s in starts)
    assert [s["symbol"] for s in starts] == ["AAA", "BBB"]


def test_failed_entry_finishes_not_ok_and_queue_continues(journal, stores):
    signal_store, memo_store = stores
    signal_store.record_transactions(
        "AAA", [_buy("A", accession="AAA-1"), _buy("B", accession="AAA-2")])
    signal_store.record_entry("AAA", asof=ASOF)
    signal_store.record_transactions(
        "BBB", [_buy("A", accession="BBB-1"), _buy("B", accession="BBB-2")])
    signal_store.record_entry("BBB", asof=ASOF)

    def boom(symbol):
        if symbol == "AAA":
            raise RuntimeError("no price")
        return _prices(symbol)

    written = author_pending_memos(
        signal_store=signal_store, memo_store=memo_store,
        thesis_llm=FakeLLM(_draft()), reporter=ActivityReporter(journal),
        price_fetcher=boom,
    )
    assert written == 1
    fins = [e["payload"] for e in journal.read_events()
            if e["kind"] == events.KIND_ACTIVITY_FINISHED]
    assert [f["ok"] for f in fins] == [False, True]
