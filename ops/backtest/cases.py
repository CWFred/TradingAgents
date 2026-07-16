"""Deterministic case-date sampling and screen-hit selection.

This module deliberately does not fetch a universe or market data.  A case
source supplies exchange sessions and scored hits; the functions here make the
selection reproducible and enforce the model-training cutoff before a case can
be constructed.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, TypeVar

from ops.backtest.models import BacktestCase, CaseSource, enforce_cutoff

HISTORICAL_SOURCE_MODE = "point-in-time"
RECONSTRUCTION_SOURCE_MODE = "exploratory/current-universe-reconstruction"


@dataclass(frozen=True)
class CaseCandidate:
    """One scored screener hit before it becomes a persisted backtest case."""

    symbol: str
    asof: date
    score: Decimal | int | float | str
    trigger: Mapping[str, Any]
    screen_payload: Mapping[str, Any] = field(default_factory=dict)
    source_ref: str | None = None

    def normalized_symbol(self) -> str:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("case candidate symbol must not be empty")
        return symbol

    def decimal_score(self) -> Decimal:
        try:
            score = Decimal(str(self.score))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid case score {self.score!r}") from exc
        if not score.is_finite():
            raise ValueError(f"case score must be finite, got {self.score!r}")
        return score


class CaseSourceProtocol(Protocol):
    """Source of screen hits for sampled historical dates."""

    source_mode: str

    def candidates(self, *, asof: date) -> Sequence[CaseCandidate]: ...


@dataclass(frozen=True)
class HistoricalCaseSource:
    """Adapter for a source that can prove historical universe membership."""

    fetch: Callable[[date], Sequence[CaseCandidate]]
    source_mode: str = HISTORICAL_SOURCE_MODE

    def candidates(self, *, asof: date) -> Sequence[CaseCandidate]:
        return self.fetch(asof)


@dataclass(frozen=True)
class CurrentUniverseReconstructionSource:
    """Explicitly biased fallback over today's universe membership.

    The label is load-bearing: reports must never render reconstructed cases
    as a clean point-in-time historical screen.
    """

    fetch: Callable[[date], Sequence[CaseCandidate]]
    source_mode: str = RECONSTRUCTION_SOURCE_MODE

    def candidates(self, *, asof: date) -> Sequence[CaseCandidate]:
        return self.fetch(asof)


def sample_sessions(
    sessions: Iterable[date],
    *,
    start: date,
    end: date,
    spacing_sessions: int = 10,
) -> tuple[date, ...]:
    """Sample sorted exchange sessions at a roughly two-week cadence.

    ``sessions`` comes from the exchange calendar (not weekday arithmetic), so
    holidays remain correct and tests can be completely offline.  The first
    eligible session is always included; subsequent dates are exactly
    ``spacing_sessions`` observations apart.
    """
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    if spacing_sessions <= 0:
        raise ValueError("spacing_sessions must be positive")
    eligible = sorted({session for session in sessions if start <= session <= end})
    return tuple(eligible[::spacing_sessions])


def collect_candidates(
    source: CaseSourceProtocol,
    sampled_dates: Iterable[date],
) -> tuple[CaseCandidate, ...]:
    """Read candidates in date order and stamp the requested as-of date.

    A source returning a hit for another date is a provenance failure, not a
    convenience to silently repair.
    """
    out: list[CaseCandidate] = []
    for asof in sorted(set(sampled_dates)):
        for candidate in source.candidates(asof=asof):
            if candidate.asof != asof:
                raise ValueError(
                    f"case source returned {candidate.symbol} asof {candidate.asof} "
                    f"for requested date {asof}"
                )
            out.append(candidate)
    return tuple(out)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _candidate_tiebreak(candidate: CaseCandidate) -> tuple[str, str, str]:
    return (
        candidate.source_ref or "",
        _canonical(candidate.trigger),
        _canonical(candidate.screen_payload),
    )


def select_candidates(
    candidates: Iterable[CaseCandidate],
    *,
    target_count: int,
    per_date_cap: int,
) -> tuple[CaseCandidate, ...]:
    """Select a stable, date-spread set of cases.

    Duplicate ``(symbol, asof)`` hits collapse to the highest score (then a
    canonical provenance tie-break).  Within a date hits rank by descending
    score and ascending symbol.  Selection is round-robin by rank across dates
    so one prolific screen date cannot consume the target before other regimes
    contribute cases.
    """
    if target_count < 0:
        raise ValueError("target_count must not be negative")
    if per_date_cap <= 0:
        raise ValueError("per_date_cap must be positive")
    if target_count == 0:
        return ()

    deduped: dict[tuple[str, date], CaseCandidate] = {}
    for candidate in candidates:
        symbol = candidate.normalized_symbol()
        score = candidate.decimal_score()
        normalized = CaseCandidate(
            symbol=symbol,
            asof=candidate.asof,
            score=score,
            trigger=dict(candidate.trigger),
            screen_payload=dict(candidate.screen_payload),
            source_ref=candidate.source_ref,
        )
        key = (symbol, candidate.asof)
        current = deduped.get(key)
        if current is None:
            deduped[key] = normalized
            continue
        if score > current.decimal_score() or (
            score == current.decimal_score()
            and _candidate_tiebreak(normalized) < _candidate_tiebreak(current)
        ):
            deduped[key] = normalized

    by_date: dict[date, list[CaseCandidate]] = {}
    for candidate in deduped.values():
        by_date.setdefault(candidate.asof, []).append(candidate)
    for hits in by_date.values():
        hits.sort(
            key=lambda hit: (
                -hit.decimal_score(),
                hit.normalized_symbol(),
                _candidate_tiebreak(hit),
            )
        )

    selected: list[CaseCandidate] = []
    dates = sorted(by_date)
    for rank in range(per_date_cap):
        for asof in dates:
            hits = by_date[asof]
            if rank < len(hits):
                selected.append(hits[rank])
                if len(selected) == target_count:
                    return tuple(selected)
    return tuple(selected)


def validate_cutoff(asof: date, *, cutoff: date) -> None:
    """Fail closed for any case before the effective cutoff."""
    enforce_cutoff(asof, cutoff)


T = TypeVar("T")


def construct_case(
    candidate: CaseCandidate,
    *,
    sleeve: str,
    cutoff: date,
    source: CaseSource | str,
    factory: Callable[..., T] | Any = BacktestCase,
) -> T:
    """Construct a domain case only after validating cutoff and provenance.

    ``factory`` is explicit to keep selection independent of the persistence
    model. Production passes ``BacktestCase``, whose ``create`` factory repeats
    the cutoff check as a defense in depth.
    """
    validate_cutoff(candidate.asof, cutoff=cutoff)
    symbol = candidate.normalized_symbol()
    create = getattr(factory, "create", factory)
    trigger = dict(candidate.trigger)
    if candidate.source_ref is not None:
        trigger.setdefault("source_ref", candidate.source_ref)
    if candidate.screen_payload:
        trigger.setdefault("screen_payload", dict(candidate.screen_payload))
    return create(
        sleeve=sleeve,
        symbol=symbol,
        asof=candidate.asof,
        trigger=trigger,
        source=source,
        score=candidate.decimal_score(),
        cutoff=cutoff,
    )
