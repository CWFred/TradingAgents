from decimal import Decimal

import pytest

from ops.broker.types import Order, OrderType, Side


def _order(side, **kw):
    return Order(
        client_order_id="t-1", symbol="XYZ", side=side,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET, **kw,
    )


def test_short_and_cover_sides_exist():
    assert Side.SHORT.value == "SHORT"
    assert Side.COVER.value == "COVER"


def test_short_order_without_stop_is_valid():
    assert _order(Side.SHORT).side is Side.SHORT


def test_stop_pct_rejected_on_short_and_cover():
    for side in (Side.SHORT, Side.COVER):
        with pytest.raises(ValueError, match="SHORT/COVER"):
            _order(side, stop_pct=Decimal("-0.08"))


def test_buy_stop_pct_unchanged():
    assert _order(Side.BUY, stop_pct=Decimal("-0.08")).stop_pct == Decimal("-0.08")
    with pytest.raises(ValueError):
        _order(Side.BUY, stop_pct=Decimal("0.08"))
