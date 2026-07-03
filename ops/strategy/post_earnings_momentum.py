"""Post-earnings momentum strategy: for each candidate that the pipeline
labels BUY, build a sized order with an entry-relative stop.

The stop is carried as Order.stop_pct (entry-relative, e.g. -0.08) rather
than an absolute price: cand.last_price is a stale previous-close reference
(from the 20-day history call), and a gap between that reference and the
actual fill can put an absolute stop on the wrong side of the fill. The
broker resolves stop_pct to an absolute price from the real fill price at
fill time (see PaperBroker/RobinhoodBroker)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from ops.broker.types import Order, OrderType, Side
from ops.config import OpsConfig
from ops.pipeline_adapter import PipelineAdapter, PipelineDecision
from ops.strategy.base import StrategyOrder
from ops.universe import Candidate


def _client_order_id(symbol: str, asof: date, idx: int) -> str:
    return f"pem-{asof.isoformat()}-{symbol}-{idx}"


def _quantize_money(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"))


class PostEarningsMomentumStrategy:
    def __init__(self, *, config: OpsConfig):
        self._cfg = config

    def propose_orders(
        self,
        *,
        candidates: list[Candidate],
        pipeline: PipelineAdapter,
        current_equity: Decimal,
        asof_date: date,
    ) -> list[StrategyOrder]:
        notional = _quantize_money(current_equity * self._cfg.per_position_cap_pct)
        if notional < self._cfg.per_trade_dollar_floor:
            return []
        out: list[StrategyOrder] = []
        for idx, cand in enumerate(candidates):
            result = pipeline.propagate(cand.symbol, asof_date)
            if result.decision != PipelineDecision.BUY:
                continue
            order = Order(
                client_order_id=_client_order_id(cand.symbol, asof_date, idx),
                symbol=cand.symbol,
                side=Side.BUY,
                notional_dollars=notional,
                order_type=OrderType.MARKET,
                stop_pct=self._cfg.per_position_stop_pct,
            )
            out.append(StrategyOrder(
                order=order,
                reason=f"post-earnings beat (EPS {cand.earnings.eps_actual} vs "
                       f"est {cand.earnings.eps_estimate}); pipeline BUY",
                candidate=cand,
                pipeline=result,
            ))
        return out
