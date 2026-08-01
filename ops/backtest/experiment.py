"""Paired holdout efficacy: replay control vs treated frozen memos.

The experiment answers one narrow question: on cases the lesson distiller
never saw (the pre-fixed holdout), does a memo generated *with* the eligible
lessons grade better than the memo generated without them?  Both arms are
frozen memos already on disk -- this module makes zero model and zero
network calls; it only looks memos up by cache identity and replays them
against cached prices, exactly the way ``backtest run`` does.

Variant identity: the control arm is the ordinary memo whose generation
identity carries ``lesson_fingerprint="none"``; the treated arm is the memo
whose identity carries the fingerprint of the lessons eligible as of that
case's ``asof``.  That fingerprint is computed the same way in both
``run_paired_efficacy`` and :func:`ops.backtest.service._generation_requests`
(``lesson_set_hash(eligible_lessons(...))``), and this evaluator asserts the
two agree rather than trusting either alone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ops.backtest.lessons import PairedCaseInput
from ops.backtest.models import Lesson
from ops.backtest.service import (
    DEFAULT_BRAIN_VERSION,
    DEFAULT_PROMPT_VERSION,
    BacktestServiceError,
    MissingBacktestArtifacts,
    _generation_requests,
    _resolved_settings,
    replay_and_evaluate_case,
)
from ops.backtest.store import BacktestStore
from ops.config import OpsConfig


def generate_command(case) -> str:
    """The exact command that would create this case's missing memo."""
    return (
        f"ops backtest generate --sleeve {case.sleeve} "
        f"--start {case.asof.isoformat()} --end {case.asof.isoformat()} --execute"
    )


def variant_request(
    store: BacktestStore,
    case,
    *,
    config: OpsConfig,
    lessons: Sequence[Lesson],
    brain_version: str = DEFAULT_BRAIN_VERSION,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
):
    """Rebuild one case's generation request for a lesson set.

    ``lessons=()`` yields the control identity (``lesson_fingerprint="none"``);
    passing the experiment's lessons yields the treated identity, with
    per-case eligibility applied inside ``_generation_requests``.
    """
    requests = _generation_requests(
        store, [case], config=config,
        brain_version=brain_version, prompt_version=prompt_version,
        lessons=lessons,
    )
    if not requests:  # pragma: no cover - _generation_requests raises first
        raise MissingBacktestArtifacts(
            f"case {case.case_id} lacks a sealed context manifest"
        )
    return requests[0]


def primary_utility(outcomes, result) -> float:
    """Float of the primary-horizon decision utility for a replayed case.

    ``CaseResult`` has no ``utility`` field (the plan's ``primary_utility``
    does not exist): utility lives on the per-horizon ``HorizonOutcome``, so
    the primary horizon's row is selected here.  An ungraded horizon
    (unpriceable/pending -- ``utility is None``) scores 0.0: no measurable
    edge either way, which keeps the paired delta finite and honest instead
    of dropping the pair.
    """
    for outcome in outcomes:
        if outcome.horizon_sessions == result.primary_horizon:
            return float(outcome.utility) if outcome.utility is not None else 0.0
    raise BacktestServiceError(
        f"replay of {result.case_id} produced no {result.primary_horizon}-session outcome"
    )


@dataclass
class ReplayPairedEvaluator:
    """`PairedEvaluator` that grades a variant's frozen memo by replay."""

    store: BacktestStore
    config: OpsConfig
    lessons: Sequence[Lesson]
    today: date
    experiment_id: str
    settings: Mapping[str, Any] = field(default_factory=dict)
    brain_version: str = DEFAULT_BRAIN_VERSION
    prompt_version: str = DEFAULT_PROMPT_VERSION

    def __post_init__(self) -> None:
        self._resolved = _resolved_settings(self.config, self.settings)
        self._prices = None

    def evaluate(
        self,
        *,
        case_input: PairedCaseInput,
        variant: str,
        lesson_fingerprint: str | None,
    ) -> float:
        if variant not in ("control", "treated"):
            raise ValueError(f"unknown paired variant {variant!r}")
        case = self.store.get_case(case_input.case_id)
        if case is None:
            raise BacktestServiceError(f"holdout case {case_input.case_id!r} disappeared")
        lessons = () if variant == "control" else self.lessons
        request = variant_request(
            self.store, case, config=self.config, lessons=lessons,
            brain_version=self.brain_version, prompt_version=self.prompt_version,
        )
        expected = "none" if variant == "control" else (lesson_fingerprint or "none")
        if request.lesson_fingerprint != expected:
            raise BacktestServiceError(
                f"{variant} memo identity for {case.case_id} carries fingerprint "
                f"{request.lesson_fingerprint!r}, expected {expected!r}"
            )
        record = self.store.get_frozen_memo(request.memo_key)
        if record is None:
            raise MissingBacktestArtifacts(
                f"{variant} memo missing for case {case.case_id} "
                f"({case.symbol} {case.asof.isoformat()}, "
                f"lesson_fingerprint={request.lesson_fingerprint}); "
                f"create it with: {generate_command(case)}"
            )
        if self._prices is None:
            from ops.backtest.prices import PriceCache

            self._prices = PriceCache(self.store.path)
        _replay, outcomes, result = replay_and_evaluate_case(
            prices=self._prices, run_id=f"{self.experiment_id}-{variant}",
            case=case, record=record, resolved=self._resolved, today=self.today,
        )
        return primary_utility(outcomes, result)
