"""Deterministic, offline trade-level replay over cached exchange bars."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ops.backtest.models import (
    BacktestCase,
    Decision,
    DecisionAction,
    Execution,
    ExecutionSide,
    PriceBar,
    stable_hash,
)
from ops.backtest.prices import PriceSeriesStatus, next_session_after

ExitPolicy = Callable[[PriceBar, Decimal], str | None]


@dataclass(frozen=True)
class FalsifierEvaluation:
    falsifier_index: int
    name: str
    status: str
    observed: Decimal | None
    detail: str


@dataclass(frozen=True)
class ExitEvaluation:
    reason: str | None
    observations: tuple[FalsifierEvaluation, ...] = ()


@dataclass(frozen=True)
class FalsifierObservation:
    run_id: str
    case_id: str
    decision_id: str
    session: date
    falsifier_index: int
    name: str
    status: str
    observed: Decimal | None
    detail: str


@dataclass(frozen=True)
class InitialDecision:
    action: DecisionAction
    reason: str
    conviction: str = ""
    memo_key: str | None = None

    def __post_init__(self) -> None:
        if self.action not in {DecisionAction.BUY, DecisionAction.PASS}:
            raise ValueError("initial replay decision must be BUY or PASS")


@dataclass(frozen=True)
class ReplayResult:
    run_id: str
    case_id: str
    settings_hash: str
    decisions: tuple[Decision, ...]
    executions: tuple[Execution, ...]
    entry_session: date | None
    exit_session: date | None
    exit_reason: str | None
    actual_return: Decimal | None
    max_drawdown: Decimal | None
    status: str
    falsifier_observations: tuple[FalsifierObservation, ...] = ()


def settings_fingerprint(settings: Mapping[str, object]) -> str:
    """Fingerprint resolved defaults, not raw settings-file bytes."""
    return stable_hash(settings)


def _decision(
    *, run_id: str, case_id: str, sequence: int, observed_session: date,
    action: DecisionAction, reason: str, settings_hash: str,
    observed_price: Decimal | None, memo_key: str | None,
    metadata: Mapping[str, object] | None = None,
) -> Decision:
    payload = {
        "run_id": run_id, "case_id": case_id, "sequence": sequence,
        "observed_session": observed_session, "action": action,
        "settings_hash": settings_hash,
    }
    return Decision(
        decision_id=f"decision-{stable_hash(payload)[:24]}",
        run_id=run_id, case_id=case_id, sequence=sequence,
        observed_session=observed_session, action=action, reason=reason,
        settings_hash=settings_hash, observed_price=observed_price,
        memo_key=memo_key, metadata=dict(metadata or {}),
    )


def _execution(
    *, run_id: str, case_id: str, decision: Decision, session: date,
    side: ExecutionSide, price: Decimal, quantity: Decimal,
) -> Execution:
    payload = {
        "decision_id": decision.decision_id, "session": session, "side": side,
    }
    return Execution(
        execution_id=f"execution-{stable_hash(payload)[:24]}",
        run_id=run_id, case_id=case_id, decision_id=decision.decision_id,
        session=session, side=side, price=price, quantity=quantity,
        notional=price * quantity,
    )


def replay_case(
    *,
    run_id: str,
    case: BacktestCase,
    initial: InitialDecision,
    bars: Sequence[PriceBar],
    notional: Decimal,
    settings: Mapping[str, object],
    exit_policy: ExitPolicy | None = None,
    price_status: PriceSeriesStatus = PriceSeriesStatus.READY,
    price_state_reason: str | None = None,
    terminal_value: Decimal = Decimal("0"),
    next_session: Callable[[date], date] = next_session_after,
) -> ReplayResult:
    """Replay one independent case using next-session-open executions.

    Bars must already be drawn from the persistent cache. The function neither
    fetches data nor calls a model, which makes settings replays intrinsically
    free of generation work.
    """
    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    case.validate_cutoff()
    if notional <= 0:
        raise ValueError("notional must be positive")
    if terminal_value < 0:
        raise ValueError("terminal_value must be nonnegative")
    settings_hash = settings_fingerprint(settings)
    ordered = sorted(
        (bar for bar in bars if bar.symbol.upper() == case.symbol.upper()),
        key=lambda bar: bar.session,
    )
    if len({bar.session for bar in ordered}) != len(ordered):
        raise ValueError("duplicate price-bar session")

    decisions: list[Decision] = []
    executions: list[Execution] = []
    observations: list[FalsifierObservation] = []
    by_session = {bar.session: bar for bar in ordered}
    expected_entry_session = next_session(case.asof)
    expected_entry_bar = by_session.get(expected_entry_session)
    first_price = (
        expected_entry_bar.adjusted_open if expected_entry_bar is not None else None
    )
    initial_row = _decision(
        run_id=run_id, case_id=case.case_id, sequence=0,
        observed_session=case.asof, action=initial.action, reason=initial.reason,
        settings_hash=settings_hash, observed_price=first_price,
        memo_key=initial.memo_key,
        metadata={
            "conviction": initial.conviction,
            "price_status": price_status.value,
            "price_state_reason": price_state_reason or "",
        },
    )
    decisions.append(initial_row)
    if initial.action == DecisionAction.PASS:
        return ReplayResult(
            run_id=run_id, case_id=case.case_id, settings_hash=settings_hash,
            decisions=tuple(decisions), executions=(), entry_session=None,
            exit_session=None, exit_reason=None, actual_return=None,
            max_drawdown=None, status="complete",
        )

    if expected_entry_bar is None:
        missing_status = (
            "pending" if price_status == PriceSeriesStatus.PENDING
            else "stale" if price_status == PriceSeriesStatus.STALE
            else "unpriceable"
        )
        return ReplayResult(
            run_id=run_id, case_id=case.case_id, settings_hash=settings_hash,
            decisions=tuple(decisions), executions=(), entry_session=None,
            exit_session=None, exit_reason=None, actual_return=None,
            max_drawdown=None, status=missing_status,
        )

    eligible = [bar for bar in ordered if bar.session >= expected_entry_session]
    entry_bar = expected_entry_bar
    entry_price = entry_bar.adjusted_open
    quantity = notional / entry_price
    executions.append(_execution(
        run_id=run_id, case_id=case.case_id, decision=initial_row,
        session=entry_bar.session, side=ExecutionSide.BUY,
        price=entry_price, quantity=quantity,
    ))

    sequence = 1
    closes: list[Decimal] = []
    exit_session = None
    exit_reason = None
    exit_price = None
    for bar in eligible:
        closes.append(bar.adjusted_close)
        evaluation = ExitEvaluation(None)
        if exit_policy is not None:
            rich_evaluate = getattr(exit_policy, "evaluate", None)
            if callable(rich_evaluate):
                evaluation = rich_evaluate(bar, entry_price)
            else:
                evaluation = ExitEvaluation(exit_policy(bar, entry_price))
        reason = evaluation.reason
        action = DecisionAction.SELL if reason is not None else DecisionAction.HOLD
        row = _decision(
            run_id=run_id, case_id=case.case_id, sequence=sequence,
            observed_session=bar.session, action=action,
            reason=reason or "hold", settings_hash=settings_hash,
            observed_price=bar.adjusted_close, memo_key=initial.memo_key,
        )
        decisions.append(row)
        observations.extend(
            FalsifierObservation(
                run_id=run_id,
                case_id=case.case_id,
                decision_id=row.decision_id,
                session=bar.session,
                falsifier_index=item.falsifier_index,
                name=item.name,
                status=item.status,
                observed=item.observed,
                detail=item.detail,
            )
            for item in evaluation.observations
        )
        sequence += 1
        if reason is None:
            continue
        expected_exit_session = next_session(bar.session)
        fill_bar = by_session.get(expected_exit_session)
        if fill_bar is None:
            exit_reason = reason
            break
        exit_session = fill_bar.session
        exit_price = fill_bar.adjusted_open
        exit_reason = reason
        executions.append(_execution(
            run_id=run_id, case_id=case.case_id, decision=row,
            session=fill_bar.session, side=ExecutionSide.SELL,
            price=exit_price, quantity=quantity,
        ))
        break

    terminal = price_status == PriceSeriesStatus.TERMINAL and exit_price is None
    if terminal:
        mark = terminal_value
        terminal_detail = price_state_reason or "terminal price series"
        exit_reason = f"terminal: {terminal_detail}"
    else:
        mark = exit_price if exit_price is not None else (
            closes[-1] if closes else entry_price
        )
    actual_return = (mark - entry_price) / entry_price
    peak = entry_price
    max_drawdown = Decimal("0")
    for close in closes:
        peak = max(peak, close)
        max_drawdown = min(max_drawdown, (close - peak) / peak)
    if terminal:
        max_drawdown = min(max_drawdown, (terminal_value - peak) / peak)
    if terminal or exit_session is not None:
        status = "complete"
    elif price_status == PriceSeriesStatus.PENDING:
        status = "pending"
    elif price_status == PriceSeriesStatus.STALE:
        status = "stale"
    elif price_status == PriceSeriesStatus.UNPRICEABLE or exit_reason is not None:
        status = "unpriceable"
    else:
        status = "complete"
    return ReplayResult(
        run_id=run_id, case_id=case.case_id, settings_hash=settings_hash,
        decisions=tuple(decisions), executions=tuple(executions),
        entry_session=entry_bar.session, exit_session=exit_session,
        exit_reason=exit_reason, actual_return=actual_return,
        max_drawdown=max_drawdown,
        status=status,
        falsifier_observations=tuple(observations),
    )
