"""Merge the sleeve journals' event streams and render them for humans.

Rendering is defensive by contract: payload shapes evolve, and a feed that
crashes on a new event kind is worse than one that prints the raw payload.
Every renderer uses .get with fallbacks; any renderer exception falls back
to the compact form.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from typing import Any

from ops.dashboard.snapshot import ro_conn

_MAX_FALLBACK = 200


def _fmt_money(v: Any) -> str:
    return f"${v}" if v is not None else "$?"


_RENDERERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "fill": lambda p: (
        f"{str(p.get('side', '?')).upper()} {p.get('quantity', '?')} "
        f"{p.get('symbol', '?')} @ {_fmt_money(p.get('price'))}"),
    "order_rejected": lambda p: (
        f"Order rejected: {p.get('symbol', '?')} — "
        f"{p.get('reason', p.get('rule', 'no reason recorded'))}"),
    "stop_hit": lambda p: (
        f"STOP HIT: {p.get('symbol', '?')} at {_fmt_money(p.get('price'))}"),
    "stop_failed": lambda p: (
        f"STOP FAILED: {p.get('symbol', '?')} — {p.get('error', 'unknown')}"),
    "daily_halt": lambda p: "Daily drawdown halt — trading paused for the day",
    "kill_switch": lambda p: "KILL SWITCH — weekly drawdown breached",
    "service_started": lambda p: (
        f"Service started (pid {p.get('pid', '?')}, "
        f"{p.get('broker_mode', '?')} mode)"),
    "service_stopping": lambda p: (
        f"Service stopping (exit code {p.get('exit_code', '?')})"),
    "startup_halted": lambda p: "Startup halted: reconciliation found diffs",
    "inconsistency": lambda p: f"Reconciliation inconsistency: {p}",
    "guardian_check_error": lambda p: (
        f"Guardian check error: {p.get('error', 'unknown')}"),
    "heartbeat_error": lambda p: (
        f"Heartbeat ping failed: {p.get('error', 'unknown')}"),
    "daily_cycle_run": lambda p: "Daily cycle started",
    "daily_cycle_completed": lambda p: "Daily cycle completed",
    "analysis_decision": lambda p: (
        f"Analysis: {p.get('symbol', '?')} → {p.get('decision', '?')}"),
    "baseline_screen_run": lambda p: "Baseline screen run",
    "research_vetting_run": lambda p: (
        f"Vetting run: {p.get('vetted', '?')} vetted, "
        f"{p.get('passed', '?')} passed"),
    "research_drain_run": lambda p: (
        f"Overnight drain: {p.get('researched', p.get('count', '?'))} name(s)"),
    "research_position_opened": lambda p: (
        f"Research position opened: {p.get('symbol', '?')}"),
    "research_position_closed": lambda p: (
        f"Research position closed: {p.get('symbol', '?')}"),
    "falsifier_tripped": lambda p: (
        f"FALSIFIER TRIPPED: memo {p.get('memo_id', '?')} "
        f"({p.get('ticker', p.get('symbol', ''))})"),
    "research_escalation": lambda p: (
        f"Research escalation: {p.get('reason', p.get('memo_id', '?'))}"),
    "resolution_due": lambda p: f"Resolution due: memo {p.get('memo_id', '?')}",
    "catalyst_due": lambda p: f"Catalyst due: memo {p.get('memo_id', '?')}",
}


def render_event(kind: str, payload: dict[str, Any]) -> str:
    fn = _RENDERERS.get(kind)
    if fn is not None:
        try:
            return fn(payload)
        except Exception:  # noqa: BLE001 — fall through to compact form
            pass
    if not payload:
        return kind
    compact = json.dumps(payload, default=str)
    return f"{kind}: {compact}"[:_MAX_FALLBACK]


def merged_events(
    journal_paths: dict[str, str],
    *,
    limit: int = 100,
    kinds: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for source, path in journal_paths.items():
        try:
            with closing(ro_conn(path)) as conn:
                if kinds:
                    marks = ",".join("?" for _ in kinds)
                    rows = conn.execute(
                        f"SELECT id, at, kind, payload FROM events"
                        f" WHERE kind IN ({marks})"
                        f" ORDER BY id DESC LIMIT ?",
                        (*sorted(kinds), limit)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, at, kind, payload FROM events"
                        " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        except sqlite3.OperationalError:
            continue  # missing/locked journal: feed shows the others
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except (TypeError, ValueError):
                payload = {}
            items.append({
                "source": source, "id": r["id"], "at": r["at"],
                "kind": r["kind"], "text": render_event(r["kind"], payload),
                "payload": payload,
            })
    # ISO-8601 UTC strings (journal normalizes to +00:00) sort correctly
    # as strings — same property the journal itself relies on.
    items.sort(key=lambda i: i["at"], reverse=True)
    return items[:limit]
