"""Deterministic in-memory MCP client for unit tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import uuid4

from ops.broker.mcp_client import (
    AccountInfo, MCPOrderAck, MCPPosition, MCPUnavailable, RobinhoodMCPClient,
)
from ops.broker.types import OrderType, Side


class FakeMCPClient:
    def __init__(self, *, cash: Decimal = Decimal("1000")):
        self._cash = cash
        self._positions: dict[str, MCPPosition] = {}
        self._quotes: dict[str, Decimal] = {}
        self.placed: list[MCPOrderAck] = []
        self.cancelled: list[str] = []
        self._raise_on_next_call: Exception | None = None

    # --- helpers for tests ---
    def set_quote(self, symbol: str, price: Decimal) -> None:
        self._quotes[symbol] = price

    def seed_position(self, symbol: str, quantity: Decimal, avg_price: Decimal) -> None:
        self._positions[symbol] = MCPPosition(symbol=symbol, quantity=quantity, avg_price=avg_price)

    def fail_next(self, exc: Exception) -> None:
        self._raise_on_next_call = exc

    # --- protocol ---
    def _check_fail(self) -> None:
        if self._raise_on_next_call is not None:
            exc = self._raise_on_next_call
            self._raise_on_next_call = None
            raise exc

    def get_account(self) -> AccountInfo:
        self._check_fail()
        equity = self._cash + sum(
            (p.quantity * self._quotes.get(p.symbol, p.avg_price) for p in self._positions.values()),
            start=Decimal("0"),
        )
        return AccountInfo(cash=self._cash, equity=equity, buying_power=self._cash)

    def get_positions(self) -> list[MCPPosition]:
        self._check_fail()
        return list(self._positions.values())

    def get_quote(self, symbol: str) -> Decimal:
        self._check_fail()
        if symbol not in self._quotes:
            raise MCPUnavailable(f"no quote for {symbol}")
        return self._quotes[symbol]

    def place_equity_order(
        self, *, symbol: str, side: Side,
        notional: Decimal | None, quantity: Decimal | None,
        order_type: OrderType, limit_price: Decimal | None,
        client_order_id: str,
    ) -> MCPOrderAck:
        self._check_fail()
        price = self._quotes.get(symbol, Decimal("1"))
        if side == Side.BUY:
            assert notional is not None
            qty = notional / price
            self._cash -= notional
            existing = self._positions.get(symbol)
            if existing is None:
                self._positions[symbol] = MCPPosition(symbol=symbol, quantity=qty, avg_price=price)
            else:
                new_qty = existing.quantity + qty
                new_avg = (existing.avg_price * existing.quantity + price * qty) / new_qty
                self._positions[symbol] = MCPPosition(symbol=symbol, quantity=new_qty, avg_price=new_avg)
            ack_qty = qty
        else:  # SELL
            existing = self._positions.get(symbol)
            if existing is None:
                raise ValueError(f"SELL with no position in {symbol}")
            if quantity is not None:
                ack_qty = quantity
            else:
                assert notional is not None
                ack_qty = notional / price
            self._cash += ack_qty * price
            remaining = existing.quantity - ack_qty
            if remaining > Decimal("1e-9"):
                self._positions[symbol] = MCPPosition(symbol=symbol, quantity=remaining, avg_price=existing.avg_price)
            else:
                del self._positions[symbol]
        ack = MCPOrderAck(
            order_id=str(uuid4()), client_order_id=client_order_id,
            symbol=symbol, side=side, quantity=ack_qty,
            notional=notional, status="filled", fill_price=price,
        )
        self.placed.append(ack)
        return ack

    def cancel_equity_order(self, order_id: str) -> None:
        self._check_fail()
        self.cancelled.append(order_id)
