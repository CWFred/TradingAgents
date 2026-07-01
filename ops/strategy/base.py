"""Strategy primitives."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from ops.broker.types import Order
from ops.pipeline_adapter import PipelineAdapter, PipelineResult
from ops.universe import Candidate


@dataclass(frozen=True)
class StrategyOrder:
    order: Order
    reason: str
    candidate: Candidate
    pipeline: PipelineResult


class Strategy(Protocol):
    def propose_orders(
        self,
        *,
        candidates: list[Candidate],
        pipeline: PipelineAdapter,
        current_equity: Decimal,
        asof_date: date,
    ) -> list[StrategyOrder]: ...
