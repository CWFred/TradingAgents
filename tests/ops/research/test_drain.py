"""Unit tests for the deadline/shutdown-boxed research drain."""
from datetime import datetime, timezone

import pytest

from ops.research.brain import ResearchError, ResearchOutcome
from ops.research.drain import DrainSummary, drain_pending

pytestmark = pytest.mark.unit


class FakeStore:
    def __init__(self, symbols):
        self._hits = [{"id": i, "symbol": s} for i, s in enumerate(symbols, 1)]
        self.researched, self.failed = [], []

    def pending_hits(self):
        done = set(self.researched) | set(self.failed)
        return [h for h in self._hits if h["id"] not in done]

    def mark_researched(self, hid):
        self.researched.append(hid)

    def mark_failed(self, hid):
        self.failed.append(hid)


def _outcome(hit, status):
    return ResearchOutcome(symbol=hit["symbol"], hit_id=hit["id"], status=status)


def test_drains_whole_queue(monkeypatch):
    store = FakeStore(["AAA", "BBB", "CCC"])
    monkeypatch.setattr(
        "ops.research.drain.research_hit",
        lambda hit, **kw: _outcome(hit, "researched"),
    )
    summary = drain_pending(
        store=store, memo_store=object(), evidence_llm=None, thesis_llm=None,
        thesis_model_spec="spec",
    )
    assert summary == DrainSummary(researched=3, failed=0, still_pending=0,
                                   hit_deadline=False)
    assert store.researched == [1, 2, 3]


def test_deadline_stops_between_names(monkeypatch):
    store = FakeStore(["AAA", "BBB", "CCC"])
    calls = {"n": 0}

    def fake_hit(hit, **kw):
        calls["n"] += 1
        return _outcome(hit, "researched")

    monkeypatch.setattr("ops.research.drain.research_hit", fake_hit)
    base = datetime(2026, 7, 9, 6, tzinfo=timezone.utc)
    deadline = datetime(2026, 7, 9, 8, tzinfo=timezone.utc)
    # now() returns 06:00 for the first check, 09:00 (past deadline) after.
    times = iter([base, datetime(2026, 7, 9, 9, tzinfo=timezone.utc)])
    summary = drain_pending(
        store=store, memo_store=object(), evidence_llm=None, thesis_llm=None,
        thesis_model_spec="spec", deadline=deadline, now=lambda: next(times),
    )
    assert calls["n"] == 1
    assert summary.researched == 1
    assert summary.still_pending == 2
    assert summary.hit_deadline is True


def test_should_stop_halts(monkeypatch):
    store = FakeStore(["AAA", "BBB"])
    monkeypatch.setattr(
        "ops.research.drain.research_hit",
        lambda hit, **kw: _outcome(hit, "researched"),
    )
    summary = drain_pending(
        store=store, memo_store=object(), evidence_llm=None, thesis_llm=None,
        thesis_model_spec="spec", should_stop=lambda: True,
    )
    assert summary.researched == 0
    assert summary.still_pending == 2


def test_failed_outcome_marks_failed(monkeypatch):
    store = FakeStore(["AAA"])
    calls = {"n": 0}

    def fake_hit(hit, **kw):
        calls["n"] += 1
        return _outcome(hit, "failed")

    monkeypatch.setattr("ops.research.drain.research_hit", fake_hit)
    summary = drain_pending(
        store=store, memo_store=object(), evidence_llm=None, thesis_llm=None,
        thesis_model_spec="spec",
    )
    assert calls["n"] == 1
    assert summary.failed == 1
    assert store.failed == [1]


def test_exception_marks_failed_and_continues(monkeypatch):
    monkeypatch.setattr("ops.research.drain.time.sleep", lambda s: None)
    store = FakeStore(["AAA", "BBB"])

    def fake_hit(hit, **kw):
        if hit["symbol"] == "AAA":
            raise RuntimeError("boom")
        return _outcome(hit, "researched")

    monkeypatch.setattr("ops.research.drain.research_hit", fake_hit)
    summary = drain_pending(
        store=store, memo_store=object(), evidence_llm=None, thesis_llm=None,
        thesis_model_spec="spec",
    )
    assert store.failed == [1]
    assert store.researched == [2]
    assert summary == DrainSummary(researched=1, failed=1, still_pending=0,
                                   hit_deadline=False)


def test_retries_transient_exception_then_succeeds(monkeypatch):
    store = FakeStore(["AAA"])
    calls = {"n": 0}

    def flaky(hit, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("too many open files")
        return _outcome(hit, "researched")

    monkeypatch.setattr("ops.research.drain.research_hit", flaky)
    monkeypatch.setattr("ops.research.drain.time.sleep", lambda s: None)
    summary = drain_pending(
        store=store, memo_store=object(), evidence_llm=None, thesis_llm=None,
        thesis_model_spec="spec",
    )

    assert calls["n"] == 3
    assert summary == DrainSummary(researched=1, failed=0, still_pending=0,
                                   hit_deadline=False)
    assert store.researched == [1]


def test_exhausts_retries_then_marks_failed(monkeypatch):
    store = FakeStore(["AAA"])
    calls = {"n": 0}

    def always_flaky(hit, **kw):
        calls["n"] += 1
        raise OSError("too many open files")

    monkeypatch.setattr("ops.research.drain.research_hit", always_flaky)
    monkeypatch.setattr("ops.research.drain.time.sleep", lambda s: None)
    summary = drain_pending(
        store=store, memo_store=object(), evidence_llm=None, thesis_llm=None,
        thesis_model_spec="spec",
    )

    assert calls["n"] == 3
    assert store.failed == [1]
    assert summary.failed == 1


def test_should_stop_during_retry_leaves_ticker_pending(monkeypatch):
    store = FakeStore(["AAA", "BBB"])
    calls = {"n": 0}

    def flaky(hit, **kw):
        calls["n"] += 1
        raise RuntimeError("boom")

    # First should_stop() call (before AAA starts) is False; the second
    # (the retry check before attempt 2) is True, as if an operator pause
    # landed during the backoff sleep.
    stop_flags = iter([False, True])

    monkeypatch.setattr("ops.research.drain.research_hit", flaky)
    monkeypatch.setattr("ops.research.drain.time.sleep", lambda s: None)
    summary = drain_pending(
        store=store, memo_store=object(), evidence_llm=None, thesis_llm=None,
        thesis_model_spec="spec", should_stop=lambda: next(stop_flags, True),
    )

    assert calls["n"] == 1
    assert store.failed == []
    assert store.researched == []
    assert summary.still_pending == 2


def test_reaps_resources_after_each_attempt(monkeypatch):
    monkeypatch.setattr("ops.research.drain.time.sleep", lambda s: None)
    store = FakeStore(["AAA", "BBB"])
    reaps = []

    def fake_hit(hit, **kw):
        if hit["symbol"] == "AAA":
            raise RuntimeError("boom")
        return _outcome(hit, "researched")

    monkeypatch.setattr("ops.research.drain.research_hit", fake_hit)
    monkeypatch.setattr("ops.research.drain.gc.collect", lambda: reaps.append(True))

    drain_pending(
        store=store, memo_store=object(), evidence_llm=None, thesis_llm=None,
        thesis_model_spec="spec",
    )

    assert reaps == [True, True]


def test_interrupted_name_stays_pending_when_pause_lands(monkeypatch):
    store = FakeStore(["AAA", "BBB"])
    paused = {"value": False}

    def interrupted(_hit, **_kw):
        paused["value"] = True
        raise RuntimeError("model connection closed")

    monkeypatch.setattr("ops.research.drain.research_hit", interrupted)
    summary = drain_pending(
        store=store, memo_store=object(), evidence_llm=None, thesis_llm=None,
        thesis_model_spec="spec", should_stop=lambda: paused["value"],
    )

    assert summary == DrainSummary(0, 0, 2, False)
    assert store.failed == []
    assert store.researched == []


def test_swallowed_model_error_outcome_stays_pending_when_paused(monkeypatch):
    store = FakeStore(["AAA"])
    paused = {"value": False}

    def interrupted(hit, **_kw):
        paused["value"] = True
        return _outcome(hit, "failed")

    monkeypatch.setattr("ops.research.drain.research_hit", interrupted)
    summary = drain_pending(
        store=store, memo_store=object(), evidence_llm=None, thesis_llm=None,
        thesis_model_spec="spec", should_stop=lambda: paused["value"],
    )

    assert summary == DrainSummary(0, 0, 1, False)
    assert store.failed == []


def test_research_error_propagates(monkeypatch):
    store = FakeStore(["AAA"])
    calls = {"n": 0}

    def fake_hit(hit, **kw):
        calls["n"] += 1
        raise ResearchError("config problem")

    monkeypatch.setattr("ops.research.drain.research_hit", fake_hit)
    with pytest.raises(ResearchError):
        drain_pending(
            store=store, memo_store=object(), evidence_llm=None,
            thesis_llm=None, thesis_model_spec="spec",
        )
    assert calls["n"] == 1


def test_max_names_caps_batch(monkeypatch):
    store = FakeStore(["AAA", "BBB", "CCC"])
    monkeypatch.setattr(
        "ops.research.drain.research_hit",
        lambda hit, **kw: _outcome(hit, "researched"),
    )
    summary = drain_pending(
        store=store, memo_store=object(), evidence_llm=None, thesis_llm=None,
        thesis_model_spec="spec", max_names=2,
    )
    assert summary.researched == 2
    assert summary.still_pending == 1
