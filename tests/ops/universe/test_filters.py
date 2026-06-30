from decimal import Decimal

from ops.universe.filters import apply_deny_list, apply_liquidity_filter


def test_deny_list_strips_excluded_symbols():
    result = apply_deny_list(["AAPL", "SPOT", "MSFT", "TQQQ"], frozenset({"SPOT", "TQQQ"}))
    assert result == ["AAPL", "MSFT"]


def test_liquidity_filter_keeps_above_both_floors():
    metrics = {
        "AAPL": (Decimal("200"), Decimal("60000000")),  # passes
        "PENNY": (Decimal("2"),  Decimal("60000000")),  # price floor
        "ILLIQ": (Decimal("200"), Decimal("10000000")),  # adv floor
        "ZZZZ": None,                                    # no data
    }
    result = apply_liquidity_filter(
        ["AAPL", "PENNY", "ILLIQ", "ZZZZ"],
        min_adv=Decimal("50000000"),
        min_price=Decimal("5"),
        fetch_metrics=lambda s: metrics[s],
    )
    syms = [r[0] for r in result]
    assert syms == ["AAPL"]
