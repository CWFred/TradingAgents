"""Derive the dashboard's activity section from breadcrumb events.

Read-only (ro_conn), same isolation contract as the other snapshot
sections. Everything is computed from the last ~300 activity/service
events: `current` from the newest dangling start, `recent_runs` from
job-scope start/finish pairs, interruption from service_started markers."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Any

from ops import events
from ops.dashboard.snapshot import ro_conn

# A single item never legitimately runs this long; a dangling start older
# than this is a crash artifact, not live work.
MAX_CURRENT_AGE_S = 4 * 3600.0
_FETCH_LIMIT = 300
_MAX_RUNS = 20


def _rows(config) -> list[dict[str, Any]]:
    """Newest-first activity + service_started rows."""
    try:
        with closing(ro_conn(config.journal_path)) as conn:
            rows = conn.execute(
                "SELECT id, at, kind, payload FROM events WHERE kind IN (?,?,?)"
                " ORDER BY id DESC LIMIT ?",
                (events.KIND_ACTIVITY_STARTED, events.KIND_ACTIVITY_FINISHED,
                 events.KIND_SERVICE_STARTED, _FETCH_LIMIT),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except (TypeError, ValueError):
            payload = {}
        out.append({"id": r["id"], "at": datetime.fromisoformat(r["at"]),
                    "kind": r["kind"], "payload": payload})
    return out


def _as_current(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    p = row["payload"]
    return {
        "job": p.get("job"), "stage": p.get("stage"),
        "symbol": p.get("symbol"), "seq": p.get("seq"),
        "reason": p.get("reason"), "started_at": row["at"],
        "age_seconds": (now - row["at"]).total_seconds(),
    }


def _find_current(rows: list[dict[str, Any]], now: datetime) -> tuple[dict | None, bool]:
    """(current, is_dangling): newest activity event decides; item finishes
    fall back to the still-open enclosing job start.

    A service_started row closes anything older than it: a restart means
    the process that owned a dangling start is dead, so that start must
    not resurface as "current" (recent_runs already reports it as
    interrupted). We only reach a service_started row here if it's newer
    than any activity event we've inspected so far, which is exactly the
    "the dangling work died before this restart" case."""
    for row in rows:
        if row["kind"] == events.KIND_SERVICE_STARTED:
            return None, False  # restart is newer than any dangling start -> idle
        p = row["payload"]
        if row["kind"] == events.KIND_ACTIVITY_STARTED:
            return _as_current(row, now), True
        if p.get("scope") == "job":
            return None, False  # job finished cleanly -> idle
        # item finished; is its job still open? (job start w/o later job
        # finish, and w/o an intervening service restart)
        for r2 in rows:
            if r2["id"] >= row["id"]:
                continue
            if r2["kind"] == events.KIND_SERVICE_STARTED:
                return None, False  # job start is on the far side of a restart -> dead
            p2 = r2["payload"]
            if p2.get("scope") != "job":
                continue
            if r2["kind"] == events.KIND_ACTIVITY_STARTED:
                return _as_current(r2, now), True
            return None, False  # newest job-scope event is a finish -> idle
        return None, False
    return None, False


def _recent_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Job starts newest-first, joined to the next finish for the same job.
    rows is newest-first; walk chronologically (reversed) tracking one open
    run per job name."""
    runs: list[dict[str, Any]] = []
    open_runs: dict[str, dict[str, Any]] = {}
    for row in reversed(rows):
        p = row["payload"]
        if row["kind"] == events.KIND_SERVICE_STARTED:
            for run in open_runs.values():
                run["ok"] = False
                run["outcome"] = "interrupted"
                run["_closed"] = True
            open_runs.clear()
            continue
        if p.get("scope") != "job":
            continue
        if row["kind"] == events.KIND_ACTIVITY_STARTED:
            run = {"job": p.get("job"), "reason": p.get("reason"),
                   "started_at": row["at"], "finished_at": None, "ok": None,
                   "duration_s": None, "outcome": None}
            # a same-job start while one is open supersedes it (crash w/o
            # a service_started in the fetch window)
            prev = open_runs.get(run["job"])
            if prev is not None and not prev.get("_closed"):
                prev["ok"] = False
                prev["outcome"] = "interrupted"
            open_runs[run["job"]] = run
            runs.append(run)
        else:
            run = open_runs.pop(p.get("job"), None)
            if run is not None:
                run["finished_at"] = row["at"]
                run["ok"] = bool(p.get("ok"))
                run["duration_s"] = p.get("duration_s")
                run["outcome"] = p.get("outcome")
    for run in runs:
        run.pop("_closed", None)
    runs.reverse()
    return runs[:_MAX_RUNS]


def activity_section(config, now: datetime, *, health_verdict: str) -> dict[str, Any]:
    from ops.dashboard.forecast import next_work

    rows = _rows(config)
    current, dangling = _find_current(rows, now)
    stale = False
    if (dangling and current is not None
            and (health_verdict != "RUNNING"
                 or current["age_seconds"] > MAX_CURRENT_AGE_S)):
        current, stale = None, True
    return {
        "current": current, "stale": stale, "recent_runs": _recent_runs(rows),
        "next_work": next_work(config, now=now),
    }
