"""Graph vetting of brain memos: verdict from the native rating, bounded
falsifier enrichment, promote/reject persistence, deadline-boxed queue."""
from datetime import date, datetime, timezone

import pytest

from ops.pipeline_adapter import StubPipelineAdapter
from ops.research.vetting import (
    CONFIRM_TIERS, SHORT_CONFIRM_TIERS, FalsifierBatch, VettingSummary,
    extract_risk_falsifiers, vet_memo, vet_pending,
)
from tradingagents.memos.schema import EvidenceItem, Falsifier, Memo, ValueThesis
from tradingagents.memos.store import MemoStore


def _memo(ticker="ACME", **overrides):
    base = dict(
        ticker=ticker, as_of_date=date(2026, 7, 1), thesis_type="value",
        thesis="cheap for a fixable reason",
        evidence=[EvidenceItem(claim="c", source_type="filing", source_ref="0001:mdna")],
        value_block=ValueThesis(
            why_cheap="segment decline", change_trigger="new CEO",
            normalized_earnings_view="2x", quality_assessment="net cash",
        ),
        conviction_tier="starter", entry_price_ref=10.0,
        price_target_low=15.0, price_target_high=20.0,
        expected_holding_months=12, must_be_true=["m"],
        falsifiers=[Falsifier(description="margin collapse",
                              check_type="fundamental", metric="gross_margin_pct",
                              operator="<", threshold=30.0)],
        status="pending_vetting",
    )
    base.update(overrides)
    return Memo(**base)


@pytest.fixture
def store(tmp_path):
    return MemoStore(tmp_path / "memos.sqlite")


class NoFalsifierLLM:
    """with_structured_output unsupported -> extraction skipped cleanly."""

    def with_structured_output(self, schema):
        raise NotImplementedError


class FixedFalsifierLLM:
    """Structured extraction returns a fixed batch."""

    def __init__(self, items):
        self._items = items

    def with_structured_output(self, schema):
        items = self._items

        class _Runner:
            def invoke(self, prompt):
                return FalsifierBatch(items=items)

        return _Runner()


class BoomFalsifierLLM:
    def with_structured_output(self, schema):
        class _Runner:
            def invoke(self, prompt):
                raise RuntimeError("boom")

        return _Runner()


class BoomBindLLM:
    """with_structured_output itself raises something bind_structured doesn't catch."""

    def with_structured_output(self, schema):
        raise RuntimeError("bind exploded")


# --- verdict + conviction mapping (native rating) ------------------------

def test_confirm_map_is_the_spec_table():
    assert CONFIRM_TIERS == {"Buy": "high", "Overweight": "medium"}


@pytest.mark.parametrize("rating,tier", [("Buy", "high"), ("Overweight", "medium")])
def test_confirm_promotes_with_mapped_conviction(store, rating, tier):
    memo = _memo()
    store.save(memo)
    adapter = StubPipelineAdapter(ratings={"ACME": rating})
    outcome = vet_memo(memo, adapter=adapter, falsifier_llm=NoFalsifierLLM(),
                       memo_store=store, vetted_by_model="graph:ds4")
    assert outcome.verdict == "confirm"
    got = store.get(memo.memo_id)
    assert got.status == "open"
    assert got.conviction_tier == tier
    assert got.vetting.verdict == "confirm"
    assert got.vetting.rating == rating
    assert got.vetting.conviction_before == "starter"
    assert got.vetting.conviction_after == tier
    assert got.vetting.vetted_by_model == "graph:ds4"


def test_short_confirm_map_is_the_spec_table():
    assert SHORT_CONFIRM_TIERS == {"Sell": "high", "Underweight": "medium"}


@pytest.mark.parametrize("rating,tier", [("Sell", "high"), ("Underweight", "medium")])
def test_inverted_map_confirms_a_short_on_bearish_ratings(store, rating, tier):
    memo = _memo()
    store.save(memo)
    adapter = StubPipelineAdapter(ratings={"ACME": rating})
    outcome = vet_memo(memo, adapter=adapter, falsifier_llm=NoFalsifierLLM(),
                       memo_store=store, confirm_tiers=SHORT_CONFIRM_TIERS)
    assert outcome.verdict == "confirm"
    got = store.get(memo.memo_id)
    assert got.status == "open"
    assert got.conviction_tier == tier


@pytest.mark.parametrize("rating", ["Buy", "Overweight", "Hold", ""])
def test_inverted_map_rejects_bullish_ratings(store, rating):
    memo = _memo()
    store.save(memo)
    adapter = StubPipelineAdapter(ratings={"ACME": rating})
    outcome = vet_memo(memo, adapter=adapter, falsifier_llm=NoFalsifierLLM(),
                       memo_store=store, confirm_tiers=SHORT_CONFIRM_TIERS)
    assert outcome.verdict == "reject"
    assert store.get(memo.memo_id).status == "rejected"


@pytest.mark.parametrize("rating", ["Hold", "Underweight", "Sell", "", "garbage"])
def test_non_buy_ratings_reject(store, rating):
    memo = _memo()
    store.save(memo)
    adapter = StubPipelineAdapter(ratings={"ACME": rating})
    outcome = vet_memo(memo, adapter=adapter, falsifier_llm=NoFalsifierLLM(),
                       memo_store=store)
    assert outcome.verdict == "reject"
    got = store.get(memo.memo_id)
    assert got.status == "rejected"
    assert got.conviction_tier == "starter"       # untouched on reject
    assert got.vetting.verdict == "reject"
    assert got.vetting.conviction_after is None
    assert len(got.falsifiers) == 1               # nothing appended on reject
    assert store.open_memos() == []


def test_vet_memo_passes_brief_and_asof_to_adapter(store):
    captured = {}

    class SpyAdapter:
        def propagate(self, symbol, asof_date, research_context=""):
            captured["symbol"] = symbol
            captured["asof"] = asof_date
            captured["context"] = research_context
            return StubPipelineAdapter(ratings={symbol: "Hold"}).propagate(
                symbol, asof_date, research_context)

    memo = _memo()
    store.save(memo)
    vet_memo(memo, adapter=SpyAdapter(), falsifier_llm=NoFalsifierLLM(),
             memo_store=store)
    assert captured["symbol"] == "ACME"
    assert captured["asof"] == date(2026, 7, 1)   # memo.as_of_date
    assert "RESEARCH MEMO BRIEF" in captured["context"]
    assert "cheap for a fixable reason" in captured["context"]


# --- risk-falsifier extraction (option B, gate-validated) -----------------

def test_extraction_keeps_only_machine_checkable():
    good = Falsifier(description="drawdown", check_type="price",
                     metric="drawdown_from_cost_pct", operator=">", threshold=25.0)
    prose = Falsifier(description="vibes deteriorate", check_type="event")
    partial = Falsifier(description="margin", check_type="fundamental",
                        metric="gross_margin_pct")   # no operator/threshold
    kept, notes = extract_risk_falsifiers(
        FixedFalsifierLLM([good, prose, partial]),
        {"risk_debate_state": {"history": "H", "judge_decision": "J"}},
        ticker="ACME",
    )
    assert kept == [good]
    assert any("2" in n for n in notes)   # 2 dropped, noted


def test_extraction_failure_returns_empty_with_note():
    kept, notes = extract_risk_falsifiers(
        BoomFalsifierLLM(),
        {"risk_debate_state": {"history": "H", "judge_decision": "J"}},
        ticker="ACME",
    )
    assert kept == []
    assert notes and "failed" in notes[0]


def test_extraction_bind_failure_returns_empty_with_note():
    kept, notes = extract_risk_falsifiers(
        BoomBindLLM(),
        {"risk_debate_state": {"history": "H", "judge_decision": "J"}},
        ticker="ACME",
    )
    assert kept == []
    assert notes and "failed" in notes[0]


def test_extraction_skips_on_empty_debate():
    kept, notes = extract_risk_falsifiers(
        FixedFalsifierLLM([]), {"risk_debate_state": {}}, ticker="ACME",
    )
    assert kept == []
    assert notes


def test_confirm_appends_validated_falsifiers_with_indices(store):
    memo = _memo()
    store.save(memo)
    good = Falsifier(description="drawdown", check_type="price",
                     metric="drawdown_from_cost_pct", operator=">", threshold=25.0)
    prose = Falsifier(description="vibes", check_type="event")
    adapter = StubPipelineAdapter(ratings={"ACME": "Buy"})
    outcome = vet_memo(memo, adapter=adapter,
                       falsifier_llm=FixedFalsifierLLM([good, prose]),
                       memo_store=store)
    assert outcome.verdict == "confirm"
    assert outcome.added_falsifiers == 1
    got = store.get(memo.memo_id)
    assert len(got.falsifiers) == 2
    assert got.falsifiers[1].metric == "drawdown_from_cost_pct"
    assert got.vetting.added_falsifier_indices == [1]


def test_extraction_failure_never_blocks_a_confirm(store):
    """Spec: verdict/conviction come only from the rating; a failed
    enrichment call confirms with the brain's falsifiers alone."""
    memo = _memo()
    store.save(memo)
    adapter = StubPipelineAdapter(ratings={"ACME": "Buy"})
    outcome = vet_memo(memo, adapter=adapter, falsifier_llm=BoomFalsifierLLM(),
                       memo_store=store)
    assert outcome.verdict == "confirm"
    got = store.get(memo.memo_id)
    assert got.status == "open"
    assert len(got.falsifiers) == 1
    assert got.vetting.added_falsifier_indices == []
    assert "falsifier extraction failed" in got.vetting.rationale


def test_bind_failure_never_blocks_a_confirm(store):
    """A with_structured_output that raises an uncaught exception must not
    escape vet_memo: the memo still confirms on the brain's falsifiers."""
    memo = _memo()
    store.save(memo)
    adapter = StubPipelineAdapter(ratings={"ACME": "Buy"})
    outcome = vet_memo(memo, adapter=adapter, falsifier_llm=BoomBindLLM(),
                       memo_store=store)
    assert outcome.verdict == "confirm"
    got = store.get(memo.memo_id)
    assert got.status == "open"
    assert len(got.falsifiers) == 1
    assert got.vetting.added_falsifier_indices == []
    assert "falsifier extraction failed" in got.vetting.rationale


# --- queue loop -----------------------------------------------------------

def _utc(h=1):
    return datetime(2026, 7, 9, h, 0, tzinfo=timezone.utc)


def test_vet_pending_processes_oldest_first_and_counts(store):
    older = _memo(ticker="AAA",
                  created_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    newer = _memo(ticker="BBB",
                  created_at=datetime(2026, 7, 5, tzinfo=timezone.utc))
    store.save(newer)
    store.save(older)
    order = []

    class OrderSpy(StubPipelineAdapter):
        def propagate(self, symbol, asof_date, research_context=""):
            order.append(symbol)
            return super().propagate(symbol, asof_date, research_context)

    summary = vet_pending(
        memo_store=store,
        adapter=OrderSpy(ratings={"AAA": "Buy", "BBB": "Sell"}),
        falsifier_llm=NoFalsifierLLM(), vetted_by_model="m",
    )
    assert order == ["AAA", "BBB"]
    assert summary == VettingSummary(vetted=2, confirmed=1, rejected=1,
                                     failed=0, still_pending=0, hit_deadline=False)


def test_vet_pending_stops_at_deadline_between_memos(store):
    for t in ("AAA", "BBB"):
        store.save(_memo(ticker=t))
    clock = iter([_utc(1), _utc(9)])   # first check passes, second hits deadline
    summary = vet_pending(
        memo_store=store, adapter=StubPipelineAdapter(ratings={"AAA": "Buy"}),
        falsifier_llm=NoFalsifierLLM(), vetted_by_model="m",
        deadline=_utc(8), now=lambda: next(clock),
    )
    assert summary.vetted == 1
    assert summary.hit_deadline is True
    assert summary.still_pending == 1


def test_vet_pending_honors_should_stop(store):
    store.save(_memo(ticker="AAA"))
    summary = vet_pending(
        memo_store=store, adapter=StubPipelineAdapter(),
        falsifier_llm=NoFalsifierLLM(), vetted_by_model="m",
        should_stop=lambda: True,
    )
    assert summary.vetted == 0
    assert summary.still_pending == 1


def test_vet_pending_failure_leaves_memo_pending_and_continues(store):
    for t in ("AAA", "BBB"):
        store.save(_memo(ticker=t))

    class FlakyAdapter(StubPipelineAdapter):
        def propagate(self, symbol, asof_date, research_context=""):
            if symbol == "AAA":
                raise RuntimeError("graph exploded")
            return super().propagate(symbol, asof_date, research_context)

    echoes = []
    summary = vet_pending(
        memo_store=store, adapter=FlakyAdapter(ratings={"BBB": "Buy"}),
        falsifier_llm=NoFalsifierLLM(), vetted_by_model="m",
        echo=echoes.append,
    )
    assert summary.failed == 1
    assert summary.confirmed == 1
    assert summary.still_pending == 1          # AAA retried next night
    assert [m.ticker for m in store.pending_vetting_memos()] == ["AAA"]
    assert any("AAA" in e and "FAILED" in e for e in echoes)
