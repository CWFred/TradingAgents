from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ops.backtest.models import BacktestCase, DecisionAction, PriceBar
from ops.backtest.prices import PriceSeriesStatus
from ops.backtest.replay import InitialDecision, replay_case, settings_fingerprint

pytestmark = pytest.mark.unit


def _case():
    return BacktestCase.create(
        sleeve="research", symbol="AAA", asof=date(2025, 6, 6),
        created_at=datetime(2025, 6, 6, tzinfo=timezone.utc),
    )


def _bar(day, open_, close):
    low, high = min(open_, close), max(open_, close)
    return PriceBar(
        symbol="AAA", session=day, open=Decimal(open_), high=Decimal(high),
        low=Decimal(low), close=Decimal(close), adjusted_open=Decimal(open_),
        adjusted_high=Decimal(high), adjusted_low=Decimal(low),
        adjusted_close=Decimal(close),
    )


def test_buy_fills_at_next_session_open_not_asof():
    bars = [
        _bar(date(2025, 6, 6), "9", "10"),
        _bar(date(2025, 6, 9), "11", "12"),
    ]
    result = replay_case(
        run_id="run", case=_case(),
        initial=InitialDecision(DecisionAction.BUY, "memo buy"), bars=bars,
        notional=Decimal("110"), settings={"exit": "v1"},
    )
    assert result.entry_session == date(2025, 6, 9)
    assert result.executions[0].price == Decimal("11")
    assert result.executions[0].quantity == Decimal("10")


def test_exit_signal_fills_at_following_open():
    bars = [
        _bar(date(2025, 6, 9), "10", "11"),
        _bar(date(2025, 6, 10), "12", "15"),
        _bar(date(2025, 6, 11), "14", "13"),
    ]
    result = replay_case(
        run_id="run", case=_case(),
        initial=InitialDecision(DecisionAction.BUY, "memo buy"), bars=bars,
        notional=Decimal("100"), settings={},
        exit_policy=lambda bar, entry: "target" if bar.adjusted_close >= 15 else None,
    )
    assert result.decisions[2].action == DecisionAction.SELL
    assert result.decisions[2].observed_session == date(2025, 6, 10)
    assert result.executions[-1].session == date(2025, 6, 11)
    assert result.executions[-1].price == Decimal("14")


def test_pass_is_a_case_decision_without_execution():
    result = replay_case(
        run_id="run", case=_case(),
        initial=InitialDecision(DecisionAction.PASS, "memo pass"), bars=[],
        notional=Decimal("100"), settings={},
    )
    assert len(result.decisions) == 1
    assert result.executions == ()
    assert result.status == "complete"


def test_missing_next_bar_is_explicitly_unpriceable():
    result = replay_case(
        run_id="run", case=_case(),
        initial=InitialDecision(DecisionAction.BUY, "memo buy"), bars=[],
        notional=Decimal("100"), settings={},
    )
    assert result.status == "unpriceable"


def test_later_bar_does_not_hide_missing_exact_entry_session():
    result = replay_case(
        run_id="run", case=_case(),
        initial=InitialDecision(DecisionAction.BUY, "memo buy"),
        bars=[_bar(date(2025, 6, 10), "10", "10")],
        notional=Decimal("100"), settings={},
    )
    assert result.status == "unpriceable"
    assert result.executions == ()


def test_settings_hash_uses_canonical_resolved_values():
    assert settings_fingerprint({"a": 1, "b": 2}) == settings_fingerprint({"b": 2, "a": 1})
    assert settings_fingerprint({"a": 1}) != settings_fingerprint({"a": 2})


def test_max_drawdown_is_peak_to_trough_not_only_entry_to_trough():
    result = replay_case(
        run_id="run", case=_case(),
        initial=InitialDecision(DecisionAction.BUY, "memo buy"),
        bars=[
            _bar(date(2025, 6, 9), "100", "120"),
            _bar(date(2025, 6, 10), "120", "90"),
        ],
        notional=Decimal("100"), settings={},
    )
    assert result.max_drawdown == Decimal("-0.25")


def test_terminal_series_is_kept_as_a_complete_loss_after_entry():
    result = replay_case(
        run_id="run", case=_case(),
        initial=InitialDecision(DecisionAction.BUY, "memo buy"),
        bars=[_bar(date(2025, 6, 9), "100", "90")],
        notional=Decimal("100"), settings={},
        price_status=PriceSeriesStatus.TERMINAL,
        price_state_reason="delisted after bankruptcy",
    )
    assert result.status == "complete"
    assert result.actual_return == Decimal("-1")
    assert result.max_drawdown == Decimal("-1")
    assert result.exit_reason == "terminal: delisted after bankruptcy"
    assert len(result.executions) == 1


@pytest.mark.parametrize(
    ("series_status", "expected"),
    [
        (PriceSeriesStatus.PENDING, "pending"),
        (PriceSeriesStatus.STALE, "stale"),
        (PriceSeriesStatus.UNPRICEABLE, "unpriceable"),
    ],
)
def test_missing_entry_preserves_series_state(series_status, expected):
    result = replay_case(
        run_id="run", case=_case(),
        initial=InitialDecision(DecisionAction.BUY, "memo buy"), bars=[],
        notional=Decimal("100"), settings={}, price_status=series_status,
    )
    assert result.status == expected
    assert result.decisions[0].metadata["price_status"] == series_status.value
