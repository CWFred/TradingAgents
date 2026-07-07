"""Unit tests for the two-stage research brain (no network, no real LLMs)."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ops.research import brain
from ops.research.brain import EvidenceBatch, MemoDraft, research_hit
from ops.research.prices import PriceContext
from tradingagents.dataflows.edgar import Filing
from tradingagents.memos.schema import EvidenceItem, Falsifier, ValueThesis
from tradingagents.memos.store import MemoStore

pytestmark = pytest.mark.unit

TODAY = date(2026, 7, 6)
ACC_10K = "0000000001-26-000001"
ACC_10K_OLD = "0000000001-25-000001"
ACC_10Q = "0000000001-26-000050"

TEN_K_TEXT = "\n".join([
    "Item 1. Business", "We make widgets.",
    "Item 1A. Risk Factors", "Customer concentration is 40%.",
    "Item 7. Management's Discussion and Analysis", "Revenue fell 12%.",
    "Item 8. Financial Statements", "Notes.",
])
TEN_Q_TEXT = "\n".join([
    "Item 2. Management's Discussion and Analysis", "Q1 revenue stabilized.",
    "Item 3. Quantitative Disclosures", "None.",
])


def _filing(accession, form, filed, report):
    return Filing(
        ticker="WIDG", cik=1, accession_number=accession, form=form,
        filing_date=filed, report_date=report, primary_document="doc.htm",
    )


FILINGS = [
    _filing(ACC_10Q, "10-Q", date(2026, 5, 10), date(2026, 3, 31)),
    _filing(ACC_10K, "10-K", date(2026, 3, 1), date(2025, 12, 31)),
    _filing(ACC_10K_OLD, "10-K", date(2025, 3, 1), date(2024, 12, 31)),
]
TEXTS = {ACC_10K: TEN_K_TEXT, ACC_10K_OLD: TEN_K_TEXT, ACC_10Q: TEN_Q_TEXT}


def _hit():
    return {
        "id": 7, "run_id": "screen-2026-07-04-abcd1234", "symbol": "WIDG",
        "asof": "2026-07-04", "status": "pending",
        "payload": {
            "symbol": "WIDG", "asof": "2026-07-04", "passed": True,
            "cheap": True, "quality": True,
            "valuation_bars": [
                {"name": "fcf_yield", "passed": True, "detail": "FCF yield 9.1% vs 6%"},
            ],
            "quality_bars": [
                {"name": "roic_5y", "passed": True, "detail": "mean ROIC 15.2% vs 12%"},
            ],
            "triggers": [
                {"kind": "selloff", "description": "40% below high",
                 "date": "2026-07-04", "source": "price"},
            ],
            "market_cap": "450000000", "ev_ebit": "6.1",
        },
    }


def _evidence_item(ref):
    return EvidenceItem(
        claim="revenue fell 12%", source_type="filing", source_ref=ref, quote="Revenue fell 12%.",
    )


def _draft(**overrides):
    kwargs = {
        "company_name": "Widget Co", "thesis_type": "value",
        "thesis": "Mispriced on distributor loss. Earnings normalize.",
        "value_block": ValueThesis(
            why_cheap="lost largest distributor", change_trigger="selloff",
            normalized_earnings_view="$1.20", quality_assessment="net cash",
        ),
        "conviction_tier": "starter", "price_target_low": 5.0, "price_target_high": 8.0,
        "expected_holding_months": 12, "must_be_true": ["volume replaced"],
        "falsifiers": [Falsifier(
            description="margin collapse", check_type="fundamental",
            metric="gross_margin_pct", operator="<", threshold=30.0,
        )],
        "recommendation": "buy",
    }
    kwargs.update(overrides)
    return MemoDraft(**kwargs)


class FakeLLM:
    """Covers both bind_structured (returns self) and plain .invoke paths.

    ``responses`` is consumed in order. Pydantic instances are returned as-is
    (structured call results); strings come back as .content objects (plain
    calls); Exceptions are raised.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, str):
            return SimpleNamespace(content=result)
        return result


@pytest.fixture
def memo_store(tmp_path):
    return MemoStore(tmp_path / "memos.sqlite")


def _price_fetcher(symbol):
    return PriceContext(closes={TODAY: Decimal("4.10")})


def _run(evidence_llm, thesis_llm, memo_store, hit=None):
    return research_hit(
        hit or _hit(), evidence_llm=evidence_llm, thesis_llm=thesis_llm,
        memo_store=memo_store,
        list_filings=lambda ticker, **kw: FILINGS,
        fetch_text=lambda f, **kw: TEXTS[f.accession_number],
        price_fetcher=_price_fetcher, today=TODAY,
    )


def _good_evidence_llm():
    # 5 sections read: 10-K mdna/risk_factors/business, 10-Q mdna, 10-K diff.
    return FakeLLM([
        EvidenceBatch(items=[_evidence_item(f"{ACC_10K}:mdna")]),
        EvidenceBatch(items=[_evidence_item(f"{ACC_10K}:risk_factors")]),
        EvidenceBatch(items=[_evidence_item(f"{ACC_10K}:business")]),
        EvidenceBatch(items=[_evidence_item(f"{ACC_10Q}:mdna")]),
        EvidenceBatch(items=[_evidence_item(f"{ACC_10K}+{ACC_10K_OLD}:mdna_diff")]),
    ])


def test_happy_path_saves_open_memo(memo_store):
    thesis_llm = FakeLLM(["bear: distributor loss is permanent", _draft()])
    outcome = _run(_good_evidence_llm(), thesis_llm, memo_store)
    assert outcome.status == "researched"
    assert outcome.recommendation == "buy"
    memo = memo_store.get(outcome.memo_id)
    assert memo.status == "open"
    assert memo.ticker == "WIDG"
    assert memo.entry_price_ref == pytest.approx(4.10)
    assert memo.as_of_date == TODAY
    assert len(memo.evidence) == 5


def test_pass_recommendation_shadow_tracks(memo_store):
    thesis_llm = FakeLLM(["bear case", _draft(recommendation="pass")])
    outcome = _run(_good_evidence_llm(), thesis_llm, memo_store)
    assert outcome.status == "researched"
    assert memo_store.get(outcome.memo_id).status == "passed"


def test_uncited_evidence_stripped_and_thin_research_fails(memo_store):
    # Model cites a section that was never read -> all items dropped -> fail
    # before any thesis-stage spend.
    bad = FakeLLM([EvidenceBatch(items=[_evidence_item("invented:mdna")])] * 5)
    thesis_llm = FakeLLM([])
    outcome = _run(bad, thesis_llm, memo_store)
    assert outcome.status == "failed"
    assert outcome.evidence_dropped == 5
    assert thesis_llm.prompts == []  # thesis stage never ran
    assert any("evidence" in e for e in outcome.errors)


def test_invalid_memo_retries_once_with_feedback_then_fails(memo_store):
    bad_draft = _draft(falsifiers=[
        Falsifier(description="prose only", check_type="fundamental"),
    ])
    thesis_llm = FakeLLM(["bear case", bad_draft, bad_draft])
    outcome = _run(_good_evidence_llm(), thesis_llm, memo_store)
    assert outcome.status == "failed"
    assert any("machine-checkable" in e for e in outcome.errors)
    # 3 thesis calls: bear, emission, retry emission — and the retry prompt
    # carried the validation feedback.
    assert len(thesis_llm.prompts) == 3
    assert "machine-checkable" in thesis_llm.prompts[2]
    assert memo_store.list(ticker="WIDG") == []


def test_retry_success_saves(memo_store):
    bad_draft = _draft(precedent_memo_ids=["invented"])
    thesis_llm = FakeLLM(["bear case", bad_draft, _draft()])
    outcome = _run(_good_evidence_llm(), thesis_llm, memo_store)
    assert outcome.status == "researched"


def test_no_price_fails_fast(memo_store):
    outcome = research_hit(
        _hit(), evidence_llm=FakeLLM([]), thesis_llm=FakeLLM([]),
        memo_store=memo_store,
        list_filings=lambda ticker, **kw: FILINGS,
        fetch_text=lambda f, **kw: TEXTS[f.accession_number],
        price_fetcher=lambda s: None, today=TODAY,
    )
    assert outcome.status == "failed"
    assert any("price" in e for e in outcome.errors)


def test_structured_output_unsupported_raises_research_error(memo_store):
    class NoStructured:
        def with_structured_output(self, schema):
            raise NotImplementedError

    with pytest.raises(brain.ResearchError):
        _run(NoStructured(), FakeLLM([]), memo_store)


def test_past_memos_feed_precedents(memo_store):
    # Seed a prior memo; the new draft may cite its id and validation passes.
    thesis_llm1 = FakeLLM(["bear", _draft()])
    first = _run(_good_evidence_llm(), thesis_llm1, memo_store)
    prior_id = first.memo_id

    thesis_llm2 = FakeLLM(["bear", _draft(precedent_memo_ids=[prior_id])])
    second = _run(_good_evidence_llm(), thesis_llm2, memo_store)
    assert second.status == "researched"
    # The thesis prompt actually contained the precedent summary.
    assert prior_id in thesis_llm2.prompts[1]
