"""Append-only SQLite journal for the live-trading layer.

The journal is the source of truth for state recovery. Every state change
in the system MUST land here before any other side effect.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ops.trading_time import trading_day_start, trading_week_start

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    notional_dollars TEXT NOT NULL,
    stop_loss_price TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_coid ON orders(client_order_id);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    order_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    filled_at TEXT NOT NULL,
    stop_loss_price TEXT
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'manual',
    equity TEXT NOT NULL,
    cash TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS dispatch_cursors (
    consumer TEXT PRIMARY KEY,
    last_event_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cash_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount TEXT NOT NULL,
    note TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetimes are not allowed in the journal")
    return dt.isoformat()


def _from_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"stored datetime lacks timezone info: {s!r}")
    return dt


@dataclass(frozen=True)
class EquitySnapshot:
    at: datetime
    kind: str
    equity: Decimal
    cash: Decimal
    note: str | None


class Journal:
    def __init__(self, path: str):
        self._path = path
        # Non-reentrant: no Journal method ever calls another Journal method,
        # so a plain Lock (not RLock) cannot deadlock against itself. Journal
        # never calls back into GuardedBroker either, so the only lock
        # nesting order in the system is (GuardedBroker._lock outer, holds
        # it while delegating to inner broker + journal writes) wrapping
        # (Journal._lock inner) — never the reverse. See ops/broker/guarded.py.
        self._lock = threading.Lock()
        # Create the parent directory on open (not at import time) — the
        # default journal_path now lives under an XDG state directory
        # (~/.local/state/tradingagents/) that may not exist yet on first
        # run. A relative/CWD path has no parent to create (Path("x").parent
        # == Path(".")), so this is a harmless no-op in that case.
        parent = Path(path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)

            # Defensive migration for DBs created before kind/note existed.
            cur = self._conn.execute("PRAGMA table_info(equity_snapshots)")
            cols = {row[1] for row in cur.fetchall()}
            if "kind" not in cols:
                self._conn.execute(
                    "ALTER TABLE equity_snapshots ADD COLUMN kind TEXT NOT NULL DEFAULT 'manual'"
                )
            if "note" not in cols:
                self._conn.execute("ALTER TABLE equity_snapshots ADD COLUMN note TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_equity_kind_at ON equity_snapshots (kind, at)"
            )

            # Defensive migration for DBs created before stop_loss_price existed.
            cur = self._conn.execute("PRAGMA table_info(fills)")
            cols = {row[1] for row in cur.fetchall()}
            if "stop_loss_price" not in cols:
                self._conn.execute("ALTER TABLE fills ADD COLUMN stop_loss_price TEXT")

    def record_event(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (at, kind, payload) VALUES (?, ?, ?)",
                (_now_iso(), kind, json.dumps(payload, default=str)),
            )

    def read_events(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute("SELECT at, kind, payload FROM events ORDER BY id")
            rows = cur.fetchall()
        return [
            {"at": _from_iso(row[0]), "kind": row[1], "payload": json.loads(row[2])}
            for row in rows
        ]

    def read_events_since(self, min_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, at, kind, payload FROM events WHERE id > ? ORDER BY id"
        params: tuple[Any, ...] = (min_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (min_id, limit)
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
        return [
            {"id": row[0], "at": _from_iso(row[1]), "kind": row[2],
             "payload": json.loads(row[3])}
            for row in rows
        ]

    def get_cursor(self, consumer: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_event_id FROM dispatch_cursors WHERE consumer = ?",
                (consumer,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def set_cursor(self, consumer: str, last_event_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO dispatch_cursors (consumer, last_event_id) VALUES (?, ?)"
                " ON CONFLICT(consumer) DO UPDATE SET last_event_id = excluded.last_event_id",
                (consumer, last_event_id),
            )

    def record_order(
        self, *, client_order_id: str, symbol: str, side: str,
        notional_dollars: Decimal, stop_loss_price: Decimal | None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO orders (at, client_order_id, symbol, side, notional_dollars, stop_loss_price)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _now_iso(), client_order_id, symbol, side,
                    str(notional_dollars),
                    str(stop_loss_price) if stop_loss_price is not None else None,
                ),
            )

    def read_orders(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT at, client_order_id, symbol, side, notional_dollars, stop_loss_price"
                " FROM orders ORDER BY id"
            )
            rows = cur.fetchall()
        return [
            {
                "at": _from_iso(row[0]), "client_order_id": row[1],
                "symbol": row[2], "side": row[3],
                "notional_dollars": Decimal(row[4]),
                "stop_loss_price": Decimal(row[5]) if row[5] is not None else None,
            }
            for row in rows
        ]

    def record_fill(
        self, *, order_id: str, client_order_id: str, symbol: str, side: str,
        quantity: Decimal, price: Decimal, filled_at: datetime,
        stop_loss_price: Decimal | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO fills (at, order_id, client_order_id, symbol, side, quantity, price, filled_at, stop_loss_price)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now_iso(), order_id, client_order_id, symbol, side,
                    str(quantity), str(price), _to_iso(filled_at),
                    str(stop_loss_price) if stop_loss_price is not None else None,
                ),
            )

    def read_fills(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT at, order_id, client_order_id, symbol, side, quantity, price, filled_at, stop_loss_price"
                " FROM fills ORDER BY id"
            )
            rows = cur.fetchall()
        return [
            {
                "at": _from_iso(row[0]), "order_id": row[1],
                "client_order_id": row[2], "symbol": row[3], "side": row[4],
                "quantity": Decimal(row[5]), "price": Decimal(row[6]),
                "filled_at": _from_iso(row[7]),
                "stop_loss_price": Decimal(row[8]) if row[8] is not None else None,
            }
            for row in rows
        ]

    def last_buy_fill_for(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT at, order_id, client_order_id, symbol, side, quantity, price, filled_at, stop_loss_price"
                " FROM fills WHERE symbol = ? AND side = 'BUY' ORDER BY filled_at DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        if row is None:
            return None
        return {
            "at": _from_iso(row[0]), "order_id": row[1],
            "client_order_id": row[2], "symbol": row[3], "side": row[4],
            "quantity": Decimal(row[5]), "price": Decimal(row[6]),
            "filled_at": _from_iso(row[7]),
            "stop_loss_price": Decimal(row[8]) if row[8] is not None else None,
        }

    def has_event_today(self, kind: str, *, now: datetime | None = None) -> bool:
        when = now if now is not None else datetime.now(timezone.utc)
        start = trading_day_start(when)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM events WHERE kind = ? AND at >= ? LIMIT 1",
                (kind, _to_iso(start)),
            ).fetchone()
        return row is not None

    def has_event_since_last_monday(self, kind: str, *, now: datetime | None = None) -> bool:
        when = now if now is not None else datetime.now(timezone.utc)
        monday = trading_week_start(when)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM events WHERE kind = ? AND at >= ? LIMIT 1",
                (kind, _to_iso(monday)),
            ).fetchone()
        return row is not None

    def record_cash_adjustment(
        self, *, kind: str, amount: Decimal, note: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """Non-trade cash movement: startup seed, deposit, withdrawal, or the
        one-time live-mode baseline. Replay adds these to cash so the journal
        can account for money that did not arrive via a fill."""
        ts = _to_iso(at) if at is not None else _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO cash_adjustments (at, kind, amount, note) VALUES (?, ?, ?, ?)",
                (ts, kind, str(amount), note),
            )

    def read_cash_adjustments(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT at, kind, amount, note FROM cash_adjustments ORDER BY id"
            )
            rows = cur.fetchall()
        return [
            {"at": _from_iso(row[0]), "kind": row[1],
             "amount": Decimal(row[2]), "note": row[3]}
            for row in rows
        ]

    def record_equity_snapshot(
        self, *, kind: str, equity: Decimal, cash: Decimal,
        at: datetime | None = None, note: str | None = None,
    ) -> None:
        ts = _to_iso(at) if at is not None else _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO equity_snapshots (at, kind, equity, cash, note) VALUES (?, ?, ?, ?, ?)",
                (ts, kind, str(equity), str(cash), note),
            )

    def get_latest_equity_snapshot(
        self, *, kind: str, since: datetime | None = None,
    ) -> EquitySnapshot | None:
        with self._lock:
            if since is None:
                row = self._conn.execute(
                    "SELECT at, kind, equity, cash, note FROM equity_snapshots"
                    " WHERE kind = ? ORDER BY at DESC LIMIT 1",
                    (kind,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT at, kind, equity, cash, note FROM equity_snapshots"
                    " WHERE kind = ? AND at >= ? ORDER BY at DESC LIMIT 1",
                    (kind, _to_iso(since)),
                ).fetchone()
        if row is None:
            return None
        return EquitySnapshot(
            at=_from_iso(row[0]), kind=row[1],
            equity=Decimal(row[2]), cash=Decimal(row[3]), note=row[4],
        )

    def read_equity_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT at, kind, equity, cash, note FROM equity_snapshots ORDER BY id"
            )
            rows = cur.fetchall()
        return [
            {
                "at": _from_iso(row[0]), "kind": row[1],
                "equity": Decimal(row[2]), "cash": Decimal(row[3]), "note": row[4],
            }
            for row in rows
        ]

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()
