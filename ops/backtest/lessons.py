"""Leakage-safe lesson distillation and deterministic paired efficacy tests."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from statistics import mean
from typing import Any, Protocol

from ops.backtest.models import (
    ExperimentRecord,
    Lesson,
    ThesisAssessment,
    stable_hash,
)


@dataclass(frozen=True)
class DistilledLesson:
    lesson: Lesson
    distillation_key: str
    source_assessment_keys: tuple[str, ...]


class LessonDistiller(Protocol):
    def distill(
        self,
        *,
        assessments: Sequence[ThesisAssessment],
        model_id: str,
        prompt_version: str,
    ) -> Sequence[Mapping[str, Any]]: ...


class LessonCache(Protocol):
    def get_distilled_lessons(self, distillation_key: str) -> Sequence[DistilledLesson] | None: ...

    def save_distilled_lessons(
        self, distillation_key: str, lessons: Sequence[DistilledLesson]
    ) -> None: ...


def split_holdout(
    case_ids: Iterable[str], *, holdout_size: int, seed: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return stable ``(train, holdout)`` membership before distillation.

    SHA-256 ranking avoids dependence on input order or a Python RNG
    implementation while still making the seed an explicit experiment input.
    """
    ids = sorted(set(case_ids))
    if any(not isinstance(case_id, str) or not case_id.strip() for case_id in ids):
        raise ValueError("case ids must not be empty")
    if holdout_size < 0 or holdout_size > len(ids):
        raise ValueError("holdout_size must be between zero and the case count")
    ranked = sorted(
        ids,
        key=lambda case_id: (
            hashlib.sha256(f"{seed}\0{case_id}".encode()).digest(), case_id,
        ),
    )
    holdout = tuple(sorted(ranked[:holdout_size]))
    holdout_set = set(holdout)
    train = tuple(case_id for case_id in ids if case_id not in holdout_set)
    return train, holdout


@dataclass(frozen=True)
class DistillationRequest:
    distillation_key: str
    sleeve: str
    training_case_ids: tuple[str, ...]
    holdout_case_ids: tuple[str, ...]
    assessments: tuple[ThesisAssessment, ...]
    model_id: str
    prompt_version: str

    @classmethod
    def create(
        cls,
        *,
        sleeve: str,
        training_case_ids: Iterable[str],
        holdout_case_ids: Iterable[str],
        assessments: Sequence[ThesisAssessment],
        model_id: str,
        prompt_version: str,
    ) -> DistillationRequest:
        train = tuple(sorted(set(training_case_ids)))
        holdout = tuple(sorted(set(holdout_case_ids)))
        if not sleeve.strip() or not model_id.strip() or not prompt_version.strip():
            raise ValueError("sleeve, model_id, and prompt_version must not be empty")
        if set(train) & set(holdout):
            raise ValueError("training and holdout cases must be disjoint")
        if any(not isinstance(case_id, str) or not case_id.strip()
               for case_id in (*train, *holdout)):
            raise ValueError("training and holdout case ids must not be empty")
        ordered = tuple(sorted(assessments, key=lambda row: row.assessment_key))
        if len({row.assessment_key for row in ordered}) != len(ordered):
            raise ValueError("assessment keys must be unique")
        leaked = sorted({row.case_id for row in ordered} - set(train))
        if leaked:
            raise ValueError(f"distillation assessments include non-training cases: {leaked}")
        key = "distillation-" + stable_hash({
            "sleeve": sleeve.strip().lower(),
            "training_case_ids": train,
            "holdout_case_ids": holdout,
            "assessment_keys": [row.assessment_key for row in ordered],
            "model_id": model_id,
            "prompt_version": prompt_version,
        })
        return cls(
            distillation_key=key,
            sleeve=sleeve.strip().lower(),
            training_case_ids=train,
            holdout_case_ids=holdout,
            assessments=ordered,
            model_id=model_id,
            prompt_version=prompt_version,
        )


def _validate_distilled_lessons(
    lessons: Sequence[DistilledLesson], request: DistillationRequest,
) -> tuple[DistilledLesson, ...]:
    known = {row.assessment_key: row for row in request.assessments}
    out: dict[str, DistilledLesson] = {}
    for row in lessons:
        if row.distillation_key != request.distillation_key:
            raise ValueError("cached lesson belongs to another distillation request")
        if not row.source_assessment_keys:
            raise ValueError("each lesson must link at least one source assessment")
        unknown = sorted(set(row.source_assessment_keys) - set(known))
        if unknown:
            raise ValueError(f"lesson links unknown/non-training assessments: {unknown}")
        expected_cases = tuple(sorted({known[key].case_id for key in row.source_assessment_keys}))
        if row.lesson.source_case_ids != expected_cases:
            raise ValueError("lesson source cases do not match source assessments")
        validate_lesson_sources(row.lesson, training_case_ids=set(request.training_case_ids))
        expected_eligible = max(known[key].evidence_cutoff for key in row.source_assessment_keys)
        if row.lesson.eligible_from != expected_eligible:
            raise ValueError("lesson eligible_from must equal its latest evidence cutoff")
        if row.lesson.sleeve != request.sleeve:
            raise ValueError("cached lesson sleeve does not match distillation request")
        required_tags = {
            "backtest-lesson",
            f"distillation:{request.distillation_key}",
            f"model:{request.model_id}",
            f"prompt:{request.prompt_version}",
        }
        if not required_tags.issubset(row.lesson.tags):
            raise ValueError("distilled lesson is missing immutable provenance tags")
        if row.lesson.fingerprint in out:
            raise ValueError("distillation returned duplicate lesson fingerprints")
        out[row.lesson.fingerprint] = row
    return tuple(sorted(out.values(), key=lambda row: row.lesson.lesson_id))


def distill_lessons_cached(
    distiller: LessonDistiller,
    cache: LessonCache,
    *,
    request: DistillationRequest,
) -> tuple[DistilledLesson, ...]:
    """Distill training assessments and atomically cache only valid lessons."""
    cached = cache.get_distilled_lessons(request.distillation_key)
    if cached is not None:
        return _validate_distilled_lessons(tuple(cached), request)
    raw_lessons = distiller.distill(
        assessments=request.assessments,
        model_id=request.model_id,
        prompt_version=request.prompt_version,
    )
    known = {row.assessment_key: row for row in request.assessments}
    built: list[DistilledLesson] = []
    for raw in raw_lessons:
        if not isinstance(raw, Mapping):
            raise ValueError("distiller outputs must be mappings")
        text = raw.get("text")
        sources = raw.get("source_assessment_keys")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("distilled lesson text must be non-empty")
        if (
            not isinstance(sources, (list, tuple))
            or not sources
            or not all(isinstance(key, str) for key in sources)
        ):
            raise ValueError("distilled lesson requires source_assessment_keys")
        source_keys = tuple(sorted(set(sources)))
        unknown = sorted(set(source_keys) - set(known))
        if unknown:
            raise ValueError(f"lesson links unknown/non-training assessments: {unknown}")
        source_cases = tuple(sorted({known[key].case_id for key in source_keys}))
        eligible_from = max(known[key].evidence_cutoff for key in source_keys)
        fingerprint = stable_hash({
            "distillation_key": request.distillation_key,
            "text": text.strip(),
            "source_assessment_keys": source_keys,
            "eligible_from": eligible_from,
        })
        lesson = Lesson(
            lesson_id=f"lesson-{fingerprint[:24]}",
            sleeve=request.sleeve,
            text=text.strip(),
            source_case_ids=source_cases,
            eligible_from=eligible_from,
            fingerprint=fingerprint,
            tags=(
                "backtest-lesson",
                f"distillation:{request.distillation_key}",
                f"model:{request.model_id}",
                f"prompt:{request.prompt_version}",
            ),
        )
        built.append(DistilledLesson(lesson, request.distillation_key, source_keys))
    validated = _validate_distilled_lessons(built, request)
    cache.save_distilled_lessons(request.distillation_key, validated)
    return validated


def validate_lesson_sources(lesson: Lesson, *, training_case_ids: set[str]) -> None:
    if not lesson.source_case_ids:
        raise ValueError("lesson must have at least one source case")
    leaked = sorted(set(lesson.source_case_ids) - training_case_ids)
    if leaked:
        raise ValueError(f"lesson sources include non-training cases: {leaked}")


def eligible_lessons(lessons: Iterable[Lesson], *, asof: date) -> tuple[Lesson, ...]:
    return tuple(sorted(
        (lesson for lesson in lessons if lesson.eligible_from < asof),
        key=lambda lesson: (lesson.eligible_from, lesson.lesson_id),
    ))


def lesson_set_hash(lessons: Iterable[Lesson]) -> str:
    ordered = sorted(lessons, key=lambda item: (item.lesson_id, item.fingerprint))
    return stable_hash([
        {
            "lesson_id": lesson.lesson_id,
            "sleeve": lesson.sleeve,
            "text": lesson.text,
            "fingerprint": lesson.fingerprint,
            "eligible_from": lesson.eligible_from,
            "source_case_ids": lesson.source_case_ids,
            "tags": lesson.tags,
        }
        for lesson in ordered
    ])


@dataclass(frozen=True)
class PairedCaseInput:
    case_id: str
    asof: date
    pinned_input_hash: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PairedResult:
    case_id: str
    control_quality: float
    treated_quality: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.control_quality) or not math.isfinite(self.treated_quality):
            raise ValueError("paired quality values must be finite")

    @property
    def delta(self) -> float:
        return self.treated_quality - self.control_quality


@dataclass(frozen=True)
class EfficacyPlan:
    experiment_id: str
    sleeve: str
    seed: int
    training_case_ids: tuple[str, ...]
    holdout_case_ids: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        sleeve: str,
        case_ids: Iterable[str],
        holdout_size: int,
        seed: int,
    ) -> EfficacyPlan:
        train, holdout = split_holdout(case_ids, holdout_size=holdout_size, seed=seed)
        if not sleeve.strip():
            raise ValueError("sleeve must not be empty")
        identity = {"sleeve": sleeve.strip().lower(), "seed": seed,
                    "train": train, "holdout": holdout}
        return cls(
            experiment_id=f"experiment-{stable_hash(identity)[:24]}",
            sleeve=sleeve.strip().lower(), seed=seed,
            training_case_ids=train, holdout_case_ids=holdout,
        )

    def record(self, *, lesson_fingerprint: str) -> ExperimentRecord:
        return ExperimentRecord(
            experiment_id=self.experiment_id,
            sleeve=self.sleeve,
            seed=self.seed,
            holdout_case_ids=self.holdout_case_ids,
            lesson_fingerprint=lesson_fingerprint,
        )


class PairedEvaluator(Protocol):
    def evaluate(
        self,
        *,
        case_input: PairedCaseInput,
        variant: str,
        lesson_fingerprint: str | None,
    ) -> float: ...


def run_paired_efficacy(
    plan: EfficacyPlan,
    *,
    case_inputs: Mapping[str, PairedCaseInput],
    lessons: Sequence[Lesson],
    evaluator: PairedEvaluator,
) -> tuple[PairedResult, ...]:
    """Evaluate paired control/treated memos over fixed holdout membership."""
    for lesson in lessons:
        validate_lesson_sources(lesson, training_case_ids=set(plan.training_case_ids))
    rows: list[PairedResult] = []
    for case_id in plan.holdout_case_ids:
        case_input = case_inputs.get(case_id)
        if case_input is None or case_input.case_id != case_id:
            raise ValueError(f"missing or mismatched pinned input for holdout case {case_id}")
        if not case_input.pinned_input_hash.strip():
            raise ValueError(f"holdout case {case_id} has no pinned input hash")
        case_lessons = eligible_lessons(lessons, asof=case_input.asof)
        fingerprint = lesson_set_hash(case_lessons) if case_lessons else None
        control = evaluator.evaluate(
            case_input=case_input, variant="control", lesson_fingerprint=None,
        )
        treated = evaluator.evaluate(
            case_input=case_input, variant="treated", lesson_fingerprint=fingerprint,
        )
        try:
            rows.append(PairedResult(case_id, float(control), float(treated)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"paired evaluator returned non-numeric quality for {case_id}") from exc
    return tuple(rows)


def paired_experiment_summary(results: Iterable[PairedResult]) -> dict:
    rows = sorted(results, key=lambda row: row.case_id)
    if len({row.case_id for row in rows}) != len(rows):
        raise ValueError("paired results contain duplicate case ids")
    deltas = [row.delta for row in rows]
    return {
        "pairs": len(rows),
        "mean_delta": mean(deltas) if deltas else None,
        "improved": sum(delta > 0 for delta in deltas),
        "worsened": sum(delta < 0 for delta in deltas),
        "unchanged": sum(delta == 0 for delta in deltas),
        "deltas": tuple(deltas),
        "case_ids": tuple(row.case_id for row in rows),
        "claim": "paired descriptive result; no significance claim",
    }
