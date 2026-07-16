"""Resumable, frozen memo generation for backtest cases.

The coordinator is intentionally independent of SQLite details.  Its store
protocol describes atomic queue operations; :class:`BacktestStore` can satisfy
that protocol without putting LLM or network behavior inside the persistence
layer.  Tests use an in-memory fake and make no external calls.
"""
from __future__ import annotations

import ipaddress
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from ops.backtest.context import manifest_filing_adapters, manifest_price_fetcher
from ops.backtest.models import (
    BacktestCase,
    ContextManifest,
    canonical_json,
    stable_hash,
)
from ops.research.models import ModelSpec, parse_model_spec

GuardrailStatus = Literal["accepted", "rejected", "failed"]


class NonLocalModelError(ValueError):
    """A memo-generation model could send case information off machine."""


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_local_model_spec(spec: str) -> ModelSpec:
    """Resolve the endpoint actually used and require a loopback transport."""
    parsed = parse_model_spec(spec)
    if parsed.provider not in {"openai_compatible", "ollama"}:
        raise NonLocalModelError(
            f"memo model provider {parsed.provider!r} is not an approved local provider"
        )
    if parsed.provider == "openai_compatible":
        if parsed.base_url is None:
            raise NonLocalModelError(
                f"memo model {spec!r} requires an explicit loopback base URL"
            )
        resolved_url = parsed.base_url
    else:
        # Match the client registry's exact resolution precedence. Validation
        # must see a late-bound OLLAMA_BASE_URL or a remote env override could
        # bypass a check performed only on the textual model spec.
        resolved_url = (
            parsed.base_url
            or os.environ.get("OLLAMA_BASE_URL")
            or "http://localhost:11434/v1"
        ).strip()

    url = urlparse(resolved_url)
    try:
        _port = url.port
    except ValueError as exc:
        raise NonLocalModelError(
            f"memo model base URL has an invalid port: {resolved_url!r}"
        ) from exc
    if url.scheme not in {"http", "https"} or not _is_loopback_host(url.hostname):
        raise NonLocalModelError(
            f"memo model base URL must use a loopback host, got {resolved_url!r}"
        )
    if url.username is not None or url.password is not None:
        raise NonLocalModelError("memo model loopback URL must not contain credentials")
    return ModelSpec(parsed.provider, parsed.model, resolved_url)


def canonical_local_model_id(spec: str) -> str:
    """Return a model identity containing the endpoint that was validated."""
    parsed = validate_local_model_spec(spec)
    return f"{parsed.provider}:{parsed.model}@{parsed.base_url}"


@dataclass(frozen=True)
class GenerationRequest:
    generation_key: str
    memo_key: str
    case: BacktestCase
    manifest: ContextManifest
    brain_version: str
    prompt_version: str
    evidence_model_id: str
    thesis_model_id: str
    context_hash: str
    lesson_fingerprint: str
    conditioning_hash: str
    hit_payload: Mapping[str, Any]
    conditioning: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        case: BacktestCase,
        manifest: ContextManifest,
        brain_version: str,
        prompt_version: str,
        evidence_model_id: str,
        thesis_model_id: str,
        lesson_fingerprint: str = "none",
        conditioning: Mapping[str, Any] | None = None,
        hit_payload: Mapping[str, Any] | None = None,
    ) -> GenerationRequest:
        case.validate_cutoff()
        manifest.validate_point_in_time()
        if manifest.case_id != case.case_id or manifest.asof != case.asof:
            raise ValueError("generation manifest does not match its case identity/asof")
        for name, value in (
            ("brain_version", brain_version),
            ("prompt_version", prompt_version),
            ("lesson_fingerprint", lesson_fingerprint),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        evidence_model_id = canonical_local_model_id(evidence_model_id)
        thesis_model_id = canonical_local_model_id(thesis_model_id)
        conditioning_payload = dict(conditioning or {})
        conditioning_hash = stable_hash({
            "lesson_fingerprint": lesson_fingerprint,
            "conditioning": conditioning_payload,
        })
        identity = {
            "case_id": case.case_id,
            "brain_version": brain_version,
            "prompt_version": prompt_version,
            "evidence_model_id": evidence_model_id,
            "thesis_model_id": thesis_model_id,
            "context_hash": manifest.manifest_hash,
            "lesson_fingerprint": lesson_fingerprint,
            "conditioning_hash": conditioning_hash,
        }
        memo_key = f"memo-{stable_hash(identity)}"
        payload = dict(hit_payload or case.trigger.get("screen_payload", case.trigger))
        return cls(
            generation_key=memo_key,
            memo_key=memo_key,
            case=case,
            manifest=manifest,
            brain_version=brain_version,
            prompt_version=prompt_version,
            evidence_model_id=evidence_model_id,
            thesis_model_id=thesis_model_id,
            context_hash=manifest.manifest_hash,
            lesson_fingerprint=lesson_fingerprint,
            conditioning_hash=conditioning_hash,
            hit_payload=payload,
            conditioning=conditioning_payload,
        )


@dataclass(frozen=True)
class FrozenMemoRecord:
    memo_key: str
    case_id: str
    manifest_id: str
    brain_version: str
    prompt_version: str
    evidence_model_id: str
    thesis_model_id: str
    context_hash: str
    lesson_fingerprint: str
    conditioning_hash: str
    recommendation: str
    conviction: str | None
    guardrail_status: GuardrailStatus
    guardrail_reason: str | None
    memo_json: str | None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def terminal(
        cls,
        request: GenerationRequest,
        *,
        status: GuardrailStatus,
        reason: str | None,
        recommendation: str = "pass",
        conviction: str | None = None,
        memo_json: str | None = None,
    ) -> FrozenMemoRecord:
        if status == "accepted" and memo_json is None:
            raise ValueError("an accepted generation requires memo_json")
        if status != "accepted" and recommendation.lower() != "pass":
            raise ValueError("rejected/failed generation must be gradeable as PASS")
        return cls(
            memo_key=request.memo_key,
            case_id=request.case.case_id,
            manifest_id=request.manifest.manifest_id,
            brain_version=request.brain_version,
            prompt_version=request.prompt_version,
            evidence_model_id=request.evidence_model_id,
            thesis_model_id=request.thesis_model_id,
            context_hash=request.context_hash,
            lesson_fingerprint=request.lesson_fingerprint,
            conditioning_hash=request.conditioning_hash,
            recommendation=recommendation.lower(),
            conviction=conviction,
            guardrail_status=status,
            guardrail_reason=reason,
            memo_json=memo_json,
        )


@dataclass(frozen=True)
class GenerationClaim:
    generation_key: str
    case_id: str
    attempt_count: int


class GenerationStore(Protocol):
    """Atomic persistence boundary required by the generation coordinator."""

    def get_frozen_memo(self, memo_key: str) -> FrozenMemoRecord | None: ...

    def ensure_generation_job(self, request: GenerationRequest) -> None: ...

    def requeue_stale_generation_jobs(self, *, stale_before: datetime) -> int: ...

    def claim_next_generation_job(self) -> GenerationClaim | None:
        """Atomically claim the oldest pending job by case asof/creation order."""
        ...

    def finish_generation_job(
        self, claim: GenerationClaim, record: FrozenMemoRecord
    ) -> None: ...


@dataclass(frozen=True)
class GenerationPlan:
    requests: tuple[GenerationRequest, ...]
    cached: tuple[str, ...]
    pending: tuple[str, ...]

    def request_by_key(self) -> dict[str, GenerationRequest]:
        return {request.generation_key: request for request in self.requests}


def plan_generation(
    requests: Sequence[GenerationRequest],
    *,
    store: GenerationStore,
) -> GenerationPlan:
    """Idempotently enqueue missing requests, oldest case first."""
    by_key: dict[str, GenerationRequest] = {}
    for request in requests:
        existing = by_key.get(request.generation_key)
        if existing is not None and _request_content(existing) != _request_content(request):
            raise ValueError(f"conflicting generation request {request.generation_key}")
        by_key[request.generation_key] = request
    ordered = tuple(sorted(
        by_key.values(),
        key=lambda request: (request.case.asof, request.case.symbol, request.generation_key),
    ))
    cached: list[str] = []
    pending: list[str] = []
    for request in ordered:
        if store.get_frozen_memo(request.memo_key) is not None:
            cached.append(request.generation_key)
            continue
        store.ensure_generation_job(request)
        pending.append(request.generation_key)
    return GenerationPlan(ordered, tuple(cached), tuple(pending))


def _request_content(request: GenerationRequest) -> str:
    """Logical request content, excluding observational creation timestamps."""
    return canonical_json({
        "generation_key": request.generation_key,
        "case_id": request.case.case_id,
        "case_trigger": request.case.trigger,
        "source": request.case.source,
        "manifest_hash": request.manifest.manifest_hash,
        "brain_version": request.brain_version,
        "prompt_version": request.prompt_version,
        "evidence_model_id": request.evidence_model_id,
        "thesis_model_id": request.thesis_model_id,
        "lesson_fingerprint": request.lesson_fingerprint,
        "conditioning_hash": request.conditioning_hash,
        "hit_payload": request.hit_payload,
    })


class _MemoCaptureStore:
    """Minimal isolated MemoStore interface consumed by ``research_hit``."""

    def __init__(self, precedents: Sequence[object] = ()) -> None:
        self._precedents = tuple(precedents)
        self._captured: dict[str, Any] = {}

    def list(self, *, ticker: str | None = None, **_kwargs: Any) -> list[object]:
        if ticker is None:
            return list(self._precedents)
        return [memo for memo in self._precedents
                if str(getattr(memo, "ticker", "")).upper() == ticker.upper()]

    def save(self, memo: Any) -> None:
        memo_id = str(memo.memo_id)
        if memo_id in self._captured:
            raise ValueError(f"duplicate generated memo id {memo_id}")
        self._captured[memo_id] = memo

    def mark_passed(self, memo_id: str) -> None:
        memo = self._captured[memo_id]
        memo.status = "passed"

    def get(self, memo_id: str) -> object | None:
        return self._captured.get(memo_id)


def _memo_json(memo: object) -> str:
    dump_json = getattr(memo, "model_dump_json", None)
    if callable(dump_json):
        return str(dump_json())
    return canonical_json(memo)


def generate_research_memo(
    request: GenerationRequest,
    *,
    evidence_llm: object,
    thesis_llm: object,
    list_filings: Callable[..., Sequence[object]] | None = None,
    research_fn: Callable[..., object] | None = None,
    fetch_text: Callable[..., str] | None = None,
    price_fetcher: Callable[..., object] | None = None,
    precedent_memos: Sequence[object] = (),
) -> FrozenMemoRecord:
    """Run the research brain using only content sealed in the manifest.

    The legacy fetcher arguments remain accepted for API compatibility, but
    are intentionally never called. They cannot add filing text, references,
    or prices that were absent when the manifest was hashed.
    """
    validate_local_model_spec(request.evidence_model_id)
    validate_local_model_spec(request.thesis_model_id)
    del list_filings, fetch_text, price_fetcher
    if precedent_memos:
        raise ValueError(
            "precedent memos must be materialized into the frozen manifest before generation"
        )
    if research_fn is None:
        from ops.research.brain import research_hit

        research_fn = research_hit
    memo_sink = _MemoCaptureStore()
    frozen_filings, frozen_text = manifest_filing_adapters(
        request.manifest, symbol=request.case.symbol,
    )
    frozen_prices = manifest_price_fetcher(
        request.manifest, symbol=request.case.symbol,
    )
    hit = {
        "id": request.generation_key,
        "run_id": "backtest",
        "symbol": request.case.symbol,
        "asof": request.case.asof.isoformat(),
        "status": "pending",
        "payload": dict(request.hit_payload),
    }
    kwargs: dict[str, Any] = {
        "evidence_llm": evidence_llm,
        "thesis_llm": thesis_llm,
        "memo_store": memo_sink,
        "list_filings": frozen_filings,
        "fetch_text": frozen_text,
        "price_fetcher": frozen_prices,
        "today": request.case.asof,
        "thesis_model_spec": request.thesis_model_id,
    }
    outcome = research_fn(hit, **kwargs)
    errors = tuple(str(error) for error in getattr(outcome, "errors", ()))
    if getattr(outcome, "status", None) != "researched":
        return FrozenMemoRecord.terminal(
            request,
            status="rejected",
            reason="; ".join(errors) or "research brain rejected memo without a reason",
        )

    memo_id = getattr(outcome, "memo_id", None)
    memo = memo_sink.get(str(memo_id)) if memo_id is not None else None
    if memo is None:
        raise RuntimeError("research brain reported success without saving an isolated memo")
    if getattr(memo, "as_of_date", request.case.asof) != request.case.asof:
        raise RuntimeError("research brain emitted a memo with the wrong as_of_date")
    recommendation = str(getattr(outcome, "recommendation", "pass")).lower()
    conviction = getattr(memo, "conviction_tier", None)
    return FrozenMemoRecord.terminal(
        request,
        status="accepted",
        reason=None,
        recommendation=recommendation,
        conviction=str(conviction) if conviction is not None else None,
        memo_json=_memo_json(memo),
    )


@dataclass(frozen=True)
class GenerationSummary:
    attempted: int
    accepted: int
    rejected: int
    failed: int
    still_pending: int


def run_generation_jobs(
    plan: GenerationPlan,
    *,
    store: GenerationStore,
    generator: Callable[[GenerationRequest], FrozenMemoRecord],
    stale_before: datetime | None = None,
    max_jobs: int | None = None,
) -> GenerationSummary:
    """Resume stale work and drain claimed jobs without duplicating memos."""
    if max_jobs is not None and max_jobs < 0:
        raise ValueError("max_jobs must not be negative")
    if stale_before is not None:
        if stale_before.tzinfo is None or stale_before.utcoffset() is None:
            raise ValueError("stale_before must be timezone-aware")
        store.requeue_stale_generation_jobs(stale_before=stale_before)

    request_by_key = plan.request_by_key()
    attempted = accepted = rejected = failed = 0
    while max_jobs is None or attempted < max_jobs:
        claim = store.claim_next_generation_job()
        if claim is None:
            break
        request = request_by_key.get(claim.generation_key)
        if request is None:
            raise KeyError(
                f"claimed generation key {claim.generation_key!r} was not in the frozen plan"
            )
        attempted += 1
        try:
            record = generator(request)
            if record.memo_key != request.memo_key or record.case_id != request.case.case_id:
                raise ValueError("generator returned a record for another request")
        except Exception as exc:  # one failed name must not strand the resumable queue
            record = FrozenMemoRecord.terminal(
                request,
                status="failed",
                reason=f"{type(exc).__name__}: {exc}",
            )
        store.finish_generation_job(claim, record)
        if record.guardrail_status == "accepted":
            accepted += 1
        elif record.guardrail_status == "rejected":
            rejected += 1
        else:
            failed += 1

    still_pending = sum(
        store.get_frozen_memo(key) is None for key in plan.pending
    )
    return GenerationSummary(attempted, accepted, rejected, failed, still_pending)
