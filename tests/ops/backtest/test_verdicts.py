from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ops.backtest.models import (
    BacktestCase,
    DecisionAction,
    OutcomeLabel,
    OutcomeState,
    PriceBar,
)
from ops.backtest.prices import PriceSeriesStatus
from ops.backtest.replay import InitialDecision, replay_case
from ops.backtest.verdicts import (
    action_utility,
    case_result_from_replay,
    compute_horizon_outcomes,
    utility_label,
)

pytestmark = pytest.mark.unit


class DailyCalendar:
    def is_trading_day(self, day):
        return True


CALENDAR = DailyCalendar()
ASOF = date(2025, 6, 6)
ENTRY = date(2025, 6, 7)


def _bar(symbol, day, *, open_="100", close="100"):
    open_d, close_d = Decimal(open_), Decimal(close)
    high, low = max(open_d, close_d), min(open_d, close_d)
    return PriceBar(
        symbol=symbol, session=day,
        open=open_d, high=high, low=low, close=close_d,
        adjusted_open=open_d, adjusted_high=high,
        adjusted_low=low, adjusted_close=close_d,
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _outcome(
    stock_close,
    *,
    action=DecisionAction.BUY,
    horizon=5,
    adjudication=date(2025, 7, 1),
    include_entry=True,
    include_target=True,
    include_benchmark_target=True,
):
    target = ENTRY + timedelta(days=horizon)
    stock = []
    benchmark = []
    if include_entry:
        stock.append(_bar("AAA", ENTRY))
        benchmark.append(_bar("SPY", ENTRY))
    if include_target:
        stock.append(_bar("AAA", target, close=stock_close))
    if include_benchmark_target:
        benchmark.append(_bar("SPY", target))
    return compute_horizon_outcomes(
        run_id="run", case_id="case", action=action, decision_asof=ASOF,
        stock_bars=stock, benchmark_bars=benchmark,
        adjudication_date=adjudication, horizons=(horizon,),
        wash_band=Decimal("0.03"), calendar=CALENDAR,
    )[0]


def test_exact_wash_boundaries_are_decisive():
    assert _outcome("103").label == OutcomeLabel.WIN
    assert _outcome("97").label == OutcomeLabel.LOSS
    assert _outcome("102.999").label == OutcomeLabel.WASH
    assert _outcome("97.001").label == OutcomeLabel.WASH
    assert utility_label(Decimal("0"), Decimal("0")) == OutcomeLabel.WASH


def test_pass_and_sell_reverse_excess_polarity():
    assert _outcome("110", action=DecisionAction.PASS).label == OutcomeLabel.LOSS
    assert _outcome("90", action=DecisionAction.PASS).label == OutcomeLabel.WIN
    assert _outcome("90", action=DecisionAction.SELL).label == OutcomeLabel.WIN
    assert action_utility(DecisionAction.BUY, Decimal(".1")) == Decimal(".1")
    assert action_utility(DecisionAction.PASS, Decimal(".1")) == Decimal("-.1")


def test_horizon_uses_exact_session_count_and_next_open():
    result = _outcome("110", horizon=21)
    assert result.entry_session == ENTRY
    assert result.horizon_session == date(2025, 6, 28)
    assert result.stock_return == Decimal("0.1")
    assert result.benchmark_return == Decimal("0")
    assert result.excess_return == Decimal("0.1")
    assert result.utility == Decimal("0.1")


def test_unreached_horizon_is_pending_even_if_future_bars_are_supplied():
    result = _outcome(
        "999", horizon=21, adjudication=date(2025, 6, 20),
    )
    assert result.state == OutcomeState.PENDING
    assert result.label == OutcomeLabel.PENDING
    assert result.stock_return is None


@pytest.mark.parametrize(
    ("kwargs", "detail"),
    [
        ({"include_entry": False}, "entry bar"),
        ({"include_target": False}, "stock horizon"),
        ({"include_benchmark_target": False}, "benchmark horizon"),
    ],
)
def test_mature_missing_exact_bar_is_unpriceable(kwargs, detail):
    result = _outcome("110", **kwargs)
    assert result.state == OutcomeState.UNPRICEABLE
    assert result.label == OutcomeLabel.UNPRICEABLE
    assert detail in result.detail


def test_all_configured_horizons_are_returned_in_order():
    horizons = (5, 21, 63, 126)
    stock = [_bar("AAA", ENTRY)]
    benchmark = [_bar("SPY", ENTRY)]
    for horizon in horizons:
        target = ENTRY + timedelta(days=horizon)
        stock.append(_bar("AAA", target, close="105"))
        benchmark.append(_bar("SPY", target))
    results = compute_horizon_outcomes(
        run_id="run", case_id="case", action=DecisionAction.BUY,
        decision_asof=ASOF, stock_bars=stock, benchmark_bars=benchmark,
        adjudication_date=date(2026, 1, 1), calendar=CALENDAR,
    )
    assert [result.horizon_sessions for result in results] == list(horizons)


def test_case_result_keeps_actual_replay_return_separate_from_horizon():
    case = BacktestCase.create(
        sleeve="research", symbol="AAA", asof=ASOF,
        created_at=datetime(2025, 6, 6, tzinfo=timezone.utc),
    )
    replay = replay_case(
        run_id="run", case=case,
        initial=InitialDecision(DecisionAction.BUY, "buy"),
        bars=[_bar("AAA", ENTRY, open_="100", close="120")],
        notional=Decimal("100"), settings={},
        next_session=lambda day: day + timedelta(days=1),
    )
    primary = _outcome("105", horizon=63, adjudication=date(2025, 9, 1))
    primary = type(primary)(**{**primary.__dict__, "case_id": case.case_id})
    result = case_result_from_replay(replay, [primary], primary_horizon=63)
    assert result.actual_return == Decimal("0.2")
    assert result.primary_label == OutcomeLabel.WIN


def test_invalid_wash_band_and_duplicate_sessions_fail_loudly():
    with pytest.raises(ValueError, match="wash_band"):
        utility_label(Decimal("0"), Decimal("-0.01"))
    duplicate = [_bar("AAA", ENTRY), _bar("AAA", ENTRY)]
    with pytest.raises(ValueError, match="duplicate stock"):
        compute_horizon_outcomes(
            run_id="run", case_id="case", action=DecisionAction.BUY,
            decision_asof=ASOF, stock_bars=duplicate,
            benchmark_bars=[_bar("SPY", ENTRY)],
            adjudication_date=date(2025, 7, 1), horizons=(5,),
            calendar=CALENDAR,
        )


def test_terminal_stock_is_graded_at_zero_instead_of_excluded():
    result = compute_horizon_outcomes(
        run_id="run", case_id="case", action=DecisionAction.BUY,
        decision_asof=ASOF,
        stock_bars=[_bar("AAA", ENTRY)],
        benchmark_bars=[
            _bar("SPY", ENTRY),
            _bar("SPY", ENTRY + timedelta(days=5)),
        ],
        adjudication_date=date(2025, 7, 1), horizons=(5,),
        stock_status=PriceSeriesStatus.TERMINAL,
        stock_status_reason="delisted", stock_terminal_session=ENTRY + timedelta(days=2),
        calendar=CALENDAR,
    )[0]
    assert result.state == OutcomeState.MATURE
    assert result.label == OutcomeLabel.LOSS
    assert result.stock_return == Decimal("-1")
    assert "terminal stock marked at zero" in result.detail


@pytest.mark.parametrize(
    ("status", "state", "word"),
    [
        (PriceSeriesStatus.PENDING, OutcomeState.PENDING, "pending"),
        (PriceSeriesStatus.STALE, OutcomeState.UNPRICEABLE, "stale"),
        (PriceSeriesStatus.UNPRICEABLE, OutcomeState.UNPRICEABLE, "unpriceable"),
    ],
)
def test_missing_mature_target_preserves_price_state(status, state, word):
    result = compute_horizon_outcomes(
        run_id="run", case_id="case", action=DecisionAction.BUY,
        decision_asof=ASOF,
        stock_bars=[_bar("AAA", ENTRY)],
        benchmark_bars=[
            _bar("SPY", ENTRY),
            _bar("SPY", ENTRY + timedelta(days=5)),
        ],
        adjudication_date=date(2025, 7, 1), horizons=(5,),
        stock_status=status, calendar=CALENDAR,
    )[0]
    assert result.state == state
    assert word in result.detail
