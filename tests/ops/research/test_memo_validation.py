"""Unit tests for mechanical memo validation (the weak-model gate)."""

from datetime import date

import pytest

from ops.research.memo_validation import resolve_evidence, validate_memo
from tradingagents.memos.schema import EvidenceItem, Falsifier, Memo, ValueThesis

pytestmark = pytest.mark.unit

REF = "0000000001-26-000001:mdna"


def _evidence(ref=REF, source_type="filing"):
    return EvidenceItem(claim="revenue fell 12%", source_type=source_type, source_ref=ref)


def _machine_falsifier():
    return Falsifier(
        description="gross margin below 30% for two quarters",
        check_type="fundamental", metric="gross_margin_pct",
        operator="<", threshold=30.0, consecutive_periods=2,
    )


def _memo(**overrides):
    kwargs = {
        "ticker": "WIDG", "as_of_date": date(2026, 7, 6), "thesis_type": "value",
        "thesis": "Mispriced on a temporary distributor loss.",
        "evidence": [_evidence()],
        "value_block": ValueThesis(
            why_cheap="lost its largest distributor last quarter",
            change_trigger="insider cluster", normalized_earnings_view="~$1.20 EPS",
            quality_assessment="net cash, 20% ROIC",
        ),
        "conviction_tier": "starter", "entry_price_ref": 4.10,
        "price_target_low": 5.0, "price_target_high": 8.0, "expected_holding_months": 12,
        "must_be_true": ["distributor volume replaced within 3 quarters"],
        "falsifiers": [_machine_falsifier()],
    }
    kwargs.update(overrides)
    return Memo(**kwargs)


def test_valid_memo_passes():
    assert validate_memo(_memo(), allowed_refs={REF}, known_precedents=set()) == []


def test_prose_only_falsifiers_rejected():
    memo = _memo(falsifiers=[Falsifier(description="thesis stops working", check_type="fundamental")])
    errors = validate_memo(memo, allowed_refs={REF}, known_precedents=set())
    assert any("machine-checkable" in e for e in errors)


def test_unresolvable_citation_rejected():
    memo = _memo(evidence=[_evidence(ref="9999999999-26-000001:mdna")])
    errors = validate_memo(memo, allowed_refs={REF}, known_precedents=set())
    assert any("9999999999" in e for e in errors)


def test_invented_precedent_rejected():
    memo = _memo(precedent_memo_ids=["deadbeef"])
    errors = validate_memo(memo, allowed_refs={REF}, known_precedents={"cafebabe"})
    assert any("deadbeef" in e for e in errors)


def test_known_precedent_and_empty_precedents_ok():
    assert validate_memo(
        _memo(precedent_memo_ids=["cafebabe"]),
        allowed_refs={REF}, known_precedents={"cafebabe"},
    ) == []


def test_inverted_targets_and_bad_price_rejected():
    memo = _memo(price_target_low=9.0, price_target_high=5.0, entry_price_ref=0.0)
    errors = validate_memo(memo, allowed_refs={REF}, known_precedents=set())
    assert len(errors) == 2


def test_resolve_evidence_strips_unknown_refs_and_non_filing():
    kept, dropped = resolve_evidence(
        [
            _evidence(),
            _evidence(ref="bad-ref:mdna"),
            _evidence(source_type="news"),
        ],
        allowed_refs={REF},
    )
    assert [e.source_ref for e in kept] == [REF]
    assert len(dropped) == 2


def test_drawdown_falsifier_wrong_operator_rejected():
    # Signed-return form (< -25): under the canonical positive-percent-down
    # convention this can never trip; must be rejected at authoring time.
    memo = _memo(falsifiers=[Falsifier(
        description="drawdown", check_type="price",
        metric="drawdown_from_cost_pct", operator="<", threshold=-25.0,
    )])
    errors = validate_memo(memo, allowed_refs={REF}, known_precedents=set())
    assert any("must use > or >=" in e for e in errors)


def test_drawdown_falsifier_ratio_threshold_rejected():
    # Ratio form (> 0.25) trips on a 0.25% dip — the CRC 2026-07-13 false
    # escalation was this form against the old signed evaluator.
    memo = _memo(falsifiers=[Falsifier(
        description="drawdown", check_type="price",
        metric="drawdown_from_cost_pct", operator=">", threshold=0.25,
    )])
    errors = validate_memo(memo, allowed_refs={REF}, known_precedents=set())
    assert any("percent in [1, 100]" in e for e in errors)


def test_drawdown_falsifier_canonical_form_accepted():
    memo = _memo(falsifiers=[Falsifier(
        description="drawdown", check_type="price",
        metric="drawdown_from_cost_pct", operator=">", threshold=25.0,
    )])
    assert validate_memo(memo, allowed_refs={REF}, known_precedents=set()) == []


def test_falsifier_operator_sanitizes_common_model_variants():
    """ds4 emits unicode and space-padded comparison operators; the schema
    rejected them and crashed whole memos (2026-08-03 autopsy: 7 of 11
    changed experiment pairs were operator/format crashes, not decisions)."""
    from tradingagents.memos.schema import Falsifier

    for raw, want in [("≥", ">="), ("≤", "<="), (" >=", ">="), (">= ", ">="),
                      ("=>", ">="), ("=<", "<="), (" < ", "<")]:
        f = Falsifier(description="d", check_type="fundamental", metric="m",
                      operator=raw, threshold=1.0)
        assert f.operator == want, (raw, f.operator)
    assert Falsifier(description="d", check_type="event").operator is None


def test_memo_draft_drops_precedent_sentinels():
    """The brain's prompt says 'empty if none apply' but the model writes
    'none found' — validation then rejects the whole memo."""
    from ops.research.brain import MemoDraft

    draft = MemoDraft(
        thesis_type="value", thesis="t", conviction_tier="starter",
        price_target_low=1.0, price_target_high=2.0,
        expected_holding_months=6, must_be_true=["x"],
        falsifiers=[{"description": "d", "check_type": "price"}],
        precedent_memo_ids=["none found", "N/A", "", "memo-real123"],
        recommendation="pass",
    )
    assert draft.precedent_memo_ids == ["memo-real123"]
