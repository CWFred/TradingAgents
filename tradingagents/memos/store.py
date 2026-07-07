"""SQLite-backed store for structured investment memos.

Follows the conventions of ``ops.journal``: stdlib sqlite3, a process-wide
lock, ISO-8601 UTC timestamps stored as TEXT. The full memo is stored as its
Pydantic JSON payload; the columns exist purely as query indexes so the corpus
can be sliced (by ticker, thesis type, status, resolution outcome) without
deserializing everything.

The store is deliberately separate from the trading journal: the journal is
the source of truth for *money* (orders, fills, equity), the memo store is the
source of truth for *reasoning*. A position links to its memo by ``memo_id``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from tradingagents.memos.schema import Memo, MemoStatus, Resolution, ThesisType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memos (
    memo_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    thesis_type TEXT NOT NULL,
    status TEXT NOT NULL,
    conviction_tier TEXT NOT NULL,
    created_at TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    resolved_at TEXT,
    outcome_label TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memos_ticker ON memos(ticker);
CREATE INDEX IF NOT EXISTS idx_memos_status ON memos(status);
CREATE INDEX IF NOT EXISTS idx_memos_type ON memos(thesis_type);
"""


def default_memo_store_path() -> str:
    """Default memo DB location, shared by ops config and the agent tools.

    Env override first so the tools and OpsConfig always agree on which
    corpus they are reading.
    """
    override = os.environ.get("OPS_MEMO_STORE_PATH")
    if override:
        return override
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(os.path.expanduser(base), "tradingagents", "memos.sqlite")


class MemoStore:
    """Append-mostly store: memos are written once, then resolved once."""

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # --- Write path ---

    def save(self, memo: Memo) -> None:
        """Insert a new memo. Rejects duplicates and mismatched thesis blocks."""
        if not memo.block_matches_type():
            raise ValueError(
                f"memo {memo.memo_id}: thesis_type={memo.thesis_type!r} does not match "
                "the populated thesis block (exactly one of value_block/event_block, "
                "matching the type, must be set)"
            )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memos (memo_id, ticker, thesis_type, status, conviction_tier,
                                   created_at, as_of_date, resolved_at, outcome_label, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memo.memo_id,
                    memo.ticker.upper(),
                    memo.thesis_type,
                    memo.status,
                    memo.conviction_tier,
                    memo.created_at.isoformat(),
                    memo.as_of_date.isoformat(),
                    memo.resolution.resolved_at.isoformat() if memo.resolution else None,
                    memo.resolution.outcome_label if memo.resolution else None,
                    memo.model_dump_json(),
                ),
            )

    def resolve(self, memo_id: str, resolution: Resolution) -> Memo:
        """Attach a resolution to an open/passed memo and mark it resolved."""
        memo = self.get(memo_id)
        if memo is None:
            raise KeyError(f"no memo with id {memo_id!r}")
        if memo.status == "resolved":
            raise ValueError(f"memo {memo_id!r} is already resolved")
        memo.status = "resolved"
        memo.resolution = resolution
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE memos
                SET status = 'resolved', resolved_at = ?, outcome_label = ?, payload = ?
                WHERE memo_id = ?
                """,
                (
                    resolution.resolved_at.isoformat(),
                    resolution.outcome_label,
                    memo.model_dump_json(),
                    memo_id,
                ),
            )
        return memo

    def mark_passed(self, memo_id: str) -> None:
        """Mark a memo as researched-but-not-bought. Passed memos are still
        shadow-tracked and resolved later — selection skill is measured by
        comparing bought vs passed outcomes, so passing is data, not discard."""
        memo = self.get(memo_id)
        if memo is None:
            raise KeyError(f"no memo with id {memo_id!r}")
        if memo.status == "resolved":
            raise ValueError(f"memo {memo_id!r} is already resolved")
        memo.status = "passed"
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE memos SET status = 'passed', payload = ? WHERE memo_id = ?",
                (memo.model_dump_json(), memo_id),
            )

    # --- Read path ---

    def get(self, memo_id: str) -> Memo | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM memos WHERE memo_id = ?", (memo_id,)
            ).fetchone()
        return Memo.model_validate_json(row["payload"]) if row else None

    def list(
        self,
        *,
        ticker: str | None = None,
        status: MemoStatus | None = None,
        thesis_type: ThesisType | None = None,
    ) -> list[Memo]:
        """List memos newest-first, optionally filtered."""
        clauses, params = [], []
        if ticker is not None:
            clauses.append("ticker = ?")
            params.append(ticker.upper())
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if thesis_type is not None:
            clauses.append("thesis_type = ?")
            params.append(thesis_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload FROM memos {where} ORDER BY created_at DESC", params
            ).fetchall()
        return [Memo.model_validate_json(r["payload"]) for r in rows]

    def open_memos(self) -> list[Memo]:
        """Memos backing live positions — the monitoring loop's work list."""
        return self.list(status="open")

    def resolved_corpus(self) -> list[Memo]:
        """All resolved memos, oldest-first: the calibration/training dataset."""
        memos = self.list(status="resolved")
        return sorted(memos, key=lambda m: m.created_at)

    def due_for_resolution(self, as_of: datetime | None = None) -> list[Memo]:
        """Open/passed memos whose expected holding period has elapsed.

        These are not auto-resolved — resolution needs realized prices and a
        judgment call on the outcome label — but they must not linger silently,
        so the scheduler surfaces them.
        """
        now = as_of or datetime.now(timezone.utc)
        due = []
        for memo in self.list(status="open") + self.list(status="passed"):
            elapsed_days = (now - memo.created_at).days
            if elapsed_days >= memo.expected_holding_months * 30:
                due.append(memo)
        return due
