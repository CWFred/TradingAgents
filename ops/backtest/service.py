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
from collections.abc import Callable, Mapping, Sequence
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
from ops.backtest.store import BacktestStore
from ops.backtest.verdicts import evaluate_replay
from ops.config import OpsConfig

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
    if not 30 <= case_count <= 50:
        raise InvalidBacktestRequest("cases must be in the approved range 30..50")


def _selected_cases(
    store: BacktestStore,
    *,
    sleeve: str,
    start: date,
    end: date,
    case_count: int,
):
    cases = [
        case for case in store.list_cases(sleeve=sleeve)
        if start <= case.asof <= end
    ][:case_count]
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
        except Exception:
            store.finish_run(run_id, status="failed")
            raise
        store.finish_run(run_id)

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
) -> tuple[GenerationRequest, ...]:
    requests = []
    missing_manifests = []
    for case in cases:
        manifest = store.get_context_manifest(case.case_id)
        if manifest is None:
            missing_manifests.append(case.symbol)
            continue
        requests.append(GenerationRequest.create(
            case=case, manifest=manifest,
            brain_version=brain_version, prompt_version=prompt_version,
            evidence_model_id=config.research_evidence_model,
            thesis_model_id=config.research_thesis_model,
        ))
    if missing_manifests:
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
) -> GenerationSummary:
    from ops.llm_backend import build_managed_backend, load_managed_backend_config
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
    try:
        backend.ensure_up()

        def generator(request):
            return generate_research_memo(
                request, evidence_llm=evidence_llm, thesis_llm=thesis_llm,
            )

        return run_generation_jobs(
            plan, store=store, generator=generator,
            stale_before=datetime.now(timezone.utc) - timedelta(hours=6),
            max_jobs=max_jobs,
        )
    finally:
        backend.shutdown()


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
        store.insert_case(case)
        store.save_context_manifest(manifest)
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
        if bars:
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


def generate_cases(
    *,
    config: OpsConfig,
    sleeve: str,
    start: date,
    end: date,
    case_count: int,
    today: date,
    execute: bool = False,
    max_jobs: int | None = None,
    brain_version: str = DEFAULT_BRAIN_VERSION,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    executor: Callable[..., GenerationSummary] | None = None,
    preparer: Callable[..., Sequence[BacktestCase]] | None = None,
) -> GenerationResult:
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
        available = [
            case for case in store.list_cases(sleeve=sleeve)
            if start <= case.asof <= end
        ]
        if not available:
            prepare = preparer or _default_prepare_cases
            prepare(
                store=store, config=config, sleeve=sleeve,
                start=start, end=end, case_count=case_count,
            )
        cases = _selected_cases(
            store, sleeve=sleeve, start=start, end=end, case_count=case_count,
        )
        requests = _generation_requests(
            store, cases, config=config,
            brain_version=brain_version, prompt_version=prompt_version,
        )
        plan = plan_generation(requests, store=store)
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
