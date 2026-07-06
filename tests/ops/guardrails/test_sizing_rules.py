from decimal import Decimal
from unittest.mock import MagicMock
import pytest
from ops.broker.types import Order, Side, OrderType, Position
from ops.config import OpsConfig
from ops.guardrails.base import RuleContext
from ops.guardrails.sizing_rules import (
    PerPositionCapRule, PerTradeDollarFloorRule,
    MaxOpenPositionsRule, CashReserveRule, LiveMaxPositionRule,
)


def _ctx(notional: str, positions: list[Position], equity: str, cash: str,
         cfg: OpsConfig | None = None) -> RuleContext:
    o = Order(
        client_order_id="c", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal(notional), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    b = MagicMock()
    b.get_equity.return_value = Decimal(equity)
    b.get_cash.return_value = Decimal(cash)
    b.get_positions.return_value = positions
    return RuleContext(order=o, broker=b, config=cfg or OpsConfig())


def test_per_position_cap_allows_under_threshold():
    r = PerPositionCapRule().check(_ctx("25", [], "250", "250"))
    assert r.allowed is True


def test_per_position_cap_blocks_over_threshold():
    r = PerPositionCapRule().check(_ctx("30.01", [], "250", "250"))
    assert r.allowed is False


def test_per_trade_floor_blocks_tiny_orders():
    r = PerTradeDollarFloorRule().check(_ctx("4.99", [], "250", "250"))
    assert r.allowed is False


def test_per_trade_floor_allows_at_threshold():
    r = PerTradeDollarFloorRule().check(_ctx("5", [], "250", "250"))
    assert r.allowed is True


def _pos(sym: str) -> Position:
    return Position(symbol=sym, quantity=Decimal("0.1"),
                    avg_entry_price=Decimal("100"), stop_loss_price=Decimal("92"))


def test_max_open_positions_blocks_when_full():
    positions = [_pos(s) for s in ("AAPL", "MSFT", "NVDA", "GOOG", "AMZN", "META", "NFLX")]
    o = Order(
        client_order_id="c", symbol="TSLA", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    b = MagicMock()
    b.get_positions.return_value = positions
    ctx = RuleContext(order=o, broker=b, config=OpsConfig())
    assert MaxOpenPositionsRule().check(ctx).allowed is False


def test_max_open_positions_allows_add_to_existing():
    positions = [_pos(s) for s in ("AAPL", "MSFT", "NVDA", "GOOG", "AMZN")]
    o = Order(
        client_order_id="c", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    b = MagicMock()
    b.get_positions.return_value = positions
    ctx = RuleContext(order=o, broker=b, config=OpsConfig())
    assert MaxOpenPositionsRule().check(ctx).allowed is True


def test_max_open_positions_allows_under_cap():
    positions = [_pos(s) for s in ("AAPL", "MSFT")]
    o = Order(
        client_order_id="c", symbol="NVDA", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    b = MagicMock()
    b.get_positions.return_value = positions
    ctx = RuleContext(order=o, broker=b, config=OpsConfig())
    assert MaxOpenPositionsRule().check(ctx).allowed is True


def test_cash_reserve_blocks_if_buy_would_breach_16pct_floor():
    # Equity $250; 16% reserve = $40 floor. Cash $60. $25 BUY leaves $35 — below floor.
    r = CashReserveRule().check(_ctx("25", [], "250", "60"))
    assert r.allowed is False


def test_cash_reserve_allows_if_post_trade_cash_above_floor():
    r = CashReserveRule().check(_ctx("25", [], "250", "100"))
    assert r.allowed is True


def test_cash_reserve_does_not_constrain_sells():
    sell = Order(
        client_order_id="c", symbol="AAPL", side=Side.SELL,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
    )
    b = MagicMock()
    b.get_equity.return_value = Decimal("250")
    b.get_cash.return_value = Decimal("10")
    b.get_positions.return_value = []
    ctx = RuleContext(order=sell, broker=b, config=OpsConfig())
    assert CashReserveRule().check(ctx).allowed is True


def test_live_gate_inert_in_paper():
    rule = LiveMaxPositionRule(live_fill_count=lambda: 0)
    r = rule.check(_ctx("50", [], "250", "250", cfg=OpsConfig(broker_mode="paper")))
    assert r.allowed is True


def test_live_gate_blocks_big_buy_during_window():
    rule = LiveMaxPositionRule(live_fill_count=lambda: 0)
    r = rule.check(_ctx("15", [], "250", "250", cfg=OpsConfig(broker_mode="robinhood")))
    assert r.allowed is False


def test_live_gate_allows_small_buy_during_window():
    rule = LiveMaxPositionRule(live_fill_count=lambda: 0)
    r = rule.check(_ctx("9", [], "250", "250", cfg=OpsConfig(broker_mode="robinhood")))
    assert r.allowed is True


def test_live_gate_boundary_20th_still_capped_21st_free():
    cfg = OpsConfig(broker_mode="robinhood")   # gate=20, cap=$10
    at_20 = LiveMaxPositionRule(live_fill_count=lambda: 19)  # 20th fill in flight
    r20 = at_20.check(_ctx("15", [], "250", "250", cfg=cfg))
    assert r20.allowed is False                # still within first 20
    at_21 = LiveMaxPositionRule(live_fill_count=lambda: 20)  # 20 fills done, gate lifted
    r21 = at_21.check(_ctx("15", [], "250", "250", cfg=cfg))
    assert r21.allowed is True                 # normal cap now applies (handled elsewhere)


def test_live_gate_allows_sell():
    rule = LiveMaxPositionRule(live_fill_count=lambda: 0)
    o = _ctx("15", [], "250", "250", cfg=OpsConfig(broker_mode="robinhood"))
    sell = RuleContext(
        order=Order(client_order_id="c", symbol="AAPL", side=Side.SELL,
                    notional_dollars=Decimal("15"), order_type=OrderType.MARKET),
        broker=o.broker, config=o.config,
    )
    assert rule.check(sell).allowed is True
