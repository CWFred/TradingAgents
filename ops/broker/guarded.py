"""GuardedBroker — wraps any Broker and runs the rule chain on every order.

This is the only Broker callers ever see outside the broker package. The
inner broker is name-mangled (`__inner`) so external access is mechanically
awkward, and the canonical assembly path is the factory `build_guarded_paper_broker`
in `ops.__init__` — callers should not construct GuardedBroker directly.

Concurrency: every place_order call holds `_lock` for the full guardrail
evaluation + inner fill, so two concurrent BUYs cannot both read pre-trade
state, both pass sizing/cash rules against stale numbers, and both fill.

get_positions/get_equity/get_cash also acquire `_lock` (read-only, but the
inner broker's dicts are plain Python dicts mutated in place by
place_order/close_position; without serializing reads against those writes
a concurrent guardian pass can observe a dict mid-mutation and raise "dict
changed size during iteration"). `_lock` is a plain, non-reentrant
threading.Lock: place_order/close_position must never call
self.get_positions/get_equity/get_cash (or any other `_lock`-holding
method) while already holding `_lock` — they read state via
`ctx.broker`/`self.__inner` directly instead, and the guardrail rule chain
receives `broker=self.__inner` (never `self`, see RuleContext below), so
rule reads never re-enter this lock either. That keeps the only lock
nesting order in the system as GuardedBroker._lock (outer) wrapping
Journal._lock (inner) — Journal never calls back into GuardedBroker — so
there is no cycle and no deadlock.
"""
from __future__ import annotations

import threading
import uuid
from decimal import Decimal

from ops.broker.base import Broker, BrokerError, NoSuchPosition, OrderRejected
from ops.broker.types import Fill, Order, OrderType, Position, Side
from ops.config import OpsConfig
from ops.guardrails.base import RuleContext
from ops.guardrails.engine import RuleEngine
from ops.journal import Journal


class GuardedBroker(Broker):
    def __init__(self, *, inner: Broker, engine: RuleEngine, journal: Journal, config: OpsConfig):
        self.__inner = inner
        self._engine = engine
        self._journal = journal
        self._config = config
        self._lock = threading.Lock()

    @property
    def journal(self) -> Journal:
        return self._journal

    def _journal_fill_event(self, fill: Fill, context: str) -> None:
        self._journal.record_event(
            "fill",
            {
                "client_order_id": fill.client_order_id,
                "order_id": fill.order_id,
                "symbol": fill.symbol,
                "side": fill.side.value,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "filled_at": fill.filled_at.isoformat(),
                "context": context,
            },
        )

    def get_cash(self) -> Decimal:
        with self._lock:
            return self.__inner.get_cash()

    def get_equity(self) -> Decimal:
        with self._lock:
            return self.__inner.get_equity()

    def get_positions(self) -> list[Position]:
        with self._lock:
            return self.__inner.get_positions()

    def get_quote(self, symbol: str) -> Decimal:
        return self.__inner.get_quote(symbol)

    def place_order(self, order: Order) -> Fill:
        with self._lock:
            ctx = RuleContext(order=order, broker=self.__inner, config=self._config)
            result = self._engine.evaluate(ctx)
            if not result.allowed:
                self._journal.record_event(
                    "order_rejected",
                    {
                        "rule": result.failed_rule_name,
                        "reason": result.reason,
                        "client_order_id": order.client_order_id,
                        "symbol": order.symbol,
                        "side": order.side.value,
                        "notional_dollars": str(order.notional_dollars),
                    },
                )
                raise OrderRejected(result.failed_rule_name, result.reason)
            try:
                fill = self.__inner.place_order(order)
                self._journal_fill_event(fill, "place")
                return fill
            except BrokerError as exc:
                self._journal.record_event(
                    "order_rejected",
                    {
                        "rule": "broker",
                        "reason": f"{type(exc).__name__}: {exc}",
                        "client_order_id": order.client_order_id,
                        "symbol": order.symbol,
                        "side": order.side.value,
                        "notional_dollars": str(order.notional_dollars),
                    },
                )
                raise

    def close_position(self, symbol: str) -> Fill:
        """Close a position, holding _lock across snapshot + rule chain + inner delegate.

        Note: this signature intentionally omits the ABC's `client_order_id` kwarg.
        GuardedBroker mints the id itself (`close-{symbol}-{uuid[:8]}`) so any
        order_rejected event and the successful fill in the inner broker share the
        same id. Passing an override here would break that traceability, so
        callers are not given that lever.
        """
        with self._lock:
            positions = self.__inner.get_positions()
            existing = next((p for p in positions if p.symbol == symbol), None)
            if existing is None:
                raise NoSuchPosition(f"no position in {symbol}")
            price = self.__inner.get_quote(symbol)
            notional = existing.quantity * price
            close_order = Order(
                client_order_id=f"close-{symbol}-{uuid.uuid4().hex[:8]}",
                symbol=symbol,
                side=Side.SELL,
                notional_dollars=notional,
                order_type=OrderType.MARKET,
            )
            ctx = RuleContext(order=close_order, broker=self.__inner, config=self._config)
            result = self._engine.evaluate(ctx)
            if not result.allowed:
                self._journal.record_event(
                    "order_rejected",
                    {
                        "rule": result.failed_rule_name,
                        "reason": result.reason,
                        "client_order_id": close_order.client_order_id,
                        "symbol": symbol,
                        "side": "SELL",
                        "notional_dollars": str(notional),
                        "context": "close_position",
                    },
                )
                raise OrderRejected(result.failed_rule_name, result.reason)
            try:
                fill = self.__inner.close_position(
                    symbol, client_order_id=close_order.client_order_id,
                )
                self._journal_fill_event(fill, "close")
                return fill
            except BrokerError as exc:
                self._journal.record_event(
                    "order_rejected",
                    {
                        "rule": "broker",
                        "reason": f"{type(exc).__name__}: {exc}",
                        "client_order_id": close_order.client_order_id,
                        "symbol": symbol,
                        "side": "SELL",
                        "notional_dollars": str(notional),
                        "context": "close_position",
                    },
                )
                raise
