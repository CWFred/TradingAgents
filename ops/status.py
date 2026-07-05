"""`ops status` (A4): journal-only system snapshot.

build_status reads ONLY the journal (WAL concurrent reads) plus static
config — no broker, no MCP, no OAuth, no quotes — so it is always safe to
run beside the live service and works when the broker is unreachable.
Positions/cash therefore come from journal replay and are labeled
"journal view": live truth may differ, and surfacing that difference is
reconciliation's job, not status's.

The CLI (`ops status`) is a thin renderer over the dict; tests assert on
the dict. Money stays Decimal and timestamps stay datetime inside the
dict — stringification happens only in format_status.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ops import events
from ops.config import OpsConfig
from ops.journal import Journal
from ops.live_gate import count_live_buy_fills, flip_epoch
from ops.trading_time import trading_day_start, trading_week_start

# Consumer name the always-on service's NotifyDispatcher registers its
# cursor under (NotifyDispatcher's default).
_NOTIFY_CONSUMER = "notify"

# Recent-anomaly kinds surfaced by section 8, per the A4 spec.
_ANOMALY_KINDS = (
    events.KIND_GUARDIAN_CHECK_ERROR,
    events.KIND_ORCHESTRATOR_TICK_ERROR,
    events.KIND_STOP_FAILED,
    events.KIND_GUARDIAN_BLIND,
    events.KIND_INCONSISTENCY,
)


def _refuse_quotes(symbol: str) -> Decimal:
    """Quote source handed to PaperBroker.from_journal: replay never
    quotes, and status must never fetch one — any call is a bug that
    would silently make `ops status` network-dependent."""
    raise RuntimeError(
        f"ops status is journal-only, but a quote was requested for "
        f"{symbol!r} — position replay must not touch quote sources"
    )


def _event_view(ev: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    if ev is None:
        return None
    return {
        "at": ev["at"],
        "age_seconds": (now - ev["at"]).total_seconds(),
        "payload": ev["payload"],
    }


def _baseline_view(
    journal: Journal, *, kind: str, period_start: datetime,
) -> dict[str, Any] | None:
    """Latest snapshot of `kind` regardless of age, flagged stale when it
    predates the current ET day/week — a stale baseline shown as current
    is exactly how a false drawdown reading starts."""
    snap = journal.get_latest_equity_snapshot(kind=kind)
    if snap is None:
        return None
    return {
        "at": snap.at,
        "equity": snap.equity,
        "cash": snap.cash,
        "stale": snap.at < period_start,
    }


def build_status(
    journal: Journal, config: OpsConfig, *, now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the status dict from journal + config alone.

    `now` is injectable for tests; every day/week boundary goes through
    ops.trading_time (ET calendar), never naive UTC-midnight math.
    """
    from ops.broker.paper import PaperBroker

    when = now if now is not None else datetime.now(timezone.utc)
    day_start = trading_day_start(when)
    week_start = trading_week_start(when)

    # Positions + cash: journal replay only. starting_cash=0 mirrors the
    # service's own replay convention (cash reconstructs from journaled
    # adjustments + fills); _refuse_quotes proves no quote is fetched.
    replay = PaperBroker.from_journal(
        journal=journal, quote_source=_refuse_quotes,
        starting_cash=Decimal("0"),
    )

    fills = journal.read_fills()
    fills_today = [f for f in fills if f["filled_at"] >= day_start]

    cursor = journal.get_cursor(_NOTIFY_CONSUMER)
    max_event_id = journal.last_event_id_before(when) or 0

    epoch = flip_epoch(journal)
    live_fills = count_live_buy_fills(journal)

    anomaly_since = when - timedelta(days=7)
    anomalies: dict[str, dict[str, Any]] = {}
    for kind in _ANOMALY_KINDS:
        last = journal.last_event(kind)
        anomalies[kind] = {
            "count": journal.count_events(kind, since=anomaly_since),
            "last_at": last["at"] if last is not None else None,
        }

    return {
        "service": {
            "journal_path": journal.path,
            "broker_mode": config.broker_mode,
            "last_started": _event_view(
                journal.last_event(events.KIND_SERVICE_STARTED), when),
            "last_stopping": _event_view(
                journal.last_event(events.KIND_SERVICE_STOPPING), when),
        },
        "positions": {
            "source": "journal_replay",
            "items": [
                {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "entry": p.avg_entry_price,
                    "stop": p.stop_loss_price,
                }
                for p in replay.get_positions()
            ],
        },
        "cash": {"cash": replay.get_cash()},
        "baselines": {
            "open_day": _baseline_view(
                journal, kind="open_day", period_start=day_start),
            "open_week": _baseline_view(
                journal, kind="open_week", period_start=week_start),
        },
        "halts": {
            "daily_halt_today": journal.has_event_today(
                events.KIND_DAILY_HALT, now=when),
            "kill_switch_this_week": journal.has_event_since_last_monday(
                events.KIND_KILL_SWITCH, now=when),
        },
        "fills": {
            "today_count": len(fills_today),
            "today": [
                {
                    "symbol": f["symbol"], "side": f["side"],
                    "quantity": f["quantity"], "price": f["price"],
                    "filled_at": f["filled_at"],
                }
                for f in fills_today
            ],
            "last": (
                {
                    "symbol": fills[-1]["symbol"], "side": fills[-1]["side"],
                    "quantity": fills[-1]["quantity"],
                    "price": fills[-1]["price"],
                    "filled_at": fills[-1]["filled_at"],
                }
                if fills else None
            ),
        },
        "notify": {
            "cursor": cursor,
            "max_event_id": max_event_id,
            "lag": max(0, max_event_id - cursor),
            "skipped_count": journal.count_events(
                events.KIND_NOTIFY_EVENT_SKIPPED),
            "render_error_count": journal.count_events(
                events.KIND_NOTIFY_RENDER_ERROR),
        },
        "live_gate": {
            "flip_marker_present": epoch is not None,
            "flip_at": epoch,
            "live_buy_fills": live_fills,
            "cap": config.live_max_position,
            "gate_count": config.live_fill_gate_count,
            "remaining": max(0, config.live_fill_gate_count - live_fills),
        },
        "anomalies_7d": anomalies,
    }


def _age(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f}s ago"
    if seconds < 7200:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def format_status(status: dict[str, Any]) -> str:
    """Plain-text rendering of build_status's dict (the CLI's only job)."""
    lines: list[str] = []
    svc = status["service"]
    lines.append(f"Journal: {svc['journal_path']}")
    lines.append(f"Broker mode (config): {svc['broker_mode']}")
    for label, key in (("Last started", "last_started"),
                       ("Last stopped", "last_stopping")):
        ev = svc[key]
        if ev is None:
            lines.append(f"{label}: never")
        else:
            extra = ""
            if key == "last_stopping" and "exit_code" in ev["payload"]:
                extra = f" (exit_code={ev['payload']['exit_code']})"
            lines.append(
                f"{label}: {ev['at'].isoformat()} "
                f"[{_age(ev['age_seconds'])}]{extra}"
            )

    lines.append("")
    items = status["positions"]["items"]
    lines.append(f"Positions (journal view — live truth may differ): {len(items)}")
    for p in items:
        stop = f"${p['stop']}" if p["stop"] is not None else "none (config fallback)"
        lines.append(
            f"  {p['symbol']}: qty {p['quantity']} "
            f"entry ${p['entry']} stop {stop}"
        )
    lines.append(f"Cash (journal view): ${status['cash']['cash']}")

    lines.append("")
    for label, key in (("Open-day baseline", "open_day"),
                       ("Open-week baseline", "open_week")):
        snap = status["baselines"][key]
        if snap is None:
            lines.append(f"{label}: none")
        else:
            flag = " (stale)" if snap["stale"] else ""
            lines.append(
                f"{label}: equity ${snap['equity']} cash ${snap['cash']} "
                f"at {snap['at'].isoformat()}{flag}"
            )

    halts = status["halts"]
    lines.append("")
    lines.append(f"Daily halt today: {'YES' if halts['daily_halt_today'] else 'no'}")
    lines.append(
        f"Kill switch this week: {'YES' if halts['kill_switch_this_week'] else 'no'}"
    )

    fills = status["fills"]
    lines.append("")
    lines.append(f"Fills today (ET, by filled_at): {fills['today_count']}")
    for f in fills["today"]:
        lines.append(
            f"  {f['side']} {f['symbol']} qty {f['quantity']} @ ${f['price']} "
            f"({f['filled_at'].isoformat()})"
        )
    if fills["last"] is not None:
        lf = fills["last"]
        lines.append(
            f"Last fill: {lf['side']} {lf['symbol']} qty {lf['quantity']} "
            f"@ ${lf['price']} ({lf['filled_at'].isoformat()})"
        )
    else:
        lines.append("Last fill: none")

    notify = status["notify"]
    lines.append("")
    lines.append(
        f"Notify: cursor {notify['cursor']}/{notify['max_event_id']} "
        f"(lag {notify['lag']}), skipped {notify['skipped_count']}, "
        f"render errors {notify['render_error_count']}"
    )

    gate = status["live_gate"]
    lines.append("")
    if gate["flip_marker_present"]:
        lines.append(
            f"Live gate: flip marker at {gate['flip_at'].isoformat()}; "
            f"{gate['live_buy_fills']} live BUY fill(s), "
            f"{gate['remaining']} of {gate['gate_count']} remaining under "
            f"${gate['cap']} cap"
        )
    else:
        lines.append(
            f"Live gate: no flip marker (paper so far); gate is "
            f"${gate['cap']} x first {gate['gate_count']} live BUY fills"
        )

    lines.append("")
    lines.append("Anomalies (last 7 days):")
    for kind, info in status["anomalies_7d"].items():
        last = info["last_at"].isoformat() if info["last_at"] is not None else "never"
        lines.append(f"  {kind}: {info['count']} (last: {last})")

    return "\n".join(lines)
