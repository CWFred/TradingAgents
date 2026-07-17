from decimal import Decimal
from types import SimpleNamespace

import pytest

from ops.research.policy import evaluate_research_exit

pytestmark = pytest.mark.unit


def _memo(*, status="open", target="125"):
    return SimpleNamespace(status=status, price_target_high=float(target))


def test_exit_policy_holds_below_target():
    assert evaluate_research_exit(
        memo=_memo(), current_price=Decimal("124.99"), falsifier_tripped=False,
    ) is None


def test_exit_policy_target_is_inclusive():
    decision = evaluate_research_exit(
        memo=_memo(), current_price=Decimal("125"), falsifier_tripped=False,
    )
    assert (decision.rule, decision.reason) == ("target", "target hit")


@pytest.mark.parametrize(
    ("memo", "falsifier", "expected"),
    [
        (None, True, "memo_missing"),
        (_memo(status="resolved"), True, "memo_resolved"),
        (_memo(), True, "falsifier"),
    ],
)
def test_exit_precedence(memo, falsifier, expected):
    decision = evaluate_research_exit(
        memo=memo, current_price=Decimal("1000"), falsifier_tripped=falsifier,
    )
    assert decision.rule == expected


def test_missing_price_never_forces_exit():
    assert evaluate_research_exit(
        memo=_memo(), current_price=None, falsifier_tripped=False,
    ) is None


def test_resolved_memo_exit_does_not_need_a_price():
    decision = evaluate_research_exit(
        memo=_memo(status="resolved"), current_price=None,
        falsifier_tripped=False,
    )
    assert decision.rule == "memo_resolved"
