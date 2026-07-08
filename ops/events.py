"""Typed journal-event contracts (A3).

Event kinds and payload shapes used to be stringly-typed and agreed on by
vibes between ~25 record_event call sites, the notify POLICY table, and
the renderers — which shipped three real bugs (daily_halt consumed but
never produced, a kill-switch notification with an empty body, fills
lacking broker_mode). This module is the single contract:

- one KIND_* constant per event kind. The string VALUES are frozen: the
  journal already contains them, and replay/queries compare raw strings.
- one <kind>_payload() builder per kind, taking typed kwargs and returning
  the exact payload dict producers must journal. Builders stringify
  Decimals at this boundary (the journal's storage convention); ints and
  lists pass through as-is because json.dumps handles them and existing
  rows already store them that way.
- AUDIT_ONLY: kinds deliberately NOT notified. Every kind in BUILDERS
  must be in the notify POLICY or in AUDIT_ONLY — the enforcement test in
  tests/ops/notify/test_policy.py fails otherwise, so a new event kind
  forces a conscious notify decision instead of silently defaulting to
  "dropped by the dispatcher".

Journal.record_event itself stays generic: the journal must not import
event semantics, so producers call journal.record_event(KIND_X,
x_payload(...)).

Payload-key stability is load-bearing beyond rendering: the fill payload's
side/broker_mode keys are filtered via SQL json_extract by
ops.live_gate.count_live_buy_fills, and the dispatcher's error events
carry event_id for cursor forensics. Do not rename keys.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

# --- Kind constants (string values frozen — already in journals) --------

# Trading / broker boundary
KIND_FILL = "fill"
KIND_ORDER_REJECTED = "order_rejected"
KIND_ORDER_NOT_FILLED = "order_not_filled"

# Guardian
KIND_STOP_HIT = "stop_hit"
KIND_STOP_FAILED = "stop_failed"
KIND_QUOTE_UNAVAILABLE = "quote_unavailable"
KIND_GUARDIAN_BLIND = "guardian_blind"
KIND_GUARDIAN_CHECK_ERROR = "guardian_check_error"
KIND_KILL_SWITCH = "kill_switch"
KIND_KILL_SWITCH_CLOSE_FAILED = "kill_switch_close_failed"
KIND_DAILY_HALT = "daily_halt"

# Orchestrator / service lifecycle
KIND_ORCHESTRATOR_TICK_ERROR = "orchestrator_tick_error"
KIND_SERVICE_STARTED = "service_started"
KIND_SERVICE_STOPPING = "service_stopping"
KIND_STARTUP_HALTED = "startup_halted"
KIND_BROKER_UNREACHABLE = "broker_unreachable"
KIND_HEARTBEAT_ERROR = "heartbeat_error"

# Reconciliation / replay
KIND_INCONSISTENCY = "inconsistency"
KIND_POSITIONS_RECOVERED_WITHOUT_STOPS = "positions_recovered_without_stops"
KIND_JOURNAL_REPLAY_FALLBACK = "journal_replay_fallback"
KIND_JOURNAL_REPLAY_ORPHAN_SELL = "journal_replay_orphan_sell"

# Live gate / flip ritual
KIND_BROKER_MODE_LIVE = "broker_mode_live"
KIND_LIVE_FLIP_REFUSED = "live_flip_refused"

# Notify subsystem
KIND_DAILY_SUMMARY = "daily_summary"
KIND_DAILY_SUMMARY_ERROR = "daily_summary_error"
KIND_NOTIFY_CURSOR_INITIALIZED = "notify_cursor_initialized"
KIND_NOTIFY_RENDER_ERROR = "notify_render_error"
KIND_NOTIFY_DISPATCH_ERROR = "notify_dispatch_error"
KIND_NOTIFY_EVENT_SKIPPED = "notify_event_skipped"

# Baseline (null-hypothesis) screen portfolio
KIND_BASELINE_SCREEN_RUN = "baseline_screen_run"
KIND_BASELINE_EXIT = "baseline_exit"
# Position lifecycle / exit engine
KIND_POSITION_OPENED = "position_opened"
KIND_EXIT_DECISION = "exit_decision"
KIND_EXIT_ORDER_PLACED = "exit_order_placed"
KIND_EXIT_SKIPPED_MISSING_DATA = "exit_skipped_missing_data"
KIND_EXIT_CHECK_ERROR = "exit_check_error"
KIND_EXIT_UNKNOWN_PROVENANCE = "exit_unknown_provenance"
# Scheduler / daily-cycle gate
KIND_DAILY_CYCLE_RUN = "daily_cycle_run"

# Universe data-feed health (A3): "found nothing" must be distinguishable
# from "could not see".
KIND_UNIVERSE_DIAGNOSTICS = "universe_diagnostics"
KIND_UNIVERSE_BLIND = "universe_blind"

KIND_BASELINE_WRITEOFF = "baseline_writeoff"

# --- Research monitoring (Phase C) ---
KIND_FALSIFIER_TRIPPED = "falsifier_tripped"
KIND_RESOLUTION_DUE = "resolution_due"
KIND_CATALYST_DUE = "catalyst_due"
KIND_RESEARCH_ESCALATION = "research_escalation"
KIND_RESEARCH_MONITOR_RUN = "research_monitor_run"
KIND_RESEARCH_MONITOR_ERROR = "research_monitor_error"
KIND_BASELINE_QUOTE_FAILURE = "baseline_quote_failure"
KIND_BASELINE_AUTO_WRITEOFF = "baseline_auto_writeoff"

# --- Research sleeve trading (Phase D) ---
KIND_RESEARCH_TRADE_RUN = "research_trade_run"
KIND_RESEARCH_TRADE_ERROR = "research_trade_error"
KIND_RESEARCH_POSITION_OPENED = "research_position_opened"
KIND_RESEARCH_POSITION_CLOSED = "research_position_closed"

# Momentum sleeve: per-name pipeline verdict (BUY/HOLD/SELL), one per
# analyzed candidate regardless of whether it turned into an order.
KIND_ANALYSIS_DECISION = "analysis_decision"

# --- Daily overview delivery (DO-Task 3) ---
# Cross-sleeve "everything that happened today" digest: weekday post-close +
# Saturday evening daemon job, plus `ops digest` on demand.
KIND_DAILY_OVERVIEW = "daily_overview"
KIND_DAILY_OVERVIEW_ERROR = "daily_overview_error"

# Kinds deliberately NOT notified. Everything here is an audit trail the
# operator reads via `ops status` or sqlite, not a push/email — either
# because it fires during normal operation (service lifecycle, replay
# bookkeeping), because notifying it would recurse through the notify
# subsystem's own error events, or because the surrounding flow already
# surfaces it (order_rejected raises OrderRejected at the caller;
# live_flip_refused prints and exits 4 at an interactive terminal).
AUDIT_ONLY: frozenset[str] = frozenset({
    KIND_ORDER_REJECTED,
    KIND_SERVICE_STARTED,
    KIND_SERVICE_STOPPING,
    KIND_JOURNAL_REPLAY_FALLBACK,
    KIND_JOURNAL_REPLAY_ORPHAN_SELL,
    KIND_BROKER_MODE_LIVE,
    KIND_LIVE_FLIP_REFUSED,
    KIND_DAILY_SUMMARY_ERROR,
    KIND_NOTIFY_CURSOR_INITIALIZED,
    KIND_NOTIFY_RENDER_ERROR,
    KIND_NOTIFY_DISPATCH_ERROR,
    KIND_NOTIFY_EVENT_SKIPPED,
    KIND_BASELINE_SCREEN_RUN,
    KIND_BASELINE_EXIT,
    # Exit lifecycle: the sell itself already notifies via KIND_FILL (push);
    # these are audit breadcrumbs.
    KIND_POSITION_OPENED,
    KIND_EXIT_DECISION,
    KIND_EXIT_ORDER_PLACED,
    KIND_EXIT_SKIPPED_MISSING_DATA,
    # Backward-compat audit trail — position predates position_opened events.
    KIND_EXIT_UNKNOWN_PROVENANCE,
    # Operational bookkeeping, gates the once-daily universe/exit cycle.
    KIND_DAILY_CYCLE_RUN,
    # Universe diagnostics: fire-and-forget breadcrumb for the audit trail.
    KIND_UNIVERSE_DIAGNOSTICS,
    # Baseline write-off: manual resolution of a delisted position.
    KIND_BASELINE_WRITEOFF,
    # Research monitoring events: audit trail of monitor runs and failures.
    KIND_RESEARCH_MONITOR_RUN,
    KIND_RESEARCH_MONITOR_ERROR,
    KIND_BASELINE_QUOTE_FAILURE,
    KIND_BASELINE_AUTO_WRITEOFF,
    # Research sleeve trading: audit trail of trades executed by research sleeve.
    KIND_RESEARCH_TRADE_ERROR,
    KIND_RESEARCH_POSITION_OPENED,
    KIND_RESEARCH_POSITION_CLOSED,
    # Per-name momentum pipeline verdict: audit trail, not a push — the BUY
    # case already notifies via position_opened/fill.
    KIND_ANALYSIS_DECISION,
    # Daily overview delivery: audit record of the once-per-day cross-sleeve
    # digest run. The push itself is a direct Pushover call inside the tick
    # (see ops/main.py::_daily_overview_tick), not routed through the notify
    # dispatcher/POLICY table, so this gate event is audit-only.
    KIND_DAILY_OVERVIEW,
    KIND_DAILY_OVERVIEW_ERROR,
})


# --- Payload builders ----------------------------------------------------
# Decimal arguments are stringified here (journal storage convention);
# datetime arguments become ISO strings. Optional keys that today's
# producers omit entirely when absent (fill-less context, missing git sha)
# are omitted by the builders too, so stored payloads are byte-identical
# to the pre-A3 shapes.


def fill_payload(
    *, client_order_id: str, order_id: str, symbol: str, side: str,
    quantity: Decimal, price: Decimal, filled_at: datetime, context: str,
    broker_mode: str,
) -> dict[str, Any]:
    """side/broker_mode are filtered by SQL json_extract in
    ops.live_gate.count_live_buy_fills — keys and string values frozen."""
    return {
        "client_order_id": client_order_id,
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "quantity": str(quantity),
        "price": str(price),
        "filled_at": filled_at.isoformat(),
        "context": context,
        "broker_mode": broker_mode,
    }


def order_rejected_payload(
    *, rule: str, reason: str, client_order_id: str, symbol: str,
    side: str, notional_dollars: Decimal, context: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "rule": rule,
        "reason": reason,
        "client_order_id": client_order_id,
        "symbol": symbol,
        "side": side,
        "notional_dollars": str(notional_dollars),
    }
    # The place_order path has no context key; close_position adds one.
    if context is not None:
        payload["context"] = context
    return payload


def order_not_filled_payload(
    *, order_id: str, client_order_id: str, symbol: str, side: str,
    status: str, quantity: Decimal | None, fill_price: Decimal | None,
) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "symbol": symbol,
        "side": side,
        "status": status,
        "quantity": str(quantity) if quantity is not None else None,
        "fill_price": str(fill_price) if fill_price is not None else None,
    }


def stop_hit_payload(
    *, symbol: str, entry: Decimal, current: Decimal, pct: Decimal,
    mode: str, threshold_repr: str,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "entry": str(entry),
        "current": str(current),
        "pct": str(pct),
        "mode": mode,
        "threshold_repr": threshold_repr,
    }


def stop_failed_payload(
    *, symbol: str, entry: Decimal, current: Decimal, pct: Decimal,
    mode: str, threshold_repr: str, error: str,
) -> dict[str, Any]:
    payload = stop_hit_payload(
        symbol=symbol, entry=entry, current=current, pct=pct,
        mode=mode, threshold_repr=threshold_repr,
    )
    payload["error"] = error
    return payload


def quote_unavailable_payload(
    *, symbol: str, context: str, error: str,
) -> dict[str, Any]:
    return {"symbol": symbol, "context": context, "error": error}


def guardian_blind_payload(*, consecutive_failed_passes: int) -> dict[str, Any]:
    return {"consecutive_failed_passes": consecutive_failed_passes}


def guardian_check_error_payload(*, error: str) -> dict[str, Any]:
    """`error` is inherently dynamic ("<ExcType>: <message>") — the
    guardian's catch-all cannot know shapes in advance."""
    return {"error": error}


def kill_switch_payload(
    *, mode: str, equity_now: Decimal, equity_open_week: Decimal,
    pct: Decimal, threshold: Decimal,
) -> dict[str, Any]:
    """pct/threshold/equity_now/equity_open_week/mode are read by the
    kill-switch renderer — keys frozen (the empty-body bug class)."""
    return {
        "mode": mode,
        "equity_now": str(equity_now),
        "equity_open_week": str(equity_open_week),
        "pct": str(pct),
        "threshold": str(threshold),
    }


def kill_switch_close_failed_payload(*, symbol: str, error: str) -> dict[str, Any]:
    return {"symbol": symbol, "error": error}


def daily_halt_payload(
    *, mode: str, equity_now: Decimal, equity_open_day: Decimal,
    pct: Decimal, threshold: Decimal,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "equity_now": str(equity_now),
        "equity_open_day": str(equity_open_day),
        "pct": str(pct),
        "threshold": str(threshold),
    }


def orchestrator_tick_error_payload(*, error: str) -> dict[str, Any]:
    return {"error": error}


def service_started_payload(
    *, broker_mode: str, journal_path: str, pid: int,
    git_sha: str | None = None,
) -> dict[str, Any]:
    """Uptime record (A1.2). git_sha is omitted (not None) when provenance
    is unavailable — matching the pre-A3 conditional-key behavior."""
    payload: dict[str, Any] = {
        "broker_mode": broker_mode,
        "journal_path": journal_path,
        "pid": pid,
    }
    if git_sha is not None:
        payload["git_sha"] = git_sha
    return payload


def service_stopping_payload(*, exit_code: int) -> dict[str, Any]:
    return {"exit_code": exit_code}


def startup_halted_payload(*, reason: str) -> dict[str, Any]:
    return {"reason": reason}


def broker_unreachable_payload(*, error_type: str) -> dict[str, Any]:
    """Only the exception TYPE — broker-connectivity exception text can
    embed credentials/hostnames and the journal is durable."""
    return {"error_type": error_type}


def heartbeat_error_payload(*, error_type: str) -> dict[str, Any]:
    """Only the exception TYPE — requests exception text embeds the ping
    URL, a secret-bearing token."""
    return {"error_type": error_type}


def inconsistency_payload(
    *, diffs: list[dict[str, Any]], cash_journal: Decimal,
    cash_broker: Decimal, cash_diff: Decimal,
) -> dict[str, Any]:
    """`diffs` is inherently dynamic (one dict per PositionDiff, already
    stringified by the reconciler); the cash keys are the fixed part."""
    return {
        "diffs": diffs,
        "cash_journal": str(cash_journal),
        "cash_broker": str(cash_broker),
        "cash_diff": str(cash_diff),
    }


def positions_recovered_without_stops_payload(
    *, symbols: list[str],
) -> dict[str, Any]:
    return {"symbols": symbols}


def journal_replay_fallback_payload(
    *, client_order_id: str, symbol: str, side: str, reason: str,
) -> dict[str, Any]:
    return {
        "client_order_id": client_order_id,
        "symbol": symbol,
        "side": side,
        "reason": reason,
    }


def journal_replay_orphan_sell_payload(
    *, client_order_id: str, symbol: str, quantity: Decimal,
    price: Decimal, reason: str,
) -> dict[str, Any]:
    return {
        "client_order_id": client_order_id,
        "symbol": symbol,
        "quantity": str(quantity),
        "price": str(price),
        "reason": reason,
    }


def broker_mode_live_payload() -> dict[str, Any]:
    return {"note": "paper->robinhood flip"}


def live_flip_refused_payload(*, reason: str) -> dict[str, Any]:
    return {"reason": reason}


def daily_summary_payload(
    *, headline: str, body: str, equity: Decimal, n_fills_today: int,
) -> dict[str, Any]:
    return {
        "headline": headline,
        "body": body,
        "equity": str(equity),
        "n_fills_today": n_fills_today,
    }


def daily_summary_error_payload(*, error: str) -> dict[str, Any]:
    return {"error": error}


def notify_cursor_initialized_payload(
    *, consumer: str, skipped_through: int,
) -> dict[str, Any]:
    return {"consumer": consumer, "skipped_through": skipped_through}


def notify_render_error_payload(
    *, event_id: int, kind: str, error_type: str,
) -> dict[str, Any]:
    return {"event_id": event_id, "kind": kind, "error_type": error_type}


def notify_dispatch_error_payload(
    *, event_id: int, kind: str, error_type: str,
) -> dict[str, Any]:
    return {"event_id": event_id, "kind": kind, "error_type": error_type}


def notify_event_skipped_payload(
    *, event_id: int, kind: str, consecutive_failures: int,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "kind": kind,
        "consecutive_failures": consecutive_failures,
    }


def baseline_screen_run_payload(
    *, asof: str, passers: int, buys: list[str], exits: list[str],
    skipped: list[str], equity: Decimal,
) -> dict[str, Any]:
    return {
        "asof": asof, "passers": passers, "buys": buys,
        "exits": exits, "skipped": skipped, "equity": str(equity),
    }


def baseline_exit_payload(*, symbol: str, held_days: int) -> dict[str, Any]:
    return {"symbol": symbol, "held_days": held_days}


def position_opened_payload(
    *, symbol: str, source: str, entry_date: date,
    client_order_id: str, entry_rank: int | None = None,
) -> dict[str, Any]:
    """symbol/source/entry_date are read back by the exit engine's
    provenance loader (json_extract on symbol) — keys frozen."""
    payload: dict[str, Any] = {
        "symbol": symbol,
        "source": source,
        "entry_date": entry_date.isoformat(),
        "client_order_id": client_order_id,
    }
    if entry_rank is not None:
        payload["entry_rank"] = entry_rank
    return payload


def exit_decision_payload(*, symbol: str, rule: str, evidence: str) -> dict[str, Any]:
    return {"symbol": symbol, "rule": rule, "evidence": evidence}


def exit_order_placed_payload(
    *, symbol: str, client_order_id: str, rule: str,
) -> dict[str, Any]:
    return {"symbol": symbol, "client_order_id": client_order_id, "rule": rule}


def exit_skipped_missing_data_payload(*, symbol: str, reason: str) -> dict[str, Any]:
    return {"symbol": symbol, "reason": reason}


def exit_check_error_payload(*, error: str) -> dict[str, Any]:
    """`error` is inherently dynamic — mirrors guardian_check_error."""
    return {"error": error}


def exit_unknown_provenance_payload(*, symbol: str) -> dict[str, Any]:
    return {"symbol": symbol}


def daily_cycle_run_payload(*, asof_date: date) -> dict[str, Any]:
    return {"asof_date": asof_date.isoformat()}


def universe_diagnostics_payload(
    *, asof_date, candidates: int, fetch_ok: int, fetch_failed: int,
    by_label: dict[str, dict[str, int]],
) -> dict[str, Any]:
    return {
        "asof_date": str(asof_date), "candidates": candidates,
        "fetch_ok": fetch_ok, "fetch_failed": fetch_failed,
        "by_label": by_label,
    }


def baseline_writeoff_payload(
    *, symbol: str, quantity: Decimal, price: Decimal, note: str | None,
) -> dict[str, Any]:
    return {"symbol": symbol, "quantity": str(quantity), "price": str(price), "note": note}


def falsifier_tripped_payload(
    *, memo_id: str, ticker: str, falsifier_index: str, description: str,
    metric: str, observed: str, threshold: str, consecutive_periods: int,
) -> dict[str, Any]:
    return {
        "memo_id": memo_id, "ticker": ticker, "falsifier_index": falsifier_index,
        "description": description, "metric": metric, "observed": observed,
        "threshold": threshold, "consecutive_periods": consecutive_periods,
    }


def resolution_due_payload(
    *, memo_id: str, ticker: str, thesis_type: str, status: str,
    expected_holding_months: int, elapsed_days: int, checklist: str,
) -> dict[str, Any]:
    return {
        "memo_id": memo_id, "ticker": ticker, "thesis_type": thesis_type,
        "status": status, "expected_holding_months": expected_holding_months,
        "elapsed_days": elapsed_days, "checklist": checklist,
    }


def catalyst_due_payload(
    *, memo_id: str, ticker: str, catalyst_index: str, description: str,
    expected_date: str,
) -> dict[str, Any]:
    return {
        "memo_id": memo_id, "ticker": ticker, "catalyst_index": catalyst_index,
        "description": description, "expected_date": expected_date,
    }


def research_escalation_payload(
    *, ticker: str, memo_id: str, reason: str, hit_id: int | None,
) -> dict[str, Any]:
    return {
        "ticker": ticker, "memo_id": memo_id, "reason": reason, "hit_id": hit_id,
    }


def research_monitor_run_payload(
    *, asof: str, memos_checked: int, falsifiers_evaluated: int, tripped: int,
    unevaluable: int, escalations: int, resolution_due: int, catalyst_due: int,
    errors: list[str],
) -> dict[str, Any]:
    return {
        "asof": asof, "memos_checked": memos_checked,
        "falsifiers_evaluated": falsifiers_evaluated, "tripped": tripped,
        "unevaluable": unevaluable, "escalations": escalations,
        "resolution_due": resolution_due, "catalyst_due": catalyst_due,
        "errors": errors,
    }


def research_monitor_error_payload(*, error: str) -> dict[str, Any]:
    return {"error": error}


def baseline_quote_failure_payload(
    *, symbol: str, asof: str, error: str,
) -> dict[str, Any]:
    return {"symbol": symbol, "asof": asof, "error": error}


def baseline_auto_writeoff_payload(
    *, symbol: str, quantity: str, price: str, failing_runs: int, note: str,
) -> dict[str, Any]:
    return {
        "symbol": symbol, "quantity": quantity, "price": price,
        "failing_runs": failing_runs, "note": note,
    }


def universe_blind_payload(
    *, asof_date, fetch_ok: int, fetch_failed: int, detail: str,
) -> dict[str, Any]:
    return {
        "asof_date": str(asof_date), "fetch_ok": fetch_ok,
        "fetch_failed": fetch_failed, "detail": detail,
    }


def research_trade_run_payload(
    *, asof: str, entered: list[str], exited: list[str], skipped: list[str],
    equity: str, cash: str,
) -> dict[str, Any]:
    """Research sleeve execution: symbols traded and current balances.
    equity/cash are stringified to match journal storage convention."""
    return {
        "asof": asof, "entered": entered, "exited": exited,
        "skipped": skipped, "equity": equity, "cash": cash,
    }


def research_trade_error_payload(*, error: str) -> dict[str, Any]:
    """Research sleeve trading error: execution or connection failure."""
    return {"error": error}


def research_position_opened_payload(
    *, symbol: str, memo_id: str, conviction_tier: str, entry_date: str,
    client_order_id: str, notional: str,
) -> dict[str, Any]:
    """Research position lifecycle: journal provenance record.
    symbol/memo_id are strings for json_extract / latest_event_payload_by_symbol
    compatibility; entry_date and notional are stringified."""
    return {
        "symbol": symbol, "memo_id": memo_id, "conviction_tier": conviction_tier,
        "entry_date": entry_date, "client_order_id": client_order_id,
        "notional": notional,
    }


def research_position_closed_payload(
    *, symbol: str, memo_id: str, reason: str, exit_date: str, price: str,
) -> dict[str, Any]:
    """Research position closed: audit trail of exits.
    symbol/memo_id are strings for json_extract compatibility; price is
    stringified to match journal storage convention."""
    return {
        "symbol": symbol, "memo_id": memo_id, "reason": reason,
        "exit_date": exit_date, "price": price,
    }


def analysis_decision_payload(
    *, symbol: str, decision: str, source: str, asof: str, rank: int | None = None,
) -> dict[str, Any]:
    """Momentum sleeve per-name pipeline verdict. `decision` is one of
    "BUY"/"HOLD"/"SELL"; `rank` is omitted (not None) when the candidate
    has no momentum payload (an earnings-only candidate)."""
    payload: dict[str, Any] = {
        "symbol": symbol, "decision": decision, "source": source, "asof": asof,
    }
    if rank is not None:
        payload["rank"] = rank
    return payload


def daily_overview_payload(*, date: str, headline: str, path: str) -> dict[str, Any]:
    """Daily overview gate event: audit record of a completed run (once-per-
    day gate for _daily_overview_tick). The push itself is a direct Pushover
    call in the tick, not routed through the notify dispatcher/POLICY table
    — this payload is metadata only."""
    return {"date": date, "headline": headline, "path": path}


def daily_overview_error_payload(*, error: str) -> dict[str, Any]:
    return {"error": error}


# Kind -> builder registry: the enforcement test walks this to prove every
# POLICY kind has a builder and every builder's kind has been classified
# (POLICY or AUDIT_ONLY). Register every new builder here.
BUILDERS: dict[str, Callable[..., dict[str, Any]]] = {
    KIND_FILL: fill_payload,
    KIND_ORDER_REJECTED: order_rejected_payload,
    KIND_ORDER_NOT_FILLED: order_not_filled_payload,
    KIND_STOP_HIT: stop_hit_payload,
    KIND_STOP_FAILED: stop_failed_payload,
    KIND_QUOTE_UNAVAILABLE: quote_unavailable_payload,
    KIND_GUARDIAN_BLIND: guardian_blind_payload,
    KIND_GUARDIAN_CHECK_ERROR: guardian_check_error_payload,
    KIND_KILL_SWITCH: kill_switch_payload,
    KIND_KILL_SWITCH_CLOSE_FAILED: kill_switch_close_failed_payload,
    KIND_DAILY_HALT: daily_halt_payload,
    KIND_ORCHESTRATOR_TICK_ERROR: orchestrator_tick_error_payload,
    KIND_SERVICE_STARTED: service_started_payload,
    KIND_SERVICE_STOPPING: service_stopping_payload,
    KIND_STARTUP_HALTED: startup_halted_payload,
    KIND_BROKER_UNREACHABLE: broker_unreachable_payload,
    KIND_HEARTBEAT_ERROR: heartbeat_error_payload,
    KIND_INCONSISTENCY: inconsistency_payload,
    KIND_POSITIONS_RECOVERED_WITHOUT_STOPS: positions_recovered_without_stops_payload,
    KIND_JOURNAL_REPLAY_FALLBACK: journal_replay_fallback_payload,
    KIND_JOURNAL_REPLAY_ORPHAN_SELL: journal_replay_orphan_sell_payload,
    KIND_BROKER_MODE_LIVE: broker_mode_live_payload,
    KIND_LIVE_FLIP_REFUSED: live_flip_refused_payload,
    KIND_DAILY_SUMMARY: daily_summary_payload,
    KIND_DAILY_SUMMARY_ERROR: daily_summary_error_payload,
    KIND_NOTIFY_CURSOR_INITIALIZED: notify_cursor_initialized_payload,
    KIND_NOTIFY_RENDER_ERROR: notify_render_error_payload,
    KIND_NOTIFY_DISPATCH_ERROR: notify_dispatch_error_payload,
    KIND_NOTIFY_EVENT_SKIPPED: notify_event_skipped_payload,
    KIND_BASELINE_SCREEN_RUN: baseline_screen_run_payload,
    KIND_BASELINE_EXIT: baseline_exit_payload,
    KIND_POSITION_OPENED: position_opened_payload,
    KIND_EXIT_DECISION: exit_decision_payload,
    KIND_EXIT_ORDER_PLACED: exit_order_placed_payload,
    KIND_EXIT_SKIPPED_MISSING_DATA: exit_skipped_missing_data_payload,
    KIND_EXIT_CHECK_ERROR: exit_check_error_payload,
    KIND_EXIT_UNKNOWN_PROVENANCE: exit_unknown_provenance_payload,
    KIND_DAILY_CYCLE_RUN: daily_cycle_run_payload,
    KIND_UNIVERSE_DIAGNOSTICS: universe_diagnostics_payload,
    KIND_UNIVERSE_BLIND: universe_blind_payload,
    KIND_BASELINE_WRITEOFF: baseline_writeoff_payload,
    KIND_FALSIFIER_TRIPPED: falsifier_tripped_payload,
    KIND_RESOLUTION_DUE: resolution_due_payload,
    KIND_CATALYST_DUE: catalyst_due_payload,
    KIND_RESEARCH_ESCALATION: research_escalation_payload,
    KIND_RESEARCH_MONITOR_RUN: research_monitor_run_payload,
    KIND_RESEARCH_MONITOR_ERROR: research_monitor_error_payload,
    KIND_BASELINE_QUOTE_FAILURE: baseline_quote_failure_payload,
    KIND_BASELINE_AUTO_WRITEOFF: baseline_auto_writeoff_payload,
    KIND_RESEARCH_TRADE_RUN: research_trade_run_payload,
    KIND_RESEARCH_TRADE_ERROR: research_trade_error_payload,
    KIND_RESEARCH_POSITION_OPENED: research_position_opened_payload,
    KIND_RESEARCH_POSITION_CLOSED: research_position_closed_payload,
    KIND_ANALYSIS_DECISION: analysis_decision_payload,
    KIND_DAILY_OVERVIEW: daily_overview_payload,
    KIND_DAILY_OVERVIEW_ERROR: daily_overview_error_payload,
}
