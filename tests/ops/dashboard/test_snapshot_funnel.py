"""Funnel: memo/screen column queries (ro), overnight run views, 7d signals."""
import sqlite3
from datetime import date

from ops import events
from ops.config import OpsConfig
from ops.dashboard.snapshot import build_snapshot
from ops.journal import Journal
from ops.research.store import ScreenStore  # seeding only
from tradingagents.memos.store import MemoStore  # seeding only


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


def _seed_memos_raw(path: str) -> None:
    # Raw INSERT: the dashboard reads columns only, so tests need not
    # build full pydantic Memos.
    MemoStore(path)  # creates schema
    conn = sqlite3.connect(path)
    for i, status in enumerate(["open", "open", "passed", "rejected"]):
        conn.execute(
            "INSERT INTO memos (memo_id, ticker, thesis_type, status,"
            " conviction_tier, created_at, as_of_date, payload)"
            " VALUES (?, ?, 'value', ?, 'core', ?, '2026-07-01', '{}')",
            (f"m{i}", f"TK{i}", status, f"2026-07-0{i + 1}T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()


def test_memo_counts_and_open_list(tmp_path):
    cfg = _config(tmp_path)
    _seed_memos_raw(cfg.memo_store_path)
    ScreenStore(cfg.screen_store_path)  # empty but present
    Journal(cfg.journal_path).close()
    funnel = build_snapshot(cfg)["funnel"]
    assert funnel["memos"]["by_status"] == {"open": 2, "passed": 1, "rejected": 1}
    assert [m["ticker"] for m in funnel["memos"]["open"]] == ["TK1", "TK0"]  # newest first


def test_screener_last_run_and_hit_counts(tmp_path):
    cfg = _config(tmp_path)
    _seed_memos_raw(cfg.memo_store_path)
    store = ScreenStore(cfg.screen_store_path)
    # record_run's real signature (ops/research/store.py:84) takes
    # asof: date + results: list[ScreenResult] and MINTS the run_id; there
    # are no run_id/passed_count/hits kwargs. Assert against the returned id.
    run_id = store.record_run(asof=date(2026, 7, 11), universe_size=500, results=[])
    Journal(cfg.journal_path).close()
    funnel = build_snapshot(cfg)["funnel"]
    assert funnel["screener"]["last_run"]["run_id"] == run_id
    assert funnel["screener"]["last_run"]["universe_size"] == 500


def test_overnight_runs_and_signals(tmp_path):
    cfg = _config(tmp_path)
    _seed_memos_raw(cfg.memo_store_path)
    ScreenStore(cfg.screen_store_path)
    # Overnight/monitor events live in the MAIN ops journal (the service's
    # ticks write there); the research journal holds only sleeve money.
    with Journal(cfg.journal_path) as j:
        j.record_event(events.KIND_RESEARCH_VETTING_RUN, {"vetted": 3, "passed": 1})
        j.record_event(events.KIND_FALSIFIER_TRIPPED, {"memo_id": "m0"})
    funnel = build_snapshot(cfg)["funnel"]
    assert funnel["overnight"]["last_vetting_run"]["payload"]["vetted"] == 3
    assert funnel["overnight"]["last_drain_run"] is None
    assert funnel["signals_7d"]["falsifier_tripped"] == 1
    assert funnel["overnight"]["paused"] is False


def test_missing_memo_store_isolated(tmp_path):
    cfg = _config(tmp_path)  # nothing seeded
    snap = build_snapshot(cfg)
    assert "error" in snap["funnel"]
