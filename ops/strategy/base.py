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


@dataclass(frozen=True)
class AnalyzedDecision:
    """One analyzed candidate's pipeline verdict, BUY or not. Collected via
    propose_orders' optional decision_sink so callers can journal a
    per-name audit trail without changing the returned order list."""
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
        live_max_position_cap: Decimal | None = None,
        decision_sink: list[AnalyzedDecision] | None = None,
    ) -> list[StrategyOrder]: ...
