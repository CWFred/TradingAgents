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

Live-mode latency (M6, documented decision): for the Robinhood inner
broker, place_order holds `_lock` through the fill-poll window
(`RealRobinhoodMCPClient._await_fill`, bounded at 30s), during which the
guardian cannot read positions. That bound — not a submit/await lock
split — is the deliberate mitigation; see the lock-scope note in
docs/superpowers/specs/2026-07-04-tradingagents-mcp-live-design.md.
"""
from __future__ import annotations

import threading
import uuid
from decimal import Decimal

from ops import events
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
            events.KIND_FILL,
            events.fill_payload(
                client_order_id=fill.client_order_id,
                order_id=fill.order_id,
                symbol=fill.symbol,
                side=fill.side.value,
                quantity=fill.quantity,
                price=fill.price,
                filled_at=fill.filled_at,
                context=context,
                broker_mode=self._config.broker_mode,
            ),
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
                    events.KIND_ORDER_REJECTED,
                    events.order_rejected_payload(
                        rule=result.failed_rule_name,
                        reason=result.reason,
                        client_order_id=order.client_order_id,
                        symbol=order.symbol,
                        side=order.side.value,
                        notional_dollars=order.notional_dollars,
                    ),
                )
                raise OrderRejected(result.failed_rule_name, result.reason)
            try:
                fill = self.__inner.place_order(order)
                self._journal_fill_event(fill, "place")
                return fill
            except BrokerError as exc:
                self._journal.record_event(
                    events.KIND_ORDER_REJECTED,
                    events.order_rejected_payload(
                        rule="broker",
                        reason=f"{type(exc).__name__}: {exc}",
                        client_order_id=order.client_order_id,
                        symbol=order.symbol,
                        side=order.side.value,
                        notional_dollars=order.notional_dollars,
                    ),
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
            client_order_id = f"close-{symbol}-{uuid.uuid4().hex[:8]}"
            # Dry-run the rule chain against sellable_quantity (NOT
            # existing.quantity) — post-T5, RobinhoodBroker.close_position
            # actually sells shares_available_for_sells, and LongOnlyRule
            # bounds a SELL against sellable_quantity too. Sizing the
            # synthetic order off the full quantity here made the dry-run
            # reject closes on positions with held/unsettled shares even
            # though the real close would have succeeded (MCP-T5 bug).
            # PaperBroker never sets shares_available_for_sells, so
            # sellable_quantity == quantity there and this is a no-op for
            # paper — same synthetic order as before.
            notional = Decimal("0")
            sellable = existing.sellable_quantity
            if sellable > 0:
                price = self.__inner.get_quote(symbol)
                notional = sellable * price
                close_order = Order(
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=Side.SELL,
                    notional_dollars=notional,
                    order_type=OrderType.MARKET,
                )
                ctx = RuleContext(order=close_order, broker=self.__inner, config=self._config)
                result = self._engine.evaluate(ctx)
                if not result.allowed:
                    self._journal.record_event(
                        events.KIND_ORDER_REJECTED,
                        events.order_rejected_payload(
                            rule=result.failed_rule_name,
                            reason=result.reason,
                            client_order_id=client_order_id,
                            symbol=symbol,
                            side="SELL",
                            notional_dollars=notional,
                            context="close_position",
                        ),
                    )
                    raise OrderRejected(result.failed_rule_name, result.reason)
            # sellable <= 0: nothing to dry-run (a zero/negative-notional
            # synthetic Order is invalid and, more to the point, there's no
            # rule question to ask — there's nothing sellable). Skip the
            # rule chain and let the inner broker's close_position raise;
            # RobinhoodBroker already does this post-T5, and the except
            # branch below journals it exactly like any other broker-layer
            # rejection.
            try:
                fill = self.__inner.close_position(
                    symbol, client_order_id=client_order_id,
                )
                self._journal_fill_event(fill, "close")
                return fill
            except BrokerError as exc:
                self._journal.record_event(
                    events.KIND_ORDER_REJECTED,
                    events.order_rejected_payload(
                        rule="broker",
                        reason=f"{type(exc).__name__}: {exc}",
                        client_order_id=client_order_id,
                        symbol=symbol,
                        side="SELL",
                        notional_dollars=notional,
                        context="close_position",
                    ),
                )
                raise
