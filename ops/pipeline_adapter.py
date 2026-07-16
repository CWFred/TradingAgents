"""Adapter around the upstream TradingAgentsGraph.

Production code uses TradingAgentsPipelineAdapter; tests and dry-runs use
StubPipelineAdapter to avoid LLM costs. The graph is constructed lazily so
importing this module is free of side effects."""
from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Protocol

from ops.activity import NullReporter
from ops.llm_backend import ManagedBackend, NullManagedBackend
from tradingagents.graph.trading_graph import TradingAgentsGraph


class PipelineDecision(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    TRIM = "TRIM"


@dataclass(frozen=True)
class PipelineResult:
    symbol: str
    date: date
    decision: PipelineDecision
    raw: dict = field(default_factory=dict)
    # Native 5-tier rating word (Buy/Overweight/Hold/Underweight/Sell) from
    # the graph's signal processor. The vetting path reads this ungraded
    # rating; the momentum path keeps consuming the collapsed `decision`.
    rating: str = ""
    # Conviction tier implied by the rating: TIER_HIGH, TIER_STARTER, or "".
    tier: str = ""


class PipelineAdapter(Protocol):
    def propagate(
        self, symbol: str, asof_date: date, research_context: str = "",
    ) -> PipelineResult: ...

    def session(self):
        """Context manager bracketing a batch of analyses.

        On exit, any managed local model backend is torn down. Bringing the
        backend *up* is lazy (done inside propagate), so an empty batch never
        starts a server.
        """
        ...


# Conviction tiers carried on PipelineResult.tier. TIER_HIGH sizes at
# per_position_cap_pct and is what displacement funds; TIER_STARTER sizes
# at starter_position_pct and is what displacement trims. "" = no tier.
TIER_HIGH = "high"
TIER_STARTER = "starter"

# Upstream ratings are one of: Buy, Overweight, Hold, Underweight, Sell.
# v2 posture (spec 2026-07-14): the full scale acts. Overweight enters at
# starter size; Underweight trims half of a held position (the TRIM
# decision is a no-op for unheld symbols — enforced by the orchestrator,
# which is the only layer that knows holdings). Unknown text still
# defaults to HOLD.
_RATING_ACTIONS: dict[str, tuple[PipelineDecision, str]] = {
    "BUY": (PipelineDecision.BUY, TIER_HIGH),
    "OVERWEIGHT": (PipelineDecision.BUY, TIER_STARTER),
    "HOLD": (PipelineDecision.HOLD, ""),
    "UNDERWEIGHT": (PipelineDecision.TRIM, ""),
    "SELL": (PipelineDecision.SELL, ""),
}


def parse_rating_action(text: str) -> tuple[PipelineDecision, str]:
    """Map the upstream rating word to (decision, conviction tier).

    Accepts a leading 'FINAL TRANSACTION PROPOSAL: <X>' wrapper for
    defensive matching against older upstream formats."""
    if not text:
        return (PipelineDecision.HOLD, "")
    m = re.search(r"FINAL TRANSACTION PROPOSAL:\s*(\S+)", text, re.IGNORECASE)
    candidate = m.group(1) if m else text.strip().split()[0] if text.strip() else ""
    candidate = candidate.strip().rstrip(".,").upper()
    return _RATING_ACTIONS.get(candidate, (PipelineDecision.HOLD, ""))


def parse_decision(text: str) -> PipelineDecision:
    """Decision-only view of parse_rating_action (kept for existing callers)."""
    return parse_rating_action(text)[0]


class TradingAgentsPipelineAdapter:
    """Wraps the upstream graph. Constructs lazily and reuses one instance."""

    def __init__(self, *, backend: ManagedBackend | None = None,
                 reporter=None, activity_job: str = "daily_cycle",
                 activity_stage: str = "analyzing", **graph_kwargs):
        self._kwargs = graph_kwargs
        self._graph: TradingAgentsGraph | None = None
        self._lock = threading.Lock()
        self._backend: ManagedBackend = backend or NullManagedBackend()
        self._reporter = reporter or NullReporter()
        self._activity_job = activity_job
        self._activity_stage = activity_stage
        self._seq = 0

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

    def propagate(
        self, symbol: str, asof_date: date, research_context: str = "",
    ) -> PipelineResult:
        self._seq += 1
        with self._reporter.item(
            self._activity_job, stage=self._activity_stage,
            symbol=symbol, seq=str(self._seq),
        ):
            # Bring the managed backend up lazily — only when an analysis
            # actually runs, so ticks with no candidates never load a model.
            self._backend.ensure_up()
            graph = self._ensure_graph()
            raw, decision_text = graph.propagate(
                symbol, asof_date.isoformat(), research_memo_context=research_context,
            )
            decision, tier = parse_rating_action(decision_text or "")
            raw_dict = raw if isinstance(raw, dict) else {"output": str(raw)}
            return PipelineResult(
                symbol=symbol, date=asof_date, decision=decision, raw=raw_dict,
                rating=(decision_text or "").strip(), tier=tier,
            )

    @contextmanager
    def session(self) -> Iterator[TradingAgentsPipelineAdapter]:
        """Bracket a batch of analyses; tear the managed backend down on exit."""
        self._seq = 0
        try:
            yield self
        finally:
            self._backend.shutdown()


class StubPipelineAdapter:
    """In-memory adapter for tests and dry-runs. Returns fixed decisions.

    ``research_context`` is accepted and ignored; ``ratings`` maps symbols
    to a stub native rating (default "Hold") so vetting tests stay cheap.
    ``raw`` carries a non-empty stub ``risk_debate_state`` so the vetting
    stage's falsifier-extraction path is exercisable in stub/dry-run mode
    (a real graph always produces a debate on the confirm path).
    """

    def __init__(
        self,
        decisions: dict[str, PipelineDecision] | None = None,
        ratings: dict[str, str] | None = None,
        tiers: dict[str, str] | None = None,
    ):
        self._decisions = decisions or {}
        self._ratings = ratings or {}
        self._tiers = tiers or {}

    def propagate(
        self, symbol: str, asof_date: date, research_context: str = "",
    ) -> PipelineResult:
        decision = self._decisions.get(symbol, PipelineDecision.HOLD)
        raw = {
            "final_trade_decision": "",
            "risk_debate_state": {
                "history": f"stub risk debate for {symbol}",
                "judge_decision": "stub judge decision",
            },
        }
        default_tier = TIER_HIGH if decision is PipelineDecision.BUY else ""
        tier = self._tiers.get(symbol, default_tier)
        return PipelineResult(
            symbol=symbol, date=asof_date, decision=decision, raw=raw,
            rating=self._ratings.get(symbol, "Hold"), tier=tier,
        )

    @contextmanager
    def session(self) -> Iterator[StubPipelineAdapter]:
        yield self
