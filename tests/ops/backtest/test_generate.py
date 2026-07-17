from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ops.backtest.generate import (
    FrozenMemoRecord,
    GenerationClaim,
    GenerationRequest,
    GenerationSummary,
    NonLocalModelError,
    generate_research_memo,
    plan_generation,
    run_generation_jobs,
    validate_local_model_spec,
)
from ops.backtest.models import BacktestCase, ContextItem, ContextManifest

LOCAL_MODEL = "openai_compatible:ds4@http://127.0.0.1:8000/v1"


def _case(symbol: str, asof: date) -> BacktestCase:
    return BacktestCase.create(
        sleeve="research",
        symbol=symbol,
        asof=asof,
        trigger={"screen_payload": {"symbol": symbol, "asof": asof.isoformat()}},
    )


def _request(
    symbol: str = "ABC",
    asof: date = date(2025, 6, 15),
    *,
    brain: str = "brain-v1",
    content: str = "known",
    conditioning=None,
) -> GenerationRequest:
    case = _case(symbol, asof)
    item = ContextItem.create(
        kind="filing", source_ref=f"{symbol}-10k", available_at=asof, content=content,
    )
    manifest = ContextManifest.create(case_id=case.case_id, asof=asof, included=[item])
    return GenerationRequest.create(
        case=case,
        manifest=manifest,
        brain_version=brain,
        prompt_version="prompt-v1",
        evidence_model_id=LOCAL_MODEL,
        thesis_model_id=LOCAL_MODEL,
        lesson_fingerprint="lessons-v1",
        conditioning=conditioning or {},
    )


def _terminal(request: GenerationRequest, status="accepted") -> FrozenMemoRecord:
    return FrozenMemoRecord.terminal(
        request,
        status=status,
        reason=None if status == "accepted" else status,
        recommendation="buy" if status == "accepted" else "pass",
        conviction="high" if status == "accepted" else None,
        memo_json="{}" if status == "accepted" else None,
    )


class _Store:
    def __init__(self):
        self.jobs = {}
        self.frozen = {}
        self.enqueue_order = []
        self.claim_order = []
        self.requeued = 0

    def get_frozen_memo(self, memo_key):
        return self.frozen.get(memo_key)

    def ensure_generation_job(self, request):
        if request.generation_key not in self.jobs:
            self.jobs[request.generation_key] = {
                "request": request, "status": "pending", "attempts": 0,
            }
            self.enqueue_order.append(request.case.symbol)

    def requeue_stale_generation_jobs(self, *, stale_before):
        del stale_before
        count = 0
        for job in self.jobs.values():
            if job["status"] == "running":
                job["status"] = "pending"
                count += 1
        self.requeued += count
        return count

    def claim_next_generation_job(self):
        pending = [job for job in self.jobs.values() if job["status"] == "pending"]
        if not pending:
            return None
        job = min(pending, key=lambda row: (
            row["request"].case.asof, row["request"].case.symbol,
            row["request"].generation_key,
        ))
        job["status"] = "running"
        job["attempts"] += 1
        request = job["request"]
        self.claim_order.append(request.case.symbol)
        return GenerationClaim(request.generation_key, request.case.case_id, job["attempts"])

    def finish_generation_job(self, claim, record):
        job = self.jobs[claim.generation_key]
        assert job["status"] == "running"
        job["status"] = "failed" if record.guardrail_status == "failed" else "complete"
        self.frozen[record.memo_key] = record

    def requeue_generation_job(self, claim):
        job = self.jobs[claim.generation_key]
        assert job["status"] == "running"
        job["status"] = "pending"


def test_local_model_validation_accepts_only_resolved_loopback_endpoints(monkeypatch):
    assert validate_local_model_spec(LOCAL_MODEL).model == "ds4"
    assert validate_local_model_spec("openai_compatible:x@http://localhost:1234/v1")
    assert validate_local_model_spec("openai_compatible:x@http://[::1]:1234/v1")
    assert (
        validate_local_model_spec("ollama:qwen").base_url
        == "http://localhost:11434/v1"
    )

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://192.168.1.4:11434/v1")
    with pytest.raises(NonLocalModelError):
        validate_local_model_spec("ollama:qwen")
    assert validate_local_model_spec(
        "ollama:qwen@http://127.0.0.1:11434/v1"
    ).base_url == "http://127.0.0.1:11434/v1"

    for unsafe in (
        "anthropic:claude",
        "local:qwen",
        "llama_cpp:qwen",
        "openai_compatible:x",
        "openai_compatible:x@https://models.example.com/v1",
        "openai_compatible:x@http://0.0.0.0:8000/v1",
        "ollama:qwen@http://192.168.1.4:11434",
        "openai_compatible:x@http://user:pass@127.0.0.1:8000/v1",
        "openai_compatible:x@http://127.0.0.1:notaport/v1",
    ):
        with pytest.raises(NonLocalModelError):
            validate_local_model_spec(unsafe)


def test_generation_identity_includes_brain_context_and_conditioning():
    base = _request()
    changed_brain = _request(brain="brain-v2")
    changed_context = _request(content="different filing")
    changed_conditioning = _request(conditioning={"lessons": ["avoid leverage"]})

    assert len({
        base.memo_key, changed_brain.memo_key,
        changed_context.memo_key, changed_conditioning.memo_key,
    }) == 4
    assert base.generation_key == base.memo_key


def test_generation_request_rejects_manifest_from_another_case():
    request = _request("ABC")
    other = _case("XYZ", request.case.asof)
    with pytest.raises(ValueError, match="does not match"):
        GenerationRequest.create(
            case=other, manifest=request.manifest, brain_version="v1", prompt_version="v1",
            evidence_model_id=LOCAL_MODEL, thesis_model_id=LOCAL_MODEL,
        )


def test_plan_is_oldest_first_idempotent_and_skips_frozen_cache():
    newest = _request("NEW", date(2025, 7, 1))
    oldest = _request("OLD", date(2025, 6, 2))
    middle = _request("MID", date(2025, 6, 16))
    store = _Store()
    store.frozen[middle.memo_key] = _terminal(middle)

    first = plan_generation([newest, middle, oldest, oldest], store=store)
    second = plan_generation([newest, middle, oldest], store=store)

    assert store.enqueue_order == ["OLD", "NEW"]
    assert first.cached == (middle.generation_key,)
    assert first.pending == (oldest.generation_key, newest.generation_key)
    assert second.pending == first.pending


@dataclass
class _Memo:
    memo_id: str
    ticker: str
    as_of_date: date
    conviction_tier: str = "high"
    status: str = "pending_vetting"

    def model_dump_json(self):
        return json.dumps({
            "memo_id": self.memo_id,
            "ticker": self.ticker,
            "as_of_date": self.as_of_date.isoformat(),
            "conviction_tier": self.conviction_tier,
            "status": self.status,
        }, sort_keys=True)


def test_research_adapter_uses_only_manifest_inputs_and_never_calls_live_fetchers():
    case = _case("ABC", date(2025, 6, 15))
    filing_item = ContextItem.create(
        kind="filing", source_ref="known", available_at=case.asof,
        content="sealed filing text",
        metadata={"symbol": "ABC", "accession_number": "known", "form": "10-K"},
    )
    price_item = ContextItem.create(
        kind="price_history", source_ref="sealed-prices", available_at=case.asof,
        content=json.dumps({"closes": {case.asof.isoformat(): "42"}}),
        metadata={"symbol": "ABC"},
    )
    manifest = ContextManifest.create(
        case_id=case.case_id, asof=case.asof, included=[filing_item, price_item],
    )
    request = GenerationRequest.create(
        case=case, manifest=manifest, brain_version="brain-v1", prompt_version="prompt-v1",
        evidence_model_id=LOCAL_MODEL, thesis_model_id=LOCAL_MODEL,
    )
    seen = {}

    def research_fn(hit, **kwargs):
        seen.update(hit=hit, kwargs=kwargs)
        gated = kwargs["list_filings"]("ABC", limit=200)
        assert [row.accession_number for row in gated] == ["known"]
        assert kwargs["fetch_text"](gated[0]) == "sealed filing text"
        assert kwargs["price_fetcher"]("ABC").close_on_or_before(case.asof) == 42
        assert kwargs["memo_store"].list(ticker="ABC") == []
        memo = _Memo("memo-1", "ABC", request.case.asof)
        kwargs["memo_store"].save(memo)
        return SimpleNamespace(
            status="researched", memo_id=memo.memo_id,
            recommendation="buy", errors=[],
        )

    record = generate_research_memo(
        request,
        evidence_llm=object(),
        thesis_llm=object(),
        list_filings=lambda *_args, **_kwargs: pytest.fail("live filing list called"),
        research_fn=research_fn,
        fetch_text=lambda _filing: pytest.fail("live filing text called"),
        price_fetcher=lambda _symbol: pytest.fail("live price fetch called"),
    )

    assert seen["kwargs"]["today"] == request.case.asof
    assert seen["kwargs"]["thesis_model_spec"] == request.thesis_model_id
    assert seen["hit"]["payload"] == request.hit_payload
    assert record.guardrail_status == "accepted"
    assert record.recommendation == "buy"
    assert record.conviction == "high"
    assert json.loads(record.memo_json)["as_of_date"] == request.case.asof.isoformat()


def test_generation_rejects_unsealed_precedent_memos():
    request = _request()
    precedent = SimpleNamespace(
        memo_id="old", ticker="ABC", as_of_date=request.case.asof - timedelta(days=1),
    )
    with pytest.raises(ValueError, match="materialized into the frozen manifest"):
        generate_research_memo(
            request, evidence_llm=object(), thesis_llm=object(),
            research_fn=lambda *_args, **_kwargs: None,
            precedent_memos=[precedent],
        )


def test_normal_research_rejection_is_frozen_as_a_gradeable_pass():
    request = _request()
    record = generate_research_memo(
        request,
        evidence_llm=object(), thesis_llm=object(),
        list_filings=lambda *_args, **_kwargs: [],
        research_fn=lambda *_args, **_kwargs: SimpleNamespace(
            status="failed", memo_id=None, recommendation=None,
            errors=["no machine-checkable falsifier"],
        ),
    )

    assert record.guardrail_status == "rejected"
    assert record.recommendation == "pass"
    assert record.memo_json is None
    assert record.guardrail_reason == "no machine-checkable falsifier"


def test_worker_requeues_stale_claims_continues_after_failure_and_records_terminal_shape():
    old = _request("OLD", date(2025, 6, 2))
    new = _request("NEW", date(2025, 6, 16))
    store = _Store()
    plan = plan_generation([new, old], store=store)
    store.jobs[old.generation_key]["status"] = "running"  # crashed prior process

    def generator(request):
        if request.case.symbol == "OLD":
            raise RuntimeError("local model crashed")
        return _terminal(request)

    summary = run_generation_jobs(
        plan,
        store=store,
        generator=generator,
        stale_before=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    assert store.requeued == 1
    assert store.claim_order == ["OLD", "NEW"]
    assert summary == GenerationSummary(
        attempted=2, accepted=1, rejected=0, failed=1, still_pending=0
    )
    failed = store.frozen[old.memo_key]
    assert failed.guardrail_status == "failed"
    assert failed.recommendation == "pass"
    assert failed.guardrail_reason == "RuntimeError: local model crashed"


def test_worker_honors_max_jobs_and_leaves_remaining_work_pending():
    requests = [_request("A", date(2025, 6, 2)), _request("B", date(2025, 6, 3))]
    store = _Store()
    plan = plan_generation(requests, store=store)

    summary = run_generation_jobs(
        plan, store=store, generator=_terminal, max_jobs=1,
    )

    assert summary.attempted == 1
    assert summary.still_pending == 1


def test_worker_requeues_inflight_job_when_pause_interrupts_generator():
    request = _request()
    store = _Store()
    plan = plan_generation([request], store=store)
    paused = {"value": False}

    def interrupted(_request):
        paused["value"] = True
        raise RuntimeError("model connection closed")

    summary = run_generation_jobs(
        plan, store=store, generator=interrupted,
        should_stop=lambda: paused["value"],
    )

    assert summary == GenerationSummary(1, 0, 0, 0, 1)
    assert store.jobs[request.generation_key]["status"] == "pending"
    assert store.frozen == {}


def test_worker_discards_terminal_result_if_pause_landed_inside_generator():
    request = _request()
    store = _Store()
    plan = plan_generation([request], store=store)
    paused = {"value": False}

    def swallowed_interruption(req):
        paused["value"] = True
        return _terminal(req, status="rejected")

    summary = run_generation_jobs(
        plan, store=store, generator=swallowed_interruption,
        should_stop=lambda: paused["value"],
    )

    assert summary == GenerationSummary(1, 0, 0, 0, 1)
    assert store.jobs[request.generation_key]["status"] == "pending"
    assert store.frozen == {}


def test_worker_requires_aware_stale_boundary():
    request = _request()
    store = _Store()
    plan = plan_generation([request], store=store)
    with pytest.raises(ValueError, match="timezone-aware"):
        run_generation_jobs(
            plan, store=store, generator=_terminal,
            stale_before=datetime(2025, 1, 1),
        )
