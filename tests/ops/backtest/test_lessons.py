from datetime import date

import pytest

from ops.backtest.lessons import (
    DistillationRequest,
    EfficacyPlan,
    PairedCaseInput,
    PairedResult,
    distill_lessons_cached,
    eligible_lessons,
    lesson_set_hash,
    paired_experiment_summary,
    run_paired_efficacy,
    split_holdout,
    validate_lesson_sources,
)
from ops.backtest.models import Lesson, ThesisAssessment, ThesisCorrectness

pytestmark = pytest.mark.unit


def _assessment(key: str, case_id: str, cutoff: date) -> ThesisAssessment:
    return ThesisAssessment(
        assessment_key=key, memo_key=f"memo-{case_id}", case_id=case_id,
        correctness=ThesisCorrectness.WRONG, rationale="reason",
        evidence_cutoff=cutoff, model_id="local:judge", prompt_version="pm-v1",
    )


def _lesson(
    lesson_id: str, source_cases: tuple[str, ...], eligible_from: date,
    *, text="Prefer cash flow",
) -> Lesson:
    return Lesson(
        lesson_id=lesson_id, sleeve="research", text=text,
        source_case_ids=source_cases, eligible_from=eligible_from,
        fingerprint=f"fingerprint-{lesson_id}",
    )


class _Distiller:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def distill(self, **_kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class _Cache:
    def __init__(self):
        self.rows = {}
        self.saves = 0

    def get_distilled_lessons(self, key):
        return self.rows.get(key)

    def save_distilled_lessons(self, key, lessons):
        self.saves += 1
        self.rows[key] = tuple(lessons)


def _distillation_request(*, prompt="lesson-v1"):
    return DistillationRequest.create(
        sleeve="research", training_case_ids={"train-a", "train-b"},
        holdout_case_ids={"holdout"},
        assessments=[
            _assessment("a1", "train-a", date(2026, 1, 1)),
            _assessment("a2", "train-b", date(2026, 2, 1)),
        ],
        model_id="local:distiller", prompt_version=prompt,
    )


def test_holdout_is_deterministic_disjoint_and_input_order_independent():
    first = split_holdout(["a", "b", "c", "d"], holdout_size=2, seed=7)
    second = split_holdout(reversed(["a", "b", "c", "d"]), holdout_size=2, seed=7)
    assert first == second
    assert set(first[0]).isdisjoint(first[1])
    assert sorted(first[0] + first[1]) == ["a", "b", "c", "d"]


def test_holdout_assessment_cannot_enter_distillation_request():
    with pytest.raises(ValueError, match="non-training"):
        DistillationRequest.create(
            sleeve="research", training_case_ids={"train"}, holdout_case_ids={"holdout"},
            assessments=[_assessment("bad", "holdout", date(2026, 1, 1))],
            model_id="local:d", prompt_version="v1",
        )


def test_distillation_key_is_versioned_and_holdout_membership_is_frozen():
    first = _distillation_request()
    versioned = _distillation_request(prompt="lesson-v2")
    assert first.distillation_key != versioned.distillation_key
    assert first.holdout_case_ids == ("holdout",)


def test_distillation_is_cached_source_linked_and_eligible_at_latest_source_cutoff():
    request = _distillation_request()
    distiller = _Distiller([{
        "text": " Avoid leverage. ", "source_assessment_keys": ["a2", "a1"],
    }])
    cache = _Cache()

    first = distill_lessons_cached(distiller, cache, request=request)
    second = distill_lessons_cached(distiller, cache, request=request)

    assert first == second
    assert distiller.calls == 1
    assert cache.saves == 1
    row = first[0]
    assert row.source_assessment_keys == ("a1", "a2")
    assert row.lesson.source_case_ids == ("train-a", "train-b")
    assert row.lesson.eligible_from == date(2026, 2, 1)
    assert row.lesson.text == "Avoid leverage."
    assert f"distillation:{request.distillation_key}" in row.lesson.tags
    assert "model:local:distiller" in row.lesson.tags


def test_invalid_distiller_source_is_not_partially_cached():
    request = _distillation_request()
    cache = _Cache()
    with pytest.raises(ValueError, match="unknown/non-training"):
        distill_lessons_cached(
            _Distiller([{
                "text": "leaked", "source_assessment_keys": ["holdout-assessment"],
            }]),
            cache,
            request=request,
        )
    assert cache.rows == {}
    assert cache.saves == 0


def test_distiller_failure_is_atomic():
    request = _distillation_request()
    cache = _Cache()
    with pytest.raises(RuntimeError, match="model down"):
        distill_lessons_cached(
            _Distiller(error=RuntimeError("model down")), cache, request=request,
        )
    assert cache.rows == {}


def test_holdout_case_cannot_source_lesson():
    lesson = _lesson("l1", ("holdout",), date(2026, 1, 1))
    with pytest.raises(ValueError, match="non-training"):
        validate_lesson_sources(lesson, training_case_ids={"train"})


def test_lessons_are_asof_gated_and_set_hash_uses_provenance():
    old = _lesson("old", ("a",), date(2026, 1, 1))
    same_day = _lesson("same", ("a",), date(2026, 1, 31))
    future = _lesson("future", ("b",), date(2026, 2, 1))
    assert eligible_lessons([future, same_day, old], asof=date(2026, 1, 31)) == (old,)
    changed_source = _lesson("old", ("other",), date(2026, 1, 1))
    assert lesson_set_hash([old]) != lesson_set_hash([changed_source])


class _Evaluator:
    def __init__(self):
        self.calls = []

    def evaluate(self, **kwargs):
        self.calls.append(kwargs)
        return 1.0 if kwargs["variant"] == "treated" else 0.25


def test_seeded_paired_efficacy_uses_same_pinned_inputs_and_fixed_holdout():
    plan = EfficacyPlan.create(
        sleeve="research", case_ids=["a", "b", "c", "d"], holdout_size=2, seed=11,
    )
    lesson = _lesson("l1", (plan.training_case_ids[0],), date(2026, 1, 1))
    too_late = _lesson("late", (plan.training_case_ids[0],), date(2027, 1, 1))
    inputs = {
        case_id: PairedCaseInput(
            case_id, date(2026, 2, 1), f"pinned-{case_id}", {"case": case_id}
        )
        for case_id in plan.holdout_case_ids
    }
    evaluator = _Evaluator()

    rows = run_paired_efficacy(
        plan, case_inputs=inputs, lessons=[too_late, lesson], evaluator=evaluator,
    )

    assert tuple(row.case_id for row in rows) == plan.holdout_case_ids
    assert all(row.delta == 0.75 for row in rows)
    for offset in range(0, len(evaluator.calls), 2):
        control, treated = evaluator.calls[offset:offset + 2]
        assert control["case_input"] is treated["case_input"]
        assert control["lesson_fingerprint"] is None
        assert treated["lesson_fingerprint"] == lesson_set_hash([lesson])
    assert plan.record(lesson_fingerprint=lesson_set_hash([lesson, too_late])).holdout_case_ids == (
        plan.holdout_case_ids
    )


def test_paired_efficacy_rejects_lesson_sourced_from_holdout():
    plan = EfficacyPlan.create(
        sleeve="research", case_ids=["train", "held"], holdout_size=1, seed=1,
    )
    leaked = _lesson("bad", (plan.holdout_case_ids[0],), date(2026, 1, 1))
    with pytest.raises(ValueError, match="non-training"):
        run_paired_efficacy(plan, case_inputs={}, lessons=[leaked], evaluator=_Evaluator())


def test_lesson_learned_after_holdout_asof_is_not_injected():
    plan = EfficacyPlan.create(
        sleeve="research", case_ids=["train", "held"], holdout_size=1, seed=3,
    )
    late = _lesson("late", (plan.training_case_ids[0],), date(2027, 1, 1))
    case_id = plan.holdout_case_ids[0]
    evaluator = _Evaluator()
    run_paired_efficacy(
        plan,
        case_inputs={case_id: PairedCaseInput(
            case_id, date(2026, 1, 1), "pinned", {},
        )},
        lessons=[late], evaluator=evaluator,
    )
    assert evaluator.calls[1]["lesson_fingerprint"] is None


def test_paired_summary_is_sorted_and_makes_no_significance_claim():
    summary = paired_experiment_summary([
        PairedResult("b", 2.0, 1.5), PairedResult("a", 0.0, 1.0),
    ])
    assert summary == {
        "pairs": 2, "mean_delta": 0.25, "improved": 1, "worsened": 1,
        "unchanged": 0, "deltas": (1.0, -0.5), "case_ids": ("a", "b"),
        "claim": "paired descriptive result; no significance claim",
    }


def test_paired_results_reject_nonfinite_and_duplicates():
    with pytest.raises(ValueError, match="finite"):
        PairedResult("a", float("nan"), 1.0)
    with pytest.raises(ValueError, match="duplicate"):
        paired_experiment_summary([
            PairedResult("a", 0, 1), PairedResult("a", 0, 1),
        ])
