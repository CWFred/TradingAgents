"""CLI-facing orchestration for cached backtest workflows.

``run`` and ``report`` are deliberately offline: they read only frozen memos
and cached prices.  Expensive generation is a separate explicit operation.
The report path opens SQLite in ``mode=ro`` and therefore cannot create or
migrate a missing database.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:  # Python 3.11+ stdlib; project still supports Python 3.10.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

from ops.backtest.cases import CaseCandidate, construct_case, select_candidates
from ops.backtest.context import ContextArtifact, asof_gated_filings, build_context_manifest
from ops.backtest.generate import (
    GenerationPlan,
    GenerationRequest,
    GenerationSummary,
    generate_research_memo,
    plan_generation,
    run_generation_jobs,
    validate_local_model_spec,
)
from ops.backtest.models import (
    BacktestCase,
    CaseResult,
    CaseSource,
    DecisionAction,
    HorizonOutcome,
    OutcomeLabel,
    OutcomeState,
    ProcessOutcomeQuadrant,
    canonical_json,
    stable_hash,
)
from ops.backtest.postmortem import AssessmentRequest, assess_thesis_cached
from ops.backtest.prices import PriceCache, PriceSeriesStatus
from ops.backtest.replay import InitialDecision, replay_case
from ops.backtest.report import (
    FalsifierCase,
    FalsifierFiring,
    ReportCase,
    build_report,
    render_report,
)
from ops.backtest.sleeves import make_research_exit_policy, size_research_case
from ops.backtest.store import BacktestStore, CaseConflictError
from ops.backtest.verdicts import evaluate_replay
from ops.config import OpsConfig, load_config

DEFAULT_BRAIN_VERSION = "research-brain-v1"
DEFAULT_PROMPT_VERSION = "research-prompt-v1"


class BacktestServiceError(RuntimeError):
    """Stable operator-facing failure; CLI renders it without a traceback."""


class InvalidBacktestRequest(BacktestServiceError):
    pass


class MissingBacktestArtifacts(BacktestServiceError):
    pass


class UnknownBacktestRun(BacktestServiceError):
    pass


@dataclass(frozen=True)
class BacktestRunResult:
    run_id: str
    case_count: int
    rendered_report: str


@dataclass(frozen=True)
class GenerationResult:
    total: int
    cached: int
    pending: int
    summary: GenerationSummary | None = None


@dataclass(frozen=True)
class PostmortemResult:
    run_id: str
    total: int
    cached: int
    pending: int
    updated: int = 0


@dataclass(frozen=True)
class LessonsResult:
    experiment_id: str
    training: tuple[str, ...]
    holdout: tuple[str, ...]
    assessments: int
    lessons: int
    executed: bool


@dataclass(frozen=True)
class PostmortemAdapter:
    """Explicit operator-supplied boundaries for adjudication work."""

    assessor: Any
    evidence_provider: Any
    model_id: str
    prompt_version: str
    evidence_cutoff: date | None = None


def load_postmortem_adapter(spec: str) -> PostmortemAdapter:
    """Load ``module:attribute`` returning a configured post-mortem adapter."""
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name.strip() or not attribute.strip():
        raise InvalidBacktestRequest(
            "post-mortem adapter must be 'module:attribute'"
        )
    try:
        target = getattr(importlib.import_module(module_name), attribute)
        configured = target() if callable(target) else target
    except Exception as exc:
        raise BacktestServiceError(
            f"cannot load post-mortem adapter {spec!r}: {exc}"
        ) from exc
    if isinstance(configured, Mapping):
        values = configured
        get = values.get
    else:
        def get(name):
            return getattr(configured, name, None)
    adapter = PostmortemAdapter(
        assessor=get("assessor"), evidence_provider=get("evidence_provider"),
        model_id=str(get("model_id") or ""),
        prompt_version=str(get("prompt_version") or ""),
        evidence_cutoff=get("evidence_cutoff"),
    )
    if (
        adapter.assessor is None
        or adapter.evidence_provider is None
        or not adapter.model_id.strip()
        or not adapter.prompt_version.strip()
    ):
        raise BacktestServiceError(
            "post-mortem adapter must provide assessor, evidence_provider, "
            "model_id, and prompt_version"
        )
    if adapter.evidence_cutoff is not None and not isinstance(
        adapter.evidence_cutoff, date
    ):
        raise BacktestServiceError("post-mortem adapter evidence_cutoff must be a date")
    return adapter


def _repository_state() -> dict[str, Any]:
    """Best-effort code identity for reproducibility metadata."""
    root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root, check=True, capture_output=True, text=True, timeout=2,
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": "unknown", "git_dirty": None}
    return {"git_commit": commit, "git_dirty": dirty}


def parse_cli_date(value: str, *, today: date) -> date:
    if value.lower() == "today":
        return today
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidBacktestRequest(
            f"invalid date {value!r}; expected YYYY-MM-DD or 'today'"
        ) from exc


def load_settings(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    settings_path = Path(path).expanduser()
    try:
        with settings_path.open("rb") as handle:
            parsed = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InvalidBacktestRequest(
            f"cannot read settings file {settings_path}: {exc}"
        ) from exc
    selected = parsed.get("backtest", parsed)
    if not isinstance(selected, dict):
        raise InvalidBacktestRequest("settings [backtest] value must be a table")
    return selected


def _validate_window(
    *, start: date, end: date, today: date, cutoff: date, case_count: int,
) -> None:
    if start < cutoff:
        raise InvalidBacktestRequest(
            f"start {start} precedes effective cutoff {cutoff}; no override exists"
        )
    if end < start:
        raise InvalidBacktestRequest(f"end {end} is before start {start}")
    if end > today:
        raise InvalidBacktestRequest(f"end {end} is after resolved today {today}")
    if not 30 <= case_count <= 100:
        raise InvalidBacktestRequest("cases must be in the approved range 30..100")


def _is_control_case(case) -> bool:
    return case.trigger.get("kind") == "near_miss_control"


def _selected_cases(
    store: BacktestStore,
    *,
    sleeve: str,
    start: date,
    end: date,
    case_count: int,
):
    """Select cases for replay: passers are budgeted, controls are additive.

    ``case_count`` truncates only non-control cases, in (asof, symbol) order.
    Near-miss control cases in the window are always included in full -- they
    exist to falsify the screen and must never be displaced by passer top-up,
    nor count against the passer budget.
    """
    windowed = [
        case for case in store.list_cases(sleeve=sleeve)
        if start <= case.asof <= end
    ]
    passers = [case for case in windowed if not _is_control_case(case)][:case_count]
    controls = [case for case in windowed if _is_control_case(case)]
    cases = passers + controls
    if not cases:
        raise MissingBacktestArtifacts(
            f"no {sleeve!r} cases in {start}..{end}; preload/select cases first"
        )
    store.validate_cases_for_replay([case.case_id for case in cases])
    return cases


def _resolved_settings(config: OpsConfig, overrides: Mapping[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "benchmark": config.backtest_benchmark,
        "case_notional": str(config.backtest_case_notional),
        "horizons": list(config.backtest_horizons),
        "primary_horizon": config.backtest_primary_horizon,
        "wash_band": str(config.backtest_wash_band),
    }
    unknown = set(overrides) - set(defaults)
    if unknown:
        raise InvalidBacktestRequest(f"unknown settings: {sorted(unknown)}")
    defaults.update(overrides)
    try:
        notional = Decimal(str(defaults["case_notional"]))
        wash_band = Decimal(str(defaults["wash_band"]))
        horizons = tuple(int(item) for item in defaults["horizons"])
        primary = int(defaults["primary_horizon"])
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidBacktestRequest(f"invalid replay setting: {exc}") from exc
    if notional <= 0:
        raise InvalidBacktestRequest("case_notional must be positive")
    if wash_band < 0 or wash_band >= 1:
        raise InvalidBacktestRequest("wash_band must be in [0, 1)")
    if not horizons or any(item <= 0 for item in horizons) or len(set(horizons)) != len(horizons):
        raise InvalidBacktestRequest("horizons must be nonempty, positive, and unique")
    if primary not in horizons:
        raise InvalidBacktestRequest("primary_horizon must be one of horizons")
    benchmark = str(defaults["benchmark"]).strip().upper()
    if not benchmark:
        raise InvalidBacktestRequest("benchmark must not be empty")
    return {
        "benchmark": benchmark,
        "case_notional": str(notional),
        "horizons": list(horizons),
        "primary_horizon": primary,
        "wash_band": str(wash_band),
    }


def _initial_decision(record) -> InitialDecision:
    accepted_buy = (
        record.guardrail_status == "accepted"
        and record.recommendation.lower() == "buy"
    )
    if accepted_buy:
        return InitialDecision(
            DecisionAction.BUY,
            "frozen memo recommends buy",
            conviction=record.conviction or "",
            memo_key=record.memo_key,
        )
    reason = record.guardrail_reason or f"frozen memo {record.recommendation}"
    return InitialDecision(
        DecisionAction.PASS, reason,
        conviction=record.conviction or "", memo_key=record.memo_key,
    )


def _memo_and_exit_policy(record):
    if record.memo_json is None:
        return None, None
    from tradingagents.memos.schema import Memo

    memo = Memo.model_validate_json(record.memo_json)
    return memo, make_research_exit_policy(memo=memo)


def run_cached_backtest(
    *,
    config: OpsConfig,
    sleeve: str,
    start: date,
    end: date,
    case_count: int,
    settings: Mapping[str, Any],
    today: date,
    now: datetime | None = None,
    brain_version: str = DEFAULT_BRAIN_VERSION,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> BacktestRunResult:
    """Replay preloaded cases with no fetches and no model calls."""
    _validate_window(
        start=start, end=end, today=today, cutoff=config.backtest_cutoff,
        case_count=case_count,
    )
    if sleeve != "research":
        raise InvalidBacktestRequest(f"unknown backtest sleeve {sleeve!r}")
    resolved = _resolved_settings(config, settings)
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None or when.utcoffset() is None:
        raise InvalidBacktestRequest("run timestamp must be timezone-aware")

    with BacktestStore(config.backtest_store_path, cutoff=config.backtest_cutoff) as store:
        store.fail_stale_runs(older_than=when - timedelta(hours=24))
        effective_cutoff = store.effective_cutoff
        _validate_window(
            start=start, end=end, today=today, cutoff=effective_cutoff,
            case_count=case_count,
        )
        cases = _selected_cases(
            store, sleeve=sleeve, start=start, end=end, case_count=case_count,
        )
        requests = _generation_requests(
            store, cases, config=config,
            brain_version=brain_version, prompt_version=prompt_version,
        )
        records = {
            request.case.case_id: store.get_frozen_memo(request.memo_key)
            for request in requests
        }
        missing = [case for case in cases if records[case.case_id] is None]
        if missing:
            preview = ", ".join(case.symbol for case in missing[:5])
            suffix = "..." if len(missing) > 5 else ""
            raise MissingBacktestArtifacts(
                f"{len(missing)} of {len(cases)} memo(s) missing ({preview}{suffix}); "
                "run `ops backtest generate` first"
            )
        identity = {
            "sleeve": sleeve, "start": start, "end": end,
            "settings": resolved, "created_at": when,
        }
        run_id = f"backtest-{today.isoformat()}-{stable_hash(identity)[:12]}"
        config_snapshot = {
            key: value for key, value in asdict(config).items()
            if key.startswith("backtest_")
        }
        manifests = [store.get_context_manifest(case.case_id) for case in cases]
        included_context = sum(len(manifest.included) for manifest in manifests)
        excluded_context = sum(len(manifest.excluded) for manifest in manifests)
        substitutions = sorted({
            item for manifest in manifests for item in manifest.substitutions
        })
        with store.transaction() as conn:
            probe = conn.execute(
                "SELECT probe_id, contaminated, recommended_cutoff "
                "FROM cutoff_probes ORDER BY created_at DESC, probe_id DESC LIMIT 1"
            ).fetchone()
        metadata = {
            "cutoff": effective_cutoff,
            "configured_cutoff": config.backtest_cutoff,
            "adjudication_date": today,
            "case_source_modes": sorted({case.source.value for case in cases}),
            "case_selection": "stored cases ordered by asof then symbol",
            "requested_cases": case_count,
            "selected_cases": len(cases),
            "context_items_included": included_context,
            "context_items_excluded": excluded_context,
            "context_substitutions": substitutions,
            "evidence_model_ids": sorted({record.evidence_model_id for record in records.values()}),
            "thesis_model_ids": sorted({record.thesis_model_id for record in records.values()}),
            "latest_cutoff_probe": (
                {
                    "probe_id": probe["probe_id"],
                    "contaminated": bool(probe["contaminated"]),
                    "recommended_cutoff": probe["recommended_cutoff"],
                }
                if probe is not None else None
            ),
            **_repository_state(),
        }
        store.create_run(
            run_id=run_id, sleeve=sleeve, start_date=start, end_date=end,
            benchmark=resolved["benchmark"], settings=resolved,
            resolved_config=config_snapshot, metadata=metadata,
            case_ids=[case.case_id for case in cases], created_at=when,
        )
        prices = PriceCache(config.backtest_store_path)
        run_succeeded = False
        try:
            for case in cases:
                record = records[case.case_id]
                initial = _initial_decision(record)
                _memo, exit_policy = _memo_and_exit_policy(record)
                notional = Decimal(resolved["case_notional"])
                if initial.action == DecisionAction.BUY:
                    sizing = size_research_case(
                        tier=initial.conviction, fixed_equity=notional,
                        symbol=case.symbol,
                    )
                    if sizing.rejected is not None:
                        raise BacktestServiceError(
                            f"{case.symbol}: frozen conviction cannot be sized: {sizing.rejected}"
                        )
                    notional = sizing.notional
                explicit_stock_state = prices.state(case.symbol)
                stock_status = prices.classify(
                    case.symbol, required_through=today,
                )
                stock_reason = (
                    explicit_stock_state.reason
                    if explicit_stock_state is not None
                    else None
                )
                if stock_status == PriceSeriesStatus.STALE and not stock_reason:
                    stock_reason = f"cached series does not reach {today}"
                explicit_benchmark_state = prices.state(resolved["benchmark"])
                benchmark_status = prices.classify(
                    resolved["benchmark"], required_through=today,
                )
                benchmark_reason = (
                    explicit_benchmark_state.reason
                    if explicit_benchmark_state is not None
                    else None
                )
                if benchmark_status == PriceSeriesStatus.STALE and not benchmark_reason:
                    benchmark_reason = f"cached series does not reach {today}"
                stock_bars = prices.bars(
                    case.symbol, start=case.asof, end=today,
                    adjusted_to=case.asof,
                )
                benchmark_bars = prices.bars(
                    resolved["benchmark"], start=case.asof, end=today,
                    adjusted_to=case.asof,
                )
                replay = replay_case(
                    run_id=run_id, case=case, initial=initial,
                    bars=stock_bars, notional=notional, settings=resolved,
                    exit_policy=exit_policy,
                    price_status=stock_status,
                    price_state_reason=stock_reason,
                )
                outcomes, result = evaluate_replay(
                    replay, stock_bars=stock_bars,
                    benchmark_bars=benchmark_bars,
                    adjudication_date=today,
                    horizons=tuple(resolved["horizons"]),
                    primary_horizon=resolved["primary_horizon"],
                    wash_band=Decimal(resolved["wash_band"]),
                    stock_status=stock_status,
                    benchmark_status=benchmark_status,
                    stock_status_reason=stock_reason,
                    benchmark_status_reason=benchmark_reason,
                    stock_terminal_session=(
                        explicit_stock_state.asof
                        if explicit_stock_state is not None else None
                    ),
                )
                store.save_replay_evaluation(replay, outcomes, result)
            run_succeeded = True
        finally:
            # Any escape -- including KeyboardInterrupt/SystemExit from a
            # killed replay, not just Exception -- must not leave the run
            # stuck at 'running' forever; a stale row is a lie about the
            # backtest's outcome and blocks future replays from noticing it.
            store.finish_run(run_id, status="complete" if run_succeeded else "failed")

    return BacktestRunResult(
        run_id=run_id, case_count=len(cases),
        rendered_report=render_saved_report(config.backtest_store_path, run_id),
    )


def _generation_requests(
    store: BacktestStore,
    cases: Sequence,
    *,
    config: OpsConfig,
    brain_version: str,
    prompt_version: str,
    on_missing_manifest: str = "raise",
) -> tuple[GenerationRequest, ...]:
    """Build requests for cases with a sealed manifest.

    ``on_missing_manifest="raise"`` (default, used by replay/run) fails the
    whole batch on any orphan case -- a manual-SQL dead end is preferable to
    silently replaying an unsealed case. ``"skip"`` (used by
    :func:`generate_cases`) instead drops orphan cases and prints a warning
    listing them, so a batch with one orphan can still make progress while
    the orphan resurfaces for re-prepare (Task 5, 2026-07-31 plan).
    """
    if on_missing_manifest not in ("raise", "skip"):
        raise ValueError(f"unknown on_missing_manifest mode: {on_missing_manifest!r}")
    requests = []
    missing_manifests = []
    skipped_case_ids = []
    for case in cases:
        manifest = store.get_context_manifest(case.case_id)
        if manifest is None:
            missing_manifests.append(case.symbol)
            skipped_case_ids.append(case.case_id)
            continue
        requests.append(GenerationRequest.create(
            case=case, manifest=manifest,
            brain_version=brain_version, prompt_version=prompt_version,
            evidence_model_id=config.research_evidence_model,
            thesis_model_id=config.research_thesis_model,
        ))
    if missing_manifests:
        if on_missing_manifest == "skip":
            print(
                f"[generate] skipped {len(skipped_case_ids)} case(s) without a "
                "sealed context manifest (orphan, will resurface for "
                "re-prepare): " + ", ".join(skipped_case_ids)
            )
        else:
            raise MissingBacktestArtifacts(
                f"{len(missing_manifests)} case(s) lack PIT context manifests: "
                + ", ".join(missing_manifests[:5])
            )
    return tuple(requests)


def _execute_generation(
    plan: GenerationPlan,
    *,
    store: BacktestStore,
    config: OpsConfig,
    max_jobs: int | None,
    auto_only: bool = False,
) -> GenerationSummary:
    from ops.llm_backend import (
        build_managed_backend,
        load_managed_backend_config,
        register_background_backend,
        unregister_background_backend,
    )
    from ops.work_pause import pause_state
    from tradingagents.llm_clients import create_llm_client

    evidence_spec = validate_local_model_spec(config.research_evidence_model)
    thesis_spec = validate_local_model_spec(config.research_thesis_model)
    evidence_llm = create_llm_client(
        provider=evidence_spec.provider, model=evidence_spec.model,
        base_url=evidence_spec.base_url,
    ).get_llm()
    thesis_llm = create_llm_client(
        provider=thesis_spec.provider, model=thesis_spec.model,
        base_url=thesis_spec.base_url,
    ).get_llm()
    backend = build_managed_backend(load_managed_backend_config())
    if auto_only:
        register_background_backend(backend)
    try:
        backend.ensure_up()

        def generator(request):
            return generate_research_memo(
                request, evidence_llm=evidence_llm, thesis_llm=thesis_llm,
            )

        return run_generation_jobs(
            plan, store=store, generator=generator,
            stale_before=datetime.now(timezone.utc) - timedelta(hours=6),
            max_jobs=max_jobs, auto_only=auto_only,
            should_stop=(
                lambda: pause_state(
                    config.research_pause_flag_path, cleanup_expired=True,
                ).paused
            ) if auto_only else None,
        )
    finally:
        try:
            backend.shutdown()
        finally:
            if auto_only:
                unregister_background_backend(backend)


def process_enqueued_generation(
    *, config: OpsConfig, max_jobs: int = 1,
) -> GenerationSummary | None:
    """Run an explicitly auto-queued backtest batch after live queues are idle."""
    if max_jobs <= 0:
        raise InvalidBacktestRequest("max-jobs must be positive")
    from ops.work_pause import pause_state

    if pause_state(
        config.research_pause_flag_path, cleanup_expired=True,
    ).paused:
        return None
    if not Path(config.backtest_store_path).expanduser().is_file():
        return None
    with BacktestStore(
        config.backtest_store_path, cutoff=config.backtest_cutoff,
    ) as store:
        requests = store.queued_generation_requests(auto_only=True)
        if not requests:
            return None
        plan = GenerationPlan(
            requests=requests, cached=(),
            pending=tuple(request.generation_key for request in requests),
        )
        return _execute_generation(
            plan, store=store, config=config, max_jobs=max_jobs, auto_only=True,
        )


def prepare_cases(
    *,
    store: BacktestStore,
    sleeve: str,
    start: date,
    end: date,
    case_count: int,
    case_source: Callable[..., Sequence[CaseCandidate]],
    context_builder: Callable[[BacktestCase, CaseCandidate], Any],
) -> tuple[BacktestCase, ...]:
    """Import true historical hits and seal their context before planning.

    ``case_source`` and ``context_builder`` are explicit seams so tests and
    alternate PIT corpora never need a live network. The default caller uses
    recorded live screen hits; current-universe reconstruction is not used or
    mislabeled as point-in-time data.
    """
    candidates = case_source(start=start, end=end)
    eligible = [candidate for candidate in candidates if start <= candidate.asof <= end]
    selected = select_candidates(
        eligible, target_count=case_count, per_date_cap=max(1, case_count),
    )
    prepared: list[BacktestCase] = []
    for candidate in selected:
        case = construct_case(
            candidate, sleeve=sleeve, cutoff=store.effective_cutoff,
            source=CaseSource.LIVE_IMPORT,
        )
        manifest = context_builder(case, candidate)
        if manifest.case_id != case.case_id or manifest.asof != case.asof:
            raise InvalidBacktestRequest(
                f"context builder returned a manifest for another case: {case.symbol}"
            )
        store.insert_case_with_manifest(case, manifest)
        prepared.append(case)
    if not prepared:
        raise MissingBacktestArtifacts(
            f"no recorded live screen hits in {start}..{end}; "
            "current-universe reconstruction is never used implicitly"
        )
    return tuple(prepared)


def _screen_hit_source(config: OpsConfig) -> Callable[..., Sequence[CaseCandidate]]:
    def load(*, start: date, end: date) -> tuple[CaseCandidate, ...]:
        path = Path(config.screen_store_path).expanduser().resolve()
        if not path.is_file():
            raise MissingBacktestArtifacts(f"screen store does not exist: {path}")
        uri = path.as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, run_id, symbol, asof, payload FROM screen_hits "
                    "WHERE asof BETWEEN ? AND ? ORDER BY asof, id",
                    (start.isoformat(), end.isoformat()),
                ).fetchall()
            except sqlite3.Error as exc:
                raise MissingBacktestArtifacts(
                    f"cannot read recorded screen hits from {path}: {exc}"
                ) from exc
        candidates = []
        for row in rows:
            payload = json.loads(row["payload"])
            triggers = payload.get("triggers", [])
            score = payload.get("score", len(triggers) or 1)
            candidates.append(CaseCandidate(
                symbol=row["symbol"], asof=date.fromisoformat(row["asof"]),
                score=score,
                trigger={"kind": "recorded_live_screen", "run_id": row["run_id"]},
                screen_payload=payload,
                source_ref=f"screen:{row['run_id']}:{row['id']}",
            ))
        return tuple(candidates)

    return load


def _sealed_context_builder(
    config: OpsConfig,
) -> Callable[[BacktestCase, CaseCandidate], Any]:
    from tradingagents.dataflows import edgar

    def gated(asof: date):
        return asof_gated_filings(edgar.list_filings, asof=asof)

    def build(case: BacktestCase, _candidate: CaseCandidate):
        edgar.get_user_agent()
        artifacts: list[ContextArtifact] = []
        for filing in gated(case.asof)(case.symbol, limit=200):
            try:
                content = edgar.fetch_filing_text(filing)
            except Exception:
                continue
            artifacts.append(ContextArtifact(
                kind="filing", source_ref=filing.accession_number,
                available_at=filing.filing_date, content=content,
                metadata={
                    "symbol": case.symbol,
                    "accession_number": filing.accession_number,
                    "form": filing.form,
                    "filing_date": filing.filing_date,
                    "report_date": filing.report_date,
                    "cik": filing.cik,
                    "primary_document": filing.primary_document,
                    "primary_doc_description": filing.primary_doc_description,
                    "items": filing.items,
                },
            ))
        prices = PriceCache(config.backtest_store_path)
        bars = prices.bars(case.symbol, end=case.asof, adjusted_to=case.asof)
        if not bars:
            raise MissingBacktestArtifacts(
                f"price cache has no bars for {case.symbol} on/before {case.asof}; "
                "backfill prices for the case window before sealing manifests"
            )
        artifacts.append(ContextArtifact(
            kind="price_history", source_ref=f"price-cache:{case.symbol}:{case.asof}",
            available_at=case.asof,
            content=canonical_json({
                "closes": {bar.session: bar.adjusted_close for bar in bars},
            }),
            metadata={"symbol": case.symbol},
        ))
        return build_context_manifest(
            case_id=case.case_id, asof=case.asof, artifacts=artifacts,
        )

    return build


def _default_prepare_cases(
    *, store: BacktestStore, config: OpsConfig, sleeve: str,
    start: date, end: date, case_count: int,
) -> tuple[BacktestCase, ...]:
    return prepare_cases(
        store=store, sleeve=sleeve, start=start, end=end, case_count=case_count,
        case_source=_screen_hit_source(config),
        context_builder=_sealed_context_builder(config),
    )


def _reconstruction_prepare_cases(
    *,
    store: BacktestStore,
    config: OpsConfig,
    sleeve: str,
    start: date,
    end: date,
    case_count: int,
    spacing_sessions: int = 10,
    universe: Any = None,
    fetcher: Callable[[date], Sequence[CaseCandidate]] | None = None,
    existing: Collection[tuple[str, date]] = (),
    controls_count: int = 0,
    price_backfiller: Callable[..., Any] | None = None,
    fresh_sweep: bool = False,
) -> tuple[BacktestCase, ...]:
    """Reconstruct cases by replaying the screener at sampled historical dates.

    Every case is stamped :attr:`CaseSource.CURRENT_UNIVERSE_RECONSTRUCTION` and the fetcher is
    wrapped in :class:`CurrentUniverseReconstructionSource`: these cases are
    survivorship-biased over today's universe membership and must never be
    rendered as a clean point-in-time historical screen.

    ``controls_count`` near-miss control cases (names failing exactly one
    screen condition) are selected via a *separate* ``select_candidates``
    call so they never crowd out passer selection.

    Per-date results are checkpointed under a ``sweep_key`` (schema v4:
    ``store.save_sweep_candidates``/``load_sweep_candidates``) so a killed
    sweep resumes at the first unfinished date. ``sweep_key`` is a content
    hash of the sweep *parameters* (sleeve/start/end/spacing/controls/
    cutoff) only -- it is NOT sensitive to screener or trigger-source code
    changes. After editing screener/trigger-source logic, pass
    ``fresh_sweep=True`` (CLI: ``--fresh-sweep``) or old checkpoints will be
    silently reused, serving stale results with no error.

    Before sealing, prices are backfilled for exactly the selected
    candidates: ``_sealed_context_builder`` fails closed when the price
    cache has no bars for a symbol, and the only other cache writer
    (``backfill_prices``) derives its symbols from *already-stored* cases --
    a fresh symbol could otherwise never be prepared (2026-07-28 review,
    finding 1: prepare/prices sequencing deadlock).
    """
    from ops.backtest.cases import CurrentUniverseReconstructionSource, sample_sessions
    from ops.scheduler.market_calendar import MarketCalendar

    if fetcher is None:
        from ops.backtest.fetch_cache import FetchCache, default_fetch_cache_path
        from ops.backtest.historical_source import ReconstructionScreenerFetcher
        from ops.research.run import fetch_price_context
        from ops.universe.smallcap import build_smallcap_universe
        from tradingagents.dataflows import edgar
        from tradingagents.dataflows.edgar_facts import get_company_facts

        edgar.get_user_agent()  # fail fast, same as run_screen
        universe = universe if universe is not None else build_smallcap_universe()
        # No explicit triggers_finder: this takes the disk-backed cached path
        # (trigger sources/facts/price context all through FetchCache) --
        # see ReconstructionScreenerFetcher's docstring.
        fetcher = ReconstructionScreenerFetcher(
            universe=universe, facts_fetcher=get_company_facts,
            price_context_fetcher=fetch_price_context,
            fetch_cache=FetchCache(default_fetch_cache_path()),
            include_near_misses=controls_count > 0,
        )

    sessions = MarketCalendar().sessions_between(start, end)
    sampled = sample_sessions(
        sessions, start=start, end=end, spacing_sessions=spacing_sessions,
    )
    source = CurrentUniverseReconstructionSource(fetch=fetcher)

    # Per-date sweep checkpoints (schema v4): a killed multi-hour sweep
    # resumes at the first unfinished date instead of losing everything held
    # only in memory. sweep_key intentionally excludes case_count (the
    # passer budget doesn't change what a date's screen found) and universe
    # (opaque, not hashable identity) -- it identifies the *screen replay*,
    # not the downstream selection.
    #
    # CAVEAT: sweep_key is NOT sensitive to screener/trigger-source code
    # changes -- it is a content hash of the sweep *parameters* only. After
    # editing screener or trigger-source logic, an old sweep_key's
    # checkpoints would be silently reused (stale results, no error). Pass
    # ``--fresh-sweep`` explicitly whenever such logic changes.
    sweep_key = stable_hash({
        "sleeve": sleeve, "start": start, "end": end,
        "spacing": spacing_sessions, "controls": controls_count,
        "cutoff": store.effective_cutoff,
    })[:16]
    if fresh_sweep:
        store.clear_sweep_candidates(sweep_key)
    checkpointed = store.load_sweep_candidates(sweep_key)
    candidates: list[CaseCandidate] = []
    for asof in sampled:
        if asof in checkpointed:
            candidates.extend(checkpointed[asof])
            continue
        hits = tuple(source.candidates(asof=asof))
        for candidate in hits:
            if candidate.asof != asof:
                raise ValueError(
                    f"case source returned {candidate.symbol} asof {candidate.asof} "
                    f"for requested date {asof}"
                )
        store.save_sweep_candidates(sweep_key, asof, hits)
        candidates.extend(hits)

    existing_keys = set(existing)
    candidates = [
        candidate for candidate in candidates
        if (candidate.normalized_symbol(), candidate.asof) not in existing_keys
    ]
    passer_candidates = [
        candidate for candidate in candidates
        if candidate.trigger.get("kind") != "near_miss_control"
    ]
    control_candidates = [
        candidate for candidate in candidates
        if candidate.trigger.get("kind") == "near_miss_control"
    ]
    selected = list(select_candidates(
        passer_candidates, target_count=case_count,
        per_date_cap=max(1, -(-case_count // max(1, len(sampled)))),
    ))
    if controls_count > 0:
        selected.extend(select_candidates(
            control_candidates, target_count=controls_count,
            per_date_cap=max(1, -(-controls_count // max(1, len(sampled)))),
        ))
    if selected:
        if price_backfiller is None:
            from ops.backtest.price_backfill import backfill_symbol_windows

            price_backfiller = backfill_symbol_windows
        pairs = [(candidate.normalized_symbol(), candidate.asof) for candidate in selected]
        pairs.append((config.backtest_benchmark, min(candidate.asof for candidate in selected)))
        price_backfiller(config, pairs)

    prepared: list[BacktestCase] = []
    context_builder = _sealed_context_builder(config)
    for candidate in selected:
        case = construct_case(
            candidate, sleeve=sleeve, cutoff=store.effective_cutoff,
            source=CaseSource.CURRENT_UNIVERSE_RECONSTRUCTION,
        )
        try:
            manifest = context_builder(case, candidate)
            if manifest.case_id != case.case_id or manifest.asof != case.asof:
                raise InvalidBacktestRequest(
                    f"context builder returned a manifest for another case: {case.symbol}"
                )
            store.insert_case_with_manifest(case, manifest)
        except (MissingBacktestArtifacts, CaseConflictError) as exc:
            # One unsealable or conflicting symbol (e.g. its price backfill
            # failed, or a resurfaced orphan's re-prepared content drifted
            # from a pre-existing conflicting row) must not abort a
            # multi-hour sweep; the fail-closed guard still keeps the bad
            # case out because we skip before any further store write.
            print(f"[prepare] skipped {case.symbol} {case.asof}: {exc}")
            continue
        prepared.append(case)
    if not prepared:
        raise MissingBacktestArtifacts(
            f"reconstruction screen produced no passing cases in {start}..{end}"
        )
    return tuple(prepared)


def generate_cases(
    *,
    config: OpsConfig,
    sleeve: str,
    start: date,
    end: date,
    case_count: int,
    today: date,
    execute: bool = False,
    enqueue: bool = False,
    max_jobs: int | None = None,
    brain_version: str = DEFAULT_BRAIN_VERSION,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    source: str = "recorded",
    executor: Callable[..., GenerationSummary] | None = None,
    preparer: Callable[..., Sequence[BacktestCase]] | None = None,
    spacing_sessions: int = 10,
    append: bool = False,
    controls_count: int = 0,
    fresh_sweep: bool = False,
) -> GenerationResult:
    if execute and enqueue:
        raise InvalidBacktestRequest("choose either immediate execution or background enqueue")
    if append and source == "recorded":
        raise InvalidBacktestRequest("append is reconstruction-only")
    if fresh_sweep and source == "recorded":
        raise InvalidBacktestRequest("fresh-sweep is reconstruction-only")
    _validate_window(
        start=start, end=end, today=today, cutoff=config.backtest_cutoff,
        case_count=case_count,
    )
    if max_jobs is not None and max_jobs <= 0:
        raise InvalidBacktestRequest("max-jobs must be positive")
    with BacktestStore(config.backtest_store_path, cutoff=config.backtest_cutoff) as store:
        effective_cutoff = store.effective_cutoff
        _validate_window(
            start=start, end=end, today=today, cutoff=effective_cutoff,
            case_count=case_count,
        )
        windowed = [
            case for case in store.list_cases(sleeve=sleeve)
            if start <= case.asof <= end
        ]
        # Orphan cases (case row inserted but the paired manifest never was,
        # e.g. a kill before Task 5's atomic insert_case_with_manifest
        # existed) must not count as available -- they need_prepare/top-up
        # resurfaces and re-seals, and must never dedupe-block their own
        # resurfacing via `existing` either (Task 5, 2026-07-31 plan).
        sealed_ids = store.case_ids_with_manifests([case.case_id for case in windowed])
        orphans = [case for case in windowed if case.case_id not in sealed_ids]
        if orphans:
            print(
                f"[generate] {len(orphans)} orphan case(s) (case row without a "
                "sealed manifest) excluded from availability and will resurface "
                "for re-prepare: " + ", ".join(case.case_id for case in orphans)
            )
        available = [case for case in windowed if case.case_id in sealed_ids]
        # case_count budgets passers only; controls are additive and must
        # never shrink the passer top-up (see finding 2, 2026-07-28 review).
        available_passers = [case for case in available if not _is_control_case(case)]
        need_prepare = (
            not available_passers
        ) or (append and len(available_passers) < case_count)
        if need_prepare:
            if preparer is not None:
                prepare = preparer
            elif source == "reconstruction":
                prepare = _reconstruction_prepare_cases
            elif source == "recorded":
                prepare = _default_prepare_cases
            else:
                raise InvalidBacktestRequest(f"unknown case source {source!r}")
            prepare_kwargs = (
                {
                    "spacing_sessions": spacing_sessions,
                    # Dedupe against ALL stored cases (including controls) so
                    # a control is never re-inserted as a duplicate.
                    "existing": tuple((c.symbol, c.asof) for c in available),
                    "controls_count": controls_count,
                    "fresh_sweep": fresh_sweep,
                }
                if preparer is not None or source == "reconstruction"
                else {}
            )
            prepare(
                store=store, config=config, sleeve=sleeve,
                start=start, end=end,
                case_count=case_count - len(available_passers),
                **prepare_kwargs,
            )
        cases = _selected_cases(
            store, sleeve=sleeve, start=start, end=end, case_count=case_count,
        )
        requests = _generation_requests(
            store, cases, config=config,
            brain_version=brain_version, prompt_version=prompt_version,
            on_missing_manifest="skip",
        )
        plan = plan_generation(requests, store=store)
        if enqueue:
            store.enqueue_generation_jobs(plan.pending)
        summary = None
        if execute and plan.pending:
            runner = executor or _execute_generation
            summary = runner(
                plan, store=store, config=config, max_jobs=max_jobs,
            )
        return GenerationResult(
            total=len(plan.requests), cached=len(plan.cached),
            pending=(summary.still_pending if summary is not None else len(plan.pending)),
            summary=summary,
        )


def _readonly_connection(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path).expanduser().resolve()
    if not db_path.is_file():
        raise BacktestServiceError(f"backtest store does not exist: {db_path}")
    uri = db_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _load_report_case(conn: sqlite3.Connection, run_id: str, row) -> ReportCase:
    result = CaseResult(
        run_id=run_id, case_id=row["case_id"],
        initial_action=DecisionAction(row["initial_action"]), status=row["result_status"],
        primary_horizon=int(row["primary_horizon"]),
        primary_label=OutcomeLabel(row["primary_label"]),
        actual_return=_decimal(row["actual_return"]),
        max_drawdown=_decimal(row["max_drawdown"]),
        exit_session=date.fromisoformat(row["exit_session"]) if row["exit_session"] else None,
        exit_reason=row["exit_reason"],
        quadrant=ProcessOutcomeQuadrant(row["quadrant"]),
    )
    outcomes = conn.execute(
        "SELECT * FROM horizon_outcomes WHERE run_id = ? AND case_id = ? "
        "ORDER BY horizon_sessions",
        (run_id, row["case_id"]),
    ).fetchall()
    parsed = tuple(HorizonOutcome(
        run_id=run_id, case_id=row["case_id"],
        horizon_sessions=int(item["horizon_sessions"]),
        state=OutcomeState(item["state"]), label=OutcomeLabel(item["label"]),
        stock_return=_decimal(item["stock_return"]),
        benchmark_return=_decimal(item["benchmark_return"]),
        excess_return=_decimal(item["excess_return"]), utility=_decimal(item["utility"]),
        entry_session=(date.fromisoformat(item["entry_session"])
                       if item["entry_session"] else None),
        horizon_session=(date.fromisoformat(item["horizon_session"])
                         if item["horizon_session"] else None),
        detail=item["detail"],
    ) for item in outcomes)
    return ReportCase(
        case_id=row["case_id"], symbol=row["symbol"],
        conviction=row["conviction"] or "", result=result, outcomes=parsed,
        price_status=(
            json.loads(row["decision_metadata_json"]).get("price_status", "ready")
            if row["decision_metadata_json"] else "ready"
        ),
    )


def _load_falsifier_cases(
    conn: sqlite3.Connection,
    run_id: str,
    rows: Sequence[ReportCase],
) -> tuple[FalsifierCase, ...]:
    columns = {
        item[1]
        for item in conn.execute("PRAGMA table_info(falsifier_observations)")
    }
    name_sql = "name" if "name" in columns else "'' AS name"
    observations = conn.execute(
        f"SELECT case_id, session, falsifier_index, {name_sql}, status, "
        "observed, detail FROM falsifier_observations WHERE run_id = ? "
        "ORDER BY case_id, session, falsifier_index",
        (run_id,),
    ).fetchall()
    by_case: dict[str, list[sqlite3.Row]] = {}
    for observation in observations:
        by_case.setdefault(observation["case_id"], []).append(observation)
    result = []
    for row in rows:
        case_observations = by_case.get(row.case_id, [])
        if not case_observations:
            continue
        names = tuple(sorted({
            item["name"] or f"falsifier-{item['falsifier_index']}"
            for item in case_observations
        }))
        firings = tuple(
            FalsifierFiring(
                name=item["name"] or f"falsifier-{item['falsifier_index']}",
                session=date.fromisoformat(item["session"]),
                status=item["status"],
            )
            for item in case_observations
            if item["status"] in {"tripped", "unevaluable"}
        )
        primary = row.primary()
        losing = primary.label == OutcomeLabel.LOSS
        result.append(FalsifierCase(
            case_id=row.case_id,
            names=names,
            losing=losing,
            damage_session=primary.horizon_session if losing else None,
            firings=firings,
        ))
    return tuple(result)


def render_saved_report(path: str | Path, run_id: str) -> str:
    """Rerender a completed run through a strictly read-only connection."""
    with _readonly_connection(path) as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise UnknownBacktestRun(f"unknown backtest run {run_id!r}")
        rows = conn.execute(
            """
            SELECT rc.ordinal, c.case_id, c.symbol, cr.status AS result_status,
                   cr.initial_action, cr.primary_horizon, cr.primary_label,
                   cr.actual_return, cr.max_drawdown, cr.exit_session, cr.exit_reason,
                   cr.quadrant, fm.conviction,
                   d.metadata_json AS decision_metadata_json
            FROM run_cases AS rc
            JOIN cases AS c ON c.case_id = rc.case_id
            LEFT JOIN case_results AS cr
              ON cr.run_id = rc.run_id AND cr.case_id = rc.case_id
            LEFT JOIN decisions AS d
              ON d.run_id = rc.run_id AND d.case_id = rc.case_id AND d.sequence = 0
            LEFT JOIN frozen_memos AS fm ON fm.memo_key = d.memo_key
            WHERE rc.run_id = ? ORDER BY rc.ordinal
            """,
            (run_id,),
        ).fetchall()
        incomplete = [row["case_id"] for row in rows if row["result_status"] is None]
        if incomplete:
            raise BacktestServiceError(
                f"run {run_id!r} is incomplete; {len(incomplete)} case result(s) missing"
            )
        resolved_config = json.loads(run["resolved_config_json"])
        metadata = json.loads(run["metadata_json"])
        metadata.update({
            "benchmark": run["benchmark"], "settings_hash": run["settings_hash"],
            "start": run["start_date"], "end": run["end_date"],
            "status": run["status"],
        })
        report_rows = tuple(_load_report_case(conn, run_id, row) for row in rows)
        report = build_report(
            run_id=run_id,
            rows=report_rows,
            falsifier_cases=_load_falsifier_cases(conn, run_id, report_rows),
            metadata=metadata,
            min_mature_cases=int(
                resolved_config.get("backtest_min_mature_cases", 20)
            ),
            promising_min_hit_rate=Decimal(
                resolved_config.get("backtest_promising_min_hit_rate", "0.55")
            ),
            promising_min_mean_excess=Decimal(
                resolved_config.get("backtest_promising_min_mean_excess", "0.03")
            ),
            dead_max_hit_rate=Decimal(
                resolved_config.get("backtest_dead_max_hit_rate", "0.40")
            ),
            dead_max_mean_excess=Decimal(
                resolved_config.get("backtest_dead_max_mean_excess", "0")
            ),
        )
        return render_report(report)


def postmortem_run(
    *,
    path: str | Path,
    run_id: str,
    execute: bool = False,
    runner: Callable[[str | Path, str], int] | None = None,
    assessor: Any | None = None,
    evidence_provider: Any | None = None,
    model_id: str | None = None,
    prompt_version: str | None = None,
    evidence_cutoff: date | None = None,
) -> PostmortemResult:
    """Plan or execute resumable, cutoff-bounded thesis post-mortems."""
    with _readonly_connection(path) as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise UnknownBacktestRun(f"unknown backtest run {run_id!r}")
        total = int(conn.execute(
            "SELECT COUNT(DISTINCT d.memo_key) FROM decisions AS d "
            "WHERE d.run_id = ? AND d.memo_key IS NOT NULL",
            (run_id,),
        ).fetchone()[0])
        cached = int(conn.execute(
            "SELECT COUNT(DISTINCT a.memo_key) FROM thesis_assessments AS a "
            "JOIN decisions AS d ON d.memo_key = a.memo_key WHERE d.run_id = ?",
            (run_id,),
        ).fetchone()[0])
        metadata = json.loads(run["metadata_json"])
    pending = total - cached
    updated = 0
    if execute and pending:
        if runner is not None:
            updated = runner(path, run_id)
            if updated < 0 or updated > pending:
                raise BacktestServiceError(
                    "post-mortem runner returned an invalid update count"
                )
            return PostmortemResult(
                run_id, total, cached + updated, pending - updated, updated,
            )
        if (
            assessor is None
            or evidence_provider is None
            or not (model_id or "").strip()
            or not (prompt_version or "").strip()
        ):
            raise BacktestServiceError(
                "post-mortem execution requires a configured PIT facts/assessor adapter"
            )
        cutoff = evidence_cutoff
        if cutoff is None:
            raw_cutoff = metadata.get("adjudication_date")
            if not isinstance(raw_cutoff, str):
                raise BacktestServiceError(
                    "run has no adjudication date; provide an explicit evidence cutoff"
                )
            cutoff = date.fromisoformat(raw_cutoff)
        with BacktestStore(path) as store:
            with store.transaction() as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT d.memo_key, d.case_id, fm.memo_json
                    FROM decisions AS d
                    JOIN frozen_memos AS fm ON fm.memo_key = d.memo_key
                    WHERE d.run_id = ? AND d.sequence = 0 AND d.memo_key IS NOT NULL
                    ORDER BY d.case_id, d.memo_key
                    """,
                    (run_id,),
                ).fetchall()
            for row in rows:
                memo_json = row["memo_json"]
                if not memo_json:
                    continue
                case = store.get_case(row["case_id"])
                if case is None:
                    raise BacktestServiceError(
                        f"post-mortem case {row['case_id']!r} disappeared"
                    )
                if hasattr(evidence_provider, "evidence_for"):
                    evidence = evidence_provider.evidence_for(
                        case=case, memo_json=memo_json, facts_through=cutoff,
                    )
                elif callable(evidence_provider):
                    evidence = evidence_provider(
                        case=case, memo_json=memo_json, facts_through=cutoff,
                    )
                else:
                    raise BacktestServiceError(
                        "post-mortem evidence provider must be callable or expose evidence_for"
                    )
                request = AssessmentRequest.create(
                    memo_key=row["memo_key"], case_id=case.case_id,
                    case_asof=case.asof, memo_json=memo_json,
                    evidence=tuple(evidence), evidence_cutoff=cutoff,
                    model_id=model_id or "", prompt_version=prompt_version or "",
                )
                was_cached = store.get_thesis_assessment(request.assessment_key) is not None
                assess_thesis_cached(assessor, store, request=request)
                if not was_cached:
                    updated += 1
            store.refresh_process_quadrants(run_id=run_id)
            with store.transaction() as conn:
                cached = int(conn.execute(
                    "SELECT COUNT(DISTINCT a.memo_key) FROM thesis_assessments AS a "
                    "JOIN decisions AS d ON d.memo_key = a.memo_key WHERE d.run_id = ?",
                    (run_id,),
                ).fetchone()[0])
    return PostmortemResult(run_id, total, cached, total - cached, updated)


def lessons_run(
    *,
    path: str | Path,
    run_id: str,
    execute: bool = False,
    distiller: Any | None = None,
    holdout_size: int | None = None,
    seed: int | None = None,
) -> LessonsResult:
    """Fix holdout membership for ``run_id``, then plan or distill lessons.

    Case universe: every distinct case attached to the run, MINUS near-miss
    control cases (``_is_control_case`` -- ``trigger.kind ==
    "near_miss_control"``). Controls exist to falsify the screener, not to
    teach the research brain, so they are excluded from the run's case ids
    before ``EfficacyPlan.create`` and therefore land in neither the
    training set nor the holdout set.

    Order of operations (load-bearing): (1) collect the run's distinct,
    non-control case ids; (2) ``EfficacyPlan.create`` with the configured (or
    overridden) holdout size/seed; (3) ``store.save_experiment`` the plan,
    idempotently -- if an experiment with this id already exists, it is left
    untouched (its ``lesson_fingerprint`` stays "pending" forever; re-saving
    with a different fingerprint would violate the store's identity
    invariant); (4) load thesis assessments for TRAINING cases only; (5) if
    not executing, report the plan; otherwise distill lessons from the
    training assessments via ``distill_lessons_cached``.
    """
    from ops.backtest.lessons import DistillationRequest, EfficacyPlan, distill_lessons_cached

    config = load_config()
    holdout_n = holdout_size if holdout_size is not None else config.backtest_holdout_size
    seed_n = seed if seed is not None else config.backtest_experiment_seed
    with BacktestStore(path) as store:
        with store.transaction() as conn:
            case_rows = conn.execute(
                "SELECT DISTINCT case_id FROM run_cases WHERE run_id = ? ORDER BY case_id",
                (run_id,),
            ).fetchall()
        if not case_rows:
            raise UnknownBacktestRun(f"unknown or empty backtest run {run_id!r}")
        case_ids = []
        for row in case_rows:
            case = store.get_case(row["case_id"])
            if case is None:
                raise BacktestServiceError(f"lessons case {row['case_id']!r} disappeared")
            if _is_control_case(case):
                continue
            case_ids.append(case.case_id)
        if not case_ids:
            raise BacktestServiceError(
                f"run {run_id!r} has no non-control cases to distill lessons from"
            )
        plan = EfficacyPlan.create(
            sleeve="research", case_ids=case_ids,
            holdout_size=holdout_n, seed=seed_n,
        )
        if store.get_experiment(plan.experiment_id) is None:
            store.save_experiment(plan.record(lesson_fingerprint="pending"))
        assessments = _training_assessments(store, run_id, plan.training_case_ids)
        if not execute:
            return LessonsResult(
                plan.experiment_id, plan.training_case_ids, plan.holdout_case_ids,
                len(assessments), 0, False,
            )
        if not assessments:
            raise BacktestServiceError(
                "no thesis assessments for training cases; "
                "run `backtest postmortem --execute` first"
            )
        if distiller is None:
            from ops.backtest.distiller import DISTILL_PROMPT_VERSION, Ds4LessonDistiller

            distiller = Ds4LessonDistiller(config.research_thesis_model)
            prompt_version = DISTILL_PROMPT_VERSION
            model_id = config.research_thesis_model
        else:
            prompt_version = getattr(distiller, "prompt_version", "distill-v1")
            model_id = getattr(distiller, "model_spec", "injected")
        request = DistillationRequest.create(
            sleeve="research",
            training_case_ids=plan.training_case_ids,
            holdout_case_ids=plan.holdout_case_ids,
            assessments=assessments,
            model_id=model_id, prompt_version=prompt_version,
        )
        distilled = distill_lessons_cached(distiller, store, request=request)
        return LessonsResult(
            plan.experiment_id, plan.training_case_ids, plan.holdout_case_ids,
            len(assessments), len(distilled), True,
        )


def _training_assessments(store, run_id, training_case_ids):
    with store.transaction() as conn:
        rows = conn.execute(
            "SELECT DISTINCT a.assessment_key FROM thesis_assessments AS a "
            "JOIN decisions AS d ON d.memo_key = a.memo_key "
            "WHERE d.run_id = ? ORDER BY a.assessment_key",
            (run_id,),
        ).fetchall()
    keys = [row["assessment_key"] for row in rows]
    out = []
    training = set(training_case_ids)
    for key in keys:
        assessment = store.get_thesis_assessment(key)
        if assessment is not None and assessment.case_id in training:
            out.append(assessment)
    return tuple(out)
