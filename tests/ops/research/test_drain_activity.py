"""drain_pending emits item breadcrumbs per attempted hit."""
import pytest

from ops import events
from ops.activity import ActivityReporter
from ops.journal import Journal
from ops.research.drain import drain_pending


class _Store:
    def __init__(self, hits):
        self._hits = list(hits)
        self.failed = []

    def pending_hits(self):
        return list(self._hits)

    def mark_researched(self, hit_id):
        self._hits = [h for h in self._hits if h["id"] != hit_id]

    def mark_failed(self, hit_id):
        self.failed.append(hit_id)
        self._hits = [h for h in self._hits if h["id"] != hit_id]


class _Outcome:
    status = "researched"
    symbol = "AAA"
    memo_id = "m1"
    recommendation = "Buy"
    evidence_kept = 1
    evidence_dropped = 0


@pytest.fixture()
def journal(tmp_path):
    j = Journal(str(tmp_path / "j.db"))
    yield j
    j.close()


def test_items_emitted_with_seq_over_chunk_total(journal):
    store = _Store([{"id": 1, "symbol": "AAA"}, {"id": 2, "symbol": "BBB"}])
    drain_pending(
        store=store, memo_store=None, evidence_llm=None, thesis_llm=None,
        thesis_model_spec="", reporter=ActivityReporter(journal),
        research_fn=lambda hit, **kw: _Outcome(),
    )
    starts = [e["payload"] for e in journal.read_events()
              if e["kind"] == events.KIND_ACTIVITY_STARTED]
    assert starts[0] == {"scope": "item", "job": "overnight",
                         "stage": "researching", "symbol": "AAA", "seq": "1/2"}
    assert starts[1]["symbol"] == "BBB" and starts[1]["seq"] == "2/2"


def test_failed_name_finishes_not_ok_and_queue_continues(journal):
    store = _Store([{"id": 1, "symbol": "BAD"}, {"id": 2, "symbol": "AAA"}])

    def research(hit, **kw):
        if hit["symbol"] == "BAD":
            raise RuntimeError("boom")
        return _Outcome()

    summary = drain_pending(
        store=store, memo_store=None, evidence_llm=None, thesis_llm=None,
        thesis_model_spec="", reporter=ActivityReporter(journal),
        research_fn=research,
    )
    assert summary.failed == 1 and summary.researched == 1
    fins = [e["payload"] for e in journal.read_events()
            if e["kind"] == events.KIND_ACTIVITY_FINISHED]
    assert [f["ok"] for f in fins] == [False, True]
