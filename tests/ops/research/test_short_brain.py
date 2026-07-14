"""Unit tests for the short-thesis brain (no network, no real LLMs).

Reuses the fixture idioms of test_brain.py: canned filings/texts, FakeLLM
with ordered responses, injected price fetcher.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ops.research.brain import EvidenceBatch
from ops.research.prices import PriceContext
from ops.research.short_brain import ShortMemoDraft, research_short_hit
from tradingagents.dataflows.edgar import Filing
from tradingagents.memos.schema import EvidenceItem, Falsifier, ShortThesis
from tradingagents.memos.store import MemoStore

pytestmark = pytest.mark.unit

TODAY = date(2026, 7, 13)
ACC_10K = "0000000002-26-000001"
ACC_10K_OLD = "0000000002-25-000001"
ACC_10Q = "0000000002-26-000050"

TEN_K_TEXT = "\n".join([
    "Item 1. Business", "We make gadgets.",
    "Item 1A. Risk Factors", "Auditor noted substantial doubt.",
    "Item 7. Management's Discussion and Analysis", "Margins fell 500bps.",
    "Item 8. Financial Statements", "Notes.",
])
TEN_Q_TEXT = "\n".join([
    "Item 2. Management's Discussion and Analysis", "Backlog declined again.",
    "Item 3. Quantitative Disclosures", "None.",
])


def _filing(accession, form, filed, report):
    return Filing(
        ticker="GHST", cik=2, accession_number=accession, form=form,
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
        "id": 3, "run_id": "short-screen-2026-07-13-ab12", "symbol": "GHST",
        "asof": "2026-07-13", "status": "pending",
        "payload": {
            "symbol": "GHST", "asof": "2026-07-13", "passed": True,
            "bars": [
                {"name": "ev_ebit_expensive", "passed": True,
                 "detail": "EV/EBIT 34.0 vs 20"},
                {"name": "net_debt_ebitda_high", "passed": True,
                 "detail": "net debt/EBITDA 5.1 vs 4"},
            ],
            "red_flags": [
                {"kind": "red_flag_8k", "description": "4.02",
                 "date": "2026-07-01", "source": ACC_10Q},
            ],
            "market_cap": "900000000", "ev_ebit": "34.0",
        },
    }


def _evidence_item(ref):
    return EvidenceItem(
        claim="margins fell 500bps", source_type="filing", source_ref=ref,
        quote="Margins fell 500bps.",
    )


def _draft(**overrides):
    kwargs = {
        "company_name": "Ghost Co",
        "thesis": "Priced as growth; restatement guts the story.",
        "short_block": ShortThesis(
            overvaluation_mechanism="story multiple on a shrinking segment",
            red_flags=["8-K 4.02 non-reliance"],
            why_now="guidance reset within two quarters",
            squeeze_risk="modest: 6% of float short",
            downside_scenario="8x normalized EBIT, ~-40%",
        ),
        "conviction_tier": "starter",
        "price_target_low": 25.0, "price_target_high": 55.0,
        "expected_holding_months": 6,
        "must_be_true": ["restated margins are materially lower"],
        "falsifiers": [Falsifier(
            description="margins recover", check_type="fundamental",
            metric="gross_margin_pct", operator=">", threshold=45.0,
        )],
        "recommendation": "short",
    }
    kwargs.update(overrides)
    return ShortMemoDraft(**kwargs)


class FakeLLM:
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
    return MemoStore(tmp_path / "short_memos.sqlite")


def _run(evidence_llm, thesis_llm, memo_store):
    return research_short_hit(
        _hit(), evidence_llm=evidence_llm, thesis_llm=thesis_llm,
        memo_store=memo_store,
        list_filings=lambda ticker, **kw: FILINGS,
        fetch_text=lambda f, **kw: TEXTS[f.accession_number],
        price_fetcher=lambda s: PriceContext(closes={TODAY: Decimal("42")}),
        today=TODAY,
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


def test_short_draft_saves_pending_vetting_memo(memo_store):
    thesis_llm = FakeLLM(["the bulls say backlog turns", _draft()])
    outcome = _run(_good_evidence_llm(), thesis_llm, memo_store)
    assert outcome.status == "researched"
    assert outcome.recommendation == "short"
    memo = memo_store.get(outcome.memo_id)
    assert memo.thesis_type == "short"
    assert memo.status == "pending_vetting"
    assert memo.short_block is not None
    assert memo.entry_price_ref == 42.0


def test_pass_draft_is_shadow_tracked(memo_store):
    thesis_llm = FakeLLM(["defense", _draft(recommendation="pass")])
    outcome = _run(_good_evidence_llm(), thesis_llm, memo_store)
    assert outcome.recommendation == "pass"
    assert memo_store.get(outcome.memo_id).status == "passed"


def test_insufficient_evidence_fails_without_saving(memo_store):
    starved = FakeLLM([EvidenceBatch(items=[])] * 5)
    thesis_llm = FakeLLM([])  # must never be consulted
    outcome = _run(starved, thesis_llm, memo_store)
    assert outcome.status == "failed"
    assert any("insufficient cited evidence" in e for e in outcome.errors)
    assert memo_store.list() == []
    assert thesis_llm.prompts == []


def test_inverted_price_band_is_rejected_then_fixed_on_retry(memo_store):
    # First draft puts the cover target ABOVE entry (42): rejected with the
    # short-specific error; the retry with a sane band saves.
    bad = _draft(price_target_low=50.0, price_target_high=55.0)
    good = _draft()
    thesis_llm = FakeLLM(["defense", bad, good])
    outcome = _run(_good_evidence_llm(), thesis_llm, memo_store)
    assert outcome.status == "researched"
    assert any("COVER" in e for e in outcome.errors)
    memo = memo_store.get(outcome.memo_id)
    assert memo.price_target_low == 25.0


def test_thesis_wrong_level_below_entry_is_rejected(memo_store):
    bad = _draft(price_target_high=30.0)   # below entry 42
    thesis_llm = FakeLLM(["defense", bad, bad])
    outcome = _run(_good_evidence_llm(), thesis_llm, memo_store)
    assert outcome.status == "failed"
    assert any("thesis-wrong" in e for e in outcome.errors)
