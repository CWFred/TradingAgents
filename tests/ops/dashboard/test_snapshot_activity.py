"""Snapshot activity section: current-work derivation + recent runs."""
from datetime import datetime, timedelta, timezone

import pytest

from ops import events
from ops.dashboard.activity_view import activity_section
from ops.journal import Journal


NOW = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)


@pytest.fixture()
def config(tmp_path):
    class _C:
        journal_path = str(tmp_path / "j.db")
    # touch the journal file so ro_conn can open it
    Journal(_C.journal_path).close()
    return _C()


def _journal(config):
    return Journal(config.journal_path)


def _start(j, *, scope, job, at, **kw):
    j.record_event(events.KIND_ACTIVITY_STARTED,
                   events.activity_started_payload(scope=scope, job=job, **kw),
                   at=at)


def _finish(j, *, scope, job, at, ok=True, duration_s=1.0, **kw):
    j.record_event(events.KIND_ACTIVITY_FINISHED,
                   events.activity_finished_payload(
                       scope=scope, job=job, ok=ok, duration_s=duration_s, **kw),
                   at=at)


def test_open_item_is_current(config):
    with _journal(config) as j:
        _start(j, scope="job", job="daily_cycle", at=NOW - timedelta(minutes=10),
               reason="attempt 1 of 3")
        _start(j, scope="item", job="daily_cycle", stage="analyzing",
               symbol="BAH", seq="3", at=NOW - timedelta(minutes=6))
    out = activity_section(config, NOW, health_verdict="RUNNING")
    assert out["current"]["symbol"] == "BAH"
    assert out["current"]["stage"] == "analyzing"
    assert out["current"]["age_seconds"] == 360.0
    assert out["stale"] is False


def test_item_finish_falls_back_to_open_job(config):
    with _journal(config) as j:
        _start(j, scope="job", job="overnight", at=NOW - timedelta(minutes=30),
               reason="2 hit(s) to research")
        _start(j, scope="item", job="overnight", stage="researching",
               symbol="AAA", at=NOW - timedelta(minutes=20))
        _finish(j, scope="item", job="overnight", stage="researching",
                symbol="AAA", at=NOW - timedelta(minutes=10))
    out = activity_section(config, NOW, health_verdict="RUNNING")
    assert out["current"]["job"] == "overnight"
    assert out["current"]["stage"] is None
    assert out["current"]["reason"] == "2 hit(s) to research"


def test_job_finish_means_idle(config):
    with _journal(config) as j:
        _start(j, scope="job", job="overnight", at=NOW - timedelta(hours=2))
        _finish(j, scope="job", job="overnight", at=NOW - timedelta(hours=1),
                outcome="researched 2, vetted 1, failed 0")
    out = activity_section(config, NOW, health_verdict="RUNNING")
    assert out["current"] is None
    assert out["stale"] is False


def test_dangling_start_with_dead_service_is_stale(config):
    with _journal(config) as j:
        _start(j, scope="item", job="daily_cycle", stage="analyzing",
               symbol="BAH", at=NOW - timedelta(minutes=5))
    out = activity_section(config, NOW, health_verdict="STOPPED")
    assert out["current"] is None
    assert out["stale"] is True


def test_dangling_start_older_than_cap_is_stale(config):
    with _journal(config) as j:
        _start(j, scope="item", job="daily_cycle", stage="analyzing",
               symbol="BAH", at=NOW - timedelta(hours=5))
    out = activity_section(config, NOW, health_verdict="RUNNING")
    assert out["current"] is None
    assert out["stale"] is True


def test_recent_runs_joined_and_interrupted(config):
    with _journal(config) as j:
        # run 1: clean
        _start(j, scope="job", job="overnight", at=NOW - timedelta(hours=12),
               reason="screened")
        _finish(j, scope="job", job="overnight", at=NOW - timedelta(hours=10),
                duration_s=7200.0, outcome="researched 3, vetted 1, failed 0")
        # run 2: interrupted by a restart
        _start(j, scope="job", job="daily_cycle", at=NOW - timedelta(hours=4),
               reason="attempt 1 of 3")
        j.record_event(events.KIND_SERVICE_STARTED, {"pid": 1},
                       at=NOW - timedelta(hours=3))
        # run 3: still open (current)
        _start(j, scope="job", job="daily_cycle", at=NOW - timedelta(minutes=10),
               reason="attempt 2 of 3, retrying failed cycle")
    out = activity_section(config, NOW, health_verdict="RUNNING")
    runs = out["recent_runs"]
    assert [r["job"] for r in runs] == ["daily_cycle", "daily_cycle", "overnight"]
    assert runs[0]["finished_at"] is None and runs[0]["ok"] is None
    assert runs[1]["ok"] is False and runs[1]["outcome"] == "interrupted"
    assert runs[2]["ok"] is True
    assert runs[2]["outcome"] == "researched 3, vetted 1, failed 0"
    assert runs[2]["duration_s"] == 7200.0


def test_missing_journal_returns_empty(config, tmp_path):
    config.journal_path = str(tmp_path / "missing.db")
    out = activity_section(config, NOW, health_verdict="UNKNOWN")
    assert out == {"current": None, "stale": False, "recent_runs": []}
