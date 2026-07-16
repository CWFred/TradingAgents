"""ActivityReporter: journal-backed breadcrumb pairs; NullReporter no-ops."""
import pytest

from ops import events
from ops.activity import ActivityReporter, NullReporter
from ops.journal import Journal


@pytest.fixture()
def journal(tmp_path):
    j = Journal(str(tmp_path / "j.db"))
    yield j
    j.close()


def _activity_events(journal):
    return [e for e in journal.read_events()
            if e["kind"] in (events.KIND_ACTIVITY_STARTED,
                             events.KIND_ACTIVITY_FINISHED)]


def test_job_emits_start_and_ok_finish_with_outcome(journal):
    reporter = ActivityReporter(journal)
    with reporter.job("daily_cycle", reason="attempt 1 of 3") as h:
        h.outcome = "analyzed 2, placed 1"
    evs = _activity_events(journal)
    assert [e["kind"] for e in evs] == ["activity_started", "activity_finished"]
    assert evs[0]["payload"] == {
        "scope": "job", "job": "daily_cycle", "reason": "attempt 1 of 3"}
    fin = evs[1]["payload"]
    assert fin["scope"] == "job" and fin["job"] == "daily_cycle"
    assert fin["ok"] is True
    assert fin["outcome"] == "analyzed 2, placed 1"
    assert fin["duration_s"] >= 0


def test_item_emits_pair_with_stage_symbol_seq(journal):
    reporter = ActivityReporter(journal)
    with reporter.item("overnight", stage="vetting", symbol="CRC", seq="2/5"):
        pass
    evs = _activity_events(journal)
    assert evs[0]["payload"] == {
        "scope": "item", "job": "overnight", "stage": "vetting",
        "symbol": "CRC", "seq": "2/5"}
    assert evs[1]["payload"]["ok"] is True


def test_exception_finishes_not_ok_and_reraises(journal):
    reporter = ActivityReporter(journal)
    with pytest.raises(ValueError):
        with reporter.job("overnight"):
            raise ValueError("boom")
    evs = _activity_events(journal)
    assert evs[1]["payload"]["ok"] is False


def test_reporter_swallows_journal_write_failure(journal, capsys):
    reporter = ActivityReporter(journal)
    journal.close()  # every record_event now raises
    with reporter.job("daily_cycle"):
        pass  # must not raise despite emit failures
    assert "activity emit failed" in capsys.readouterr().err


def test_null_reporter_noops():
    reporter = NullReporter()
    with reporter.job("daily_cycle", reason="x") as h:
        h.outcome = "ignored"
    with reporter.item("overnight", stage="vetting", symbol="A"):
        pass
