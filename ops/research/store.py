"""SQLite store for screen runs and hits — the deep-research queue.

Follows the conventions of ``tradingagents/memos/store.py``: stdlib sqlite3,
a process-wide lock, ISO-8601 UTC TEXT timestamps, full payload as JSON with
columns as query indexes only.

Hit lifecycle: ``pending`` (awaiting deep research — build-order step 5
consumes these) -> ``researched`` (a memo exists) | ``failed`` (research
rejected) | ``expired`` (went stale). A pending symbol is never duplicated by
later runs; once researched/failed/expired it may be queued again by a fresh
screen pass.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from ops.research.screener import ScreenResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screen_runs (
    run_id TEXT PRIMARY KEY,
    asof TEXT NOT NULL,
    created_at TEXT NOT NULL,
    universe_size INTEGER NOT NULL,
    passed_count INTEGER NOT NULL,
    coverage TEXT
);
CREATE TABLE IF NOT EXISTS screen_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asof TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_hits_status ON screen_hits(status);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScreenStore:
    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(screen_runs)")}
            if "coverage" not in cols:
                conn.execute("ALTER TABLE screen_runs ADD COLUMN coverage TEXT")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def record_run(
        self, *, asof: date, universe_size: int, results: list[ScreenResult],
        coverage: dict | None = None,
    ) -> str:
        run_id = f"screen-{asof.isoformat()}-{uuid4().hex[:8]}"
        passed = [r for r in results if r.passed]
        now = _now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO screen_runs (run_id, asof, created_at, universe_size, passed_count, coverage)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, asof.isoformat(), now, universe_size, len(passed),
                 json.dumps(coverage) if coverage is not None else None),
            )
            for result in passed:
                already_pending = conn.execute(
                    "SELECT 1 FROM screen_hits WHERE symbol = ? AND status = 'pending' LIMIT 1",
                    (result.symbol,),
                ).fetchone()
                if already_pending:
                    continue
                conn.execute(
                    "INSERT INTO screen_hits (run_id, symbol, asof, status, payload, created_at)"
                    " VALUES (?, ?, ?, 'pending', ?, ?)",
                    (
                        run_id, result.symbol, asof.isoformat(),
                        json.dumps(asdict(result), default=str), now,
                    ),
                )
        return run_id

    def pending_hits(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, run_id, symbol, asof, status, payload FROM screen_hits"
                " WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        return [
            {
                "id": r["id"], "run_id": r["run_id"], "symbol": r["symbol"],
                "asof": r["asof"], "status": r["status"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    def _set_status(self, hit_id: int, status: str) -> None:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE screen_hits SET status = ? WHERE id = ?", (status, hit_id)
            )
            if cur.rowcount == 0:
                raise KeyError(f"no screen hit with id {hit_id!r}")

    def mark_researched(self, hit_id: int) -> None:
        self._set_status(hit_id, "researched")

    def mark_expired(self, hit_id: int) -> None:
        self._set_status(hit_id, "expired")

    def mark_failed(self, hit_id: int) -> None:
        """Deep research rejected this hit's memo (weak-model guardrails).

        Surfaced for human review via `ops research run` output; a later
        screen pass may queue the symbol fresh.
        """
        self._set_status(hit_id, "failed")

    def last_run(self) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id, asof, created_at, universe_size, passed_count, coverage"
                " FROM screen_runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("coverage"):
            d["coverage"] = json.loads(d["coverage"])
        else:
            d["coverage"] = None
        return d
