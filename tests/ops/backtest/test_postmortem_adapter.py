import json
from datetime import date
from decimal import Decimal

import pytest

from ops.backtest.generate import NonLocalModelError
from ops.backtest.models import BacktestCase, CaseSource, PriceBar
from ops.backtest.postmortem_adapter import (
    Ds4ThesisAssessor,
    PriceEvidenceProvider,
    configured,
)
from ops.backtest.prices import PriceCache

pytestmark = pytest.mark.unit


def _bar(symbol: str, day: date, close: str = "100") -> PriceBar:
    raw = Decimal(close)
    return PriceBar(
        symbol=symbol, session=day,
        open=raw, high=raw + 1, low=raw - 1, close=raw,
        adjusted_open=raw, adjusted_high=raw + 1,
        adjusted_low=raw - 1, adjusted_close=raw,
        volume=Decimal("1000"), dividend=Decimal("0"), split_ratio=Decimal("1"),
        provider="fixture",
    )


@pytest.fixture
def seeded_price_store(tmp_path):
    store_path = tmp_path / "backtest.sqlite"
    cache = PriceCache(store_path)
    cache.upsert_bars([
        _bar("ACME", date(2025, 6, 30)),
        _bar("ACME", date(2025, 7, 2)),
        _bar("ACME", date(2025, 7, 3)),
    ])
    case = BacktestCase.create(
        sleeve="research", symbol="ACME", asof=date(2025, 7, 1),
        source=CaseSource.POINT_IN_TIME,
    )
    return str(store_path), case


class _FakeChat:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)

        class R:
            content = self.reply

        return R()


def test_assessor_parses_json_reply_and_uses_injected_client():
    fake = _FakeChat(json.dumps({
        "thesis_correct": True, "narrative": "margin inflected as claimed",
        "evidence": ["price:ACME:2025-09-02"],
    }))
    assessor = Ds4ThesisAssessor(
        "openai_compatible:deepseek-v4-flash@http://127.0.0.1:8000/v1",
        client_factory=lambda spec: fake,
    )
    raw = assessor.assess(memo_json="{}", facts_json="[]",
                          facts_through=date(2025, 9, 30))
    assert raw["thesis_correct"] is True
    assert raw["narrative"].startswith("margin")
    assert fake.prompts and "2025-09-30" in fake.prompts[0]


def test_assessor_rejects_nonlocal_model_spec():
    with pytest.raises(NonLocalModelError):
        Ds4ThesisAssessor("openai_compatible:gpt@https://api.example.com/v1")


def test_price_evidence_provider_emits_post_asof_items(tmp_path, seeded_price_store):
    # seeded_price_store: fixture seeding bars for ACME sessions
    # 2025-06-30 (pre-asof), 2025-07-02 and 2025-07-03 (post-asof) — reuse
    # the bar-seeding helper from tests/ops/backtest/test_prices.py.
    store_path, case = seeded_price_store   # case.asof == 2025-07-01
    provider = PriceEvidenceProvider(store_path)
    items = provider.evidence_for(case=case, memo_json="{}",
                                  facts_through=date(2025, 7, 2))
    refs = [item.source_ref for item in items]
    assert refs == ["price:ACME:2025-07-02"]          # pre-asof and post-cutoff excluded
    assert all(item.available_at <= date(2025, 7, 2) for item in items)


def test_configured_shape():
    cfg = configured()
    assert set(cfg) >= {"assessor", "evidence_provider", "model_id", "prompt_version"}
    assert cfg["prompt_version"] == "postmortem-v1"


def test_assessor_filters_citations_to_provided_facts():
    """ds4 naturally cites the memo's own filing accessions; the postmortem
    validator rejects any ref outside the provided facts (2026-08-02: every
    assessment failed). The assessor must intersect citations with facts."""
    import json as _json

    facts = _json.dumps([
        {"source_ref": "price:ACME:2025-09-02", "content": "{}"},
        {"source_ref": "price:ACME:2025-09-03", "content": "{}"},
    ])
    reply = _json.dumps({
        "thesis_correct": True, "narrative": "held up",
        "evidence": ["price:ACME:2025-09-02", "0000037785-25-000127:mdna"],
    })
    assessor = Ds4ThesisAssessor(
        "openai_compatible:deepseek-v4-flash@http://127.0.0.1:8000/v1",
        client_factory=lambda spec: _FakeChat(reply),
    )
    raw = assessor.assess(memo_json="{}", facts_json=facts,
                          facts_through=date(2025, 9, 30))
    assert raw["evidence"] == ["price:ACME:2025-09-02"]
