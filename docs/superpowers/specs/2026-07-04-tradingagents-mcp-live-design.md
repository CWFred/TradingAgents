# TradingAgents — Robinhood MCP live client finalization (design)

**Date:** 2026-07-04
**Status:** design (approach pre-endorsed by the Plan-3c review "MCP integration verdict")
**Predecessor:** `docs/superpowers/plans/2026-07-02-ops-review-remaining-fixes.md` §"MCP integration verdict"

Finalizes `ops/broker/mcp_client.py::RealRobinhoodMCPClient`, the only deferred item from Plan 3c. The stub is replaced with a working client verified against the **real** Robinhood Agentic MCP endpoint (`https://agent.robinhood.com/mcp/trading`), introspected live on 2026-07-04.

## Ground truth captured live (read-only) — authoritative schema reference

The endpoint is standard MCP over streamable HTTP; auth is standard MCP OAuth (the `claude mcp add … --transport http` flow completes it with no manual client registration). **Every tool response is wrapped `{"data": {...}, "guide": "..."}`** — the `guide` is model-facing prose, ignored by code; all real fields live under `data`. Field paths differ materially from the stub's guesses.

### Accounts — `get_accounts` (no args)
```
data.accounts[] : { account_number, rhs_account_number, type ("cash"|"margin"),
  brokerage_account_type, nickname?, is_default, agentic_allowed (bool),
  option_level, management_type, state, deactivated, permanently_deactivated }
```
- **Only `agentic_allowed == true` accounts are tradeable by this agent.** In this user's list exactly one qualifies: nickname "Agentic", type `cash` (masked `••••3744`). All others are `agentic_allowed: false` and MUST be skipped.
- `agentic_allowed` is caller-relative (false = not accessible to *this* agent).
- Account numbers: mask all but last 4 in any user-facing output; pass full value to tools.

### Portfolio / buying power — `get_portfolio(account_number)`
```
data : { total_value, equity_value, options_value, cash, pending_deposits,
  buying_power: { buying_power, unleveraged_buying_power, display_currency } }
```
- Authoritative spendable figure is **`data.buying_power.buying_power`** (NOT on `get_accounts`).
- Live state of the agentic account at capture: total_value ≈ 238.07, cash 0, buying_power 0 (funded, currently fully invested — no spendable cash right now).

### Positions — `get_equity_positions(account_number, cursor?)` (paginated)
```
data.positions[] : { symbol, quantity, intraday_quantity, average_buy_price,
  shares_available_for_sells, shares_held_for_sells, ..., type ("long") }
```
- Cost basis is **`average_buy_price`** (not `avg_price`/`average_price`).
- **Sellable quantity is `shares_available_for_sells`**, not `quantity` — the long-only sell-quantity check must use this.
- Pagination via `cursor` (from the prior response's `next` URL).

### Quotes — `get_equity_quotes(symbols[])`
```
data.results[] : { quote: { symbol, last_trade_price, last_non_reg_trade_price,
  venue_last_trade_time, adjusted_previous_close, previous_close, bid_price,
  ask_price, has_traded, state }, close: { price, date } }
```
- Current price: use `last_trade_price`, falling back to `last_non_reg_trade_price` only when the former is absent; check `has_traded` and `state == "active"`. (The captured schema carries exactly ONE venue timestamp — `venue_last_trade_time`, paired with `last_trade_price` — so there is nothing to compare recency against; a "more recent by timestamp" pick is not implementable and `_pick_quote_price` deliberately does fallback-only.)
- For a marketable BUY, `ask_price` is the price-protective reference.

### Orders — `place_equity_order` / `review_equity_order` / `get_equity_orders` / `cancel_equity_order`
- `place_equity_order(account_number, symbol, side ("buy"|"sell"), type ("market"|"limit"|"stop_market"|"stop_limit"), quantity | dollar_amount, limit_price?, stop_price?, time_in_force ("gfd"|"gtc"), market_hours, ref_id)`.
  - **Notional BUYs use `dollar_amount` (string), which REQUIRES `type=market`** — not `notional`.
  - **Idempotency key is `ref_id` (a UUID), not `client_order_id`.** Re-send the same `ref_id` on transient retries.
  - Fractional shares: `type=market` + `market_hours=regular_hours` only, ≤6 decimals, no short sells — matches our fractional post-earnings strategy.
  - `review_equity_order` has the same params (minus `ref_id`); returns quote + pre-trade alerts (buying power, PDT, halt). Requires `agentic_allowed=true`.
- Orders **ack in a non-filled state** and settle asynchronously. `get_equity_orders(account_number, order_id?, state?, ...)` order states: `new, queued, confirmed, unconfirmed, partially_filled, filled, cancelled, rejected, failed, voided`. `placed_agent='agentic'` filters to MCP-placed orders.
- `cancel_equity_order(account_number, order_id)`.

## Gap analysis: stub vs reality

| Stub assumption | Reality | Impact |
|---|---|---|
| flat `result["accounts"]`, `result["quotes"]` | everything under `data.…`, quote nested `data.results[].quote` | all DTO mappings rewritten |
| Protocol methods take no `account_number` | every account-scoped call requires it | **Protocol signature change** + agentic-account resolution |
| buying power from `get_accounts` | from `get_portfolio.buying_power.buying_power` | `get_account` split into accounts + portfolio |
| position `avg_price` | `average_buy_price`; sellable = `shares_available_for_sells` | DTO + long-only sell check |
| place uses `notional` + `client_order_id` | `dollar_amount` (needs `type=market`) + `ref_id` | order mapping rewritten |
| place returns a terminal fill | acks non-filled; must poll `get_equity_orders` | **new fill-polling lifecycle** |
| per-call `asyncio.run`/stored-loop bridge | anyio cancel-scope task affinity → dead end | **worker-thread transport** |
| `except Exception → MCPUnavailable` | hides parser regressions | split `MCPProtocolError` vs `MCPUnavailable` |

## Design

1. **Transport (worker thread).** One daemon thread owns the whole async lifecycle: `async with streamablehttp_client(endpoint, auth=…) as (r,w,_): async with ClientSession(r,w) as s: await s.initialize(); <serve queue until shutdown>`. Sync Protocol methods submit coroutines via `asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=…)`. `close()` signals shutdown and joins. Replaces the stored-loop `run_until_complete` bridge entirely.
2. **OAuth.** SDK `mcp.client.auth.OAuthClientProvider` with a `TokenStorage` backed by the existing `_read_token`/`_write_token` (0600). Passed as `auth=` to `streamablehttp_client`. Browser flow on first run; cached token thereafter.
3. **Agentic account resolution.** On connect, `get_accounts` → select the single `agentic_allowed=true` account; store its `account_number`; refuse to operate if zero or if a configured `OPS_RH_ACCOUNT` override isn't `agentic_allowed`. This is defense-in-depth beneath the SPOT/guardrail layers — a non-agentic account is structurally unusable.
4. **Protocol + DTOs.** `RobinhoodMCPClient` account-scoped methods drop the caller-supplied account (the client owns its resolved agentic account). DTOs remap to the real `data.…` shapes. `MCPPosition` gains `shares_available_for_sells`. `AccountInfo` sourced from `get_accounts` (flags) + `get_portfolio` (cash/equity/buying_power).
5. **Order lifecycle.** `place_equity_order` maps notional→`dollar_amount`+`type=market`, `client_order_id`→`ref_id`. After the ack, poll `get_equity_orders(order_id)` for a bounded window (market orders, RTH); on `filled`/`partially_filled` build the `MCPOrderAck` with the real fill price; on timeout call `cancel_equity_order` and journal the outcome; map `rejected`/`failed` to a clear error. This is also where the spec's kill-switch "cancel pending orders" behavior finally has a home.

   **Lock-scope decision (M6, 2026-07-05):** `GuardedBroker.place_order` holds its lock through the entire inner place — including this fill poll — so a concurrent guardian pass cannot read positions while an order settles. The full submit/await split (release the lock during the poll) was judged too invasive for now: it reopens the double-BUY sizing race unless in-flight notional is tracked in the rule context. Instead the poll window is **bounded at 30s** (`_await_fill` `window_s` default, pinned by `test_await_fill_window_stays_inside_guardian_cycle`) so the guardian is never starved longer than half its 60s cycle. Revisit the split if the orchestrator ever proposes more than a handful of orders per tick.
6. **Error taxonomy.** `MCPUnavailable` for transport/timeout/auth; new `MCPProtocolError` for shape/parse mismatches (a renamed field must not read as an outage). Catch transport errors narrowly.

## Testing & the live boundary

- **Unit + `FakeMCPClient`:** worker-thread submit/timeout/shutdown; DTO mapping against the **recorded real `data.…` fixtures** captured above; agentic-account selection (picks the allowed one, refuses others); order mapping (dollar_amount/ref_id); fill-polling state machine (ack→filled, ack→timeout→cancel, ack→rejected) driven by a scripted fake. All in the default suite.
- **Opt-in live read-only** (`OPS_RH_LIVE_TESTS=1`, already the gate for the existing RH suite): `get_accounts`, `get_portfolio`, `get_equity_positions`, `get_equity_quotes` against the real endpoint — no orders.
- **The live boundary (explicit):** a real order round-trip **cannot be exercised offline** — it needs (a) spendable buying power in the agentic account (currently $0) and (b) a deliberate, human-initiated run. It is gated three ways regardless: `agentic_allowed` account selection, the GuardedBroker rule chain, and the `LiveMaxPositionRule` $10/first-20 cap. Per the parent spec this only happens after 8 weeks of paper trading + graduation criteria. **No live order is placed during this work.**

## Out of scope
- Options/crypto/index tools (the MCP exposes them; v1 equities-only).
- Auto-graduation. The account currently has $0 buying power and pre-existing manual holdings (GLD, MU) — reconciliation of those is a separate concern, not part of this client rewrite.
