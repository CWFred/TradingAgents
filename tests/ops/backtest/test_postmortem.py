from datetime import date, timedelta

import pytest

from ops.backtest.models import (
    ContextItem,
    ProcessOutcomeQuadrant,
    ThesisAssessment,
    ThesisCorrectness,
)
from ops.backtest.postmortem import (
    AssessmentRequest,
    assess_thesis,
    assess_thesis_cached,
    assessment_cache_key,
    prepare_adjudication_evidence,
    process_quadrant,
)

pytestmark = pytest.mark.unit


class _Assessor:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def assess(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


class _Cache:
    def __init__(self):
        self.rows = {}
        self.saves = 0

    def get_thesis_assessment(self, key):
        return self.rows.get(key)

    def save_thesis_assessment(self, assessment):
        self.saves += 1
        self.rows[assessment.assessment_key] = assessment


def _item(ref: str, available_at: date, content: str = "fact") -> ContextItem:
    return ContextItem.create(
        kind="news", source_ref=ref, available_at=available_at, content=content,
    )


def _request(*, model="local:model", prompt="v1", future_content="future"):
    case_asof = date(2025, 6, 1)
    cutoff = date(2025, 9, 1)
    return AssessmentRequest.create(
        memo_key="memo-1", case_id="case-1", case_asof=case_asof,
        memo_json='{"thesis":"works"}',
        evidence=[
            _item("entry-day", case_asof),
            _item("known", cutoff, "known fact"),
            _item("future", cutoff + timedelta(days=1), future_content),
        ],
        evidence_cutoff=cutoff, model_id=model, prompt_version=prompt,
    )


def test_cache_key_versions_every_immutable_input_and_has_no_settings_input():
    kwargs = {
        "memo_hash": "memo", "facts_through": date(2026, 1, 1),
        "model": "local:model", "prompt_version": "v1", "context_hash": "facts",
    }
    base = assessment_cache_key(**kwargs)
    assert base == assessment_cache_key(**kwargs)
    for field, value in (
        ("memo_hash", "other"), ("facts_through", date(2026, 1, 2)),
        ("model", "local:other"), ("prompt_version", "v2"),
        ("context_hash", "other-facts"),
    ):
        assert assessment_cache_key(**(kwargs | {field: value})) != base


def test_adjudication_evidence_is_post_asof_cutoff_bounded_and_future_content_proof():
    first = _request(future_content="secret A")
    second = _request(future_content="secret B")

    assert [item.source_ref for item in first.evidence.included] == ["known"]
    assert first.evidence.context_hash == second.evidence.context_hash
    assert "secret A" not in first.evidence.facts_json
    assert first.assessment_key == second.assessment_key
    assert {row.split(":", 1)[0] for row in first.evidence.excluded} == {
        "entry-day", "future",
    }


def test_adjudication_cutoff_must_be_after_case():
    with pytest.raises(ValueError, match="must be after"):
        prepare_adjudication_evidence(
            [], case_asof=date(2025, 6, 1), facts_through=date(2025, 6, 1)
        )


def test_cached_assessment_calls_model_once_and_returns_domain_model():
    request = _request()
    assessor = _Assessor({
        "thesis_correct": True, "narrative": " Thesis held. ", "evidence": ["known"],
    })
    cache = _Cache()

    first = assess_thesis_cached(assessor, cache, request=request)
    second = assess_thesis_cached(assessor, cache, request=request)

    assert first is second
    assert isinstance(first, ThesisAssessment)
    assert first.correctness is ThesisCorrectness.RIGHT
    assert first.rationale == "Thesis held."
    assert first.evidence == ("known",)
    assert len(assessor.calls) == 1
    assert cache.saves == 1
    assert assessor.calls[0]["facts_through"] == request.evidence_cutoff


def test_invalid_or_out_of_context_assessment_is_never_cached():
    request = _request()
    cache = _Cache()
    assessor = _Assessor({
        "thesis_correct": False, "narrative": "wrong", "evidence": ["future"],
    })
    with pytest.raises(ValueError, match="outside cutoff"):
        assess_thesis_cached(assessor, cache, request=request)
    assert cache.rows == {}
    assert cache.saves == 0


def test_cache_hit_must_match_immutable_request_fields():
    request = _request()
    cache = _Cache()
    cache.rows[request.assessment_key] = ThesisAssessment(
        assessment_key="wrong-key", memo_key=request.memo_key, case_id=request.case_id,
        correctness=ThesisCorrectness.RIGHT, rationale="cached",
        evidence_cutoff=request.evidence_cutoff, model_id=request.model_id,
        prompt_version=request.prompt_version,
    )
    with pytest.raises(ValueError, match="conflicts"):
        assess_thesis_cached(_Assessor(), cache, request=request)


def test_assessor_failure_is_atomic():
    request = _request()
    cache = _Cache()
    with pytest.raises(RuntimeError, match="model down"):
        assess_thesis_cached(
            _Assessor(error=RuntimeError("model down")), cache, request=request,
        )
    assert cache.rows == {}


def test_opaque_compatibility_assessment_requires_structured_result():
    with pytest.raises(ValueError, match="boolean"):
        assess_thesis(
            _Assessor({"thesis_correct": "yes", "narrative": "because"}),
            memo_json="{}", facts_json="{}", facts_through=date(2026, 1, 1),
            model="local:model", prompt_version="v1", context_hash="facts",
        )


@pytest.mark.parametrize(
    ("right", "outcome", "quadrant"),
    [
        (True, "win", ProcessOutcomeQuadrant.RIGHT_THESIS_WORKED),
        (True, "loss", ProcessOutcomeQuadrant.RIGHT_THESIS_UNLUCKY),
        (False, "win", ProcessOutcomeQuadrant.WRONG_THESIS_LUCKY),
        (False, "loss", ProcessOutcomeQuadrant.WRONG_THESIS_LOST),
        (False, "wash", ProcessOutcomeQuadrant.UNGRADED),
        (False, "pending", ProcessOutcomeQuadrant.UNGRADED),
        (False, "unpriceable", ProcessOutcomeQuadrant.UNGRADED),
        (ThesisCorrectness.INDETERMINATE, "win", ProcessOutcomeQuadrant.UNGRADED),
    ],
)
def test_process_quadrants(right, outcome, quadrant):
    assert process_quadrant(thesis_correct=right, outcome_label=outcome) is quadrant
