"""Mechanical, point-in-time verdicts over cached daily bars."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from ops.backtest.models import (
    CaseResult,
    DecisionAction,
    HorizonOutcome,
    OutcomeLabel,
    OutcomeState,
    PriceBar,
)
from ops.backtest.prices import PriceSeriesStatus, next_session_after
from ops.backtest.replay import ReplayResult
from ops.scheduler.market_calendar import MarketCalendar

DEFAULT_HORIZONS = (5, 21, 63, 126)


def action_utility(action: DecisionAction, excess_return: Decimal) -> Decimal:
    """Convert asset excess into decision utility.

    BUY/HOLD benefit from outperformance.  PASS/SELL are correct when the
    avoided/sold asset subsequently underperforms.
    """
    if action in {DecisionAction.PASS, DecisionAction.SELL}:
        return -excess_return
    return excess_return


def utility_label(utility: Decimal, wash_band: Decimal) -> OutcomeLabel:
    if wash_band < 0:
        raise ValueError("wash_band must be nonnegative")
    if utility == 0 or abs(utility) < wash_band:
        return OutcomeLabel.WASH
    return OutcomeLabel.WIN if utility > 0 else OutcomeLabel.LOSS


def _advance_sessions(
    session: date,
    count: int,
    *,
    calendar: MarketCalendar,
) -> date:
    result = session
    for _ in range(count):
        result = next_session_after(result, calendar=calendar)
    return result


def _placeholder(
    *,
    run_id: str,
    case_id: str,
    horizon: int,
    state: OutcomeState,
    entry_session: date | None,
    horizon_session: date | None,
    detail: str,
) -> HorizonOutcome:
    label = (
        OutcomeLabel.PENDING
        if state == OutcomeState.PENDING
        else OutcomeLabel.UNPRICEABLE
    )
    return HorizonOutcome(
        run_id=run_id,
        case_id=case_id,
        horizon_sessions=horizon,
        state=state,
        label=label,
        entry_session=entry_session,
        horizon_session=horizon_session,
        detail=detail,
    )


def compute_horizon_outcomes(
    *,
    run_id: str,
    case_id: str,
    action: DecisionAction,
    decision_asof: date,
    stock_bars: Sequence[PriceBar],
    benchmark_bars: Sequence[PriceBar],
    adjudication_date: date,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    wash_band: Decimal = Decimal("0.03"),
    stock_status: PriceSeriesStatus = PriceSeriesStatus.READY,
    benchmark_status: PriceSeriesStatus = PriceSeriesStatus.READY,
    stock_status_reason: str | None = None,
    benchmark_status_reason: str | None = None,
    stock_terminal_session: date | None = None,
    calendar: MarketCalendar | None = None,
) -> tuple[HorizonOutcome, ...]:
    """Evaluate exact fixed-session outcomes without filling data gaps.

    Entry is the next NYSE regular-session open after ``decision_asof``.
    Horizon N is the close exactly N exchange sessions after entry.  Bars
    after ``adjudication_date`` are ignored even when supplied.
    """
    if not run_id or not case_id:
        raise ValueError("run_id and case_id must not be empty")
    normalized = tuple(horizons)
    if not normalized or any(h <= 0 for h in normalized):
        raise ValueError("horizons must be nonempty and positive")
    if len(set(normalized)) != len(normalized):
        raise ValueError("horizons must be unique")
    calendar = calendar or MarketCalendar()
    entry_session = next_session_after(decision_asof, calendar=calendar)
    stock = {
        bar.session: bar for bar in stock_bars if bar.session <= adjudication_date
    }
    benchmark = {
        bar.session: bar for bar in benchmark_bars if bar.session <= adjudication_date
    }
    if len(stock) != len([b for b in stock_bars if b.session <= adjudication_date]):
        raise ValueError("duplicate stock-bar session")
    if len(benchmark) != len([b for b in benchmark_bars if b.session <= adjudication_date]):
        raise ValueError("duplicate benchmark-bar session")

    entry_stock = stock.get(entry_session)
    entry_benchmark = benchmark.get(entry_session)
    outcomes: list[HorizonOutcome] = []
    for horizon in normalized:
        target = _advance_sessions(entry_session, horizon, calendar=calendar)
        if adjudication_date < entry_session or adjudication_date < target:
            outcomes.append(_placeholder(
                run_id=run_id, case_id=case_id, horizon=horizon,
                state=OutcomeState.PENDING, entry_session=entry_session,
                horizon_session=target,
                detail=f"horizon matures after {adjudication_date}",
            ))
            continue
        if entry_stock is None or entry_benchmark is None:
            missing = []
            if entry_stock is None:
                missing.append("stock")
            if entry_benchmark is None:
                missing.append("benchmark")
            pending_series = (
                entry_stock is None and stock_status == PriceSeriesStatus.PENDING
            ) or (
                entry_benchmark is None
                and benchmark_status == PriceSeriesStatus.PENDING
            )
            state = (
                OutcomeState.PENDING if pending_series
                else OutcomeState.UNPRICEABLE
            )
            state_detail = []
            if entry_stock is None:
                state_detail.append(
                    f"stock {stock_status.value}"
                    + (f" ({stock_status_reason})" if stock_status_reason else "")
                )
            if entry_benchmark is None:
                state_detail.append(
                    f"benchmark {benchmark_status.value}"
                    + (
                        f" ({benchmark_status_reason})"
                        if benchmark_status_reason else ""
                    )
                )
            outcomes.append(_placeholder(
                run_id=run_id, case_id=case_id, horizon=horizon,
                state=state, entry_session=entry_session,
                horizon_session=target,
                detail=(
                    f"missing {' and '.join(missing)} entry bar on {entry_session}; "
                    + "; ".join(state_detail)
                ),
            ))
            continue
        target_stock = stock.get(target)
        target_benchmark = benchmark.get(target)
        terminal_stock = (
            target_stock is None
            and stock_status == PriceSeriesStatus.TERMINAL
            and (
                stock_terminal_session is None
                or stock_terminal_session <= target
            )
        )
        if (target_stock is None and not terminal_stock) or target_benchmark is None:
            missing = []
            if target_stock is None and not terminal_stock:
                missing.append("stock")
            if target_benchmark is None:
                missing.append("benchmark")
            pending_series = (
                target_stock is None
                and not terminal_stock
                and stock_status == PriceSeriesStatus.PENDING
            ) or (
                target_benchmark is None
                and benchmark_status == PriceSeriesStatus.PENDING
            )
            state = (
                OutcomeState.PENDING if pending_series
                else OutcomeState.UNPRICEABLE
            )
            state_detail = []
            if target_stock is None and not terminal_stock:
                state_detail.append(
                    f"stock {stock_status.value}"
                    + (f" ({stock_status_reason})" if stock_status_reason else "")
                )
            if target_benchmark is None:
                state_detail.append(
                    f"benchmark {benchmark_status.value}"
                    + (
                        f" ({benchmark_status_reason})"
                        if benchmark_status_reason else ""
                    )
                )
            outcomes.append(_placeholder(
                run_id=run_id, case_id=case_id, horizon=horizon,
                state=state, entry_session=entry_session,
                horizon_session=target,
                detail=(
                    f"missing {' and '.join(missing)} horizon bar on {target}; "
                    + "; ".join(state_detail)
                ),
            ))
            continue
        stock_return = (
            Decimal("-1")
            if terminal_stock
            else (target_stock.adjusted_close - entry_stock.adjusted_open)
            / entry_stock.adjusted_open
        )
        benchmark_return = (
            target_benchmark.adjusted_close - entry_benchmark.adjusted_open
        ) / entry_benchmark.adjusted_open
        excess = stock_return - benchmark_return
        utility = action_utility(action, excess)
        outcomes.append(HorizonOutcome(
            run_id=run_id,
            case_id=case_id,
            horizon_sessions=horizon,
            state=OutcomeState.MATURE,
            label=utility_label(utility, wash_band),
            stock_return=stock_return,
            benchmark_return=benchmark_return,
            excess_return=excess,
            utility=utility,
            entry_session=entry_session,
            horizon_session=target,
            detail=(
                f"{action.value} utility from excess return; terminal stock "
                f"marked at zero ({stock_status_reason or 'terminal series'})"
                if terminal_stock
                else f"{action.value} utility from excess return"
            ),
        ))
    return tuple(outcomes)


def case_result_from_replay(
    replay: ReplayResult,
    outcomes: Sequence[HorizonOutcome],
    *,
    primary_horizon: int = 63,
) -> CaseResult:
    """Combine actual replay P&L with the independent fixed-horizon label."""
    if not replay.decisions:
        raise ValueError("replay must contain an initial decision")
    matching = [o for o in outcomes if o.horizon_sessions == primary_horizon]
    if len(matching) != 1:
        raise ValueError(f"expected exactly one {primary_horizon}-session outcome")
    primary = matching[0]
    if primary.state == OutcomeState.PENDING or replay.status == "pending":
        status = "pending"
    elif (
        primary.state == OutcomeState.UNPRICEABLE
        or replay.status in {"unpriceable", "stale"}
    ):
        status = "unpriceable"
    elif replay.status == "failed":
        status = "failed"
    else:
        status = "complete"
    return CaseResult(
        run_id=replay.run_id,
        case_id=replay.case_id,
        initial_action=replay.decisions[0].action,
        status=status,
        primary_horizon=primary_horizon,
        primary_label=primary.label,
        actual_return=replay.actual_return,
        max_drawdown=replay.max_drawdown,
        exit_session=replay.exit_session,
        exit_reason=replay.exit_reason,
    )


def evaluate_replay(
    replay: ReplayResult,
    *,
    stock_bars: Sequence[PriceBar],
    benchmark_bars: Sequence[PriceBar],
    adjudication_date: date,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    primary_horizon: int = 63,
    wash_band: Decimal = Decimal("0.03"),
    stock_status: PriceSeriesStatus = PriceSeriesStatus.READY,
    benchmark_status: PriceSeriesStatus = PriceSeriesStatus.READY,
    stock_status_reason: str | None = None,
    benchmark_status_reason: str | None = None,
    stock_terminal_session: date | None = None,
    calendar: MarketCalendar | None = None,
) -> tuple[tuple[HorizonOutcome, ...], CaseResult]:
    if not replay.decisions:
        raise ValueError("replay must contain an initial decision")
    initial = replay.decisions[0]
    outcomes = compute_horizon_outcomes(
        run_id=replay.run_id,
        case_id=replay.case_id,
        action=initial.action,
        decision_asof=initial.observed_session,
        stock_bars=stock_bars,
        benchmark_bars=benchmark_bars,
        adjudication_date=adjudication_date,
        horizons=horizons,
        wash_band=wash_band,
        stock_status=stock_status,
        benchmark_status=benchmark_status,
        stock_status_reason=stock_status_reason,
        benchmark_status_reason=benchmark_status_reason,
        stock_terminal_session=stock_terminal_session,
        calendar=calendar,
    )
    return outcomes, case_result_from_replay(
        replay, outcomes, primary_horizon=primary_horizon,
    )
