"""In-memory paper broker. Records every order and fill to the journal."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from ops.broker.base import Broker, InsufficientFunds, NoSuchPosition
from ops.broker.types import Fill, Order, Position, Side
from ops.journal import Journal

QuoteSource = Callable[[str], Decimal]

_EPSILON = Decimal("0.0000001")


class PaperBroker(Broker):
    def __init__(self, *, journal: Journal, quote_source: QuoteSource, starting_cash: Decimal):
        self._journal = journal
        self._quote = quote_source
        self._cash = Decimal(starting_cash)
        self._positions: dict[str, Position] = {}

    @classmethod
    def from_journal(
        cls, *, journal: Journal, quote_source: QuoteSource, starting_cash: Decimal,
    ) -> "PaperBroker":
        """Rebuild in-memory state by replaying fills from the journal.

        stop_loss_price is not persisted on fills; recovered positions come
        back with stop_loss_price=None (guardian falls back to config).

        Cash is reconstructed from each fill's matching order's
        notional_dollars (looked up by client_order_id) rather than
        qty * price, because qty is derived from notional_dollars / price
        at fill time and multiplying back out reintroduces Decimal division
        rounding error. Every fill (including close_position closes) now
        has a matching order row, so the qty*price path below is a
        defensive fallback only; it should not be hit in practice, and it
        journals a journal_replay_fallback event when it is, so an ops
        engineer can see the journal is missing an order row for a fill.

        This is also the seam where we detect recovered positions that
        carry no per-position stop_loss_price (see I4): unlike
        RobinhoodBroker.get_positions(), which is called on every routine
        poll and would spam this event, from_journal only runs once at
        process start, so it is the natural place to emit a one-shot
        recovery warning."""
        broker = cls(journal=journal, quote_source=quote_source, starting_cash=starting_cash)
        orders_by_id = {o["client_order_id"]: o for o in journal.read_orders()}
        for f in journal.read_fills():
            symbol = f["symbol"]
            side = f["side"]
            qty = f["quantity"]
            price = f["price"]
            order = orders_by_id.get(f["client_order_id"])
            if order is not None:
                notional = order["notional_dollars"]
            else:
                notional = qty * price
                journal.record_event(
                    "journal_replay_fallback",
                    {
                        "client_order_id": f["client_order_id"],
                        "symbol": symbol,
                        "side": side,
                        "reason": "no matching order row; falling back to qty*price",
                    },
                )
            if side == Side.BUY.value:
                cost = notional
                broker._cash -= cost
                existing = broker._positions.get(symbol)
                if existing is None:
                    broker._positions[symbol] = Position(
                        symbol=symbol, quantity=qty,
                        avg_entry_price=price, stop_loss_price=None,
                    )
                else:
                    total_qty = existing.quantity + qty
                    avg = (
                        (existing.avg_entry_price * existing.quantity) + (price * qty)
                    ) / total_qty
                    broker._positions[symbol] = Position(
                        symbol=symbol, quantity=total_qty,
                        avg_entry_price=avg, stop_loss_price=None,
                    )
            elif side == Side.SELL.value:
                existing = broker._positions.get(symbol)
                if existing is None:
                    # Journal is inconsistent — SELL replayed without a prior BUY.
                    # Log and skip. In production this triggers reconciliation.
                    continue
                proceeds = notional
                broker._cash += proceeds
                remaining = existing.quantity - qty
                if remaining > _EPSILON:
                    broker._positions[symbol] = Position(
                        symbol=symbol, quantity=remaining,
                        avg_entry_price=existing.avg_entry_price,
                        stop_loss_price=None,
                    )
                else:
                    del broker._positions[symbol]
        unstopped = sorted(
            symbol for symbol, pos in broker._positions.items()
            if pos.stop_loss_price is None
        )
        if unstopped:
            journal.record_event(
                "positions_recovered_without_stops",
                {"symbols": unstopped, "count": len(unstopped)},
            )
        return broker

    def get_cash(self) -> Decimal:
        return self._cash

    def get_quote(self, symbol: str) -> Decimal:
        return self._quote(symbol)

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_equity(self) -> Decimal:
        total = self._cash
        for pos in self._positions.values():
            total += pos.market_value(self._quote(pos.symbol))
        return total

    def place_order(self, order: Order) -> Fill:
        self._journal.record_order(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side.value,
            notional_dollars=order.notional_dollars,
            stop_loss_price=order.stop_loss_price,
        )
        price = self._quote(order.symbol)
        if order.side == Side.BUY:
            return self._fill_buy(order, price)
        return self._fill_sell(order, price)

    def _fill_buy(self, order: Order, price: Decimal) -> Fill:
        cost = order.notional_dollars
        if cost > self._cash:
            raise InsufficientFunds(f"need ${cost}, have ${self._cash}")
        qty = cost / price
        self._cash -= cost
        existing = self._positions.get(order.symbol)
        if existing is None:
            new_pos = Position(
                symbol=order.symbol,
                quantity=qty,
                avg_entry_price=price,
                stop_loss_price=order.stop_loss_price,
            )
        else:
            total_qty = existing.quantity + qty
            avg = (
                (existing.avg_entry_price * existing.quantity) + (price * qty)
            ) / total_qty
            new_pos = Position(
                symbol=order.symbol,
                quantity=total_qty,
                avg_entry_price=avg,
                stop_loss_price=order.stop_loss_price or existing.stop_loss_price,
            )
        self._positions[order.symbol] = new_pos
        return self._make_fill(order, qty, price)

    def close_position(self, symbol: str, *, client_order_id: str | None = None) -> Fill:
        existing = self._positions.get(symbol)
        if existing is None:
            raise NoSuchPosition(f"no position in {symbol}")
        price = self._quote(symbol)
        qty = existing.quantity
        proceeds = qty * price
        order_id = client_order_id or f"close-{symbol}-{uuid4().hex[:8]}"
        self._journal.record_order(
            client_order_id=order_id, symbol=symbol, side=Side.SELL.value,
            notional_dollars=proceeds, stop_loss_price=None,
        )
        self._cash += proceeds
        del self._positions[symbol]
        fill = Fill(
            order_id=str(uuid4()),
            client_order_id=order_id,
            symbol=symbol,
            side=Side.SELL,
            quantity=qty,
            price=price,
            filled_at=datetime.now(timezone.utc),
        )
        self._journal.record_fill(
            order_id=fill.order_id,
            client_order_id=fill.client_order_id,
            symbol=fill.symbol,
            side=fill.side.value,
            quantity=fill.quantity,
            price=fill.price,
            filled_at=fill.filled_at,
        )
        return fill

    def _fill_sell(self, order: Order, price: Decimal) -> Fill:
        existing = self._positions.get(order.symbol)
        if existing is None:
            raise NoSuchPosition(f"no position in {order.symbol}")
        qty_to_sell = order.notional_dollars / price
        if qty_to_sell > existing.quantity + _EPSILON:
            raise NoSuchPosition(
                f"sell qty {qty_to_sell} exceeds position {existing.quantity}"
            )
        proceeds = qty_to_sell * price
        self._cash += proceeds
        remaining = existing.quantity - qty_to_sell
        if remaining > _EPSILON:
            self._positions[order.symbol] = Position(
                symbol=existing.symbol,
                quantity=remaining,
                avg_entry_price=existing.avg_entry_price,
                stop_loss_price=existing.stop_loss_price,
            )
        else:
            del self._positions[order.symbol]
        return self._make_fill(order, qty_to_sell, price)

    def _make_fill(self, order: Order, qty: Decimal, price: Decimal) -> Fill:
        fill = Fill(
            order_id=str(uuid4()),
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=qty,
            price=price,
            filled_at=datetime.now(timezone.utc),
        )
        self._journal.record_fill(
            order_id=fill.order_id,
            client_order_id=fill.client_order_id,
            symbol=fill.symbol,
            side=fill.side.value,
            quantity=fill.quantity,
            price=fill.price,
            filled_at=fill.filled_at,
        )
        return fill
