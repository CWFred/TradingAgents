"""RobinhoodBroker — Broker impl backed by the Robinhood MCP.

Depends only on the RobinhoodMCPClient protocol so tests inject a fake
and the factory injects RealRobinhoodMCPClient.

The SPOT hard-check at the top of place_order and close_position is
defense-in-depth: DenyListRule in GuardedBroker already blocks SPOT,
but if the guarded layer is ever misconfigured or bypassed, this if
is a second gate that no config or rule change can remove.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from ops.broker.base import (
    Broker, BrokerError, NoSuchPosition, OrderRejected,
)
from ops.broker.mcp_client import (
    MCPOrderAck, MCPUnavailable, RobinhoodMCPClient,
)
from ops.broker.types import Fill, Order, OrderType, Position, Side
from ops.journal import Journal


_SPOT_SYMBOLS = {"SPOT"}


class RobinhoodBroker(Broker):
    def __init__(self, *, client: RobinhoodMCPClient, journal: Journal):
        self._client = client
        self._journal = journal

    def get_cash(self) -> Decimal:
        try:
            return self._client.get_account().cash
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc

    def get_equity(self) -> Decimal:
        try:
            return self._client.get_account().equity
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc

    def get_positions(self) -> list[Position]:
        try:
            mcp_positions = self._client.get_positions()
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc
        return [
            Position(
                symbol=p.symbol, quantity=p.quantity,
                avg_entry_price=p.avg_price, stop_loss_price=None,
            )
            for p in mcp_positions
        ]

    def get_quote(self, symbol: str) -> Decimal:
        try:
            return self._client.get_quote(symbol)
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc

    def place_order(self, order: Order) -> Fill:
        self._enforce_spot_hard_check(order.symbol)
        self._journal.record_order(
            client_order_id=order.client_order_id, symbol=order.symbol,
            side=order.side.value, notional_dollars=order.notional_dollars,
            stop_loss_price=order.stop_loss_price,
        )
        try:
            ack = self._client.place_equity_order(
                symbol=order.symbol, side=order.side,
                notional=order.notional_dollars, quantity=None,
                order_type=order.order_type, limit_price=order.limit_price,
                client_order_id=order.client_order_id,
            )
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc
        return self._ack_to_fill(order, ack)

    def close_position(self, symbol: str, *, client_order_id: str | None = None) -> Fill:
        self._enforce_spot_hard_check(symbol)
        try:
            positions = self._client.get_positions()
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc
        existing = next((p for p in positions if p.symbol == symbol), None)
        if existing is None:
            raise NoSuchPosition(f"no position in {symbol}")
        client_order_id = client_order_id or f"close-{symbol}-{uuid.uuid4().hex[:8]}"
        try:
            quote = self._client.get_quote(symbol)
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc
        notional = existing.quantity * quote
        self._journal.record_order(
            client_order_id=client_order_id, symbol=symbol, side=Side.SELL.value,
            notional_dollars=notional, stop_loss_price=None,
        )
        try:
            ack = self._client.place_equity_order(
                symbol=symbol, side=Side.SELL,
                notional=None, quantity=existing.quantity,
                order_type=OrderType.MARKET, limit_price=None,
                client_order_id=client_order_id,
            )
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc
        return self._ack_to_fill_close(symbol, requested_qty=existing.quantity, ack=ack)

    def _enforce_spot_hard_check(self, symbol: str) -> None:
        if symbol.upper() in _SPOT_SYMBOLS:
            raise OrderRejected("SpotDenyList", "SPOT is contractually restricted")

    def _ack_to_fill(self, order: Order, ack: MCPOrderAck) -> Fill:
        # Fill quantity from ack; fall back to 0 if the ack doesn't report one.
        qty = ack.quantity if ack.quantity is not None else Decimal("0")
        price = ack.fill_price if ack.fill_price is not None else Decimal("0")
        fill = Fill(
            order_id=ack.order_id, client_order_id=ack.client_order_id,
            symbol=order.symbol, side=order.side, quantity=qty, price=price,
            filled_at=datetime.now(timezone.utc),
        )
        self._journal.record_fill(
            order_id=fill.order_id, client_order_id=fill.client_order_id,
            symbol=fill.symbol, side=fill.side.value,
            quantity=fill.quantity, price=fill.price, filled_at=fill.filled_at,
        )
        return fill

    def _ack_to_fill_close(
        self, symbol: str, *, requested_qty: Decimal, ack: MCPOrderAck,
    ) -> Fill:
        # Prefer the ack's actual filled quantity when it reports one; fall
        # back to the pre-trade snapshot only if the ack lacks a quantity.
        # A real market SELL can partially fill (halted stock, illiquid,
        # etc.), and journaling the requested quantity in that case would
        # misrecord the position.
        qty = ack.quantity if ack.quantity is not None else requested_qty
        price = ack.fill_price if ack.fill_price is not None else Decimal("0")
        fill = Fill(
            order_id=ack.order_id, client_order_id=ack.client_order_id,
            symbol=symbol, side=Side.SELL, quantity=qty, price=price,
            filled_at=datetime.now(timezone.utc),
        )
        self._journal.record_fill(
            order_id=fill.order_id, client_order_id=fill.client_order_id,
            symbol=fill.symbol, side=fill.side.value,
            quantity=fill.quantity, price=fill.price, filled_at=fill.filled_at,
        )
        return fill
