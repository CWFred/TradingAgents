# Plan 3a — Broker plumbing + safety hardening (design)

**Date:** 2026-07-01
**Status:** Design approved, ready for implementation plan
**Parent spec:** [`2026-06-30-tradingagents-live-v1-design.md`](./2026-06-30-tradingagents-live-v1-design.md)
**Predecessor plans:** Plan 1 foundation (PR #1), Plan 2 strategy + pipeline + decide-once CLI (PR #3), plan-2 review followups (PR #4).

## Scope split

Plan 3 as originally scoped (always-on orchestrator + `RobinhoodBroker` + notifications) is split into three plans, each on its own branch and PR:

- **Plan 3a (this doc)** — broker plumbing + safety hardening.
- **Plan 3b** — always-on orchestration (guardian → background thread, market calendar, orchestrator loop, `ops/main.py` entrypoint).
- **Plan 3c** — notifications (Pushover, SMTP, event dispatcher).

3a exists because Plan 2's whole-branch review pinned two Important items as Plan-3-first (see [`../plans/plan-3-inputs.md`](../plans/plan-3-inputs.md)) and because the always-on loop is easier to design after the broker layer is settled. 3a ships live-broker plumbing but does NOT enable live orders — `broker_mode=paper` remains the default and no orchestration loop exists yet to place live orders on its own.

## Goals

- Formalise sell-all as a first-class `Broker` method.
- `PositionGuardian` honours `Position.stop_loss_price` and falls back to the config default only when unset.
- `RobinhoodBroker(Broker)` exists, wired against a typed MCP client seam with a `FakeMCPClient` for unit tests and opt-in live-read integration tests.
- SPOT hard-check embedded in `RobinhoodBroker` as defense-in-depth beyond `DenyListRule`.
- Journal schema + Python API for equity snapshots (consumed by Plan 3b's drawdown baseline).
- `PaperBroker.from_journal(...)` seam so the Plan 3b orchestrator can rebuild in-memory state on restart.

## Non-goals (deferred to 3b or later)

- Guardian background thread; `check_stops_once()` stays one-shot.
- Market calendar, orchestrator loop, `ops/main.py`.
- Notifications and event dispatcher.
- `TradingAgentsPipelineAdapter._ensure_graph` lock.
- Journal-persisted `Position.stop_loss_price` on fills (design: recovered positions come back with `stop_loss_price=None` and use the config fallback; 3b addresses persistence when it starts to matter).
- Wikipedia sp500 stale-cache fallback.
- `LIVE_MAX_POSITION` first-N-trades cap (design in spec §Graduation criteria; wiring is later).

## Design

### 1. `Broker.close_position(symbol) -> Fill`

Add abstract method on `ops/broker/base.py`:

```python
class Broker(ABC):
    ...
    @abstractmethod
    def close_position(self, symbol: str) -> Fill: ...
```

- `PaperBroker.close_position`: looks up qty from `_positions`, builds an internal sized SELL, journals the fill, deletes the position; raises `NoSuchPosition` if symbol absent.
- `GuardedBroker.close_position`: acquires `_lock`, reads inner position qty, constructs a sized `Order(side=SELL, notional_dollars=qty * quote)` or a quantity-based SELL, runs the rule chain against it, delegates to inner. Guardrails still apply on close (deny-list would still block SPOT etc.).
- `RobinhoodBroker.close_position`: MCP `get_positions`, find matching symbol, call MCP `place_equity_order(quantity=<current_qty>, side=SELL)`.

**Delete the zero-notional convention.** `Order.__post_init__` gets tightened: SELL orders must have `notional_dollars > 0`. Zero-notional SELL now raises `ValueError`. Partial-sell path (`notional > 0`) stays intact for future strategies that want to trim positions.

`client_order_id` construction for stop-sells moves into each concrete broker's `close_position` (so PaperBroker and RobinhoodBroker each own their idempotency-key format). Guardian no longer synthesises IDs.

### 2. `PositionGuardian` stop semantics

Trigger:

```python
if pos.stop_loss_price is not None:
    triggered = current <= pos.stop_loss_price
    mode, threshold_repr = "absolute", f"abs {pos.stop_loss_price}"
else:
    pct = pos.unrealized_pct(current)
    triggered = pct <= self._cfg.per_position_stop_pct
    mode, threshold_repr = "pct", f"pct {self._cfg.per_position_stop_pct}"
```

`stop_hit` and `stop_failed` events include `mode` and `threshold_repr` so replay/analysis can tell which rule fired.

Close call:

```python
try:
    self._broker.close_position(pos.symbol)
except BrokerError as exc:
    # journal stop_failed as today (unchanged)
```

Threading stays one-shot in 3a. Atomicity comes from `close_position`'s lock hold (Section 3) — even if a strategy top-up races between guardian's snapshot and the close call, `close_position` reads fresh qty inside the lock and sells whatever is actually there.

### 3. `GuardedBroker._lock` semantics for close

`close_position(symbol)` holds `_lock` across:

1. Snapshot inner position qty.
2. Fetch quote for sizing (or defer to inner if inner takes a `quantity=` order).
3. Construct SELL `Order`.
4. Evaluate rule chain against constructed order.
5. Delegate to inner broker.

Any concurrent BUY/SELL on the same symbol blocks until the close returns. This closes the top-up race the Plan 2 review flagged for the eventual multi-threaded orchestrator.

### 4. `RobinhoodBroker` + MCP client seam

**New files:**

```
ops/broker/robinhood.py      # RobinhoodBroker(Broker)
ops/broker/mcp_client.py     # RobinhoodMCPClient protocol, DTOs, RealRobinhoodMCPClient
tests/ops/broker/test_robinhood.py
tests/ops/broker/test_robinhood_live.py
tests/ops/broker/fakes.py    # FakeMCPClient
```

**`RobinhoodMCPClient` protocol** — narrow, typed, mirrors just the MCP tool subset the broker calls:

```python
class RobinhoodMCPClient(Protocol):
    def get_account(self) -> AccountInfo: ...
    def get_positions(self) -> list[MCPPosition]: ...
    def get_quote(self, symbol: str) -> Decimal: ...
    def place_equity_order(
        self,
        *,
        symbol: str,
        side: Side,
        notional: Decimal | None,
        quantity: Decimal | None,
        order_type: OrderType,
        limit_price: Decimal | None,
        client_order_id: str,
    ) -> MCPOrderAck: ...
    def cancel_equity_order(self, order_id: str) -> None: ...
```

`AccountInfo`, `MCPPosition`, `MCPOrderAck` are small `@dataclass(frozen=True)` DTOs so the broker never touches raw MCP dicts.

**`RealRobinhoodMCPClient`** uses the `mcp` Python SDK against `https://agent.robinhood.com/mcp/trading`.

- First-run OAuth: browser flow, token cached at `~/.config/tradingagents/robinhood_token.json` with `0600` perms.
- Env override: `OPS_RH_TOKEN_PATH`.
- No token in repo, no token in journal, no token in logs.

**`RobinhoodBroker(Broker)`** takes a `RobinhoodMCPClient` in its constructor — dependency-injected — so tests use `FakeMCPClient` and the factory injects `RealRobinhoodMCPClient`. Mapping:

- `get_cash()` → `client.get_account().cash`
- `get_equity()` → `client.get_account().equity`
- `get_positions()` → maps `MCPPosition` → `ops.broker.types.Position`; `Position.stop_loss_price = None` for RH-sourced positions (RH doesn't persist our per-position stops — journal does, and 3b will reconcile).
- `get_quote(symbol)` → `client.get_quote(symbol)`
- `place_order(order)` → `place_equity_order(notional=order.notional_dollars, quantity=None, ...)`. Partial SELLs same shape.
- `close_position(symbol)` → `client.get_positions()` → find symbol → `place_equity_order(quantity=qty, notional=None, ...)`; raises `NoSuchPosition` if missing.

**SPOT hard-check:** top of `RobinhoodBroker.place_order` and `close_position`:

```python
if order.symbol.upper() == "SPOT":
    raise OrderRejected("SpotDenyList", "SPOT is contractually restricted")
```

Not a Rule — a plain `if` inside the broker so no config or rule-engine change can ever remove it. Defense in depth against a misconfigured `GuardedBroker`.

### 5. Journal equity snapshots

Schema addition (Plan 1's `Journal._ensure_schema` extended):

```sql
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    equity TEXT NOT NULL,
    cash TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_equity_kind_ts ON equity_snapshots (kind, ts);
```

Kinds: `open_day`, `open_week`, `manual`. Plan 3b's orchestrator writes `open_day` on first tick on/after 09:30 ET each trading day and `open_week` on first tick on/after Monday's 09:30 ET.

Python API added in 3a:

```python
def record_equity_snapshot(
    self, *, kind: str, equity: Decimal, cash: Decimal,
    ts: datetime | None = None, note: str | None = None,
) -> None: ...

def get_latest_equity_snapshot(
    self, *, kind: str, since: datetime | None = None,
) -> EquitySnapshot | None: ...
```

`EquitySnapshot` is a small frozen dataclass. Decimals stored as strings, restored via `Decimal(str)` to avoid float drift.

No orchestrator wiring here — 3a ships the mechanism and its tests; 3b's drawdown-baseline code consumes it.

### 6. `PaperBroker.from_journal(journal, quote_source, starting_cash)`

Classmethod that replays fills from the journal to rebuild `_positions` and `_cash`:

- Read all fills ordered by `filled_at`.
- BUY → apply `_fill_buy` math (avg-entry-price recomputed as production code does).
- SELL → apply `_fill_sell` math (partial or full).
- Return a `PaperBroker` in the reconstructed state.

Factory (`ops.build_guarded_paper_broker`) is NOT changed in 3a; recovery gets used by 3b's orchestrator startup. `Position.stop_loss_price` on recovered positions is `None` (config fallback) — 3b addresses persistence.

## Tasks (approximate — writing-plans finalises)

1. Branch `feat/ops-broker-plumbing`; `pyproject.toml` gains MCP SDK dep; scaffold empty files.
2. Tighten `Order.__post_init__` to forbid zero-notional SELL; fix any test fixtures relying on it.
3. `Broker.close_position` ABC + `PaperBroker.close_position` + tests.
4. `GuardedBroker.close_position` under lock + tests (including concurrency test with `threading.Event`).
5. `PositionGuardian` calls `close_position`, honours `Position.stop_loss_price` with config fallback + tests.
6. `Journal.equity_snapshots` schema + writer + reader + tests.
7. `PaperBroker.from_journal` classmethod + tests.
8. `ops/broker/mcp_client.py` — protocol + DTOs + `FakeMCPClient`.
9. `ops/broker/robinhood.py` — `RobinhoodBroker` + unit tests via `FakeMCPClient`.
10. `RealRobinhoodMCPClient` — MCP SDK wiring, OAuth flow, `0600` token file; unit tests for token perms + error mapping (no network).
11. `RobinhoodBroker` SPOT hard-check + tests independent of `DenyListRule`.
12. `ops.build_guarded_robinhood_broker` factory; config `broker_mode` switch (default: paper).
13. `tests/ops/broker/test_robinhood_live.py` — opt-in, gated on `OPS_RH_LIVE_TESTS=1`.
14. End-to-end integration test — paper flow through `close_position` replacing every prior zero-notional path.
15. `ops/README.md` section on RH setup + OAuth + `OPS_RH_LIVE_TESTS` + token file location.
16. Push branch + open PR.

## Testing

- Baseline entering 3a: 138 passing on `main`.
- Target exit: ~180–200 passing.
- No test may hit the live network or require credentials by default. Live-read suite skipped unless `OPS_RH_LIVE_TESTS=1`.
- Constraints (from Plan 1/2): Decimal end-to-end, 3.12 modern syntax, production code always goes through the factory, upstream `tradingagents/` package never modified.

## Open items deferred to the implementation plan

- Exact MCP SDK version pin (latest at plan time vs. a specific pin).
- Whether `place_equity_order` needs a `time_in_force` param exposed on the DTO (default `gfd` — good-for-day — is fine for 3a; extend later if needed).
- Whether `PaperBroker.from_journal` should also replay `order_rejected` events for a full audit trail, or only fills (fills-only recommended: rejections don't move state).
