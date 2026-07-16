import threading
from datetime import date

import pytest

from ops.pipeline_adapter import (
    TIER_HIGH,
    TIER_STARTER,
    PipelineDecision,
    PipelineResult,
    StubPipelineAdapter,
    TradingAgentsPipelineAdapter,
    parse_decision,
    parse_rating_action,
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
    # v2 spec 2026-07-14: Overweight triggers BUY, Underweight triggers TRIM
    ("Overweight", PipelineDecision.BUY),
    ("Underweight", PipelineDecision.TRIM),
    # Case-insensitive
    ("BUY", PipelineDecision.BUY),
    ("sell", PipelineDecision.SELL),
    # Legacy "FINAL TRANSACTION PROPOSAL:" wrapper still parses
    ("FINAL TRANSACTION PROPOSAL: Buy", PipelineDecision.BUY),
    ("FINAL TRANSACTION PROPOSAL: Sell", PipelineDecision.SELL),
    ("FINAL TRANSACTION PROPOSAL: Overweight", PipelineDecision.BUY),
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

        def propagate(self, ticker, dt, research_memo_context=""):
            return ({}, "Buy")

    monkeypatch.setattr("ops.pipeline_adapter.TradingAgentsGraph", FakeGraph)
    adapter = TradingAgentsPipelineAdapter()
    assert constructed == []     # not yet
    r = adapter.propagate("AAPL", date(2026, 6, 30))
    assert constructed == [{}]   # constructed exactly once on first call
    adapter.propagate("MSFT", date(2026, 6, 30))
    assert constructed == [{}]   # still only one construction
    assert r.decision == PipelineDecision.BUY


class _RecordingBackend:
    """Managed-backend double that records lifecycle calls in order."""

    def __init__(self, events):
        self.events = events
        self.ensure_calls = 0
        self.shutdown_calls = 0

    def ensure_up(self):
        self.ensure_calls += 1
        self.events.append("ensure_up")

    def shutdown(self):
        self.shutdown_calls += 1
        self.events.append("shutdown")


def test_propagate_ensures_backend_up_before_graph(monkeypatch):
    """The managed backend must be brought up before the graph runs, so a
    local server is loaded lazily only when an analysis actually happens."""
    events = []

    class FakeGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, dt, research_memo_context=""):
            events.append("propagate")
            return ({}, "Buy")

    monkeypatch.setattr("ops.pipeline_adapter.TradingAgentsGraph", FakeGraph)
    backend = _RecordingBackend(events)
    adapter = TradingAgentsPipelineAdapter(backend=backend)
    adapter.propagate("AAPL", date(2026, 6, 30))
    assert events == ["ensure_up", "propagate"]
    assert backend.ensure_calls == 1


def test_session_shuts_down_backend():
    events = []
    backend = _RecordingBackend(events)
    adapter = TradingAgentsPipelineAdapter(backend=backend)
    with adapter.session():
        pass
    assert backend.shutdown_calls == 1
    assert events == ["shutdown"]


def test_session_shuts_down_even_on_error():
    events = []
    backend = _RecordingBackend(events)
    adapter = TradingAgentsPipelineAdapter(backend=backend)
    with pytest.raises(ValueError):
        with adapter.session():
            raise ValueError("boom")
    assert backend.shutdown_calls == 1


def test_default_adapter_has_no_managed_backend(monkeypatch):
    """Constructed with no backend, propagate/session must work (Null backend)."""
    class FakeGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, dt, research_memo_context=""):
            return ({}, "Hold")

    monkeypatch.setattr("ops.pipeline_adapter.TradingAgentsGraph", FakeGraph)
    adapter = TradingAgentsPipelineAdapter()
    with adapter.session():
        r = adapter.propagate("AAPL", date(2026, 6, 30))
    assert r.decision == PipelineDecision.HOLD


def test_propagate_threads_research_context_and_exposes_native_rating(monkeypatch):
    """Vetting path: the adapter forwards research_context to the graph and
    surfaces the ungraded 5-tier rating; v2 spec 2026-07-14 acts on full scale
    (Overweight -> BUY)."""
    captured = {}

    class FakeGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, symbol, trade_date, research_memo_context=""):
            captured["research_memo_context"] = research_memo_context
            return {"final_trade_decision": "Rating: Overweight"}, "Overweight"

    monkeypatch.setattr("ops.pipeline_adapter.TradingAgentsGraph", FakeGraph)
    adapter = TradingAgentsPipelineAdapter()
    result = adapter.propagate("ACME", date(2026, 7, 9), research_context="BRIEF")
    assert captured["research_memo_context"] == "BRIEF"
    assert result.rating == "Overweight"
    assert result.decision == PipelineDecision.BUY  # v2 acts on full scale


def test_propagate_default_context_is_empty(monkeypatch):
    """Momentum callers pass no context; the graph must receive ""."""
    captured = {}

    class FakeGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, symbol, trade_date, research_memo_context=""):
            captured["research_memo_context"] = research_memo_context
            return {"final_trade_decision": "Rating: Hold"}, "Hold"

    monkeypatch.setattr("ops.pipeline_adapter.TradingAgentsGraph", FakeGraph)
    adapter = TradingAgentsPipelineAdapter()
    adapter.propagate("ACME", date(2026, 7, 9))
    assert captured["research_memo_context"] == ""


def test_stub_adapter_accepts_and_ignores_research_context():
    stub = StubPipelineAdapter(ratings={"ACME": "Buy"})
    result = stub.propagate("ACME", date(2026, 7, 9), research_context="BRIEF")
    assert result.rating == "Buy"
    assert result.decision == PipelineDecision.HOLD


def test_stub_adapter_default_rating_is_hold():
    result = StubPipelineAdapter().propagate("X", date(2026, 7, 9))
    assert result.rating == "Hold"


def test_stub_session_is_noop():
    stub = StubPipelineAdapter({})
    with stub.session():
        pass  # must be a usable context manager


def test_ensure_graph_is_thread_safe(monkeypatch):
    """Two concurrent callers get the same graph instance; build runs once."""
    build_count = 0
    build_lock = threading.Lock()
    barrier = threading.Barrier(2)

    class FakeGraph:
        pass

    def slow_build(self):
        nonlocal build_count
        # With a correct mutex around the whole build, only one thread ever
        # reaches this point concurrently, so the second party to the
        # barrier never arrives. Bound the wait so a correct (locked)
        # implementation proceeds via timeout instead of hanging forever;
        # a buggy (unlocked) implementation still lets both threads race in
        # here and rendezvous on the barrier before the timeout elapses.
        try:
            barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        # Simulate a slow build so two threads overlap.
        import time
        time.sleep(0.05)
        with build_lock:
            build_count += 1
        return FakeGraph()

    adapter = TradingAgentsPipelineAdapter.__new__(TradingAgentsPipelineAdapter)
    adapter._graph = None
    adapter._lock = threading.Lock()

    # Monkeypatch the actual builder used inside _ensure_graph:
    monkeypatch.setattr(TradingAgentsPipelineAdapter, "_build_graph", slow_build)

    results = {}
    def call(idx):
        results[idx] = adapter._ensure_graph()

    t1 = threading.Thread(target=call, args=(0,))
    t2 = threading.Thread(target=call, args=(1,))
    t1.start(); t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive(), "threads did not complete — possible deadlock"
    assert results[0] is results[1]
    assert build_count == 1


def test_parse_rating_action_full_scale():
    assert parse_rating_action("Buy") == (PipelineDecision.BUY, TIER_HIGH)
    assert parse_rating_action("Overweight") == (PipelineDecision.BUY, TIER_STARTER)
    assert parse_rating_action("Hold") == (PipelineDecision.HOLD, "")
    assert parse_rating_action("Underweight") == (PipelineDecision.TRIM, "")
    assert parse_rating_action("Sell") == (PipelineDecision.SELL, "")


def test_parse_rating_action_unknown_and_empty_default_hold():
    assert parse_rating_action("") == (PipelineDecision.HOLD, "")
    assert parse_rating_action("Banana") == (PipelineDecision.HOLD, "")


def test_parse_rating_action_strips_legacy_wrapper_and_punctuation():
    assert parse_rating_action("FINAL TRANSACTION PROPOSAL: Overweight.") == (
        PipelineDecision.BUY, TIER_STARTER,
    )


def test_parse_decision_wrapper_still_collapses_to_decision_only():
    assert parse_decision("Overweight") == PipelineDecision.BUY
    assert parse_decision("Underweight") == PipelineDecision.TRIM


def test_stub_adapter_tier_defaults_high_for_buy():
    stub = StubPipelineAdapter({"AAPL": PipelineDecision.BUY})
    result = stub.propagate("AAPL", date(2026, 7, 14))
    assert result.tier == TIER_HIGH


def test_stub_adapter_tier_explicit_and_default_empty_for_hold():
    stub = StubPipelineAdapter(
        {"AAPL": PipelineDecision.BUY, "MSFT": PipelineDecision.HOLD},
        tiers={"AAPL": TIER_STARTER},
    )
    assert stub.propagate("AAPL", date(2026, 7, 14)).tier == TIER_STARTER
    assert stub.propagate("MSFT", date(2026, 7, 14)).tier == ""
