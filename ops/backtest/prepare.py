"""Corpus preparation: case selection plus point-in-time context sealing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from ops.backtest.cases import (
    CaseSourceProtocol,
    collect_candidates,
    construct_case,
    sample_sessions,
    select_candidates,
)
from ops.backtest.context import ContextArtifact, build_context_manifest
from ops.backtest.models import BacktestCase, CaseSource


@dataclass(frozen=True)
class PreparedContext:
    artifacts: tuple[ContextArtifact, ...]
    substitutions: tuple[str, ...] = ()


class PreparationContextBuilder(Protocol):
    def build(self, case: BacktestCase) -> PreparedContext: ...


@dataclass(frozen=True)
class PreparationSummary:
    sampled_dates: tuple[date, ...]
    selected_case_ids: tuple[str, ...]
    source_mode: str


def prepare_cases(
    *,
    store,
    source: CaseSourceProtocol,
    context_builder: PreparationContextBuilder,
    exchange_sessions: Iterable[date],
    start: date,
    end: date,
    target_count: int,
    per_date_cap: int,
    sleeve: str,
    cutoff: date,
    spacing_sessions: int = 10,
) -> PreparationSummary:
    """Select cases and atomically seal each case's exact generation inputs.

    Network/data-source ownership stays outside this function. A production
    source may reconstruct today's universe only when its ``source_mode`` says
    so; that label is persisted on every case and later rendered in reports.
    """
    sampled = sample_sessions(
        exchange_sessions, start=start, end=end,
        spacing_sessions=spacing_sessions,
    )
    candidates = collect_candidates(source, sampled)
    selected = select_candidates(
        candidates, target_count=target_count, per_date_cap=per_date_cap,
    )
    try:
        source_mode = CaseSource(source.source_mode)
    except ValueError as exc:
        raise ValueError(f"unknown case source mode {source.source_mode!r}") from exc

    case_ids: list[str] = []
    for candidate in selected:
        case = construct_case(
            candidate, sleeve=sleeve, cutoff=cutoff,
            source=source_mode, factory=BacktestCase,
        )
        prepared = context_builder.build(case)
        if not isinstance(prepared, PreparedContext):
            raise TypeError("context builder must return PreparedContext")
        manifest = build_context_manifest(
            case_id=case.case_id, asof=case.asof,
            artifacts=prepared.artifacts,
            substitutions=prepared.substitutions,
        )
        # BacktestStore operations are individually transactional and
        # idempotent. Nested transaction() makes the pair atomic where the
        # concrete store supports it.
        transaction = getattr(store, "transaction", None)
        if transaction is None:
            store.insert_case(case)
            store.save_context_manifest(manifest)
        else:
            with transaction():
                store.insert_case(case)
                store.save_context_manifest(manifest)
        case_ids.append(case.case_id)
    return PreparationSummary(sampled, tuple(case_ids), source.source_mode)
