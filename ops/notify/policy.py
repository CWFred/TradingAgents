"""Per-event-kind notification policy, rendering, and SPOT redaction."""
from __future__ import annotations

import re
from dataclasses import dataclass

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

POLICY: dict[str, PolicyEntry] = {
    "kill_switch": _INSTANT_CRITICAL,
    "stop_failed": _INSTANT_CRITICAL,
    "kill_switch_close_failed": _INSTANT_CRITICAL,
    "inconsistency": _INSTANT_CRITICAL,
    "startup_halted": _INSTANT_CRITICAL,
    "positions_recovered_without_stops": _INSTANT_CRITICAL,
    # Guardian failed to get quotes for >=5 consecutive passes.
    "guardian_blind": _INSTANT_CRITICAL,
    # A live order is dangling at the broker and may need manual cancellation.
    "order_not_filled": _INSTANT_CRITICAL,
    "stop_hit": _PUSH_ONLY,
    "daily_halt": _PUSH_ONLY,
    "fill": _PUSH_ONLY,
    "broker_unreachable": _EMAIL_THROTTLED,
    "orchestrator_tick_error": _EMAIL_THROTTLED,
    "guardian_check_error": _EMAIL_THROTTLED,
    "quote_unavailable": _EMAIL_THROTTLED,
    "daily_summary": PolicyEntry(("push", "email"), "normal", None),
    # NOTE: "journal_replay_orphan_sell" is intentionally absent — it is an
    # audit-only event and must never be notified.
}


def _title(kind: str) -> str:
    return kind.replace("_", " ").title()


def render(kind: str, payload: dict) -> NotifyMessage:
    entry = POLICY.get(kind)
    urgency = entry.urgency if entry is not None else "normal"
    if kind == "fill":
        title = f"Fill: {payload.get('symbol')}"
        body = (f"{payload.get('side')} {payload.get('symbol')} "
                f"qty {payload.get('quantity')} @ ${payload.get('price')} "
                f"({payload.get('context')})")
    elif kind == "kill_switch":
        title = "KILL SWITCH TRIPPED"
        body = f"Kill switch: {payload.get('reason', '')}"
    elif kind == "daily_summary":
        title = payload.get("headline", "Daily summary")
        body = payload.get("body", str(payload))
    else:
        title = _title(kind)
        body = "; ".join(f"{k}={v}" for k, v in payload.items()) or kind
    return NotifyMessage(title=scrub_spot(title), body=scrub_spot(body), urgency=urgency)
