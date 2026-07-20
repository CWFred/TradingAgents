"""SQLite signal store for the insider sleeve.

Follows the conventions of tradingagents/memos/store.py: stdlib sqlite3, a
process-wide lock, ISO-8601 TEXT timestamps, money as TEXT of the Decimal.
Three tables:

- insider_transactions: raw Form 4 rows from the daily-index scan, upserted
  by (accession, insider_name, transaction_date, shares) so re-scanning a
  day is idempotent.
- sleeve_entries: one row per position the sleeve opened, carrying the
  memo_id once the overnight memo-lite pass authors it ('' until then) —
  this table IS the overnight memo queue and the 90-day cooldown source.
- scan_state: the daily-index watermark (last day ingested).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS insider_transactions (
    symbol TEXT NOT NULL, insider_name TEXT NOT NULL, insider_title TEXT,
    is_director INTEGER NOT NULL, is_officer INTEGER NOT NULL,
    transaction_date TEXT, code TEXT NOT NULL, ten_b5_1 INTEGER NOT NULL,
    shares TEXT, price TEXT, accession TEXT NOT NULL, filed_date TEXT NOT NULL,
    UNIQUE(accession, insider_name, transaction_date, shares)
);
CREATE INDEX IF NOT EXISTS idx_txn_symbol_date
    ON insider_transactions(symbol, transaction_date);
CREATE TABLE IF NOT EXISTS sleeve_entries (
    symbol TEXT NOT NULL, asof TEXT NOT NULL, memo_id TEXT NOT NULL DEFAULT '',
    UNIQUE(symbol, asof)
);
CREATE TABLE IF NOT EXISTS scan_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

_WATERMARK_KEY = "daily_index_watermark"


class SignalStore:
    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # --- transactions ---

    def record_transactions(self, symbol: str, txns) -> int:
        """Upsert Form 4 rows; returns how many were actually new."""
        inserted = 0
        with self._lock, closing(self._connect()) as conn, conn:
            for t in txns:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO insider_transactions"
                    " (symbol, insider_name, insider_title, is_director,"
                    "  is_officer, transaction_date, code, ten_b5_1, shares,"
                    "  price, accession, filed_date)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        symbol.upper(), t.insider_name, t.insider_title,
                        int(t.is_director), int(t.is_officer),
                        t.transaction_date.isoformat() if t.transaction_date else None,
                        t.code, int(t.ten_b5_1),
                        str(t.shares) if t.shares is not None else None,
                        str(t.price) if t.price is not None else None,
                        t.accession, t.filed_date.isoformat(),
                    ),
                )
                inserted += cur.rowcount
        return inserted

    def buys_in_window(self, symbol: str, *, since: date, until: date) -> list[dict]:
        """Open-market (code P), non-10b5-1 buys for ``symbol`` in the window."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT insider_name, insider_title, is_director, is_officer,"
                "       transaction_date, shares, price, accession"
                " FROM insider_transactions"
                " WHERE symbol = ? AND code = 'P' AND ten_b5_1 = 0"
                "   AND transaction_date IS NOT NULL"
                "   AND transaction_date >= ? AND transaction_date <= ?"
                " ORDER BY transaction_date",
                (symbol.upper(), since.isoformat(), until.isoformat()),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["transaction_date"] = date.fromisoformat(d["transaction_date"])
            d["shares"] = Decimal(d["shares"]) if d["shares"] is not None else None
            d["price"] = Decimal(d["price"]) if d["price"] is not None else None
            out.append(d)
        return out

    def symbols_with_new_buys(self, *, since: date) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM insider_transactions"
                " WHERE code = 'P' AND ten_b5_1 = 0"
                "   AND transaction_date IS NOT NULL AND transaction_date >= ?"
                " ORDER BY symbol",
                (since.isoformat(),),
            ).fetchall()
        return [r["symbol"] for r in rows]

    # --- entries / cooldown / memo queue ---

    def record_entry(self, symbol: str, *, asof: date, memo_id: str = "") -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO sleeve_entries (symbol, asof, memo_id)"
                " VALUES (?, ?, ?)",
                (symbol.upper(), asof.isoformat(), memo_id),
            )

    def last_entry_date(self, symbol: str) -> date | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT MAX(asof) AS asof FROM sleeve_entries WHERE symbol = ?",
                (symbol.upper(),),
            ).fetchone()
        return date.fromisoformat(row["asof"]) if row and row["asof"] else None

    def entries_without_memo(self) -> list[dict]:
        """The overnight memo-lite queue: entries whose memo isn't authored yet."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT symbol, asof FROM sleeve_entries WHERE memo_id = ''"
                " ORDER BY asof",
            ).fetchall()
        return [{"symbol": r["symbol"], "asof": date.fromisoformat(r["asof"])}
                for r in rows]

    def entry_memo_id(self, symbol: str, asof: date) -> str:
        """The memo authored for this entry, or "" while still queued.

        The journal's insider_position_opened payload is written at entry
        time — BEFORE the overnight memo pass exists — and journal events
        are immutable, so this table is the only live source of the id.
        The exit pass must read it from here (review finding P1)."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT memo_id FROM sleeve_entries WHERE symbol = ? AND asof = ?",
                (symbol.upper(), asof.isoformat()),
            ).fetchone()
        return row["memo_id"] if row else ""

    def set_entry_memo(self, symbol: str, asof: date, memo_id: str) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE sleeve_entries SET memo_id = ? WHERE symbol = ? AND asof = ?",
                (memo_id, symbol.upper(), asof.isoformat()),
            )

    # --- scan watermark ---

    def scan_watermark(self) -> date | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT v FROM scan_state WHERE k = ?", (_WATERMARK_KEY,),
            ).fetchone()
        return date.fromisoformat(row["v"]) if row else None

    def set_scan_watermark(self, d: date) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO scan_state (k, v) VALUES (?, ?)"
                " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (_WATERMARK_KEY, d.isoformat()),
            )
