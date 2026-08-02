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
from ops.backtest.models import Lesson, OutcomeState
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


def generate_command_span(
    *,
    sleeve: str,
    start: date | None = None,
    end: date | None = None,
    experiment_id: str | None = None,
) -> str:
    """The exact command that generates the memos being asked for.

    ``--experiment`` is what makes the treated arm reachable: without it
    ``backtest generate`` threads no lessons and can only ever (re)produce
    the control memo, so an operator following the printed command would
    loop forever on a missing treated memo.  In that mode the command takes
    no window and no ``--cases`` budget -- generation covers exactly the
    experiment's holdout membership -- so what is printed here is literally
    what the code honours, with no budget arithmetic to get wrong.
    """
    if experiment_id:
        return (
            f"ops backtest generate --sleeve {sleeve} "
            f"--experiment {experiment_id} --execute"
        )
    return (
        f"ops backtest generate --sleeve {sleeve} "
        f"--start {start.isoformat()} --end {end.isoformat()} --execute"
    )


def generate_command(case, experiment_id: str | None = None) -> str:
    """The exact command that creates this one case's missing memo variant."""
    return generate_command_span(
        sleeve=case.sleeve, start=case.asof, end=case.asof,
        experiment_id=experiment_id,
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
        # Experiment identities bypass the temporal lesson gate -- see
        # _generation_requests(enforce_lesson_eligibility=...) rationale.
        enforce_lesson_eligibility=False,
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
    the primary horizon's row is selected here.

    An ungraded horizon (``PENDING``/``UNPRICEABLE``, i.e. cached prices do
    not reach the adjudication date) is a **refusal**, not a 0.0: scoring it
    zero would make a stale-price experiment report ``mean_delta 0.0``,
    indistinguishable from a genuine null result -- and stale prices are the
    default path, since the adjudication date is today.  Both arms replay
    the same bars, so raising here is symmetric and cannot bias a variant.
    """
    for outcome in outcomes:
        if outcome.horizon_sessions != result.primary_horizon:
            continue
        if outcome.state != OutcomeState.MATURE or outcome.utility is None:
            raise MissingBacktestArtifacts(
                f"{result.case_id}: {result.primary_horizon}-session outcome is "
                f"{outcome.state.value}, not mature ({outcome.detail or 'no detail'}); "
                "refresh cached prices with `ops backtest prices` before grading "
                "this experiment"
            )
        return float(outcome.utility)
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
                "create it with: "
                + generate_command(
                    case, self.experiment_id if variant == "treated" else None,
                )
            )
        if self._prices is None:
            from ops.backtest.prices import PriceCache

            self._prices = PriceCache(self.store.path)
        _replay, outcomes, result = replay_and_evaluate_case(
            prices=self._prices, run_id=f"{self.experiment_id}-{variant}",
            case=case, record=record, resolved=self._resolved, today=self.today,
        )
        return primary_utility(outcomes, result)
