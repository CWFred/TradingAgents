"""Short-native in-memory paper broker, isolated to the short journal.

Deliberately NOT a mode of PaperBroker: retrofitting signed quantities into
the long broker risks the replay correctness of four live paper books.
Positions here carry positive quantities; the whole book is short by
construction. Equity = cash - Σ(qty × current price). Short proceeds are
credited to cash at fill; covering debits qty × price and MAY drive cash
negative (a forced cover must always succeed — the damage shows in equity,
never as a refused exit).

Paper-fidelity caveats (recorded in the spec): no borrow cost, no locate,
no squeeze modeling. Exposure discipline lives in ops/research/short_sizing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from ops.broker.base import Broker, NoSuchPosition
from ops.broker.types import Fill, Order, Position, Side

_EPSILON = Decimal("0.0000001")


class ShortPaperBroker(Broker):
    def __init__(self, *, journal, quote_source, starting_cash: Decimal):
        self._journal = journal
        self._quote = quote_source
        self._cash = Decimal(starting_cash)
        self._positions: dict[str, Position] = {}

    @classmethod
    def from_journal(
        cls, *, journal, quote_source, starting_cash: Decimal,
    ) -> ShortPaperBroker:
        """Rebuild in-memory state by replaying SHORT/COVER fills.

        Mirrors PaperBroker.from_journal's discipline: cash moves come from
        the matching order row's notional_dollars (qty*price reintroduces
        Decimal division rounding), falling back with a
        journal_replay_fallback event; a COVER with no prior SHORT is
        journaled as journal_replay_orphan_cover and skipped. Stops are not
        rehydrated — the short trade step derives its hard stop from
        avg_entry_price, never from a journaled stop.
        """
        from ops import events

        broker = cls(journal=journal, quote_source=quote_source,
                     starting_cash=starting_cash)
        for adj in journal.read_cash_adjustments():
            broker._cash += adj["amount"]
        orders_by_id = {o["client_order_id"]: o for o in journal.read_orders()}
        for f in journal.read_fills():
            symbol, side, qty, price = f["symbol"], f["side"], f["quantity"], f["price"]
            order = orders_by_id.get(f["client_order_id"])
            if order is not None:
                notional = order["notional_dollars"]
            else:
                notional = qty * price
                journal.record_event(
                    events.KIND_JOURNAL_REPLAY_FALLBACK,
                    events.journal_replay_fallback_payload(
                        client_order_id=f["client_order_id"], symbol=symbol,
                        side=side,
                        reason="no matching order row; falling back to qty*price",
                    ),
                )
            if side == Side.SHORT.value:
                broker._cash += notional
                existing = broker._positions.get(symbol)
                if existing is None:
                    broker._positions[symbol] = Position(
                        symbol=symbol, quantity=qty, avg_entry_price=price,
                        stop_loss_price=None,
                    )
                else:
                    total = existing.quantity + qty
                    avg = (
                        (existing.avg_entry_price * existing.quantity) + (price * qty)
                    ) / total
                    broker._positions[symbol] = Position(
                        symbol=symbol, quantity=total, avg_entry_price=avg,
                        stop_loss_price=None,
                    )
            elif side == Side.COVER.value:
                existing = broker._positions.get(symbol)
                if existing is None:
                    journal.record_event(
                        events.KIND_JOURNAL_REPLAY_ORPHAN_COVER,
                        events.journal_replay_orphan_sell_payload(
                            client_order_id=f["client_order_id"], symbol=symbol,
                            quantity=qty, price=price,
                            reason="COVER replayed with no matching prior SHORT position",
                        ),
                    )
                    continue
                broker._cash -= notional
                remaining = existing.quantity - qty
                if remaining > _EPSILON:
                    broker._positions[symbol] = Position(
                        symbol=symbol, quantity=remaining,
                        avg_entry_price=existing.avg_entry_price,
                        stop_loss_price=None,
                    )
                else:
                    del broker._positions[symbol]
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
            total -= pos.quantity * self._quote(pos.symbol)
        return total

    def place_order(self, order: Order) -> Fill:
        if order.side not in (Side.SHORT, Side.COVER):
            raise ValueError(
                f"ShortPaperBroker accepts SHORT/COVER only, got {order.side}"
            )
        self._journal.record_order(
            client_order_id=order.client_order_id, symbol=order.symbol,
            side=order.side.value, notional_dollars=order.notional_dollars,
            stop_loss_price=None,
        )
        price = self._quote(order.symbol)
        if order.side == Side.SHORT:
            return self._fill_short(order, price)
        return self._fill_cover(order, price)

    def _fill_short(self, order: Order, price: Decimal) -> Fill:
        qty = order.notional_dollars / price
        self._cash += order.notional_dollars
        existing = self._positions.get(order.symbol)
        if existing is None:
            pos = Position(symbol=order.symbol, quantity=qty,
                           avg_entry_price=price, stop_loss_price=None)
        else:
            total = existing.quantity + qty
            avg = ((existing.avg_entry_price * existing.quantity) + price * qty) / total
            pos = Position(symbol=order.symbol, quantity=total,
                           avg_entry_price=avg, stop_loss_price=None)
        self._positions[order.symbol] = pos
        return self._make_fill(order.client_order_id, order.symbol, Side.SHORT, qty, price)

    def _fill_cover(self, order: Order, price: Decimal) -> Fill:
        existing = self._positions.get(order.symbol)
        if existing is None:
            raise NoSuchPosition(f"no short position in {order.symbol}")
        qty = order.notional_dollars / price
        if qty > existing.quantity + _EPSILON:
            raise NoSuchPosition(
                f"cover qty {qty} exceeds short position {existing.quantity}"
            )
        self._cash -= order.notional_dollars
        remaining = existing.quantity - qty
        if remaining > _EPSILON:
            self._positions[order.symbol] = Position(
                symbol=existing.symbol, quantity=remaining,
                avg_entry_price=existing.avg_entry_price, stop_loss_price=None,
            )
        else:
            del self._positions[order.symbol]
        return self._make_fill(order.client_order_id, order.symbol, Side.COVER, qty, price)

    def close_position(self, symbol: str, *, client_order_id: str | None = None) -> Fill:
        existing = self._positions.get(symbol)
        if existing is None:
            raise NoSuchPosition(f"no short position in {symbol}")
        price = self._quote(symbol)
        qty = existing.quantity
        cost = qty * price
        order_id = client_order_id or f"cover-{symbol}-{uuid4().hex[:8]}"
        self._journal.record_order(
            client_order_id=order_id, symbol=symbol, side=Side.COVER.value,
            notional_dollars=cost, stop_loss_price=None,
        )
        self._cash -= cost
        del self._positions[symbol]
        return self._make_fill(order_id, symbol, Side.COVER, qty, price)

    def _make_fill(self, client_order_id, symbol, side, qty, price) -> Fill:
        fill = Fill(
            order_id=str(uuid4()), client_order_id=client_order_id, symbol=symbol,
            side=side, quantity=qty, price=price,
            filled_at=datetime.now(timezone.utc),
        )
        self._journal.record_fill(
            order_id=fill.order_id, client_order_id=fill.client_order_id,
            symbol=fill.symbol, side=fill.side.value, quantity=fill.quantity,
            price=fill.price, filled_at=fill.filled_at,
        )
        return fill
