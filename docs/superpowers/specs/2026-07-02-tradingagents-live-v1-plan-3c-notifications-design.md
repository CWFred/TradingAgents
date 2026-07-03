# TradingAgents live-v1 — Plan 3c: Notifications + LIVE_MAX_POSITION gate (design)

**Date:** 2026-07-02
**Status:** approved (brainstorming)
**Parent spec:** `docs/superpowers/specs/2026-06-30-tradingagents-live-v1-design.md`
**Predecessors:** Plan 3a (broker plumbing, PR #5), Plan 3b (orchestrator, PR #6). Main @ 558224b, 248 tests passing / 4 opt-in skipped.

Plan 3c is the last of the three-way split of the original Plan 3. It adds two independent features:

1. **Notifications** — Pushover push + SMTP email, driven by a pull-based event dispatcher that reads the journal's `events` table through a durable cursor.
2. **LIVE_MAX_POSITION first-N-fills gate** — a code-enforced $10 cap on the first 20 live BUY fills after `broker_mode` flips paper→robinhood (parent spec §Graduation criteria #5).

## Constraints carried in from prior work

- **SPOT blackout** (`project_spot_blackout.md`): SPOT is contractually blacklisted. Enforced today at two execution layers — `DenyListRule` (config `deny_list`) and a hard `if` inside `RobinhoodBroker._enforce_spot_hard_check`. Neither is weakened here. 3c adds a third, presentation-layer guard: **notification bodies redact any SPOT symbol** so no push/email can ever surface a SPOT price or position (which could read as a trade signal).
- **Journal is authoritative**: paper mode's reconciler expects zero diffs because `_build_broker` uses `build_guarded_paper_broker_from_journal`. Everything 3c writes to the journal is an append to the `events` table (or the new cursor table) — it never mutates broker state outside the journal path, so restarts still reconcile clean.
- **Guardian/scheduler safety**: any long-running poll wraps its body in try/except that journals an error and never raises, so an APScheduler job survives a transient failure (mirrors `check_stops_once` → `guardian_check_error`).
- **RH client owns its event loop**: `RealRobinhoodMCPClient` runs coroutines on a client-owned loop. Notification code is fully synchronous (`requests` + `smtplib`) and never calls `asyncio.run`.

## Verified starting facts (from real code, not the bootstrap)

- **Fills are NOT events.** `record_fill` writes the `fills` table; `read_events()` reads the `events` table. `GuardedBroker.place_order`/`close_position` return the inner `Fill` but emit **no `fill` event**. A pull dispatcher on `events` cannot see fills until we add one. (The bootstrap's claim that 3b emits a `fill` event is inaccurate.)
- **`read_events()` exposes no row `id` and takes no `since` argument.** The `events.id` autoincrement column exists but is not surfaced — so there is no cursor read API yet.
- **`daily_halt` is read but never written** by production code (only the kill switch writes a halt today). Not in 3c scope to fix; the notification policy table simply includes it for when it is emitted.
- **No runtime broker-mode flip and no mode tag on fills.** `broker_mode` is static config resolved at process start (`_build_broker`). "The flip" = restarting `ops run` with `OPS_BROKER_MODE=robinhood`. Fills carry `at`/`filled_at` but no mode column, so counting "live fills since the flip" needs an explicit marker event.
- **Rules receive `RuleContext(order, broker, config)`** — no journal. Rules that need journal-derived data are constructed with a callable (the `DailyDrawdownRule(start_of_day_equity=...)` pattern). The chain is assembled in `ops/__init__.py:build_default_rule_chain`.
- **HTTP: `requests>=2.32.4` is already a dep.** No `smtplib` usage anywhere yet (stdlib, available).

---

## Feature A — Notifications

### A1. Event source and cursor (pull)

Chosen over a synchronous push tap on `record_event` because it decouples the notification layer from the journal write path and survives dispatcher restarts via a persisted cursor.

Additions to `ops/journal.py`:

- `read_events_since(min_id: int, limit: int | None = None) -> list[dict]` — `SELECT id, at, kind, payload FROM events WHERE id > ? ORDER BY id`. Each dict gains an `"id": int` field (existing `read_events()` is left untouched for its current callers).
- New table `dispatch_cursors(consumer TEXT PRIMARY KEY, last_event_id INTEGER NOT NULL)` created in the same `executescript` schema block, with:
  - `get_cursor(consumer: str) -> int` (returns `0` when absent),
  - `set_cursor(consumer: str, last_event_id: int) -> None` (upsert).

The dispatcher's consumer key is a constant, e.g. `"notify"`.

### A2. Fill-event gap fix

`GuardedBroker` emits a `fill` event so fills are visible to the event cursor. After a successful `inner.place_order(order)` / `inner.close_position(symbol, ...)` returns a `Fill`, record:

```
record_event("fill", {
    "client_order_id", "order_id", "symbol", "side",
    "quantity": str, "price": str, "filled_at": iso, "context": "place"|"close",
})
```

This is a pure append to `events`; it does not touch broker state or the `fills` table, so the reconciler is unaffected. Existing 3b behaviour (the `fills` table row) is unchanged — this is additive.

### A3. Dispatcher

`ops/notify/dispatcher.py` — `NotifyDispatcher(journal, transports, policy, *, consumer="notify")`.

- `dispatch_once()`: read events since the cursor, and for each event look up its policy, render, and hand to the matching transports. On success, advance the cursor to the last successfully-processed event id.
- Wrapped so a transport failure journals `notify_dispatch_error` and **stops advancing the cursor past the failed event** — the event is retried next tick rather than lost. (At-least-once delivery; transports must tolerate an occasional duplicate, which is acceptable for push/email.)
- Runs as an APScheduler `IntervalTrigger` job (~20s) added alongside `guardian_poll` in `ops/main.py` (`_start_full_scheduler` and, for read-only alerting, `_start_guardian_only`). Runs in APScheduler's threadpool, so blocking `requests`/`smtplib` calls never stall the guardian or orchestrator.

### A4. Transports

`ops/notify/transport.py` defines a small `Transport` protocol: `send(self, message: NotifyMessage) -> None`. `NotifyMessage` = `{title, body, urgency}`.

- `ops/notify/push.py` — `PushoverTransport`: `requests.post("https://api.pushover.net/1/messages.json", ...)` with `user_key`/`app_token`. `urgency` maps to Pushover `priority`.
- `ops/notify/email.py` — `EmailTransport`: stdlib `smtplib` (`SMTP` + `starttls`), `EmailMessage` from `from_addr`→`to_addr`.

Both are constructed from `NotifyConfig`. If required credentials are missing, the transport is **disabled at construction** (a no-op that logs once) — never a crash. A disabled transport still lets the dispatcher advance its cursor (a missing channel is not a delivery failure).

### A5. Configuration — separate `NotifyConfig`

A new frozen dataclass `NotifyConfig` in `ops/notify/config.py`, loaded by `load_notify_config()` from `OPS_*` env vars (same `os.environ.get` pattern as `load_config`). Kept **separate from `OpsConfig`** to keep delivery secrets out of the risk-parameter object.

Fields: `notify_enabled: bool` (default False), `pushover_user_key`, `pushover_app_token`, `smtp_host`, `smtp_port: int` (default 587), `smtp_user`, `smtp_password`, `smtp_from`, `smtp_to`. All string secrets default to `None`.

Env: `OPS_NOTIFY_ENABLED`, `OPS_PUSHOVER_USER_KEY`, `OPS_PUSHOVER_APP_TOKEN`, `OPS_SMTP_HOST`, `OPS_SMTP_PORT`, `OPS_SMTP_USER`, `OPS_SMTP_PASSWORD`, `OPS_SMTP_FROM`, `OPS_SMTP_TO`.

### A6. Policy table and rendering

`ops/notify/policy.py` maps each event `kind` to `(transports, urgency, cooldown_seconds)`, plus a renderer producing `(title, body)`:

| Event | Push | Email | Throttle |
|---|---|---|---|
| `kill_switch`, `stop_failed`, `kill_switch_close_failed`, `inconsistency`, `startup_halted`, `positions_recovered_without_stops` | ✓ | ✓ | none |
| `stop_hit`, `daily_halt`, `fill` | ✓ | — | none |
| `broker_unreachable`, `orchestrator_tick_error`, `guardian_check_error`, `quote_unavailable` | — | ✓ | per-kind cooldown |
| `order_rejected`, `journal_replay_fallback`, `notify_dispatch_error` | — | — | not notified |
| `daily_summary` | ✓ one-line | ✓ full | one/day (enforced by the emitter, §A7) |

- **Cooldown** is a per-kind in-memory "last sent at" map inside the dispatcher: while within the window, the event is counted and skipped (its cursor still advances — it is *seen*, just not re-sent). Prevents an outage from firing an email every tick. Cooldown state is in-memory only; a restart resets it (acceptable — worst case one extra alert after a restart).
- **SPOT scrub**: the renderer passes every rendered `title`/`body` through a redaction step that removes/masks any `SPOT` token. Unit-tested with a synthetic event carrying `symbol: "SPOT"`.
- `not notified` kinds are read from the cursor and skipped (cursor advances) — they exist for the journal/audit trail, not the user.

### A7. `daily_summary` (included in 3c)

A new APScheduler job at market close (~16:05 ET, `CronTrigger(hour=16, minute=5, day_of_week="mon-fri")`) computes the day's summary from the journal (equity snapshot delta, fills today, open positions with SPOT scrubbed) and records one `daily_summary` event with a structured payload. The `has_event_today("daily_summary")` guard makes the emit idempotent (one per day even across restarts). The dispatcher then renders a one-line push + full email per the policy table.

### A8. CLI

Add `ops notify-once` (`@cli.command`) — a thin wrapper that opens the journal, builds transports from `load_notify_config()`, and runs `dispatch_once()` a single time. Useful for manual smoke-testing and for the eyes-on-glass merge gate. The always-on path remains the interval job inside `ops run`; no separate long-running process.

---

## Feature B — LIVE_MAX_POSITION first-N-fills gate

### B1. Rule

`LiveMaxPositionRule` in `ops/guardrails/sizing_rules.py`, constructed with a `live_fill_count: Callable[[], int]` closure (mirrors the drawdown rules' callables):

```
def check(self, ctx):
    if ctx.order.side != Side.BUY:                 return allow()
    if ctx.config.broker_mode != "robinhood":      return allow()   # inert in paper
    if self._live_fill_count() >= ctx.config.live_fill_gate_count:  return allow()  # gate lifted
    if ctx.order.notional_dollars > ctx.config.live_max_position:
        return reject(f"live-gate: first {gate} fills capped at ${cap}")
    return allow()
```

Inserted **before** `PerPositionCapRule` in `build_default_rule_chain` (`ops/__init__.py`). Independent of `PerPositionCapRule` — during the gate window the stricter of the two applies; after the gate lifts, only `PerPositionCapRule` constrains.

### B2. Live-fill counting

The count is **BUY fills executed in live mode since the flip** (confirmed decision — the $10 cap governs new exposure, so only the fills it constrains consume the gate; a stop-out SELL does not).

- **Flip marker**: on robinhood startup, `run()`/`_build_broker` emits a one-time `broker_mode_live` event, guarded by `has_event_since...`/existence check so only the first-ever live start records it. Its `at` timestamp is the flip epoch.
- The `live_fill_count` closure = number of `fill` events with `side == "BUY"` and `at >= flip_epoch`. (Uses the new `fill` events from §A2, which carry `side`; the closure reads them via `read_events`/a small count query.) If no `broker_mode_live` marker exists, the count is `0` (gate fully active) — fail-safe.

### B3. Config

Two new `OpsConfig` fields: `live_max_position: Decimal = Decimal("10")` and `live_fill_gate_count: int = 20`, validated in `__post_init__` (`live_max_position > 0`, `live_fill_gate_count >= 0`), sourced from `OPS_LIVE_MAX_POSITION` / `OPS_LIVE_FILL_GATE_COUNT` in `load_config`. Defaults match the parent spec.

---

## Testing strategy

Unit (default suite, no network):
- Journal: `read_events_since` returns ids and respects `min_id`; cursor get/set/upsert round-trips.
- Fill event: `GuardedBroker` emits a `fill` event on a successful place and close; none on rejection.
- Dispatcher with a **fake transport**: routes by policy, advances cursor only on success, holds cursor on transport failure (retry), resumes from cursor across a fresh dispatcher instance (restart), honours per-kind cooldown, and **SPOT-scrubs** a synthetic `SPOT` event.
- Transports constructed with missing creds are disabled no-ops (no crash, cursor still advances).
- `daily_summary` emitter: idempotent per day; payload excludes SPOT.
- `LiveMaxPositionRule`: allow SELL; inert in paper; blocks a $15 BUY at 0 fills; allows a $9 BUY; boundary — 20th live BUY still gated, 21st passes to `PerPositionCapRule`; counts BUY only (a SELL fill does not consume the gate).

Integration:
- Restart resume: emit events, run `dispatch_once`, kill, new dispatcher instance continues from the persisted cursor with no duplicate sends of already-acked events.

Opt-in live tests (NOT in the default run, gated behind an env flag like `OPS_RH_LIVE_TESTS`, e.g. `OPS_NOTIFY_LIVE_TESTS=1`): real Pushover POST and real SMTP send.

## Package layout

```
ops/notify/
    __init__.py
    config.py        # NotifyConfig + load_notify_config
    transport.py     # Transport protocol, NotifyMessage, disabled no-op
    push.py          # PushoverTransport (requests)
    email.py         # EmailTransport (smtplib)
    policy.py        # kind -> (transports, urgency, cooldown) + renderer + SPOT scrub
    dispatcher.py    # NotifyDispatcher, dispatch_once
    summary.py       # daily_summary computation + idempotent emit
```

Touched existing files: `ops/journal.py` (cursor API + `read_events_since`), `ops/broker/guarded.py` (`fill` event), `ops/guardrails/sizing_rules.py` (`LiveMaxPositionRule`), `ops/__init__.py` (chain wiring + `live_fill_count`), `ops/config.py` (two live-gate fields), `ops/main.py` (dispatcher job, `daily_summary` job, `broker_mode_live` marker), `ops/cli.py` (`notify-once`).

## Out of scope for 3c

- Auto-graduation paper→live (still a manual flip).
- Slack/webhook transports beyond Pushover + email.
- A web dashboard.
- Retry/escalation ladders beyond at-least-once + per-kind cooldown (the parent spec's "escalate to email after 5 min" for `stop_unfilled` is deferred — `stop_unfilled` is not currently emitted).
