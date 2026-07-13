"""build_snapshot: health verdict, section isolation, JSON safety."""
import json
import os
from datetime import datetime, timedelta, timezone

from ops import events
from ops.config import OpsConfig
from ops.dashboard.snapshot import build_snapshot
from ops.journal import Journal


def _config(tmp_path) -> OpsConfig:
    return OpsConfig(
        journal_path=str(tmp_path / "ops.sqlite"),
        baseline_journal_path=str(tmp_path / "baseline.sqlite"),
        research_journal_path=str(tmp_path / "research.sqlite"),
        screen_store_path=str(tmp_path / "screen.sqlite"),
        memo_store_path=str(tmp_path / "memos.sqlite"),
        guardian_liveness_path=str(tmp_path / "guardian.alive"),
        research_pause_flag_path=str(tmp_path / "research.paused"),
    )


def _started_journal(cfg: OpsConfig) -> None:
    with Journal(cfg.journal_path) as j:
        j.record_event(events.KIND_SERVICE_STARTED, {"pid": 42, "broker_mode": "paper"})


def test_verdict_running_when_guardian_fresh(tmp_path):
    cfg = _config(tmp_path)
    _started_journal(cfg)
    open(cfg.guardian_liveness_path, "w").close()  # mtime = now
    snap = build_snapshot(cfg)
    assert snap["health"]["verdict"] == "RUNNING"
    assert snap["health"]["guardian"]["age_seconds"] < 60


def test_verdict_stale_when_guardian_old(tmp_path):
    cfg = _config(tmp_path)
    _started_journal(cfg)
    open(cfg.guardian_liveness_path, "w").close()
    os.utime(cfg.guardian_liveness_path, (1, 1))  # 1970
    assert build_snapshot(cfg)["health"]["verdict"] == "STALE"


def test_verdict_unknown_without_liveness_file(tmp_path):
    cfg = _config(tmp_path)
    _started_journal(cfg)
    snap = build_snapshot(cfg)
    assert snap["health"]["verdict"] == "UNKNOWN"
    assert snap["health"]["guardian"]["alive_at"] is None


def test_verdict_stopped_when_stopping_is_latest(tmp_path):
    cfg = _config(tmp_path)
    now = datetime.now(timezone.utc)
    with Journal(cfg.journal_path) as j:
        j.record_event(events.KIND_SERVICE_STARTED, {"pid": 1},
                       at=now - timedelta(hours=2))
        j.record_event(events.KIND_SERVICE_STOPPING, {"exit_code": 0},
                       at=now - timedelta(hours=1))
    open(cfg.guardian_liveness_path, "w").close()  # fresh file must not win
    snap = build_snapshot(cfg)
    assert snap["health"]["verdict"] == "STOPPED"
    assert snap["health"]["last_stopping"]["exit_code"] == 0


def test_missing_journal_isolated_to_health_section(tmp_path):
    cfg = _config(tmp_path)  # no journal file created at all
    snap = build_snapshot(cfg)
    assert "error" in snap["health"]
    assert "is_open" in snap["market"]  # market still built


def test_snapshot_is_json_serializable(tmp_path):
    cfg = _config(tmp_path)
    _started_journal(cfg)
    json.dumps(build_snapshot(cfg))  # must not raise


def test_research_paused_flag(tmp_path):
    cfg = _config(tmp_path)
    _started_journal(cfg)
    open(cfg.research_pause_flag_path, "w").close()
    assert build_snapshot(cfg)["health"]["research_paused"] is True
