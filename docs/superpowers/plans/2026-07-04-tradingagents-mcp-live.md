# Robinhood MCP live-client finalization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the `RealRobinhoodMCPClient` stub with a working client verified against the real agentic MCP schemas, without ever placing a live order.

**Architecture:** A daemon worker thread owns the async `streamablehttp_client` + `ClientSession` lifecycle; sync Protocol methods submit coroutines via `run_coroutine_threadsafe`. DTOs map the real `{data,…}` shapes. Orders ack non-filled and are resolved by polling `get_equity_orders`.

**Tech Stack:** Python 3.12, `mcp` SDK (streamable HTTP transport + `ClientSession` + `mcp.client.auth.OAuthClientProvider`), stdlib `threading`/`asyncio`/`uuid`.

## Global Constraints

- **Design + recorded real schemas:** `docs/superpowers/specs/2026-07-04-tradingagents-mcp-live-design.md` — the `data.…` field paths there are authoritative; use them verbatim.
- **No live order, ever, in this work.** No test may call `place_equity_order`/`review_equity_order`/`cancel_equity_order` against the real endpoint. Opt-in live tests are READ-ONLY and gated behind `OPS_RH_LIVE_TESTS=1` (never in the default suite).
- **Only `agentic_allowed=true` accounts are tradeable.** The client resolves and pins the single agentic account; non-agentic accounts are structurally refused. SPOT hard-check (`_enforce_spot_hard_check`) and the guardrail chain are unchanged and un-weakened.
- **Sync Protocol surface is the stable contract.** `FakeMCPClient` (tests/ops/broker/fakes.py) must keep satisfying `RobinhoodMCPClient` after any Protocol change; update it in lockstep.
- **Test runner:** `.venv/bin/pytest tests/ops/`. Baseline on branch base: run it first and record the number.
- **Errors:** `MCPUnavailable` = transport/timeout/auth; `MCPProtocolError` (new) = response shape/parse mismatch. Catch transport errors narrowly; never blanket-`except` a parse bug into an outage.

---

### Task 1: Worker-thread transport + error taxonomy
Replace the stored-loop `run_until_complete` bridge with a daemon thread owning one event loop; `_submit(coro)` via `run_coroutine_threadsafe(...).result(timeout)`; `close()` stops the loop and joins. Add `MCPProtocolError`. No real network — unit-test the threading with a trivial coroutine and a timeout coroutine. Keep `connect()`/session establishment behind the same lazy entry, but the actual `streamablehttp_client`+`ClientSession` open lands in Task 2 (this task builds the loop/thread plumbing + submit/timeout/shutdown semantics, tested with a stand-in coroutine runner).

### Task 2: OAuth + session establishment
`connect()` opens `streamablehttp_client(endpoint, auth=OAuthClientProvider(...))` then `ClientSession`, `await initialize()`, inside the worker coroutine; `TokenStorage` backed by `_read_token`/`_write_token`. Unit-test token storage round-trip + provider construction; the real handshake is exercised only by the Task 6 opt-in live read-only test.

### Task 3: DTO rewrite + agentic-account resolution (reads)
Rewrite `get_account`→(accounts+portfolio), `get_positions`, `get_quote` against the real `data.…` shapes (`average_buy_price`, `shares_available_for_sells`, `data.results[].quote.last_trade_price`, `data.buying_power.buying_power`). Add agentic-account resolution: `get_accounts` → pick the one `agentic_allowed=true` (honor `OPS_RH_ACCOUNT` override, but only if it is agentic_allowed), store `account_number`, thread it through all account-scoped calls; refuse when none qualifies. Drive every mapping test from recorded real-fixture dicts (copied from the design doc). Update `MCPPosition` (`shares_available_for_sells`), `AccountInfo`, and `FakeMCPClient` in lockstep.

### Task 4: Order placement mapping + fill-polling lifecycle
Map `place_equity_order`: notional→`dollar_amount`+`type=market`, `client_order_id`→`ref_id`(uuid). After ack, poll `get_equity_orders(order_id)` for a bounded window; resolve `filled`/`partially_filled`→`MCPOrderAck` with real fill price; `rejected`/`failed`/`voided`→error; timeout→`cancel_equity_order` + journal + error. Kill-switch "cancel pending orders" hook. Test the whole state machine against a scripted `FakeMCPClient` (ack→filled, ack→timeout→cancel, ack→rejected) — no live calls.

### Task 5: Wire long-only sell check to `shares_available_for_sells`
Update `LongOnlyRule` (and any broker sell path) to use `shares_available_for_sells` for the sellable quantity when live, keeping paper behavior intact. Test allow/exact/over-sell against the new field.

### Task 6: Opt-in live read-only smoke tests
`tests/ops/broker/test_mcp_live.py` gated behind `OPS_RH_LIVE_TESTS=1`: `get_accounts` (asserts exactly one agentic account resolvable), `get_portfolio`, `get_equity_positions`, `get_equity_quotes` — READ-ONLY, skipped by default. Explicit module comment: NO order/review/cancel calls here.

Each task: TDD (failing test → RED → implement → GREEN → commit), then task review. Final whole-branch review before PR. The live *order* round-trip is documented as the single post-graduation follow-up, not implemented here.
