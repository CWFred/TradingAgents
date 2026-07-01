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
    # Actual upstream shape — single Title-case rating word
    ("Buy", PipelineDecision.BUY),
    ("Sell", PipelineDecision.SELL),
    ("Hold", PipelineDecision.HOLD),
    # Overweight/Underweight collapse to HOLD in v1
    ("Overweight", PipelineDecision.HOLD),
    ("Underweight", PipelineDecision.HOLD),
    # Case-insensitive
    ("BUY", PipelineDecision.BUY),
    ("sell", PipelineDecision.SELL),
    # Legacy "FINAL TRANSACTION PROPOSAL:" wrapper still parses
    ("FINAL TRANSACTION PROPOSAL: Buy", PipelineDecision.BUY),
    ("FINAL TRANSACTION PROPOSAL: Sell", PipelineDecision.SELL),
    ("FINAL TRANSACTION PROPOSAL: Overweight", PipelineDecision.HOLD),
    # Trailing punctuation tolerated
    ("Buy.", PipelineDecision.BUY),
    # Unknown/empty defaults to HOLD (safe)
    ("", PipelineDecision.HOLD),
    ("gibberish", PipelineDecision.HOLD),
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
            return ({}, "Buy")

    monkeypatch.setattr("ops.pipeline_adapter.TradingAgentsGraph", FakeGraph)
    adapter = TradingAgentsPipelineAdapter()
    assert constructed == []     # not yet
    r = adapter.propagate("AAPL", date(2026, 6, 30))
    assert constructed == [{}]   # constructed exactly once on first call
    adapter.propagate("MSFT", date(2026, 6, 30))
    assert constructed == [{}]   # still only one construction
    assert r.decision == PipelineDecision.BUY
