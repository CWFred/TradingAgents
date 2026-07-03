from decimal import Decimal
import pytest
from ops.broker.types import Order, Side, OrderType, Position

def test_order_is_frozen():
    o = Order(
        client_order_id="cid-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    with pytest.raises(Exception):
        o.symbol = "MSFT"


def test_order_stop_pct_defaults_to_none():
    o = Order(
        client_order_id="cid-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
    )
    assert o.stop_pct is None


def test_order_rejects_zero_stop_pct():
    with pytest.raises(ValueError, match="stop_pct"):
        Order(
            client_order_id="cid-1", symbol="AAPL", side=Side.BUY,
            notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
            stop_pct=Decimal("0"),
        )


def test_order_rejects_positive_stop_pct():
    with pytest.raises(ValueError, match="stop_pct"):
        Order(
            client_order_id="cid-1", symbol="AAPL", side=Side.BUY,
            notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
            stop_pct=Decimal("0.08"),
        )


def test_order_accepts_negative_stop_pct():
    o = Order(
        client_order_id="cid-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    assert o.stop_pct == Decimal("-0.08")

def test_order_buy_requires_positive_notional():
    with pytest.raises(ValueError):
        Order(
            client_order_id="x", symbol="AAPL", side=Side.BUY,
            notional_dollars=Decimal("0"), order_type=OrderType.MARKET,
        )

def test_sell_order_requires_positive_notional():
    with pytest.raises(ValueError, match="notional_dollars must be positive"):
        Order(
            client_order_id="s-1",
            symbol="AAPL",
            side=Side.SELL,
            notional_dollars=Decimal("0"),
            order_type=OrderType.MARKET,
        )

def test_buy_negative_notional_raises():
    with pytest.raises(ValueError, match="notional_dollars must be positive"):
        Order(
            client_order_id="x", symbol="AAPL", side=Side.BUY,
            notional_dollars=Decimal("-5"), order_type=OrderType.MARKET,
        )

def test_sell_negative_notional_raises():
    with pytest.raises(ValueError, match="notional_dollars must be positive"):
        Order(
            client_order_id="x", symbol="AAPL", side=Side.SELL,
            notional_dollars=Decimal("-5"), order_type=OrderType.MARKET,
        )

def test_valid_buy_order_still_constructs():
    o = Order(
        client_order_id="c1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    assert o.notional_dollars == Decimal("25")

def test_valid_sell_order_still_constructs():
    o = Order(
        client_order_id="c1", symbol="AAPL", side=Side.SELL,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
    )
    assert o.notional_dollars == Decimal("25")

def test_position_value():
    p = Position(
        symbol="AAPL", quantity=Decimal("0.5"),
        avg_entry_price=Decimal("200"), stop_loss_price=Decimal("184"),
    )
    assert p.market_value(Decimal("210")) == Decimal("105.0")
    assert p.unrealized_pct(Decimal("210")) == Decimal("0.05")
