"""Versioned, cached thesis assessments over cutoff-bounded evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from ops.backtest.models import (
    BacktestCase,
    ContextItem,
    OutcomeLabel,
    ProcessOutcomeQuadrant,
    ThesisAssessment,
    ThesisCorrectness,
    canonical_json,
    stable_hash,
)


class ThesisAssessor(Protocol):
    """Structured model boundary; implementations must not persist results."""

    def assess(self, *, memo_json: str, facts_json: str, facts_through: date) -> Mapping: ...


class AssessmentCache(Protocol):
    def get_thesis_assessment(self, assessment_key: str) -> ThesisAssessment | None: ...

    def save_thesis_assessment(self, assessment: ThesisAssessment) -> None: ...


class AdjudicationEvidenceProvider(Protocol):
    """Read-only boundary for facts known by a requested adjudication date."""

    def evidence_for(
        self,
        *,
        case: BacktestCase,
        memo_json: str,
        facts_through: date,
    ) -> Sequence[ContextItem]: ...


@dataclass(frozen=True)
class AdjudicationEvidence:
    included: tuple[ContextItem, ...]
    excluded: tuple[str, ...]
    context_hash: str
    facts_json: str


def prepare_adjudication_evidence(
    evidence: Sequence[ContextItem],
    *,
    case_asof: date,
    facts_through: date,
) -> AdjudicationEvidence:
    """Keep only facts strictly after the case and through adjudication.

    Exclusion strings contain provenance and dates, never excluded content, so
    future facts cannot influence either the model prompt or the cache key.
    """
    if facts_through <= case_asof:
        raise ValueError("facts_through must be after the case asof")
    included_by_id: dict[str, ContextItem] = {}
    excluded: list[str] = []
    for item in evidence:
        if item.available_at <= case_asof:
            excluded.append(
                f"{item.source_ref}: not post-asof ({item.available_at.isoformat()})"
            )
            continue
        if item.available_at > facts_through:
            excluded.append(
                f"{item.source_ref}: after evidence cutoff ({item.available_at.isoformat()})"
            )
            continue
        included_by_id[item.item_id] = item
    included = tuple(sorted(
        included_by_id.values(),
        key=lambda item: (item.available_at, item.kind, item.source_ref, item.item_id),
    ))
    facts_payload = [
        {
            "kind": item.kind,
            "source_ref": item.source_ref,
            "available_at": item.available_at,
            "content": item.content,
            "content_hash": item.content_hash,
            "metadata": item.metadata,
        }
        for item in included
    ]
    facts_json = canonical_json(facts_payload)
    return AdjudicationEvidence(
        included=included,
        excluded=tuple(sorted(excluded)),
        context_hash=stable_hash(facts_payload),
        facts_json=facts_json,
    )


def assessment_cache_key(
    *,
    memo_hash: str,
    facts_through: date,
    model: str,
    prompt_version: str,
    context_hash: str,
    memo_key: str | None = None,
    case_id: str | None = None,
) -> str:
    """Stable key deliberately excluding replay settings and outcome labels."""
    for name, value in (
        ("memo_hash", memo_hash),
        ("model", model),
        ("prompt_version", prompt_version),
        ("context_hash", context_hash),
    ):
        if not value.strip():
            raise ValueError(f"{name} must not be empty")
    return "assessment-" + stable_hash({
        "case_id": case_id,
        "context_hash": context_hash,
        "facts_through": facts_through,
        "memo_hash": memo_hash,
        "memo_key": memo_key,
        "model": model,
        "prompt_version": prompt_version,
    })


@dataclass(frozen=True)
class AssessmentRequest:
    assessment_key: str
    memo_key: str
    memo_hash: str
    case_id: str
    case_asof: date
    memo_json: str
    evidence_cutoff: date
    evidence: AdjudicationEvidence
    model_id: str
    prompt_version: str

    @classmethod
    def create(
        cls,
        *,
        memo_key: str,
        case_id: str,
        case_asof: date,
        memo_json: str,
        evidence: Sequence[ContextItem],
        evidence_cutoff: date,
        model_id: str,
        prompt_version: str,
    ) -> AssessmentRequest:
        if not memo_key.strip() or not case_id.strip() or not memo_json.strip():
            raise ValueError("memo_key, case_id, and memo_json must not be empty")
        bounded = prepare_adjudication_evidence(
            evidence, case_asof=case_asof, facts_through=evidence_cutoff,
        )
        memo_hash = stable_hash({"memo_json": memo_json})
        key = assessment_cache_key(
            memo_hash=memo_hash,
            facts_through=evidence_cutoff,
            model=model_id,
            prompt_version=prompt_version,
            context_hash=bounded.context_hash,
            memo_key=memo_key,
            case_id=case_id,
        )
        return cls(
            assessment_key=key,
            memo_key=memo_key,
            memo_hash=memo_hash,
            case_id=case_id,
            case_asof=case_asof,
            memo_json=memo_json,
            evidence_cutoff=evidence_cutoff,
            evidence=bounded,
            model_id=model_id,
            prompt_version=prompt_version,
        )


def _parse_assessment(raw: Mapping[str, Any], request: AssessmentRequest) -> ThesisAssessment:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("thesis_correct"), bool):
        raise ValueError("post-mortem must return boolean thesis_correct")
    narrative = raw.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        raise ValueError("post-mortem must return a non-empty narrative")
    cited = raw.get("evidence", ())
    if not isinstance(cited, (list, tuple)) or not all(isinstance(ref, str) for ref in cited):
        raise ValueError("post-mortem evidence must be a list of source refs")
    allowed = {item.source_ref for item in request.evidence.included}
    unknown = sorted(set(cited) - allowed)
    if unknown:
        raise ValueError(f"post-mortem cited evidence outside cutoff context: {unknown}")
    return ThesisAssessment(
        assessment_key=request.assessment_key,
        memo_key=request.memo_key,
        case_id=request.case_id,
        correctness=(ThesisCorrectness.RIGHT
                     if raw["thesis_correct"] else ThesisCorrectness.WRONG),
        rationale=narrative.strip(),
        evidence_cutoff=request.evidence_cutoff,
        model_id=request.model_id,
        prompt_version=request.prompt_version,
        evidence=tuple(sorted(set(cited))),
    )


def assess_thesis_cached(
    assessor: ThesisAssessor,
    cache: AssessmentCache,
    *,
    request: AssessmentRequest,
) -> ThesisAssessment:
    """Return a versioned cached assessment, saving only validated output."""
    cached = cache.get_thesis_assessment(request.assessment_key)
    if cached is not None:
        if (
            cached.assessment_key != request.assessment_key
            or
            cached.memo_key != request.memo_key
            or cached.case_id != request.case_id
            or cached.evidence_cutoff != request.evidence_cutoff
            or cached.model_id != request.model_id
            or cached.prompt_version != request.prompt_version
        ):
            raise ValueError("cached thesis assessment conflicts with immutable request")
        return cached
    raw = assessor.assess(
        memo_json=request.memo_json,
        facts_json=request.evidence.facts_json,
        facts_through=request.evidence_cutoff,
    )
    assessment = _parse_assessment(raw, request)
    cache.save_thesis_assessment(assessment)
    return assessment


def assess_thesis(
    assessor: ThesisAssessor,
    *,
    memo_json: str,
    facts_json: str,
    facts_through: date,
    model: str,
    prompt_version: str,
    context_hash: str,
) -> ThesisAssessment:
    """Compatibility wrapper for already-sealed fact JSON.

    New callers should use :func:`assess_thesis_cached` with an
    :class:`AssessmentRequest`; this wrapper cannot independently inspect an
    opaque JSON blob's artifact dates.
    """
    memo_hash = stable_hash({"memo_json": memo_json})
    request = AssessmentRequest(
        assessment_key=assessment_cache_key(
            memo_hash=memo_hash, facts_through=facts_through, model=model,
            prompt_version=prompt_version, context_hash=context_hash,
            memo_key=f"memo-{memo_hash}", case_id="unlinked",
        ),
        memo_key=f"memo-{memo_hash}",
        memo_hash=memo_hash,
        case_id="unlinked",
        case_asof=facts_through,
        memo_json=memo_json,
        evidence_cutoff=facts_through,
        evidence=AdjudicationEvidence((), (), context_hash, facts_json),
        model_id=model,
        prompt_version=prompt_version,
    )
    raw = assessor.assess(
        memo_json=memo_json, facts_json=facts_json, facts_through=facts_through,
    )
    return _parse_assessment(raw, request)


def process_quadrant(
    *, thesis_correct: bool | ThesisCorrectness, outcome_label: str | OutcomeLabel,
) -> ProcessOutcomeQuadrant:
    """Cross one cached thesis judgment with a run-specific mechanical result."""
    if isinstance(thesis_correct, ThesisCorrectness):
        correctness = thesis_correct
    elif isinstance(thesis_correct, bool):
        correctness = ThesisCorrectness.RIGHT if thesis_correct else ThesisCorrectness.WRONG
    else:
        raise ValueError(f"unknown thesis correctness {thesis_correct!r}")
    label = outcome_label.value if isinstance(outcome_label, OutcomeLabel) else outcome_label
    if label in {"wash", "pending", "unpriceable"} or correctness is ThesisCorrectness.INDETERMINATE:
        return ProcessOutcomeQuadrant.UNGRADED
    if label not in {"win", "loss"}:
        raise ValueError(f"unknown outcome label {label!r}")
    if correctness is ThesisCorrectness.RIGHT and label == "win":
        return ProcessOutcomeQuadrant.RIGHT_THESIS_WORKED
    if correctness is ThesisCorrectness.RIGHT:
        return ProcessOutcomeQuadrant.RIGHT_THESIS_UNLUCKY
    if label == "win":
        return ProcessOutcomeQuadrant.WRONG_THESIS_LUCKY
    return ProcessOutcomeQuadrant.WRONG_THESIS_LOST
