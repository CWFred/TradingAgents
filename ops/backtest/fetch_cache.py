"""Persistent SQLite key-value cache for immutable-once-published fetch payloads.

EDGAR filing indexes, Form 4 XML digests, company facts, and price context
are fetched once and never change retroactively, so a screening sweep can
serve every subsequent read from disk instead of re-hitting the network.
This mirrors ``ops/backtest/prices.py::PriceCache`` (own SQLite file, WAL,
busy_timeout) but stores an opaque JSON payload per (namespace, key) rather
than typed price rows -- lifecycle is separate from backtest.sqlite so the
cache can be deleted at any time without touching recorded results.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_cache (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);
"""


def default_fetch_cache_path() -> str:
    """``<XDG_STATE_HOME>/tradingagents/fetch_cache.sqlite``.

    Deliberately a separate file from ``backtest.sqlite`` (see
    ``ops/config.py::_default_backtest_store_path``): the fetch cache is
    disposable and rebuilds itself on demand, while the backtest store holds
    recorded results that must never be casually deleted.
    """
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(os.path.expanduser(base), "tradingagents", "fetch_cache.sqlite")


class FetchCache:
    """Persistent SQLite cache for JSON-serializable fetch payloads."""

    def __init__(self, path: str | Path):
        self.db_path = Path(path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get(
        self,
        namespace: str,
        key: str,
        *,
        max_age: timedelta | None = None,
        now: datetime | None = None,
    ) -> Any | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json, fetched_at FROM fetch_cache "
                "WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        if row is None:
            return None
        if max_age is not None:
            if now is not None and now.tzinfo is None:
                raise ValueError("now must be timezone-aware")
            fetched_at = datetime.fromisoformat(row["fetched_at"])
            current = now if now is not None else datetime.now(timezone.utc)
            if current - fetched_at > max_age:
                return None
        return json.loads(row["payload_json"])

    def put(
        self,
        namespace: str,
        key: str,
        value: Any,
        *,
        now: datetime | None = None,
    ) -> None:
        if now is not None and now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        payload_json = json.dumps(value)
        fetched_at = (now if now is not None else datetime.now(timezone.utc)).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fetch_cache (namespace, key, payload_json, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    payload_json=excluded.payload_json, fetched_at=excluded.fetched_at
                """,
                (namespace, key, payload_json, fetched_at),
            )

    def get_or_fetch(
        self,
        namespace: str,
        key: str,
        fetch: Callable[[], Any],
        *,
        max_age: timedelta | None = None,
        now: datetime | None = None,
    ) -> Any:
        cached = self.get(namespace, key, max_age=max_age, now=now)
        if cached is not None:
            return cached
        value = fetch()
        self.put(namespace, key, value, now=now)
        return value
