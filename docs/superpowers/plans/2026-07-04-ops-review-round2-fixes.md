# Ops code review round 2 — findings and required changes

**Date:** 2026-07-04
**Reviewed at:** `17b6154` (main, post PR #7/#8)
**Predecessor:** `docs/superpowers/plans/2026-07-02-ops-review-remaining-fixes.md` — every item
in that document (5 criticals, M1–M7, L1–L7, MCP redesign) is verified implemented and correct.
This document covers only NEW findings from the round-2 review. Ops suite at review time:
448 passed / 11 skipped in 3.4s.

Conventions: follow TDD (failing test first) as with the prior round. Do not weaken any
existing guard while fixing these. Line numbers are as of `17b6154`.

---

## C1. Live-gate deadlock: LIVE_MAX_POSITION rejects instead of capping — live trading can never place a fill

**Severity:** critical (live mode only; paper unaffected). Blocks the entire graduation path.

**Where:** `ops/guardrails/sizing_rules.py:80-102` (`LiveMaxPositionRule`),
`ops/strategy/post_earnings_momentum.py:49` (sizing), `ops/live_gate.py`.

**Problem:** The strategy sizes every BUY at `current_equity × per_position_cap_pct`
(≈ $23.80 at the real account's ~$238). Nothing anywhere clamps that to
`live_max_position` ($10). `LiveMaxPositionRule` therefore **rejects every live BUY**
during the first-20-fills window → zero live fills ever occur → `count_live_buy_fills`
stays 0 → the gate never lifts. Live mode is permanently order-less, journaling
`order_rejected` every 30 minutes. The parent spec's graduation criterion #5 says fills
are "**capped at** $10" — a clamp, not a rejection. (The only accidental workaround —
manually lowering `OPS_PER_POSITION_CAP_PCT` until 10% of equity ≤ $10 — is undocumented
and defeats the point.)

**Required change:**
1. The sizing path must become live-gate-aware: while the gate is active
   (`broker_mode == "robinhood"` and `count_live_buy_fills(journal) < live_fill_gate_count`),
   proposed BUY notional = `min(equity × per_position_cap_pct, live_max_position)`.
   Wire this where sizing happens — either pass a `max_notional: Callable[[], Decimal]`
   (or the live-gate state) into `PostEarningsMomentumStrategy`, or clamp in the
   orchestrator before placement. Keep the clamped notional ≥ `per_trade_dollar_floor`
   (it is: $10 > $5).
2. **Keep `LiveMaxPositionRule` unchanged as the enforcement backstop** — the rule
   rejecting is correct defense-in-depth; the bug is that nothing upstream sizes under it.
3. Fix the related gate-consumption hole: `fill` events carry no broker mode, so paper
   fills recorded after the flip marker consume the live gate count (flip live → back to
   paper → 20 paper BUYs → gate silently lifted for the next live run).
   `GuardedBroker._journal_fill_event` (`ops/broker/guarded.py:53`) has access to
   `self._config.broker_mode` — add `"broker_mode"` to the fill event payload, and filter
   `count_live_buy_fills` (`ops/live_gate.py:25`) to `payload["broker_mode"] == "robinhood"`.
   Treat fills with no `broker_mode` key (historical events) as NOT live — fail-safe:
   the gate stays active longer, never lifts early.

**Tests:** strategy in live mode with gate active proposes exactly $10 (not $23.80) and
the order passes the full default rule chain; gate inactive (21st fill or paper mode) →
normal 10% sizing; paper-mode fill events after a flip marker do not increment
`count_live_buy_fills`; historical fill events without `broker_mode` do not count.

---

## C2. `RealRobinhoodMCPClient.connect()` has a thread race — double-connect clobbers session state and leaks a transport

**Severity:** high (live mode; fires on first connect and on every reconnect-after-drop).

**Where:** `ops/broker/mcp_client.py` — `connect()` (522), `close()` (581), `_call_tool`
(615). The class has zero locks (`grep -c threading.Lock` = 0).

**Problem:** The client is reached from at least two threads: the guardian calls
`broker.get_quote` every 60s (deliberately unlocked in `GuardedBroker.get_quote`,
`ops/broker/guarded.py:80`) while the orchestrator makes `_lock`-serialized calls. Every
Protocol method does `if self._session is None: self.connect()`. Two threads can both
observe `_session is None` and enter `connect()` concurrently: thread B overwrites
`self._ready`, `self._connect_error`, `self._shutdown_event`, `self._lifetime` while
thread A waits on the originals; two `_serve()` tasks each enter a transport;
`self._session` is whichever initialized last; `close()` can only signal the second
`_serve` — the first never exits its CMs, so `worker.stop()`'s 5s join times out and the
loop is closed with a task still inside anyio cancel scopes. Same shape for concurrent
`connect()`/`close()`.

**Required change:** Add a `threading.Lock` (`self._connect_lock`) held for the entire
body of `connect()` and `close()`; re-check `self._session is not None` *inside* the
lock (double-checked locking — the fast path in the Protocol methods stays lock-free).
`_call_tool`'s lazy `_AsyncWorker` construction (mcp_client.py:636-638) must go through
the same lock or be removed in favor of requiring `connect()` first. Do NOT hold the
lock during `submit()` of ordinary tool calls — only connect/close lifecycle.

**Tests:** two threads racing `connect()` on a client whose `_serve` is a controllable
fake → exactly one worker/one serve task, both threads return connected;
`connect()` racing `close()` → no leaked worker thread (assert thread count returns to
baseline); reconnect after simulated transport death still works under the lock.

---

## M1. Notify dispatcher poison-pill: one bad event wedges all notifications forever

**Where:** `ops/notify/dispatcher.py:39-61` (`dispatch_once`), `_handle` (63).

**Problem:** The `except Exception` in `dispatch_once` treats **every** failure as a
transport failure and holds the cursor. A rendering bug or malformed payload
(`render()` KeyError/TypeError, a policy entry error) is permanent: the same event
re-fails every 20s tick, the cursor never advances, all subsequent notifications are
blocked indefinitely, and a `notify_dispatch_error` row is journaled every 20 seconds
forever (unbounded journal growth, ~4.3k rows/day).

**Required change:**
1. Split failure domains: exceptions raised by `render()` (compute the message *before*
   the send loop) are render errors → journal `notify_render_error` (sanitized: event id,
   kind, exception type only), **advance the cursor**, continue to the next event.
2. Transport `send()` exceptions keep at-least-once semantics (hold cursor, retry), but
   add a bounded retry: track consecutive failures per event id (in-memory is fine);
   after N=10 failed dispatch attempts for the same event id, journal
   `notify_event_skipped` and advance past it. A missed notification is better than a
   dead notification channel.

**Tests:** event whose render raises → cursor advances, later events still delivered,
`notify_render_error` journaled once (not per tick); transport failing 10 times →
event skipped with `notify_event_skipped`, subsequent events delivered; transient
transport failure (fails twice, then succeeds) still delivers exactly per at-least-once.

## M2. First-enable notification storm: new cursor starts at 0 over a journal full of history

**Where:** `ops/notify/dispatcher.py:40` (`get_cursor` → 0 default), `ops/main.py:332`
(`_build_dispatcher`).

**Problem:** Enabling `OPS_NOTIFY_ENABLED` on an existing journal (weeks of paper
history) replays every historical `fill`/`stop_hit`/`daily_halt` as a push notification —
push kinds have no cooldown. Dozens-to-hundreds of stale pushes on first enable.

**Required change:** On dispatcher construction (or first `dispatch_once`), if the
consumer has no cursor row yet (distinguish "absent" from "legitimately 0" — add
`has_cursor(consumer)` or make `get_cursor` return `None` when absent) AND the events
table is non-empty, fast-forward the cursor to the current max event id and journal one
`notify_cursor_initialized` event with the skipped range. Real-time events from that
moment on are delivered normally.

**Tests:** fresh consumer + journal with 50 prior events → 0 sends, cursor at max id,
one `notify_cursor_initialized`; a new event after initialization is delivered; an
existing cursor (including a legitimate 0-with-row) is never fast-forwarded.

## M3. Daily summary uses the stale-baseline anti-pattern and record-time fills

**Where:** `ops/notify/summary.py:17` (`get_latest_equity_snapshot(kind="open_day")`
with no `since=`), `:27` (fills filtered by `f["at"]`), `ops/main.py:268-272`
(cron mon-fri regardless of trading day).

**Problem:** Three in one function: (a) day P&L is computed against the latest open_day
snapshot **ever** — after downtime that's a previous day's baseline, so the email reports
wrong P&L (this exact bug class was fixed in four other places in the same changeset);
(b) fills are bucketed by `at` (journal write time) instead of `filled_at`; (c) the 16:05
cron fires on market holidays, emailing a zero-activity summary.

**Required change:** (a) pass `since=trading_day_start(when)` to the snapshot lookup and
report "n/a" when there is no same-day baseline; (b) filter on `f["filled_at"]`; (c) gate
`emit_daily_summary` on `calendar.is_trading_day(today)` — thread the `MarketCalendar`
(already constructed in `_wire`) into `_daily_summary_tick`.

**Tests:** stale (yesterday's) open_day snapshot → P&L "n/a", not a number; fill with
`filled_at` today but journaled at a different time buckets correctly; non-trading day →
no `daily_summary` event.

## M4. Kill-switch notification renders with an empty body

**Where:** `ops/notify/policy.py:63-65`.

**Problem:** `render()` reads `payload.get('reason', '')`, but the guardian's
`kill_switch` payload (`ops/position_guardian.py:225-234`) carries
`mode/equity_now/equity_open_week/pct/threshold` — no `reason` key. The single most
critical push notification in the system currently has an empty body.

**Required change:** Render the actual numbers, e.g.
`"Weekly drawdown {pct} breached {threshold} (equity ${equity_now} vs week-open ${equity_open_week}); mode={mode}"`,
falling back to the generic key=value join for unknown payload shapes. Keep the SPOT
scrub on the result.

**Tests:** render a real guardian-shaped kill_switch payload → body contains pct,
threshold, and both equity figures; body is non-empty for a payload with no keys at all.

## M5. SMTP creds ride an unverified TLS channel

**Where:** `ops/notify/email.py:31-35`.

**Problem:** Confirmed on the project's Python: `smtplib.SMTP.starttls()` with no
`context=` uses `ssl._create_stdlib_context()`, which **is**
`ssl._create_unverified_context` — no certificate verification. A MITM on the network
path can present any cert and capture the SMTP password.

**Required change:** `smtp.starttls(context=ssl.create_default_context())`. While in the
file, document (docstring or README) that the transport is STARTTLS-only — implicit-TLS
port 465 will not work with this flow; 587 is the supported default.

**Tests:** unit-assert the context passed to `starttls` verifies
(`verify_mode == ssl.CERT_REQUIRED`, `check_hostname is True`) via a mocked SMTP.

## M6. `place_order` holds the GuardedBroker lock through the entire fill-poll window

**Where:** `ops/broker/guarded.py:83-116` (lock scope) →
`ops/broker/mcp_client.py:884-935` (`_await_fill`, 30s window + per-call 30s timeouts).

**Problem:** A live BUY holds `GuardedBroker._lock` for up to ~30–60s while polling for
the fill. During that window `get_positions`/`get_equity`/`get_cash` are all blocked, so
a concurrent guardian pass cannot even read positions — stop-loss enforcement stalls
behind order placement. Tolerable at one order per 30-minute tick; a multi-proposal tick
serializes them back-to-back and can starve the guardian for minutes.

**Required change (choose deliberately, document the choice):** Preferred: keep rule
evaluation + order *submission* under `_lock`, but release it for the poll phase —
split the inner broker's place into `submit` (locked; journals the order row) and
`await_fill` (unlocked; journals the fill row on completion). The sizing rules read
pre-trade state, which the submission already fixed, so releasing during the poll does
not reopen the double-BUY race as long as a second `place_order` cannot *submit* while
another order for the same tick is unfilled — simplest guard: track in-flight order
count and have `CashReserveRule`-style checks include in-flight notional, or simply
accept one-at-a-time submission per tick (current orchestrator behavior) and document
it. If the split is judged too invasive for now, at minimum shorten the poll window
(`window_s`) so the guardian is never blocked longer than one 60s cycle, and record the
decision in the design doc.

**Tests:** with a slow-filling fake client, a concurrent `get_positions` returns while
the fill poll is still in progress (preferred fix); or window bound asserted ≤ 60s.

---

## Low

### L1. OAuth callback accepts exactly one connection and trusts it
`ops/broker/mcp_client.py:234-259`. A speculative browser preflight or any stray probe
to 127.0.0.1:51823 consumes the single `sock_accept`; if it carries no `code`, the whole
flow fails. Loop: accept → parse → if no `code`, respond 404 and accept again (bounded
by the existing connect timeout). Ignore non-`/callback` paths.

### L2. `count_live_buy_fills` / `flip_epoch` full-scan the events table per BUY evaluation
`ops/live_gate.py:14,21,29-35` — `read_events()` loads and JSON-parses every event row
on every rule pass. Replace with SQL: a `SELECT at FROM events WHERE kind=? ORDER BY id
LIMIT 1` for the marker and a `COUNT(*)` with kind+at filter for fills (payload-side
filter can use SQLite `json_extract`). Add the Journal helpers; keep the lock pattern.

### L3. `Journal._to_iso` preserves non-UTC offsets while queries compare strings lexicographically
`ops/journal.py:80-83`. All current writers pass UTC, but the first future caller passing
an ET-aware `at=` silently corrupts `>=` comparisons. Normalize:
`dt.astimezone(timezone.utc).isoformat()` in `_to_iso`. Test with an ET-aware input
round-tripping to a correctly-ordered UTC string.

### L4. `_pick_quote_price` deviates (correctly) from the design doc
`ops/broker/mcp_client.py:759-781` vs
`docs/superpowers/specs/2026-07-04-tradingagents-mcp-live-design.md` §Quotes ("more
recent by timestamp"). The code's fallback-only behavior is right (only one venue
timestamp exists); update the design doc so a future "fix" doesn't regress the code to
match stale prose.

### L5. Upstream (not ops): "unit" sentiment tests do live network
`tests/test_structured_agents.py::TestSentimentAnalystAgent` (and the render tests that
call the analyst) invoke `create_sentiment_analyst`, which calls
`fetch_stocktwits_messages`/`fetch_reddit_posts` live
(`tradingagents/agents/analysts/sentiment_analyst.py:70-71`). Under Reddit 429s the
default suite stalls for minutes at 94% with zero CPU. Patch both fetchers in those
tests (fixture returning canned blocks). This is upstream test hygiene, not ops — keep
the change scoped to tests.

---

## Explicitly verified as fixed (do not re-open)

All items from `2026-07-02-ops-review-remaining-fixes.md`: the 5 criticals (guardian
market gate, ack-status enforcement, cash seed/live baseline, live stop rehydration,
kill-switch fresh baseline + resumable close), M1–M7, L1–L7, and the MCP worker-thread
redesign including OAuth via `OAuthClientProvider`, `MCPProtocolError` split,
agentic-account resolution, and the fill-polling lifecycle with best-effort cancel.

## Suggested sequencing

1. **C1** (live-gate clamp + mode-tagged fills) and **C2** (connect lock) — both are
   prerequisites for ever flipping `OPS_BROKER_MODE=robinhood`.
2. **M1 + M2** (dispatcher poison-pill, cursor fast-forward) — before enabling
   notifications on the long-running paper journal.
3. **M4 + M5** (kill-switch body, SMTP context) — small, high value, same subsystem.
4. **M3** (summary correctness) — next time someone touches notify.
5. **M6** — decide the lock-scope strategy deliberately; fine to defer behind a
   documented window bound.
6. **L1–L5** — batch of small cleanups; L5 lives in upstream tests.
