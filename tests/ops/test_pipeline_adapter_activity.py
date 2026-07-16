"""Adapter emits item breadcrumbs around each propagate()."""
from datetime import date

import pytest

from ops import events
from ops.activity import ActivityReporter
from ops.journal import Journal
from ops.pipeline_adapter import TradingAgentsPipelineAdapter


class _FakeGraph:
    def __init__(self, fail_for=frozenset()):
        self.fail_for = fail_for

    def propagate(self, symbol, iso, research_memo_context=""):
        if symbol in self.fail_for:
            raise RuntimeError("graph blew up")
        return {}, "Buy"


class _Adapter(TradingAgentsPipelineAdapter):
    def __init__(self, fail_for=frozenset(), **kwargs):
        super().__init__(**kwargs)
        self._fake = _FakeGraph(fail_for)

    def _build_graph(self):
        return self._fake


@pytest.fixture()
def journal(tmp_path):
    j = Journal(str(tmp_path / "j.db"))
    yield j
    j.close()


def _activity(journal):
    return [(e["kind"], e["payload"]) for e in journal.read_events()
            if e["kind"].startswith("activity_")]


def test_propagate_emits_item_pair_with_ordinal_seq(journal):
    adapter = _Adapter(reporter=ActivityReporter(journal),
                       activity_job="daily_cycle")
    with adapter.session():
        adapter.propagate("BAH", date(2026, 7, 14))
        adapter.propagate("CRC", date(2026, 7, 14))
    evs = _activity(journal)
    starts = [p for k, p in evs if k == events.KIND_ACTIVITY_STARTED]
    assert starts[0] == {"scope": "item", "job": "daily_cycle",
                         "stage": "analyzing", "symbol": "BAH", "seq": "1"}
    assert starts[1]["symbol"] == "CRC" and starts[1]["seq"] == "2"
    finishes = [p for k, p in evs if k == events.KIND_ACTIVITY_FINISHED]
    assert all(p["ok"] is True for p in finishes)


def test_session_resets_seq(journal):
    adapter = _Adapter(reporter=ActivityReporter(journal),
                       activity_job="overnight", activity_stage="vetting")
    with adapter.session():
        adapter.propagate("AAA", date(2026, 7, 14))
    with adapter.session():
        adapter.propagate("BBB", date(2026, 7, 14))
    starts = [p for k, p in _activity(journal)
              if k == events.KIND_ACTIVITY_STARTED]
    assert [s["seq"] for s in starts] == ["1", "1"]
    assert starts[1]["stage"] == "vetting"


def test_failed_propagate_finishes_not_ok_and_reraises(journal):
    adapter = _Adapter(fail_for={"BAD"}, reporter=ActivityReporter(journal))
    with pytest.raises(RuntimeError):
        adapter.propagate("BAD", date(2026, 7, 14))
    finishes = [p for k, p in _activity(journal)
                if k == events.KIND_ACTIVITY_FINISHED]
    assert finishes[0]["ok"] is False


def test_default_reporter_is_null(journal):
    adapter = _Adapter()
    adapter.propagate("BAH", date(2026, 7, 14))  # must not raise
