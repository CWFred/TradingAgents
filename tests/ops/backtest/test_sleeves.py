from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ops.backtest.models import DecisionAction, PriceBar
from ops.backtest.sleeves import (
    decide_research_memo,
    make_research_exit_policy,
    resolved_settings,
    size_research_case,
)

pytestmark = pytest.mark.unit


def _memo(
    *, recommendation="buy", status="open", tier="high", target=120,
    falsifiers=(),
):
    return SimpleNamespace(
        recommendation=recommendation, status=status,
        conviction_tier=tier, price_target_high=target, falsifiers=falsifiers,
    )


def _bar(close, day=date(2025, 6, 2)):
    value = Decimal(str(close))
    return PriceBar(
        symbol="AAA", session=day, open=value, high=value,
        low=value, close=value, adjusted_open=value, adjusted_high=value,
        adjusted_low=value, adjusted_close=value,
    )


def test_frozen_buy_and_pass_are_mechanical():
    buy = decide_research_memo(memo=_memo(), recommendation="buy", memo_key="m")
    passed = decide_research_memo(memo=_memo(), recommendation="pass", memo_key="m")
    assert (buy.action, buy.conviction) == (DecisionAction.BUY, "high")
    assert passed.action == DecisionAction.PASS


def test_rejected_generation_remains_a_pass_case():
    decision = decide_research_memo(
        memo=None, recommendation=None, generation_status="rejected",
        guardrail_reasons=("no falsifier",),
    )
    assert decision.action == DecisionAction.PASS
    assert "no falsifier" in decision.reason


def test_research_sizing_reuses_live_tier_rules():
    result = size_research_case(
        tier="high", fixed_equity=Decimal("10000"), symbol="AAA",
    )
    assert result.rejected is None
    assert result.notional == Decimal("600.00")


def test_research_exit_policy_uses_target_and_falsifier():
    target = make_research_exit_policy(memo=_memo(target=120))
    falsifier = make_research_exit_policy(
        memo=_memo(target=999), falsifier_tripped=lambda bar: True,
    )
    assert target(_bar(120), Decimal("100")) == "target hit"
    assert falsifier(_bar(100), Decimal("100")) == "falsifier tripped"


def test_price_falsifier_honors_consecutive_periods():
    falsifier = SimpleNamespace(
        metric="drawdown_from_cost_pct", operator=">=", threshold=20,
        consecutive_periods=2,
    )
    policy = make_research_exit_policy(
        memo=_memo(target=999, falsifiers=(falsifier,)),
    )
    assert policy(_bar(79), Decimal("100")) is None
    assert policy(_bar(80, date(2025, 6, 3)), Decimal("100")) == "falsifier tripped"


def test_policy_surfaces_unevaluable_fundamental_falsifier():
    falsifier = SimpleNamespace(
        description="margin stays above 30%", check_type="fundamental",
        metric="gross_margin_pct", operator="<", threshold=30,
        consecutive_periods=1,
    )
    policy = make_research_exit_policy(
        memo=_memo(target=999, falsifiers=(falsifier,)),
    )
    result = policy.evaluate(_bar(100), Decimal("100"))
    assert result.reason is None
    assert len(result.observations) == 1
    assert result.observations[0].name == "margin stays above 30%"
    assert result.observations[0].status == "unevaluable"


def test_settings_reject_unknown_overrides():
    with pytest.raises(ValueError, match="unknown settings"):
        resolved_settings({"target": 1}, {"mystery": 2})
