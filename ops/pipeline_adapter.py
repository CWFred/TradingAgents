"""Adapter around the upstream TradingAgentsGraph.

Production code uses TradingAgentsPipelineAdapter; tests and dry-runs use
StubPipelineAdapter to avoid LLM costs. The graph is constructed lazily so
importing this module is free of side effects."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Protocol

from tradingagents.graph.trading_graph import TradingAgentsGraph


class PipelineDecision(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


@dataclass(frozen=True)
class PipelineResult:
    symbol: str
    date: date
    decision: PipelineDecision
    raw: dict = field(default_factory=dict)


class PipelineAdapter(Protocol):
    def propagate(self, symbol: str, asof_date: date) -> PipelineResult: ...


# Upstream ratings are one of: Buy, Overweight, Hold, Underweight, Sell.
# For v1's conservative posture, only Buy/Sell trigger action; Overweight
# and Underweight collapse to HOLD along with Hold and any unknown value.
_HIGH_CONVICTION_BUY = {"BUY"}
_HIGH_CONVICTION_SELL = {"SELL"}
_UPSTREAM_RATINGS = {"BUY", "OVERWEIGHT", "HOLD", "UNDERWEIGHT", "SELL"}


def parse_decision(text: str) -> PipelineDecision:
    """Parse the upstream's decision text into a PipelineDecision.

    The upstream `TradingAgentsGraph.propagate()` returns a bare rating word
    from SignalProcessor.parse_rating: one of Buy/Overweight/Hold/Underweight/Sell.

    v1 posture: only Buy and Sell trigger action. Overweight and Underweight
    collapse to HOLD along with Hold itself. Unknown or missing text also
    defaults to HOLD (safe posture). We also still accept a leading
    'FINAL TRANSACTION PROPOSAL: <X>' wrapper for defensive matching against
    older upstream formats.
    """
    if not text:
        return PipelineDecision.HOLD
    # Defensive: strip a legacy "FINAL TRANSACTION PROPOSAL:" prefix if present
    m = re.search(r"FINAL TRANSACTION PROPOSAL:\s*(\S+)", text, re.IGNORECASE)
    candidate = m.group(1) if m else text.strip().split()[0] if text.strip() else ""
    candidate = candidate.strip().rstrip(".,").upper()
    if candidate in _HIGH_CONVICTION_BUY:
        return PipelineDecision.BUY
    if candidate in _HIGH_CONVICTION_SELL:
        return PipelineDecision.SELL
    return PipelineDecision.HOLD


class TradingAgentsPipelineAdapter:
    """Wraps the upstream graph. Constructs lazily and reuses one instance."""

    def __init__(self, **graph_kwargs):
        self._kwargs = graph_kwargs
        self._graph: TradingAgentsGraph | None = None
        self._lock = threading.Lock()

    def _ensure_graph(self) -> TradingAgentsGraph:
        # Fast path: no lock once the cache is populated.
        if self._graph is not None:
            return self._graph
        with self._lock:
            # Double-checked: another thread may have built it while we
            # were waiting for the lock.
            if self._graph is None:
                self._graph = self._build_graph()
        return self._graph

    def _build_graph(self) -> TradingAgentsGraph:
        return TradingAgentsGraph(**self._kwargs)

    def propagate(self, symbol: str, asof_date: date) -> PipelineResult:
        graph = self._ensure_graph()
        raw, decision_text = graph.propagate(symbol, asof_date.isoformat())
        decision = parse_decision(decision_text or "")
        raw_dict = raw if isinstance(raw, dict) else {"output": str(raw)}
        return PipelineResult(symbol=symbol, date=asof_date, decision=decision, raw=raw_dict)


class StubPipelineAdapter:
    """In-memory adapter for tests and dry-runs. Returns fixed decisions."""

    def __init__(self, decisions: dict[str, PipelineDecision] | None = None):
        self._decisions = decisions or {}

    def propagate(self, symbol: str, asof_date: date) -> PipelineResult:
        decision = self._decisions.get(symbol, PipelineDecision.HOLD)
        return PipelineResult(symbol=symbol, date=asof_date, decision=decision, raw={})
