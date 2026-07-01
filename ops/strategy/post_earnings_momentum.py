"""Post-earnings momentum strategy: for each candidate that the pipeline
labels BUY, build a sized order with an entry-relative stop."""
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
            stop_price = _quantize_money(
                cand.last_price * (Decimal("1") + self._cfg.per_position_stop_pct)
            )
            order = Order(
                client_order_id=_client_order_id(cand.symbol, asof_date, idx),
                symbol=cand.symbol,
                side=Side.BUY,
                notional_dollars=notional,
                order_type=OrderType.MARKET,
                stop_loss_price=stop_price,
            )
            out.append(StrategyOrder(
                order=order,
                reason=f"post-earnings beat (EPS {cand.earnings.eps_actual} vs "
                       f"est {cand.earnings.eps_estimate}); pipeline BUY",
                candidate=cand,
                pipeline=result,
            ))
        return out
