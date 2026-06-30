"""Adapter around the upstream TradingAgentsGraph.

Production code uses TradingAgentsPipelineAdapter; tests and dry-runs use
StubPipelineAdapter to avoid LLM costs. The graph is constructed lazily so
importing this module is free of side effects."""
from __future__ import annotations

import re
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


_DECISION_PATTERN = re.compile(
    r"\b(BUY|SELL|HOLD)\b", re.IGNORECASE,
)


def parse_decision(text: str) -> PipelineDecision:
    """Parse the upstream's final decision text into the enum.

    The framework's Portfolio Manager emits 'FINAL TRANSACTION PROPOSAL: <X>'
    where X is BUY/SELL/HOLD. We accept that AND any prominent occurrence of
    those tokens, falling back to HOLD if none is found (safe default)."""
    if not text:
        return PipelineDecision.HOLD
    # Prefer the FINAL TRANSACTION PROPOSAL line if present
    m = re.search(r"FINAL TRANSACTION PROPOSAL:\s*(BUY|SELL|HOLD)", text, re.IGNORECASE)
    if m:
        return PipelineDecision(m.group(1).upper())
    m = _DECISION_PATTERN.search(text)
    if m:
        return PipelineDecision(m.group(1).upper())
    return PipelineDecision.HOLD


class TradingAgentsPipelineAdapter:
    """Wraps the upstream graph. Constructs lazily and reuses one instance."""

    def __init__(self, **graph_kwargs):
        self._kwargs = graph_kwargs
        self._graph: TradingAgentsGraph | None = None

    def _ensure_graph(self) -> TradingAgentsGraph:
        if self._graph is None:
            self._graph = TradingAgentsGraph(**self._kwargs)
        return self._graph

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
