"""Per-event-kind notification policy, rendering, and SPOT redaction."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ops import events
from ops.notify.transport import NotifyMessage

_SPOT_RE = re.compile(r"\bspot\b", re.IGNORECASE)


def scrub_spot(text: str) -> str:
    return _SPOT_RE.sub("[redacted]", text)


@dataclass(frozen=True)
class PolicyEntry:
    channels: tuple[str, ...]
    urgency: str
    cooldown_seconds: int | None


_INSTANT_CRITICAL = PolicyEntry(("push", "email"), "high", None)
_PUSH_ONLY = PolicyEntry(("push",), "normal", None)
_EMAIL_THROTTLED = PolicyEntry(("email",), "normal", 600)

# Keys are the ops.events kind constants (A3): the enforcement test in
# tests/ops/notify/test_policy.py proves every entry here has a payload
# builder whose rendered notification is non-empty, and that every builder
# not listed here is explicitly in events.AUDIT_ONLY.
POLICY: dict[str, PolicyEntry] = {
    events.KIND_KILL_SWITCH: _INSTANT_CRITICAL,
    events.KIND_STOP_FAILED: _INSTANT_CRITICAL,
    events.KIND_KILL_SWITCH_CLOSE_FAILED: _INSTANT_CRITICAL,
    events.KIND_INCONSISTENCY: _INSTANT_CRITICAL,
    events.KIND_STARTUP_HALTED: _INSTANT_CRITICAL,
    events.KIND_POSITIONS_RECOVERED_WITHOUT_STOPS: _INSTANT_CRITICAL,
    # Guardian failed to get quotes for >=5 consecutive passes.
    events.KIND_GUARDIAN_BLIND: _INSTANT_CRITICAL,
    # A live order is dangling at the broker and may need manual cancellation.
    events.KIND_ORDER_NOT_FILLED: _INSTANT_CRITICAL,
    events.KIND_STOP_HIT: _PUSH_ONLY,
    events.KIND_DAILY_HALT: _PUSH_ONLY,
    events.KIND_FILL: _PUSH_ONLY,
    events.KIND_BROKER_UNREACHABLE: _EMAIL_THROTTLED,
    events.KIND_ORCHESTRATOR_TICK_ERROR: _EMAIL_THROTTLED,
    events.KIND_GUARDIAN_CHECK_ERROR: _EMAIL_THROTTLED,
    events.KIND_EXIT_CHECK_ERROR: _EMAIL_THROTTLED,
    events.KIND_QUOTE_UNAVAILABLE: _EMAIL_THROTTLED,
    # Dead-man's-switch ping failure (A1.3): worth knowing about, but a
    # monitoring outage is not a trading emergency — email, throttled.
    events.KIND_HEARTBEAT_ERROR: _EMAIL_THROTTLED,
    events.KIND_DAILY_SUMMARY: PolicyEntry(("push", "email"), "normal", None),
    # The universe came back empty because the data feed was failing, not
    # because the market was quiet (2026-07-06 incident) — worth a push.
    events.KIND_UNIVERSE_BLIND: PolicyEntry(("push",), "high", None),
    # Research monitoring (Phase C): falsifier triggered, immediate investigation needed.
    events.KIND_FALSIFIER_TRIPPED: PolicyEntry(("push",), "high", None),
    # Research monitoring: memo reaching expected resolution date.
    events.KIND_RESOLUTION_DUE: _PUSH_ONLY,
    # Research monitoring: catalyst event expected.
    events.KIND_CATALYST_DUE: _PUSH_ONLY,
    # Research monitoring: critical issue detected by monitor, escalation needed.
    events.KIND_RESEARCH_ESCALATION: PolicyEntry(("push",), "high", None),
    # Research sleeve trading: the user learns the sleeve traded (Phase D).
    events.KIND_RESEARCH_TRADE_RUN: _PUSH_ONLY,
    # NOTE: audit-only kinds (events.AUDIT_ONLY — e.g.
    # journal_replay_orphan_sell, service_started) are intentionally
    # absent and must never be notified.
}


def _kv_body(payload: dict) -> str:
    """Generic key=value body. None-valued keys are omitted — payloads
    legitimately carry None for absent numerics (e.g. order_not_filled's
    quantity/fill_price on a queued order), and a critical push must not
    read "quantity=None"."""
    return "; ".join(f"{k}={v}" for k, v in payload.items() if v is not None)


def _title(kind: str) -> str:
    return kind.replace("_", " ").title()


def render(kind: str, payload: dict) -> NotifyMessage:
    entry = POLICY.get(kind)
    urgency = entry.urgency if entry is not None else "normal"
    if kind == events.KIND_FILL:
        title = f"Fill: {payload.get('symbol')}"
        body = (f"{payload.get('side')} {payload.get('symbol')} "
                f"qty {payload.get('quantity')} @ ${payload.get('price')} "
                f"({payload.get('context')})")
    elif kind == events.KIND_KILL_SWITCH:
        title = "KILL SWITCH TRIPPED"
        # Render the actual guardian payload fields (M4): mode, equity, pct, threshold.
        pct = payload.get("pct", payload.get("drawdown_pct", ""))
        threshold = payload.get("threshold", "")
        equity_now = payload.get("equity_now", "")
        equity_ref = payload.get("equity_open_week", payload.get("equity_open_day", ""))
        mode = payload.get("mode", "")
        if pct and threshold:
            body = (f"Weekly drawdown {pct} breached {threshold} "
                    f"(equity ${equity_now} vs week-open ${equity_ref}); "
                    f"mode={mode}")
        else:
            # Fallback to generic key=value join for unknown payload shapes.
            body = _kv_body(payload) or kind
    elif kind == events.KIND_DAILY_SUMMARY:
        title = payload.get("headline", "Daily summary")
        body = payload.get("body", str(payload))
    elif kind == events.KIND_FALSIFIER_TRIPPED:
        title = f"Falsifier tripped: {payload.get('ticker')}"
        body = (f"{payload.get('description')} ({payload.get('metric')}); "
                f"observed {payload.get('observed')} vs threshold "
                f"{payload.get('threshold')} for {payload.get('consecutive_periods')} periods")
    elif kind == events.KIND_RESOLUTION_DUE:
        title = f"Resolution due: {payload.get('ticker')}"
        body = (f"Memo {payload.get('memo_id')} thesis {payload.get('thesis_type')} "
                f"status {payload.get('status')}: {payload.get('elapsed_days')} days "
                f"elapsed of expected {payload.get('expected_holding_months')} months")
    elif kind == events.KIND_CATALYST_DUE:
        title = f"Catalyst due: {payload.get('ticker')}"
        body = (f"{payload.get('description')} expected on "
                f"{payload.get('expected_date')}")
    elif kind == events.KIND_RESEARCH_ESCALATION:
        title = f"Research escalation: {payload.get('ticker')}"
        body = f"Memo {payload.get('memo_id')}: {payload.get('reason')}"
    elif kind == events.KIND_RESEARCH_TRADE_RUN:
        entered = payload.get("entered", [])
        exited = payload.get("exited", [])
        title = "Research sleeve traded"
        body = (f"Asof {payload.get('asof')}: entered {entered}; "
                f"exited {exited}; equity ${payload.get('equity')}; "
                f"cash ${payload.get('cash')}")
    else:
        title = _title(kind)
        body = _kv_body(payload) or kind
    return NotifyMessage(title=scrub_spot(title), body=scrub_spot(body), urgency=urgency)
