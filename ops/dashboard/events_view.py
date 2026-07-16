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
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ops.dashboard.snapshot import ro_conn

_MAX_FALLBACK = 200


def _activity_desc(p: dict[str, Any]) -> str:
    """'overnight: vetting CRC (2/5)' for items; 'daily_cycle' for jobs."""
    if p.get("scope") == "item":
        bits = f"{p.get('job', '?')}: {p.get('stage', '?')}"
        if p.get("symbol"):
            bits += f" {p['symbol']}"
        if p.get("seq"):
            bits += f" ({p['seq']})"
        return bits
    return str(p.get("job", "?"))


def _render_activity_started(p: dict[str, Any]) -> str:
    desc = _activity_desc(p)
    if p.get("scope") == "job" and p.get("reason"):
        return f"▶ {desc} — {p['reason']}"
    return f"▶ {desc}"


def _render_activity_finished(p: dict[str, Any]) -> str:
    desc = _activity_desc(p)
    dur = p.get("duration_s")
    if p.get("ok"):
        return f"✓ {desc} ({dur}s)" if dur is not None else f"✓ {desc}"
    return f"✗ {desc} — failed after {dur}s" if dur is not None else f"✗ {desc} — failed"


def _dec_display(v: Any, dp: int, *, strip: bool) -> str:
    """Display-trim a Decimal-ish value to dp places (half-up). Journal
    quantities carry full paper-fill precision (25+ digits); the feed is for
    eyes, not accounting. Non-numeric input falls back to str(v) — rendering
    must never raise."""
    try:
        d = Decimal(str(v)).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, ArithmeticError):
        return str(v)
    s = format(d, "f")
    if strip and "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _fmt_qty(v: Any) -> str:
    return _dec_display(v, 4, strip=True) if v is not None else "?"


def _fmt_money(v: Any) -> str:
    return f"${_dec_display(v, 2, strip=False)}" if v is not None else "$?"


_RENDERERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "fill": lambda p: (
        f"{str(p.get('side', '?')).upper()} {_fmt_qty(p.get('quantity', '?'))} "
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
    "activity_started": _render_activity_started,
    "activity_finished": _render_activity_finished,
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
