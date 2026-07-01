"""One-shot stop-loss enforcement.

For every open position, check the current quote and place a close-all SELL
if the position is at or past the per_position_stop_pct threshold. This is
the single-pass variant; Plan 3 will wrap it in a background-thread loop."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from ops.broker.base import BrokerError, QuoteUnavailable
from ops.broker.guarded import GuardedBroker
from ops.broker.types import Order, OrderType, Side
from ops.config import OpsConfig


@dataclass(frozen=True)
class StopAction:
    symbol: str
    entry: Decimal
    current: Decimal
    pct: Decimal
    sold: bool
    reason: str


class PositionGuardian:
    def __init__(
        self,
        *,
        broker: GuardedBroker,
        quote_source: Callable[[str], Decimal],
        config: OpsConfig,
    ):
        self._broker = broker
        self._quote = quote_source
        self._cfg = config

    def check_stops_once(self) -> list[StopAction]:
        actions: list[StopAction] = []
        for pos in self._broker.get_positions():
            try:
                current = self._quote(pos.symbol)
            except QuoteUnavailable as exc:
                self._broker.journal.record_event(
                    "quote_unavailable",
                    {
                        "symbol": pos.symbol,
                        "context": "guardian_stop_check",
                        "error": str(exc),
                    },
                )
                actions.append(StopAction(
                    symbol=pos.symbol,
                    entry=pos.avg_entry_price,
                    current=Decimal("0"),
                    pct=Decimal("0"),
                    sold=False,
                    reason=f"quote unavailable: {exc}",
                ))
                continue
            pct = pos.unrealized_pct(current)
            triggered = pct <= self._cfg.per_position_stop_pct
            if not triggered:
                actions.append(StopAction(
                    symbol=pos.symbol, entry=pos.avg_entry_price,
                    current=current, pct=pct, sold=False,
                    reason=f"unrealized {pct} above stop {self._cfg.per_position_stop_pct}",
                ))
                continue
            sell = Order(
                client_order_id=f"stop-{pos.symbol}",
                symbol=pos.symbol, side=Side.SELL,
                notional_dollars=Decimal("0"),  # sell-all
                order_type=OrderType.MARKET,
            )
            try:
                self._broker.place_order(sell)
            except BrokerError as exc:
                self._broker.journal.record_event(
                    "stop_failed",
                    {
                        "symbol": pos.symbol,
                        "entry": str(pos.avg_entry_price),
                        "current": str(current),
                        "pct": str(pct),
                        "threshold": str(self._cfg.per_position_stop_pct),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                actions.append(StopAction(
                    symbol=pos.symbol, entry=pos.avg_entry_price,
                    current=current, pct=pct, sold=False,
                    reason=f"stop-sell failed: {type(exc).__name__}: {exc}",
                ))
                continue
            self._broker.journal.record_event(
                "stop_hit",
                {
                    "symbol": pos.symbol,
                    "entry": str(pos.avg_entry_price),
                    "current": str(current),
                    "pct": str(pct),
                    "threshold": str(self._cfg.per_position_stop_pct),
                },
            )
            actions.append(StopAction(
                symbol=pos.symbol, entry=pos.avg_entry_price,
                current=current, pct=pct, sold=True,
                reason=f"stop hit at {pct} (threshold {self._cfg.per_position_stop_pct})",
            ))
        return actions
