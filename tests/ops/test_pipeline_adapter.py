from datetime import date

import pytest

from ops.pipeline_adapter import (
    PipelineDecision,
    PipelineResult,
    StubPipelineAdapter,
    TradingAgentsPipelineAdapter,
    parse_decision,
)


def test_stub_returns_fixed_decision():
    stub = StubPipelineAdapter({"AAPL": PipelineDecision.BUY, "MSFT": PipelineDecision.HOLD})
    r = stub.propagate("AAPL", date(2026, 6, 30))
    assert isinstance(r, PipelineResult)
    assert r.decision == PipelineDecision.BUY
    assert r.symbol == "AAPL"


def test_stub_defaults_to_hold_for_unknown_symbol():
    stub = StubPipelineAdapter({})
    r = stub.propagate("ZZZZ", date(2026, 6, 30))
    assert r.decision == PipelineDecision.HOLD


@pytest.mark.parametrize("text,expected", [
    ("FINAL TRANSACTION PROPOSAL: BUY", PipelineDecision.BUY),
    ("FINAL TRANSACTION PROPOSAL: SELL", PipelineDecision.SELL),
    ("FINAL TRANSACTION PROPOSAL: HOLD", PipelineDecision.HOLD),
    ("buy", PipelineDecision.BUY),
    ("the analysts agree: SELL the position", PipelineDecision.SELL),
    ("we should HOLD for now", PipelineDecision.HOLD),
    ("inconclusive analysis", PipelineDecision.HOLD),   # fallback
])
def test_parse_decision_handles_various_phrasings(text, expected):
    assert parse_decision(text) == expected


def test_real_adapter_constructs_graph_lazily(monkeypatch):
    """The TradingAgentsGraph is heavy (LLM clients); construction must be
    deferred to first call so importing this module is cheap."""
    constructed = []

    class FakeGraph:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def propagate(self, ticker, dt):
            return ({}, "FINAL TRANSACTION PROPOSAL: BUY")

    monkeypatch.setattr("ops.pipeline_adapter.TradingAgentsGraph", FakeGraph)
    adapter = TradingAgentsPipelineAdapter()
    assert constructed == []     # not yet
    r = adapter.propagate("AAPL", date(2026, 6, 30))
    assert constructed == [{}]   # constructed exactly once on first call
    adapter.propagate("MSFT", date(2026, 6, 30))
    assert constructed == [{}]   # still only one construction
    assert r.decision == PipelineDecision.BUY
