# Ops code review — remaining findings and required changes

**Date:** 2026-07-02
**Reviewed at:** `ab4d5ba` (feat/ops-notifications)
**Status:** The five critical findings from the review are FIXED in the working tree
(guardian market-hours gate, ack.status enforcement, cash seed/deposit adjustments,
live stop rehydration, kill-switch fresh baseline + resumable close). This document
specifies the changes required for everything else, in priority order.

---

## Medium severity

### M1. `daily_halt` is consumed but never produced

**Where:** `ops/scheduler/orchestrator.py` (`_is_daily_halted`), `ops/position_guardian.py`.

**Problem:** The orchestrator short-circuits on a `daily_halt` event that no production
code emits. The −7% protection exists only as `DailyDrawdownRule` rejecting each BUY at
order time, which means (a) the Plan 3c `daily_halt` push notification can never fire,
and (b) on a halted day the orchestrator still runs the full universe through the LLM
pipeline every 30 minutes, paying real API money to produce orders that get rejected
one at a time.

**Required change:** The guardian computes daily drawdown in the same pass as the weekly
check: read `open_day` snapshot (`since=` today's start, same freshness rule as the
weekly baseline), compare to current equity, and when pct ≤ `daily_drawdown_pct` record
`daily_halt` once per day (guard with `has_event_today("daily_halt")`). No position
closing — the halt only stops new BUYs, which the existing orchestrator check and rule
already do. Keep `DailyDrawdownRule` as the order-boundary backstop.

**Tests:** guardian records `daily_halt` at exactly the threshold, not above it; second
pass same day does not duplicate; orchestrator `tick` returns before calling the
universe builder when the event exists (assert universe_builder not called).

### M2. Stop price computed from stale universe price, not entry fill

**Where:** `ops/strategy/post_earnings_momentum.py` (`stop = cand.last_price × (1 + stop_pct)`),
`ops/guardrails/static_rules.py` (`StopAttachedRule`).

**Problem:** `last_price` is the previous close from a 20-day history call. Spec says
*entry* × 0.92. A gap-down open can produce fill $91 with stop $92 → guaranteed
instant stop-out on the next guardian pass. A gap-up produces a stop far looser than −8%.
Nothing validates stop < fill anywhere.

**Required change:** Make the stop entry-relative by construction:
1. Add `stop_pct: Decimal | None` to `Order` (entry-relative, must be negative).
   Strategies set `stop_pct=config.per_position_stop_pct` and stop computing an
   absolute price from stale data.
2. `StopAttachedRule` requires `stop_pct is not None and stop_pct < 0` on BUYs.
3. At fill time the broker computes the absolute stop from the actual fill price
   (`fill.price × (1 + stop_pct)`) and journals THAT on the fill row (both PaperBroker
   and RobinhoodBroker — the plumbing for journaling fill stops already exists).
4. Keep `stop_loss_price` on `Order` only for SELL/limit plumbing or drop it for BUYs
   entirely; migration is easy since the journal stores the resolved absolute stop.

**Tests:** fill below the reference price still yields stop < fill; the journaled stop
equals fill×(1+pct) exactly; `StopAttachedRule` rejects `stop_pct=None` and positive pct.

### M3. `client_order_id` is not unique across ticks

**Where:** `ops/strategy/post_earnings_momentum.py::_client_order_id`
(`pem-{date}-{symbol}-{idx}`), `ops/broker/paper.py::from_journal` (`orders_by_id` dict).

**Problem:** The same symbol at the same index recurs every 30-minute tick on the same
date (e.g. after a CashReserveRule rejection), producing duplicate ids. Journal replay
keys orders by `client_order_id` — last write wins, so a fill can be matched to the
wrong order's notional and replayed cash drifts. Live brokers treat client order ids as
idempotency keys: retried orders get silently deduplicated or errored.

**Required change:** `f"pem-{date}-{symbol}-{uuid4().hex[:8]}"` (keep the greppable
prefix). Then add `CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_coid ON
orders(client_order_id)` to the journal schema so any future collision is an error at
write time, not silent corruption at replay time. (The `close-{symbol}-{uuid[:8]}` ids
are already unique.)

**Tests:** two proposals for the same symbol/date produce distinct ids; journal raises
on duplicate insert.

### M4. Earnings filter silently dropped the revenue-beat requirement

**Where:** `ops/universe/earnings.py` (`find_recent_earnings_beats` checks only
`eps_beat`; `_fetch_from_yfinance` fills revenue fields from columns that don't exist
in yfinance's `earnings_dates`, so `revenue_actual = revenue_estimate = 0` and
`revenue_beat` is unconditionally False).

**Problem:** Spec: "EPS beat **and** revenue beat." Code enforces EPS only, and had the
spec been enforced with this data source the universe would always be empty. The
`EarningsHit` fields also lie — fake zeros presented as data.

**Required change:** Pick one, deliberately:
- **(a) Amend the spec** to EPS-beat-only and make `revenue_*` fields
  `Decimal | None` / `revenue_beat: bool | None` so absent data is honest. Cheapest.
- **(b) Enforce the spec** by sourcing revenue actual/estimate from Finnhub's earnings
  calendar (`revenueActual`/`revenueEstimate`; a `FINNHUB_API_KEY` already exists in the
  upstream config). Filter requires both beats; symbols with missing revenue data are
  skipped, not passed.

Recommendation: (b), it's what the risk posture in the spec intended; fall back to (a)
only if the data quality turns out poor.

**Tests:** with revenue data present, EPS-beat-but-revenue-miss is excluded; missing
revenue data excludes (option b) or passes with `revenue_beat=None` (option a).

### M5. The "hard rules" enforce naming conventions, not behavior

**Where:** `ops/guardrails/static_rules.py`.

**Problem / required change per rule:**
- `NoMarginRule` (checks `symbol.startswith("MARGIN:")`): meaningless. Real margin
  protection: at startup in live mode, read `AccountInfo` and halt with a journaled
  event if `buying_power > cash` (margin enabled on the account). At order time the
  binding constraint is already `CashReserveRule`; keep it.
- `LongOnlyRule` (checks `client_order_id.startswith("SHORT-")`): meaningless. Real
  check: on SELL, `order.notional_dollars / quote` must not exceed the held quantity
  (+epsilon), using `ctx.broker.get_positions()`. PaperBroker enforces this internally;
  the rule makes it hold for every broker at the guarded boundary.
- `NoOptionsRule` (space + length ≥ 16): replace with an equity-symbol whitelist
  pattern: reject unless `re.fullmatch(r"[A-Z]{1,5}([.-][A-Z])?", symbol)`. This also
  subsumes most of `NoCryptoRule`'s job (BTC-USD fails the pattern).
- `DenyListRule`: add a side distinction. BUYs of denied symbols: always reject.
  SELLs: allow for the leveraged-ETF list (selling reduces risk; today a manually
  acquired TQQQ position can never be stop-sold or kill-switch-closed and the guardian
  journals `stop_failed` every 60s forever), but keep SPOT sell-blocked (contractual
  full blackout — document that a manual SPOT position is untouchable by design).
  Also normalize case: `symbol.upper()` before the membership test.

**Tests:** one allow/one block/one edge per changed rule, per the parent spec's testing
strategy. For `LongOnlyRule`, an over-sell and an exact-quantity sell.

### M6. Live startup has no broker-unreachable handling

**Where:** `ops/main.py::run` — `_build_broker` (now calls `_ensure_live_baseline` →
`get_cash`) and `reconcile` both raise `BrokerError` straight through to a traceback.

**Required change:** Wrap the build+reconcile sequence in `try/except BrokerError`:
journal `broker_unreachable` + `startup_halted` (reason `broker_unreachable`), print a
one-line hint, exit with a distinct code (3). Do NOT start the guardian in this state —
it has no broker either. The spec's "retry next cycle" behavior belongs to the
orchestrator's tick, not to startup; startup should fail loudly and let launchd/tmux
restart policy handle retry.

**Tests:** `FakeMCPClient.fail_next(MCPUnavailable(...))` through `run()` (or a
directly-tested `_startup()` helper) → exit 3, both events journaled, no traceback.

### M7. Timezone soup: risk accounting on UTC, market on ET

**Where:** `ops/journal.py` (`has_event_today`, `has_event_since_last_monday`),
`ops/scheduler/orchestrator.py::_maybe_snapshot_equity`, `ops/main.py`
(`_start_of_day_equity`/`_start_of_week_equity`), `ops/position_guardian.py`
(weekly baseline monday computation).

**Problem:** Day/week boundaries are UTC midnight / UTC Monday while the market and the
cron triggers are `America/New_York`. Examples: an event at 21:00 ET Monday timestamps
into Tuesday UTC, so a "daily" halt boundary rolls at 19:00/20:00 ET; Sunday-evening ET
events land in Monday UTC and confuse the weekly idempotency window.

**Required change:** One helper module (`ops/trading_time.py`):
`TRADING_TZ = ZoneInfo("America/New_York")`, `trading_day_start(now) -> datetime`,
`trading_week_start(now) -> datetime`, both returning tz-aware UTC instants computed
from ET calendar boundaries. Every boundary computation above calls these; stored
timestamps stay UTC. The five call sites currently roll their own — including the ones
this review's critical fixes touched (they deliberately kept the existing UTC
convention for consistency; migrate them together in one commit).

**Tests:** event at 2026-07-06 21:00 ET counts as "today" for a 2026-07-06 22:00 ET
check; Sunday 20:00 ET belongs to the previous trading week.

---

## Low severity

### L1. Journal/PaperBroker cross-thread access
One sqlite3 connection (`check_same_thread=False`, autocommit) is shared by the
orchestrator, guardian, and (Plan 3c) dispatcher threads; PaperBroker's dicts are read
by the guardian while the orchestrator mutates them. CPython's serialized SQLite mostly
tolerates the former; the latter can raise "dict changed size during iteration" and
abort a guardian pass. **Change:** add a `threading.Lock` inside `Journal` wrapping
every execute/fetch, and route `GuardedBroker.get_positions/get_equity/get_cash`
through `self._lock` (it already exists and covers writes).

### L2. Guardian live-mode quote failure aborts the whole pass
`RobinhoodBroker.get_quote` raises `BrokerError`, not `QuoteUnavailable`, so one bad
quote skips the outer catch-all and abandons every remaining position that minute.
**Change:** catch `(QuoteUnavailable, BrokerError)` at the per-position quote step.
Add blind-guardian escalation per the spec: count consecutive fully-failed passes in
the guardian; at ≥5 journal `guardian_blind` (Plan 3c policy table: push+email).

### L3. `from_journal` orphan-SELL replay is silent
`ops/broker/paper.py` comment says "log and skip"; it skips without logging and drops
the sell's cash effect. **Change:** journal a `journal_replay_orphan_sell` event
(mirrors `journal_replay_fallback`).

### L4. `journal_path` defaults to a CWD-relative file
Running `ops run` from the wrong directory silently creates a fresh journal and a fresh
paper account. **Change:** default to an absolute state path
(`~/.local/state/tradingagents/ops_journal.sqlite`, honoring `XDG_STATE_HOME`), keep
`OPS_JOURNAL_PATH` override. At minimum, `ops run` should print the resolved absolute
path at startup and warn when it's creating a brand-new file.

### L5. S&P 500 cache written into the installed package directory
`ops/universe/sp500.py` writes `ops/universe/_data/sp500_members.json` — breaks
read-only installs. **Change:** cache under `~/.cache/tradingagents/` (or
`platformdirs`), keep an optional bundled snapshot as first-run fallback.

### L6. `Order.__post_init__` redundant checks
The BUY and SELL branches are identical and both subsume the `< 0` check. Collapse to
`if self.notional_dollars <= 0: raise ValueError(...)`.

### L7. Plan 3c follow-ups (for the in-flight notifications work)
- The new `fill` event is only emitted for confirmed fills now that ack.status is
  enforced — keep it that way; a `fill` notification for a queued order is a lie.
- `notify_dispatch_error` payloads must not embed raw transport exception strings —
  `smtplib`/`requests` errors can contain credentials and hostnames. Journal exception
  type + a sanitized message only.
- `ops/notify/config.py:33`: wrap `int(port_raw)` with the same named-variable error
  message pattern as `ops/config.py::_env_int`.
- Once notifications exist, wire `order_not_filled` (new event from the ack fix) into
  the policy table — push+email, no cooldown: it means a live order is dangling at the
  broker and may need manual cancel.

---

## MCP integration verdict (feeds Task 12)

**What's correct:** the `CallToolResult` handling in `_call_tool` (`isError`,
`structuredContent`-preferred, `content` fallback) matches the installed `mcp==1.28.1`
SDK exactly; `ClientSession.call_tool` is indeed async-only as the docstring claims;
the sync `RobinhoodMCPClient` Protocol is the right seam (FakeMCPClient proves it);
token-file handling (0600 via `os.open`, env override) is fine.

**What cannot work as designed:**

1. **The client is inert.** `connect()` never establishes `_session`; every call today
   is `AttributeError` → `MCPUnavailable`. Known WIP, but see M6 — the CLI arms live
   mode with zero guard against this.
2. **The per-call `run_until_complete` bridge cannot host a persistent session.**
   `streamablehttp_client` and `ClientSession` are async context managers built on
   anyio task groups; anyio cancel scopes must be entered and exited in the same task.
   Entering the transport in one `run_until_complete` invocation and calling tools in
   later ones runs each step in a different task on that loop → `RuntimeError`
   (cancel-scope task affinity). The stored-loop design in `connect()`/`_call_tool`
   is therefore a dead end, not just unfinished.

   **Required design:** one daemon worker thread runs a single coroutine that owns the
   entire lifecycle —
   `async with streamablehttp_client(endpoint, auth=...) as (r, w, _): async with
   ClientSession(r, w) as s: await s.initialize(); <serve requests until shutdown>` —
   and the sync Protocol methods submit work with
   `asyncio.run_coroutine_threadsafe(...).result(timeout=...)`, mapping timeout/errors
   to `MCPUnavailable`. `close()` signals the coroutine and joins the thread. The
   Protocol surface doesn't change; only the plumbing behind `_call_tool` does.
3. **OAuth:** use the SDK's `mcp.client.auth.OAuthClientProvider` with a `TokenStorage`
   implementation backed by the existing `_read_token`/`_write_token` helpers, passed
   as `auth=` to `streamablehttp_client`. Don't hand-roll the flow.
4. **Every tool name and response field is an unverified guess** (`get_accounts`,
   `accounts[0].cash`, `get_equity_positions`, `get_equity_quotes`,
   `last_trade_price`, `place_equity_order` params, ack fields). Task 12 step one:
   `list_tools()` against the real endpoint, pin the actual schemas as recorded
   fixtures, then adjust the DTO mapping.
5. **`except Exception → MCPUnavailable` hides protocol regressions.** A `KeyError`
   from a renamed response field reports as an outage. Catch transport errors narrowly;
   raise a distinct `MCPProtocolError` for shape mismatches so reconciliation/alerts
   can tell "Robinhood is down" from "our parser is wrong".
6. **Order lifecycle:** a real `place_equity_order` almost certainly acks before
   filling. With ack.status now enforced (critical fix #2) the system fails safe by
   raising, but live usability needs: poll `get_order(order_id)` for a bounded window
   for market orders during RTH, and call the already-wired-but-unused
   `cancel_equity_order` on timeout, journaling the outcome either way. This is also
   the natural home for the spec's kill-switch "cancel pending orders" behavior, which
   remains unimplemented in both modes.

---

## Suggested sequencing

1. **M6 + L2** (startup and guardian resilience) — small, protects live experiments.
2. **M2 + M3** (entry-relative stops, unique order ids) — correctness of every future
   paper data point; do before accumulating the 8-week graduation dataset.
3. **M1** (daily_halt emission) — unblocks the Plan 3c policy table and stops wasted
   LLM spend on halted days.
4. **M7 + L1** (timezone unification, locking) — one focused commit each.
5. **M5, M4** (real rules, revenue beat) — behavior changes; land with spec updates.
6. **L3–L6** — batch of small cleanups.
7. **MCP worker-thread redesign** — as the first task of Task 12, before OAuth wiring.
