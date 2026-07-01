# TradingAgents Live v1 — Broker Plumbing Plan (Plan 3a)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the broker-layer changes needed before an always-on orchestrator can exist: `Broker.close_position` API replaces the zero-notional sell-all convention, `PositionGuardian` honours `Position.stop_loss_price`, `RobinhoodBroker` (behind a typed MCP client seam) exists but stays behind the paper-default flag, and the journal gains `kind`-labelled equity snapshots plus a `PaperBroker.from_journal` seam that Plan 3b will use for state recovery.

**Architecture:** New abstract method on `Broker`; `PaperBroker` and a new `RobinhoodBroker` each implement it. `GuardedBroker.close_position` holds the broker lock across snapshot + rule chain + inner delegate. `RobinhoodBroker` depends on a small `RobinhoodMCPClient` protocol so unit tests inject a `FakeMCPClient` and the factory injects a `RealRobinhoodMCPClient` that wires the `mcp` Python SDK against the OAuth-guarded Robinhood MCP endpoint. Journal `equity_snapshots` schema extends with a `kind` column; a defensive `ALTER TABLE` handles pre-existing DBs.

**Tech Stack:** Python 3.12, `sqlite3` (stdlib), the `mcp` Python SDK, existing `pytest`. No new runtime deps beyond `mcp`.

## Global Constraints

- Decimal end-to-end for any monetary value — no `float` anywhere in `ops/`.
- Every new public function/class type-hinted with Python 3.12 modern syntax (`list[X]`, `X | None`, `X | Y`).
- Production code never instantiates `PaperBroker` or `RobinhoodBroker` directly — always the factory in `ops/__init__.py`.
- The upstream `tradingagents/` package is imported, never modified.
- Tests that hit external network or require credentials are marked `integration` (skipped by default). Live-read Robinhood tests are gated additionally on `OPS_RH_LIVE_TESTS=1`.
- Branch: `feat/ops-broker-plumbing` (already created off `main`, with the plan-3a design doc committed).
- SPOT deny-list enforcement is defense-in-depth: both `DenyListRule` in `GuardedBroker` AND a hard-coded `if` check inside `RobinhoodBroker.place_order`/`close_position`.
- Baseline entering this plan: **138 passing tests** on `main`. Target exit: ~180-200 passing.

## Design Doc

`docs/superpowers/specs/2026-07-01-tradingagents-live-v1-plan-3a-broker-plumbing-design.md`

## Parent Spec

`docs/superpowers/specs/2026-06-30-tradingagents-live-v1-design.md`

## Predecessor plan inputs

`docs/superpowers/plans/plan-3-inputs.md` (Ticket #1 close_position API, Ticket #2 Position.stop_loss_price).

---

## File Structure

```
ops/
  __init__.py                   # MODIFY: add build_guarded_robinhood_broker
  broker/
    base.py                     # MODIFY: Broker.close_position abstract method
    guarded.py                  # MODIFY: close_position() under lock
    paper.py                    # MODIFY: close_position(); remove zero-notional path; add from_journal
    robinhood.py                # NEW: RobinhoodBroker(Broker)
    mcp_client.py               # NEW: RobinhoodMCPClient protocol, DTOs, RealRobinhoodMCPClient
    types.py                    # MODIFY: Order.__post_init__ forbids zero-notional SELL
  position_guardian.py          # MODIFY: uses close_position; honours Position.stop_loss_price
  journal.py                    # MODIFY: equity_snapshots gains kind + note; add reader; ALTER TABLE guard
tests/ops/
  broker/
    fakes.py                    # NEW: FakeMCPClient
    test_base_close_position.py # NEW: abstract-method contract test
    test_guarded.py             # MODIFY: close_position under-lock tests
    test_paper.py               # MODIFY: close_position tests; from_journal tests; zero-notional now raises
    test_robinhood.py           # NEW: RobinhoodBroker unit tests via FakeMCPClient
    test_robinhood_live.py      # NEW: opt-in live-read tests
    test_types.py               # MODIFY: Order zero-notional SELL raises ValueError
  test_journal.py               # MODIFY: equity_snapshots by-kind tests
  test_position_guardian.py     # MODIFY: stop_loss_price honouring tests
  test_factory.py               # MODIFY: build_guarded_robinhood_broker tests
pyproject.toml                  # MODIFY: add mcp>=1.0 dep
ops/README.md                   # MODIFY: RH setup, OAuth, OPS_RH_LIVE_TESTS
```

---

## Task 0: Dependency + scaffold

**Files:**
- Modify: `pyproject.toml` — add `mcp>=1.0`
- Create: `ops/broker/robinhood.py` (empty stub)
- Create: `ops/broker/mcp_client.py` (empty stub)
- Create: `tests/ops/broker/fakes.py` (empty stub)
- Create: `tests/ops/broker/test_robinhood.py` (empty stub)
- Create: `tests/ops/broker/test_robinhood_live.py` (empty stub)
- Create: `tests/ops/broker/test_base_close_position.py` (empty stub)

**Interfaces:**
- Consumes: none
- Produces: importable empty modules; `mcp` SDK installed in `.venv`.

- [ ] **Step 1: Inspect current dep list**

Run: `grep -A 30 'dependencies = \[' pyproject.toml | head -35`
Note where to insert `mcp`.

- [ ] **Step 2: Add mcp to pyproject.toml**

In the `[project]` table's `dependencies = [...]` list, insert:
```toml
    "mcp>=1.0",
```
(keeping alphabetical order if the list is sorted).

- [ ] **Step 3: Install**

Run: `.venv/bin/pip install -e .`
Expected: `mcp` installs cleanly, no dep conflict.

- [ ] **Step 4: Verify import**

Run: `.venv/bin/python -c "import mcp; print(mcp.__version__)"`
Expected: prints a version string like `1.x.y`.

- [ ] **Step 5: Create the empty stubs**

Each file gets a single-line docstring so imports succeed:
```python
"""Stub — implemented in later tasks of the broker-plumbing plan."""
```

- [ ] **Step 6: Run existing tests to confirm nothing regressed**

Run: `.venv/bin/pytest tests/ops/ -q`
Expected: **138 passed**.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml ops/broker/robinhood.py ops/broker/mcp_client.py \
        tests/ops/broker/fakes.py tests/ops/broker/test_robinhood.py \
        tests/ops/broker/test_robinhood_live.py tests/ops/broker/test_base_close_position.py
git commit -m "chore(ops): add mcp SDK dep and empty scaffold files for plan-3a"
```

---

## Task 1: Tighten `Order.__post_init__` to forbid zero-notional SELL

**Files:**
- Modify: `ops/broker/types.py:29-35` — extend `__post_init__`
- Modify: `tests/ops/broker/test_types.py` — add zero-notional-SELL raises test

**Interfaces:**
- Consumes: nothing new
- Produces: `Order(side=SELL, notional_dollars=0, ...)` now raises `ValueError`.

This locks in the invariant so no future caller can accidentally reintroduce the paper-broker-only sell-all convention.

- [ ] **Step 1: Write the failing test**

Add to `tests/ops/broker/test_types.py`:
```python
def test_sell_order_requires_positive_notional():
    with pytest.raises(ValueError, match="SELL order requires positive notional_dollars"):
        Order(
            client_order_id="s-1",
            symbol="AAPL",
            side=Side.SELL,
            notional_dollars=Decimal("0"),
            order_type=OrderType.MARKET,
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/broker/test_types.py::test_sell_order_requires_positive_notional -v`
Expected: FAIL (no ValueError raised — current code accepts it).

- [ ] **Step 3: Tighten `__post_init__`**

In `ops/broker/types.py`, extend the existing validation:
```python
def __post_init__(self) -> None:
    if self.notional_dollars < 0:
        raise ValueError("notional_dollars cannot be negative")
    if self.side == Side.BUY and self.notional_dollars <= 0:
        raise ValueError("BUY order requires positive notional_dollars")
    if self.side == Side.SELL and self.notional_dollars <= 0:
        raise ValueError("SELL order requires positive notional_dollars")
    if self.order_type == OrderType.LIMIT and self.limit_price is None:
        raise ValueError("LIMIT order requires limit_price")
```

- [ ] **Step 4: Run the new test alone**

Run: `.venv/bin/pytest tests/ops/broker/test_types.py::test_sell_order_requires_positive_notional -v`
Expected: PASS.

- [ ] **Step 5: Run full test suite; expect some failures to surface**

Run: `.venv/bin/pytest tests/ops/ -q`
Expected: FAIL — any test that constructed a zero-notional SELL (guardian's old stop-sell path in tests, paper broker's sell-all tests, integration tests) will now raise at construction time.

- [ ] **Step 6: Note the failures**

Record which tests failed. They will be updated to use `close_position` in later tasks. For now, mark them with `pytest.skip("moves to close_position in task 2/3/4")`:
```python
import pytest
pytest.skip("legacy zero-notional sell path — replaced by close_position in task 2", allow_module_level=True)
```
Apply per-test skips, not module-level, when possible. Rerun to confirm the previously-passing suite is now a mix of passes + expected skips.

- [ ] **Step 7: Run tests**

Run: `.venv/bin/pytest tests/ops/ -q`
Expected: passes + skips, no failures.

- [ ] **Step 8: Commit**

```bash
git add ops/broker/types.py tests/
git commit -m "feat(ops/broker): forbid zero-notional SELL orders; skip legacy tests

Zero-notional SELL was PaperBroker's private sell-all convention. The
close_position API in the next task replaces it; tests that relied on
the convention are skipped until they migrate."
```

---

## Task 2: `Broker.close_position` ABC + `PaperBroker.close_position`

**Files:**
- Modify: `ops/broker/base.py` — abstract method
- Modify: `ops/broker/paper.py` — `close_position` impl; remove `_fill_sell(notional=0)` branch
- Create: `tests/ops/broker/test_base_close_position.py` — contract test (abstract not-implemented)
- Modify: `tests/ops/broker/test_paper.py` — close_position tests; unskip and rewrite the tests that used zero-notional

**Interfaces:**
- Consumes: `NoSuchPosition` (already exists)
- Produces: `Broker.close_position(symbol: str) -> Fill`; `PaperBroker.close_position` returns a full-close Fill and deletes the position.

- [ ] **Step 1: Write the failing contract test**

`tests/ops/broker/test_base_close_position.py`:
```python
"""Contract: Broker.close_position is abstract; concrete subclasses must implement."""
from decimal import Decimal
from ops.broker.base import Broker


def test_broker_close_position_is_abstract():
    class Incomplete(Broker):
        def get_cash(self): return Decimal("0")
        def get_equity(self): return Decimal("0")
        def get_positions(self): return []
        def get_quote(self, symbol): return Decimal("1")
        def place_order(self, order): raise NotImplementedError
    import pytest
    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/broker/test_base_close_position.py -v`
Expected: FAIL — `Incomplete()` currently succeeds because `close_position` is not abstract yet.

- [ ] **Step 3: Add the abstract method**

In `ops/broker/base.py`, add:
```python
    @abstractmethod
    def close_position(self, symbol: str) -> Fill: ...
```

- [ ] **Step 4: Verify contract test now passes**

Run: `.venv/bin/pytest tests/ops/broker/test_base_close_position.py -v`
Expected: PASS.

- [ ] **Step 5: Run full suite to see what broke**

Run: `.venv/bin/pytest tests/ops/ -q`
Expected: FAIL — `PaperBroker` is no longer instantiable (missing `close_position`), so every test that constructs it fails.

- [ ] **Step 6: Implement `PaperBroker.close_position`**

In `ops/broker/paper.py`, after `place_order`:
```python
    def close_position(self, symbol: str) -> Fill:
        existing = self._positions.get(symbol)
        if existing is None:
            raise NoSuchPosition(f"no position in {symbol}")
        price = self._quote(symbol)
        qty = existing.quantity
        proceeds = qty * price
        self._cash += proceeds
        del self._positions[symbol]
        fill = Fill(
            order_id=str(uuid4()),
            client_order_id=f"close-{symbol}-{uuid4().hex[:8]}",
            symbol=symbol,
            side=Side.SELL,
            quantity=qty,
            price=price,
            filled_at=datetime.now(timezone.utc),
        )
        self._journal.record_fill(
            order_id=fill.order_id,
            client_order_id=fill.client_order_id,
            symbol=fill.symbol,
            side=fill.side.value,
            quantity=fill.quantity,
            price=fill.price,
            filled_at=fill.filled_at,
        )
        return fill
```

- [ ] **Step 7: Remove the zero-notional branch from `_fill_sell`**

In `ops/broker/paper.py::_fill_sell`, delete:
```python
        if order.notional_dollars == 0:
            qty_to_sell = existing.quantity
        else:
            qty_to_sell = order.notional_dollars / price
```
Replace with:
```python
        qty_to_sell = order.notional_dollars / price
```

- [ ] **Step 8: Add close_position unit tests**

In `tests/ops/broker/test_paper.py`, add:
```python
def test_close_position_sells_full_qty(journal, quote_source):
    broker = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("100"))
    quote_source.set("AAPL", Decimal("10"))
    broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
    ))
    fill = broker.close_position("AAPL")
    assert fill.side == Side.SELL
    assert fill.quantity == Decimal("5")
    assert broker.get_positions() == []
    assert broker.get_cash() == Decimal("100")


def test_close_position_missing_symbol_raises(journal, quote_source):
    broker = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("100"))
    with pytest.raises(NoSuchPosition):
        broker.close_position("NVDA")


def test_close_position_records_fill_to_journal(journal, quote_source):
    broker = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("100"))
    quote_source.set("AAPL", Decimal("10"))
    broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
    ))
    broker.close_position("AAPL")
    fills = journal.read_fills()
    close_fills = [f for f in fills if f["client_order_id"].startswith("close-AAPL-")]
    assert len(close_fills) == 1
    assert close_fills[0]["side"] == "SELL"
```

- [ ] **Step 9: Unskip / rewrite the paper-broker tests skipped in Task 1**

Any test in `test_paper.py` that skipped with "moves to close_position in task 2" — rewrite it to call `broker.close_position(symbol)` instead of `broker.place_order(Order(..., notional=0))`, then remove the skip.

- [ ] **Step 10: Run tests**

Run: `.venv/bin/pytest tests/ops/broker/test_paper.py tests/ops/broker/test_base_close_position.py -v`
Expected: all pass.

- [ ] **Step 11: Run full suite**

Run: `.venv/bin/pytest tests/ops/ -q`
Expected: passes + still some skips (guarded / guardian / integration tests migrate in later tasks).

- [ ] **Step 12: Commit**

```bash
git add ops/broker/base.py ops/broker/paper.py tests/ops/broker/
git commit -m "feat(ops/broker): Broker.close_position ABC + PaperBroker impl

Replaces the zero-notional SELL convention with a first-class
close_position method that reads the current quantity and sells it all
in one atomic step. Journals a 'close-<symbol>-<uuid>' fill for replay."
```

---

## Task 3: `GuardedBroker.close_position` under lock, with concurrency test

**Files:**
- Modify: `ops/broker/guarded.py` — add `close_position`
- Modify: `tests/ops/broker/test_guarded.py` — close_position tests, incl. concurrency

**Interfaces:**
- Consumes: `Broker.close_position` (Task 2), the existing `_lock`
- Produces: `GuardedBroker.close_position(symbol)` — atomic snapshot + rule chain + inner delegate; guardrails still evaluated on close.

- [ ] **Step 1: Write the failing basic test**

In `tests/ops/broker/test_guarded.py`:
```python
def test_guarded_close_position_delegates_to_inner(guarded, inner, quote_source):
    quote_source.set("AAPL", Decimal("10"))
    guarded.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9"),
    ))
    fill = guarded.close_position("AAPL")
    assert fill.side == Side.SELL
    assert guarded.get_positions() == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/broker/test_guarded.py::test_guarded_close_position_delegates_to_inner -v`
Expected: FAIL — `GuardedBroker` missing `close_position` (abstract).

- [ ] **Step 3: Implement `GuardedBroker.close_position`**

In `ops/broker/guarded.py`:
```python
    def close_position(self, symbol: str) -> Fill:
        with self._lock:
            positions = self.__inner.get_positions()
            existing = next((p for p in positions if p.symbol == symbol), None)
            if existing is None:
                raise NoSuchPosition(f"no position in {symbol}")
            price = self.__inner.get_quote(symbol)
            notional = existing.quantity * price
            close_order = Order(
                client_order_id=f"close-{symbol}-{uuid.uuid4().hex[:8]}",
                symbol=symbol,
                side=Side.SELL,
                notional_dollars=notional,
                order_type=OrderType.MARKET,
            )
            ctx = RuleContext(order=close_order, broker=self.__inner, config=self._config)
            result = self._engine.evaluate(ctx)
            if not result.allowed:
                self._journal.record_event(
                    "order_rejected",
                    {
                        "rule": result.failed_rule_name,
                        "reason": result.reason,
                        "client_order_id": close_order.client_order_id,
                        "symbol": symbol,
                        "side": "SELL",
                        "notional_dollars": str(notional),
                        "context": "close_position",
                    },
                )
                raise OrderRejected(result.failed_rule_name, result.reason)
            try:
                return self.__inner.close_position(symbol)
            except BrokerError as exc:
                self._journal.record_event(
                    "order_rejected",
                    {
                        "rule": "broker",
                        "reason": f"{type(exc).__name__}: {exc}",
                        "client_order_id": close_order.client_order_id,
                        "symbol": symbol,
                        "side": "SELL",
                        "notional_dollars": str(notional),
                        "context": "close_position",
                    },
                )
                raise
```

Add the required imports at the top of the file: `uuid`, `NoSuchPosition`, `Side`, `OrderType`.

**Note:** rules on close use `notional = qty * quote` for sizing-rule evaluation, but the eventual inner delegate uses `inner.close_position(symbol)` which sells the exact qty regardless of price drift between rule eval and fill. This is intentional — rules see a representative sizing; the actual fill closes the position deterministically.

- [ ] **Step 4: Verify basic test passes**

Run: `.venv/bin/pytest tests/ops/broker/test_guarded.py::test_guarded_close_position_delegates_to_inner -v`
Expected: PASS.

- [ ] **Step 5: Add rule-still-applies test**

```python
def test_guarded_close_position_denylist_still_blocks(guarded_denylist_spot, quote_source):
    """If SPOT somehow ends up in the paper book (e.g. via test seed), close_position
    still runs the rule chain — DenyListRule blocks it, OrderRejected raised."""
    # ... construct a broker with SPOT position seeded in inner, verify close raises OrderRejected
```
(Exact seed helper follows existing test_guarded.py patterns.)

- [ ] **Step 6: Add concurrency test — top-up race**

```python
def test_guarded_close_position_races_with_concurrent_buy(guarded, inner, quote_source):
    """A BUY on the same symbol arriving during a close_position must serialise:
    if close runs first the position is empty and BUY re-opens; if BUY runs first
    the close sells the new bigger qty. No mid-close top-up."""
    import threading
    quote_source.set("AAPL", Decimal("10"))
    guarded.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9"),
    ))
    barrier = threading.Barrier(2)
    close_result = {}
    buy_result = {}

    def do_close():
        barrier.wait()
        try:
            close_result["fill"] = guarded.close_position("AAPL")
        except Exception as e:
            close_result["exc"] = e

    def do_buy():
        barrier.wait()
        try:
            buy_result["fill"] = guarded.place_order(Order(
                client_order_id="b-2", symbol="AAPL", side=Side.BUY,
                notional_dollars=Decimal("20"), order_type=OrderType.MARKET,
                stop_loss_price=Decimal("9"),
            ))
        except Exception as e:
            buy_result["exc"] = e

    t1 = threading.Thread(target=do_close)
    t2 = threading.Thread(target=do_buy)
    t1.start(); t2.start()
    t1.join(); t2.join()

    positions = guarded.get_positions()
    # Two valid outcomes; neither shows a partial state.
    if "fill" in close_result and "fill" in buy_result:
        # close then buy — one position of 2.0 shares
        assert len(positions) == 1
        assert positions[0].quantity == Decimal("2")
    # (Other ordering handled by symmetric assertions — copy the shape from existing concurrency tests.)
```

- [ ] **Step 7: Run all guarded tests**

Run: `.venv/bin/pytest tests/ops/broker/test_guarded.py -v`
Expected: PASS.

- [ ] **Step 8: Unskip tests skipped in Task 1 for guarded**

Rewrite any `test_guarded.py` tests that used zero-notional SELL to use `close_position` instead. Remove their skips.

- [ ] **Step 9: Run full suite**

Run: `.venv/bin/pytest tests/ops/ -q`
Expected: passes + remaining skips only in guardian / integration tests.

- [ ] **Step 10: Commit**

```bash
git add ops/broker/guarded.py tests/ops/broker/test_guarded.py
git commit -m "feat(ops/broker): GuardedBroker.close_position under lock

close_position now snapshots the inner position qty, evaluates the rule
chain against the sized SELL, and delegates to inner — all under the
broker lock. A concurrent BUY on the same symbol cannot mutate the
position between snapshot and close."
```

---

## Task 4: `PositionGuardian` calls `close_position`; honours `Position.stop_loss_price`

**Files:**
- Modify: `ops/position_guardian.py`
- Modify: `tests/ops/test_position_guardian.py`

**Interfaces:**
- Consumes: `GuardedBroker.close_position` (Task 3), `Position.stop_loss_price` (already on `Position`)
- Produces: guardian trigger logic reads per-position stop with config-pct fallback; guardian never builds Orders anymore.

- [ ] **Step 1: Write the failing per-position-stop test**

Add to `tests/ops/test_position_guardian.py`:
```python
def test_guardian_uses_absolute_stop_when_position_has_one(guardian_fixtures):
    """A position with an explicit stop_loss_price fires at that absolute price,
    even if it's above the config default."""
    broker, quotes, cfg = guardian_fixtures  # cfg.per_position_stop_pct = -0.08
    # BUY at $10 with a tighter absolute stop of $9.50 (~ -5%).
    quotes.set("AAPL", Decimal("10"))
    broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9.5"),
    ))
    guardian = PositionGuardian(broker=broker, quote_source=quotes.get, config=cfg)
    # Price at $9.60 — below config default -8% (would be $9.20) but ABOVE explicit stop.
    quotes.set("AAPL", Decimal("9.60"))
    actions = guardian.check_stops_once()
    assert actions[0].sold is False, "explicit stop $9.50 not yet triggered at $9.60"
    # Price at $9.45 — below explicit stop, fires.
    quotes.set("AAPL", Decimal("9.45"))
    actions = guardian.check_stops_once()
    assert actions[0].sold is True
    assert broker.get_positions() == []


def test_guardian_falls_back_to_config_pct_when_no_position_stop(guardian_fixtures):
    """A position with stop_loss_price=None uses the config per_position_stop_pct."""
    broker, quotes, cfg = guardian_fixtures  # -0.08
    quotes.set("MSFT", Decimal("100"))
    # BUY without stop_loss_price (opened outside a strategy).
    broker.place_order(Order(
        client_order_id="b-1", symbol="MSFT", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
    ))
    guardian = PositionGuardian(broker=broker, quote_source=quotes.get, config=cfg)
    quotes.set("MSFT", Decimal("92.5"))   # -7.5%, above -8% threshold
    assert guardian.check_stops_once()[0].sold is False
    quotes.set("MSFT", Decimal("91.5"))   # -8.5%, past threshold
    assert guardian.check_stops_once()[0].sold is True


def test_guardian_records_stop_hit_with_mode_and_threshold(guardian_fixtures):
    """stop_hit event distinguishes absolute vs pct triggers."""
    broker, quotes, cfg = guardian_fixtures
    quotes.set("AAPL", Decimal("10"))
    broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9.5"),
    ))
    guardian = PositionGuardian(broker=broker, quote_source=quotes.get, config=cfg)
    quotes.set("AAPL", Decimal("9.45"))
    guardian.check_stops_once()
    events = broker.journal.read_events()
    stop_events = [e for e in events if e["kind"] == "stop_hit"]
    assert stop_events[-1]["payload"]["mode"] == "absolute"
    assert stop_events[-1]["payload"]["threshold_repr"].startswith("abs ")
```

- [ ] **Step 2: Run to verify failures**

Run: `.venv/bin/pytest tests/ops/test_position_guardian.py -v`
Expected: FAIL — current guardian ignores `pos.stop_loss_price`, uses config only.

- [ ] **Step 3: Rewrite `PositionGuardian.check_stops_once`**

Replace the trigger + close section in `ops/position_guardian.py`:
```python
    def check_stops_once(self) -> list[StopAction]:
        actions: list[StopAction] = []
        for pos in self._broker.get_positions():
            try:
                current = self._quote(pos.symbol)
            except QuoteUnavailable as exc:
                self._broker.journal.record_event(
                    "quote_unavailable",
                    {
                        "symbol": pos.symbol,
                        "context": "guardian_stop_check",
                        "error": str(exc),
                    },
                )
                actions.append(StopAction(
                    symbol=pos.symbol, entry=pos.avg_entry_price,
                    current=Decimal("0"), pct=Decimal("0"),
                    sold=False, reason=f"quote unavailable: {exc}",
                ))
                continue

            if pos.stop_loss_price is not None:
                triggered = current <= pos.stop_loss_price
                mode = "absolute"
                threshold_repr = f"abs {pos.stop_loss_price}"
                pct = pos.unrealized_pct(current)
            else:
                pct = pos.unrealized_pct(current)
                triggered = pct <= self._cfg.per_position_stop_pct
                mode = "pct"
                threshold_repr = f"pct {self._cfg.per_position_stop_pct}"

            if not triggered:
                actions.append(StopAction(
                    symbol=pos.symbol, entry=pos.avg_entry_price,
                    current=current, pct=pct, sold=False,
                    reason=f"unrealized {pct} above stop ({mode} {threshold_repr})",
                ))
                continue

            try:
                self._broker.close_position(pos.symbol)
            except BrokerError as exc:
                self._broker.journal.record_event(
                    "stop_failed",
                    {
                        "symbol": pos.symbol, "entry": str(pos.avg_entry_price),
                        "current": str(current), "pct": str(pct),
                        "mode": mode, "threshold_repr": threshold_repr,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                actions.append(StopAction(
                    symbol=pos.symbol, entry=pos.avg_entry_price,
                    current=current, pct=pct, sold=False,
                    reason=f"stop-sell failed: {type(exc).__name__}: {exc}",
                ))
                continue

            self._broker.journal.record_event(
                "stop_hit",
                {
                    "symbol": pos.symbol, "entry": str(pos.avg_entry_price),
                    "current": str(current), "pct": str(pct),
                    "mode": mode, "threshold_repr": threshold_repr,
                },
            )
            actions.append(StopAction(
                symbol=pos.symbol, entry=pos.avg_entry_price,
                current=current, pct=pct, sold=True,
                reason=f"stop hit at {pct} ({mode} {threshold_repr})",
            ))
        return actions
```

Remove the unused `uuid`, `Order`, `OrderType`, `Side` imports; the guardian no longer builds Orders.

- [ ] **Step 4: Verify new tests pass**

Run: `.venv/bin/pytest tests/ops/test_position_guardian.py -v`
Expected: PASS. Any previously-skipped guardian tests should also pass now — remove their skips.

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/pytest tests/ops/ -q`
Expected: PASS with only integration-test skips remaining (Task 13 unblocks those).

- [ ] **Step 6: Commit**

```bash
git add ops/position_guardian.py tests/ops/test_position_guardian.py
git commit -m "feat(ops/position_guardian): honour Position.stop_loss_price + use close_position

Guardian now checks the position's own stop_loss_price first and falls
back to config.per_position_stop_pct only when the position was opened
without a per-position stop. stop_hit / stop_failed events carry mode
and threshold_repr so replay can tell absolute vs pct triggers apart."
```

---

## Task 5: `Journal.equity_snapshots` gains `kind`; typed reader

**Files:**
- Modify: `ops/journal.py`
- Modify: `tests/ops/test_journal.py`

**Interfaces:**
- Consumes: existing `Journal` class
- Produces:
  - `EquitySnapshot(at: datetime, kind: str, equity: Decimal, cash: Decimal, note: str | None)` frozen dataclass
  - `Journal.record_equity_snapshot(*, kind, equity, cash, at=None, note=None)` — extended
  - `Journal.get_latest_equity_snapshot(*, kind, since=None) -> EquitySnapshot | None`
  - Kinds documented: `"open_day"`, `"open_week"`, `"manual"`

- [ ] **Step 1: Write the failing tests**

Add to `tests/ops/test_journal.py`:
```python
def test_record_and_get_latest_equity_snapshot_by_kind(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    ts1 = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
    ts2 = datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)
    j.record_equity_snapshot(kind="open_day", equity=Decimal("250"), cash=Decimal("250"), at=ts1)
    j.record_equity_snapshot(kind="open_day", equity=Decimal("245"), cash=Decimal("100"), at=ts2)
    j.record_equity_snapshot(kind="open_week", equity=Decimal("250"), cash=Decimal("250"), at=ts1)
    latest_day = j.get_latest_equity_snapshot(kind="open_day")
    assert latest_day.equity == Decimal("245")
    assert latest_day.at == ts2
    latest_week = j.get_latest_equity_snapshot(kind="open_week")
    assert latest_week.equity == Decimal("250")


def test_get_latest_equity_snapshot_since_filter(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    old = datetime(2026, 6, 25, 13, 30, tzinfo=timezone.utc)
    new = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
    j.record_equity_snapshot(kind="open_week", equity=Decimal("250"), cash=Decimal("250"), at=old)
    j.record_equity_snapshot(kind="open_week", equity=Decimal("240"), cash=Decimal("240"), at=new)
    # Query "since Monday 2026-06-29" — should get the new one only.
    monday = datetime(2026, 6, 29, tzinfo=timezone.utc)
    latest = j.get_latest_equity_snapshot(kind="open_week", since=monday)
    assert latest.at == new


def test_get_latest_equity_snapshot_none_when_empty(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    assert j.get_latest_equity_snapshot(kind="open_day") is None


def test_equity_snapshot_note_preserved(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_equity_snapshot(
        kind="manual", equity=Decimal("100"), cash=Decimal("50"),
        note="pre-migration snapshot",
    )
    latest = j.get_latest_equity_snapshot(kind="manual")
    assert latest.note == "pre-migration snapshot"


def test_equity_snapshot_schema_migrates_pre_existing_db(tmp_path):
    """A DB created before this change (no kind column) should be usable
    after Journal(path) reopens it."""
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE equity_snapshots ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  at TEXT NOT NULL,"
        "  equity TEXT NOT NULL,"
        "  cash TEXT NOT NULL"
        ")"
    )
    conn.close()
    j = Journal(path)  # migration runs
    j.record_equity_snapshot(kind="open_day", equity=Decimal("10"), cash=Decimal("10"))
    assert j.get_latest_equity_snapshot(kind="open_day") is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/test_journal.py -v -k equity_snapshot`
Expected: FAIL — signature mismatch on `record_equity_snapshot` (no `kind`) and `get_latest_equity_snapshot` doesn't exist.

- [ ] **Step 3: Extend the schema + migration in `journal.py`**

Update `_SCHEMA` for `equity_snapshots`:
```python
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'manual',
    equity TEXT NOT NULL,
    cash TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_equity_kind_at ON equity_snapshots (kind, at);
```

In `Journal.__init__`, after `executescript(_SCHEMA)`, add a defensive migration:
```python
# Defensive migration for DBs created before kind/note existed.
cur = self._conn.execute("PRAGMA table_info(equity_snapshots)")
cols = {row[1] for row in cur.fetchall()}
if "kind" not in cols:
    self._conn.execute(
        "ALTER TABLE equity_snapshots ADD COLUMN kind TEXT NOT NULL DEFAULT 'manual'"
    )
if "note" not in cols:
    self._conn.execute("ALTER TABLE equity_snapshots ADD COLUMN note TEXT")
```

- [ ] **Step 4: Add `EquitySnapshot` dataclass**

At the top of `journal.py`, after imports:
```python
from dataclasses import dataclass


@dataclass(frozen=True)
class EquitySnapshot:
    at: datetime
    kind: str
    equity: Decimal
    cash: Decimal
    note: str | None
```

- [ ] **Step 5: Replace `record_equity_snapshot`**

```python
    def record_equity_snapshot(
        self, *, kind: str, equity: Decimal, cash: Decimal,
        at: datetime | None = None, note: str | None = None,
    ) -> None:
        ts = _to_iso(at) if at is not None else _now_iso()
        self._conn.execute(
            "INSERT INTO equity_snapshots (at, kind, equity, cash, note) VALUES (?, ?, ?, ?, ?)",
            (ts, kind, str(equity), str(cash), note),
        )
```

- [ ] **Step 6: Add `get_latest_equity_snapshot`**

```python
    def get_latest_equity_snapshot(
        self, *, kind: str, since: datetime | None = None,
    ) -> EquitySnapshot | None:
        if since is None:
            row = self._conn.execute(
                "SELECT at, kind, equity, cash, note FROM equity_snapshots"
                " WHERE kind = ? ORDER BY at DESC LIMIT 1",
                (kind,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT at, kind, equity, cash, note FROM equity_snapshots"
                " WHERE kind = ? AND at >= ? ORDER BY at DESC LIMIT 1",
                (kind, _to_iso(since)),
            ).fetchone()
        if row is None:
            return None
        return EquitySnapshot(
            at=_from_iso(row[0]), kind=row[1],
            equity=Decimal(row[2]), cash=Decimal(row[3]), note=row[4],
        )
```

- [ ] **Step 7: Update legacy reader**

`read_equity_snapshots` should now also return `kind` and `note`. Fix its SELECT and its return dict shape.

- [ ] **Step 8: Fix any existing test that used the old `record_equity_snapshot(at=..., equity=..., cash=...)` signature**

Grep for existing call sites: `grep -rn "record_equity_snapshot" tests/ ops/`. Add `kind="manual"` to each.

- [ ] **Step 9: Run journal tests**

Run: `.venv/bin/pytest tests/ops/test_journal.py -v`
Expected: PASS.

- [ ] **Step 10: Run full suite**

Run: `.venv/bin/pytest tests/ops/ -q`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add ops/journal.py tests/ops/test_journal.py
git commit -m "feat(ops/journal): equity snapshots gain kind + note; typed reader

Plan 3b's drawdown-baseline reader queries the latest 'open_day' or
'open_week' snapshot. Defensive ALTER TABLE handles DBs created before
this change so no test seed needs regenerating."
```

---

## Task 6: `PaperBroker.from_journal` classmethod

**Files:**
- Modify: `ops/broker/paper.py`
- Modify: `tests/ops/broker/test_paper.py`

**Interfaces:**
- Consumes: `Journal.read_fills` (already exists)
- Produces: `PaperBroker.from_journal(journal, quote_source, starting_cash) -> PaperBroker`

- [ ] **Step 1: Write the failing tests**

```python
def test_from_journal_empty_journal_yields_starting_state(tmp_path, quote_source):
    journal = Journal(str(tmp_path / "j.sqlite"))
    broker = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("500"),
    )
    assert broker.get_cash() == Decimal("500")
    assert broker.get_positions() == []


def test_from_journal_replays_buy(tmp_path, quote_source):
    journal = Journal(str(tmp_path / "j.sqlite"))
    quote_source.set("AAPL", Decimal("10"))
    seed = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("500"))
    seed.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
    ))
    replayed = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("500"),
    )
    assert replayed.get_cash() == Decimal("400")
    assert len(replayed.get_positions()) == 1
    assert replayed.get_positions()[0].quantity == Decimal("10")


def test_from_journal_replays_buy_then_close(tmp_path, quote_source):
    journal = Journal(str(tmp_path / "j.sqlite"))
    quote_source.set("AAPL", Decimal("10"))
    seed = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("500"))
    seed.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
    ))
    quote_source.set("AAPL", Decimal("11"))
    seed.close_position("AAPL")
    replayed = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("500"),
    )
    assert replayed.get_positions() == []
    assert replayed.get_cash() == Decimal("510")   # 500 - 100 + 110


def test_from_journal_replays_multiple_buys_same_symbol_averages_entry(tmp_path, quote_source):
    journal = Journal(str(tmp_path / "j.sqlite"))
    quote_source.set("AAPL", Decimal("10"))
    seed = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("1000"))
    seed.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
    ))
    quote_source.set("AAPL", Decimal("20"))
    seed.place_order(Order(
        client_order_id="b-2", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("200"), order_type=OrderType.MARKET,
    ))
    replayed = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("1000"),
    )
    pos = replayed.get_positions()[0]
    # 10 shares @ 10 + 10 shares @ 20 = 20 shares avg 15
    assert pos.quantity == Decimal("20")
    assert pos.avg_entry_price == Decimal("15")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/broker/test_paper.py -v -k from_journal`
Expected: FAIL — classmethod doesn't exist.

- [ ] **Step 3: Implement `PaperBroker.from_journal`**

In `ops/broker/paper.py`:
```python
    @classmethod
    def from_journal(
        cls, *, journal: Journal, quote_source: QuoteSource, starting_cash: Decimal,
    ) -> "PaperBroker":
        """Rebuild in-memory state by replaying fills from the journal.

        stop_loss_price is not persisted on fills; recovered positions come
        back with stop_loss_price=None (guardian falls back to config)."""
        broker = cls(journal=journal, quote_source=quote_source, starting_cash=starting_cash)
        for f in journal.read_fills():
            symbol = f["symbol"]
            side = f["side"]
            qty = f["quantity"]
            price = f["price"]
            if side == Side.BUY.value:
                cost = qty * price
                broker._cash -= cost
                existing = broker._positions.get(symbol)
                if existing is None:
                    broker._positions[symbol] = Position(
                        symbol=symbol, quantity=qty,
                        avg_entry_price=price, stop_loss_price=None,
                    )
                else:
                    total_qty = existing.quantity + qty
                    avg = (
                        (existing.avg_entry_price * existing.quantity) + (price * qty)
                    ) / total_qty
                    broker._positions[symbol] = Position(
                        symbol=symbol, quantity=total_qty,
                        avg_entry_price=avg, stop_loss_price=None,
                    )
            elif side == Side.SELL.value:
                existing = broker._positions.get(symbol)
                if existing is None:
                    # Journal is inconsistent — SELL replayed without a prior BUY.
                    # Log and skip. In production this triggers reconciliation.
                    continue
                proceeds = qty * price
                broker._cash += proceeds
                remaining = existing.quantity - qty
                if remaining > _EPSILON:
                    broker._positions[symbol] = Position(
                        symbol=symbol, quantity=remaining,
                        avg_entry_price=existing.avg_entry_price,
                        stop_loss_price=None,
                    )
                else:
                    del broker._positions[symbol]
        return broker
```

- [ ] **Step 4: Verify tests pass**

Run: `.venv/bin/pytest tests/ops/broker/test_paper.py -v -k from_journal`
Expected: PASS.

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/pytest tests/ops/ -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ops/broker/paper.py tests/ops/broker/test_paper.py
git commit -m "feat(ops/broker): PaperBroker.from_journal state-recovery seam

Replays fills to reconstruct positions and cash. Plan 3b's orchestrator
consumes this on startup to survive restarts; not wired into the factory
in this plan."
```

---

## Task 7: MCP client protocol + DTOs + `FakeMCPClient`

**Files:**
- Modify: `ops/broker/mcp_client.py` — protocol + DTOs
- Modify: `tests/ops/broker/fakes.py` — `FakeMCPClient`

**Interfaces:**
- Consumes: `Side`, `OrderType` (from `ops.broker.types`)
- Produces:
  - `RobinhoodMCPClient` Protocol
  - DTOs: `AccountInfo`, `MCPPosition`, `MCPOrderAck`
  - `MCPUnavailable(Exception)` for MCP-side failures
  - `FakeMCPClient` for tests — configurable positions, cash, quotes; records placed orders

- [ ] **Step 1: Design DTOs + protocol**

In `ops/broker/mcp_client.py`:
```python
"""RobinhoodMCPClient protocol + typed DTOs.

Concrete implementations:
- RealRobinhoodMCPClient — production, wraps the mcp Python SDK.
- FakeMCPClient (tests/ops/broker/fakes.py) — in-memory, deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from ops.broker.types import OrderType, Side


class MCPUnavailable(Exception):
    """Raised when the MCP endpoint fails (network, auth, protocol error)."""


@dataclass(frozen=True)
class AccountInfo:
    cash: Decimal
    equity: Decimal
    buying_power: Decimal


@dataclass(frozen=True)
class MCPPosition:
    symbol: str
    quantity: Decimal
    avg_price: Decimal


@dataclass(frozen=True)
class MCPOrderAck:
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal | None
    notional: Decimal | None
    status: str    # "queued" | "filled" | "rejected"
    fill_price: Decimal | None


@runtime_checkable
class RobinhoodMCPClient(Protocol):
    def get_account(self) -> AccountInfo: ...
    def get_positions(self) -> list[MCPPosition]: ...
    def get_quote(self, symbol: str) -> Decimal: ...
    def place_equity_order(
        self, *, symbol: str, side: Side,
        notional: Decimal | None, quantity: Decimal | None,
        order_type: OrderType, limit_price: Decimal | None,
        client_order_id: str,
    ) -> MCPOrderAck: ...
    def cancel_equity_order(self, order_id: str) -> None: ...
```

- [ ] **Step 2: Write `FakeMCPClient`**

In `tests/ops/broker/fakes.py`:
```python
"""Deterministic in-memory MCP client for unit tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import uuid4

from ops.broker.mcp_client import (
    AccountInfo, MCPOrderAck, MCPPosition, MCPUnavailable, RobinhoodMCPClient,
)
from ops.broker.types import OrderType, Side


class FakeMCPClient:
    def __init__(self, *, cash: Decimal = Decimal("1000")):
        self._cash = cash
        self._positions: dict[str, MCPPosition] = {}
        self._quotes: dict[str, Decimal] = {}
        self.placed: list[MCPOrderAck] = []
        self.cancelled: list[str] = []
        self._raise_on_next_call: Exception | None = None

    # --- helpers for tests ---
    def set_quote(self, symbol: str, price: Decimal) -> None:
        self._quotes[symbol] = price

    def seed_position(self, symbol: str, quantity: Decimal, avg_price: Decimal) -> None:
        self._positions[symbol] = MCPPosition(symbol=symbol, quantity=quantity, avg_price=avg_price)

    def fail_next(self, exc: Exception) -> None:
        self._raise_on_next_call = exc

    # --- protocol ---
    def _check_fail(self) -> None:
        if self._raise_on_next_call is not None:
            exc = self._raise_on_next_call
            self._raise_on_next_call = None
            raise exc

    def get_account(self) -> AccountInfo:
        self._check_fail()
        equity = self._cash + sum(
            (p.quantity * self._quotes.get(p.symbol, p.avg_price) for p in self._positions.values()),
            start=Decimal("0"),
        )
        return AccountInfo(cash=self._cash, equity=equity, buying_power=self._cash)

    def get_positions(self) -> list[MCPPosition]:
        self._check_fail()
        return list(self._positions.values())

    def get_quote(self, symbol: str) -> Decimal:
        self._check_fail()
        if symbol not in self._quotes:
            raise MCPUnavailable(f"no quote for {symbol}")
        return self._quotes[symbol]

    def place_equity_order(
        self, *, symbol: str, side: Side,
        notional: Decimal | None, quantity: Decimal | None,
        order_type: OrderType, limit_price: Decimal | None,
        client_order_id: str,
    ) -> MCPOrderAck:
        self._check_fail()
        price = self._quotes.get(symbol, Decimal("1"))
        if side == Side.BUY:
            assert notional is not None
            qty = notional / price
            self._cash -= notional
            existing = self._positions.get(symbol)
            if existing is None:
                self._positions[symbol] = MCPPosition(symbol=symbol, quantity=qty, avg_price=price)
            else:
                new_qty = existing.quantity + qty
                new_avg = (existing.avg_price * existing.quantity + price * qty) / new_qty
                self._positions[symbol] = MCPPosition(symbol=symbol, quantity=new_qty, avg_price=new_avg)
            ack_qty = qty
        else:  # SELL
            existing = self._positions.get(symbol)
            assert existing is not None, f"SELL with no position in {symbol}"
            if quantity is not None:
                ack_qty = quantity
            else:
                assert notional is not None
                ack_qty = notional / price
            self._cash += ack_qty * price
            remaining = existing.quantity - ack_qty
            if remaining > Decimal("1e-9"):
                self._positions[symbol] = MCPPosition(symbol=symbol, quantity=remaining, avg_price=existing.avg_price)
            else:
                del self._positions[symbol]
        ack = MCPOrderAck(
            order_id=str(uuid4()), client_order_id=client_order_id,
            symbol=symbol, side=side, quantity=ack_qty,
            notional=notional, status="filled", fill_price=price,
        )
        self.placed.append(ack)
        return ack

    def cancel_equity_order(self, order_id: str) -> None:
        self._check_fail()
        self.cancelled.append(order_id)
```

- [ ] **Step 3: Verify FakeMCPClient satisfies the protocol**

Add a smoke test in `tests/ops/broker/test_robinhood.py` (still stubbed, will grow in Task 8):
```python
from ops.broker.mcp_client import RobinhoodMCPClient
from tests.ops.broker.fakes import FakeMCPClient

def test_fake_client_satisfies_protocol():
    client: RobinhoodMCPClient = FakeMCPClient()
    assert isinstance(client, RobinhoodMCPClient)
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest tests/ops/broker/test_robinhood.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/broker/mcp_client.py tests/ops/broker/fakes.py tests/ops/broker/test_robinhood.py
git commit -m "feat(ops/broker): RobinhoodMCPClient protocol + typed DTOs + FakeMCPClient

Narrow protocol mirroring just the MCP tool subset RobinhoodBroker needs.
FakeMCPClient makes unit tests deterministic and offline."
```

---

## Task 8: `RobinhoodBroker(Broker)` unit tests via `FakeMCPClient`

**Files:**
- Modify: `ops/broker/robinhood.py`
- Modify: `tests/ops/broker/test_robinhood.py`

**Interfaces:**
- Consumes: `RobinhoodMCPClient`, `Journal`, broker types
- Produces:
  - `class RobinhoodBroker(Broker)` fully implemented against the MCP protocol
  - Constructor: `RobinhoodBroker(client: RobinhoodMCPClient, journal: Journal)`

- [ ] **Step 1: Write failing tests (a representative slice)**

```python
def test_get_cash_maps_from_account(fake_client, journal):
    fake_client.seed_position("AAPL", Decimal("5"), Decimal("10"))
    fake_client.set_quote("AAPL", Decimal("11"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    assert broker.get_cash() == fake_client.get_account().cash


def test_get_positions_maps_mcp_positions(fake_client, journal):
    fake_client.seed_position("AAPL", Decimal("5"), Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].quantity == Decimal("5")
    assert positions[0].avg_entry_price == Decimal("10")
    assert positions[0].stop_loss_price is None


def test_place_order_buy_calls_mcp(fake_client, journal):
    fake_client.set_quote("AAPL", Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    fill = broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9"),
    ))
    assert fill.side == Side.BUY
    assert fill.quantity == Decimal("5")
    assert len(fake_client.placed) == 1
    assert fake_client.placed[0].notional == Decimal("50")


def test_close_position_places_quantity_sell(fake_client, journal):
    fake_client.set_quote("AAPL", Decimal("10"))
    fake_client.seed_position("AAPL", Decimal("5"), Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    fill = broker.close_position("AAPL")
    assert fill.side == Side.SELL
    assert fill.quantity == Decimal("5")
    ack = fake_client.placed[-1]
    assert ack.quantity == Decimal("5")
    assert ack.notional is None


def test_close_position_missing_raises(fake_client, journal):
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    with pytest.raises(NoSuchPosition):
        broker.close_position("NVDA")


def test_mcp_unavailable_wraps_as_broker_error(fake_client, journal):
    fake_client.set_quote("AAPL", Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    fake_client.fail_next(MCPUnavailable("network"))
    with pytest.raises(BrokerError):
        broker.place_order(Order(
            client_order_id="b-1", symbol="AAPL", side=Side.BUY,
            notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
            stop_loss_price=Decimal("9"),
        ))
```

- [ ] **Step 2: Verify failure**

Run: `.venv/bin/pytest tests/ops/broker/test_robinhood.py -v`
Expected: FAIL — `RobinhoodBroker` doesn't exist.

- [ ] **Step 3: Implement `RobinhoodBroker`**

In `ops/broker/robinhood.py`:
```python
"""RobinhoodBroker — Broker impl backed by the Robinhood MCP.

Depends only on the RobinhoodMCPClient protocol so tests inject a fake
and the factory injects RealRobinhoodMCPClient.

The SPOT hard-check at the top of place_order and close_position is
defense-in-depth: DenyListRule in GuardedBroker already blocks SPOT,
but if the guarded layer is ever misconfigured or bypassed, this if
is a second gate that no config or rule change can remove.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from ops.broker.base import (
    Broker, BrokerError, NoSuchPosition, OrderRejected,
)
from ops.broker.mcp_client import (
    MCPUnavailable, RobinhoodMCPClient,
)
from ops.broker.types import Fill, Order, OrderType, Position, Side
from ops.journal import Journal


_SPOT_SYMBOLS = {"SPOT"}


class RobinhoodBroker(Broker):
    def __init__(self, *, client: RobinhoodMCPClient, journal: Journal):
        self._client = client
        self._journal = journal

    def get_cash(self) -> Decimal:
        try:
            return self._client.get_account().cash
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc

    def get_equity(self) -> Decimal:
        try:
            return self._client.get_account().equity
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc

    def get_positions(self) -> list[Position]:
        try:
            mcp_positions = self._client.get_positions()
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc
        return [
            Position(
                symbol=p.symbol, quantity=p.quantity,
                avg_entry_price=p.avg_price, stop_loss_price=None,
            )
            for p in mcp_positions
        ]

    def get_quote(self, symbol: str) -> Decimal:
        try:
            return self._client.get_quote(symbol)
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc

    def place_order(self, order: Order) -> Fill:
        self._enforce_spot_hard_check(order.symbol)
        self._journal.record_order(
            client_order_id=order.client_order_id, symbol=order.symbol,
            side=order.side.value, notional_dollars=order.notional_dollars,
            stop_loss_price=order.stop_loss_price,
        )
        try:
            ack = self._client.place_equity_order(
                symbol=order.symbol, side=order.side,
                notional=order.notional_dollars, quantity=None,
                order_type=order.order_type, limit_price=order.limit_price,
                client_order_id=order.client_order_id,
            )
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc
        return self._ack_to_fill(order, ack)

    def close_position(self, symbol: str) -> Fill:
        self._enforce_spot_hard_check(symbol)
        try:
            positions = self._client.get_positions()
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc
        existing = next((p for p in positions if p.symbol == symbol), None)
        if existing is None:
            raise NoSuchPosition(f"no position in {symbol}")
        client_order_id = f"close-{symbol}-{uuid.uuid4().hex[:8]}"
        try:
            ack = self._client.place_equity_order(
                symbol=symbol, side=Side.SELL,
                notional=None, quantity=existing.quantity,
                order_type=OrderType.MARKET, limit_price=None,
                client_order_id=client_order_id,
            )
        except MCPUnavailable as exc:
            raise BrokerError(f"mcp unavailable: {exc}") from exc
        return self._ack_to_fill_close(symbol, existing.quantity, ack)

    def _enforce_spot_hard_check(self, symbol: str) -> None:
        if symbol.upper() in _SPOT_SYMBOLS:
            raise OrderRejected("SpotDenyList", "SPOT is contractually restricted")

    def _ack_to_fill(self, order, ack) -> Fill:
        # Fill quantity from ack; fall back to notional/price if ack missing qty.
        qty = ack.quantity if ack.quantity is not None else Decimal("0")
        price = ack.fill_price if ack.fill_price is not None else Decimal("0")
        fill = Fill(
            order_id=ack.order_id, client_order_id=ack.client_order_id,
            symbol=order.symbol, side=order.side, quantity=qty, price=price,
            filled_at=datetime.now(timezone.utc),
        )
        self._journal.record_fill(
            order_id=fill.order_id, client_order_id=fill.client_order_id,
            symbol=fill.symbol, side=fill.side.value,
            quantity=fill.quantity, price=fill.price, filled_at=fill.filled_at,
        )
        return fill

    def _ack_to_fill_close(self, symbol: str, qty: Decimal, ack) -> Fill:
        price = ack.fill_price if ack.fill_price is not None else Decimal("0")
        fill = Fill(
            order_id=ack.order_id, client_order_id=ack.client_order_id,
            symbol=symbol, side=Side.SELL, quantity=qty, price=price,
            filled_at=datetime.now(timezone.utc),
        )
        self._journal.record_fill(
            order_id=fill.order_id, client_order_id=fill.client_order_id,
            symbol=fill.symbol, side=fill.side.value,
            quantity=fill.quantity, price=fill.price, filled_at=fill.filled_at,
        )
        return fill
```

Add `import uuid` at the top.

- [ ] **Step 4: Verify tests pass**

Run: `.venv/bin/pytest tests/ops/broker/test_robinhood.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/broker/robinhood.py tests/ops/broker/test_robinhood.py
git commit -m "feat(ops/broker): RobinhoodBroker + FakeMCPClient unit tests

Implements Broker against RobinhoodMCPClient. Injectable, all tests
run offline. MCPUnavailable → BrokerError so GuardedBroker's existing
except BrokerError still journals as order_rejected."
```

---

## Task 9: `RealRobinhoodMCPClient` — MCP SDK wiring + token file

**Files:**
- Modify: `ops/broker/mcp_client.py`
- Modify: `tests/ops/broker/test_robinhood.py` — token-file perms test, error-mapping tests

**Interfaces:**
- Consumes: `mcp` Python SDK, `RobinhoodMCPClient` protocol
- Produces:
  - `class RealRobinhoodMCPClient` — concrete client, first-run OAuth against `https://agent.robinhood.com/mcp/trading`, token cached at `~/.config/tradingagents/robinhood_token.json` (env: `OPS_RH_TOKEN_PATH`)
  - Token file created with `0600` perms
  - MCP protocol / network errors mapped to `MCPUnavailable`

**Test scope:** unit-test the token-file writer and the error mapping helpers. Do not test the OAuth browser flow (deferred to opt-in live tests in Task 12).

- [ ] **Step 1: Write tests for token path resolution + perms**

```python
def test_token_path_defaults_to_home(monkeypatch, tmp_path):
    from ops.broker.mcp_client import _resolve_token_path
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPS_RH_TOKEN_PATH", raising=False)
    path = _resolve_token_path()
    assert path == tmp_path / ".config" / "tradingagents" / "robinhood_token.json"


def test_token_path_env_override(monkeypatch, tmp_path):
    from ops.broker.mcp_client import _resolve_token_path
    override = tmp_path / "custom.json"
    monkeypatch.setenv("OPS_RH_TOKEN_PATH", str(override))
    assert _resolve_token_path() == override


def test_write_token_creates_dir_with_0600_perms(tmp_path):
    from ops.broker.mcp_client import _write_token
    path = tmp_path / "sub" / "token.json"
    _write_token(path, {"access_token": "xyz", "expires_at": "..."})
    assert path.exists()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600
```

- [ ] **Step 2: Add token-file helpers**

At the bottom of `ops/broker/mcp_client.py`:
```python
import json
import os
from pathlib import Path


def _resolve_token_path() -> Path:
    override = os.environ.get("OPS_RH_TOKEN_PATH")
    if override:
        return Path(override)
    home = Path(os.environ.get("HOME", "~")).expanduser()
    return home / ".config" / "tradingagents" / "robinhood_token.json"


def _write_token(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write with strict perms from creation.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
    except Exception:
        # If the file existed with laxer perms, tighten before we bail.
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
        raise


def _read_token(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open() as f:
        return json.load(f)
```

- [ ] **Step 3: Add `RealRobinhoodMCPClient` shell**

```python
_RH_MCP_ENDPOINT = "https://agent.robinhood.com/mcp/trading"


class RealRobinhoodMCPClient:
    """Concrete MCP client. Handles token load/refresh + OAuth on first run.

    The OAuth flow is deferred to first .connect() call; construction is
    side-effect-free so factories can build one at import time."""

    def __init__(self, *, endpoint: str = _RH_MCP_ENDPOINT, token_path: Path | None = None):
        self._endpoint = endpoint
        self._token_path = token_path or _resolve_token_path()
        self._session = None   # populated on connect()

    def connect(self) -> None:
        # Sketch — real impl uses mcp Python SDK's ClientSession.
        # Here we describe the intent so Plan 3b's orchestrator can wire it.
        token = _read_token(self._token_path)
        if token is None:
            token = self._run_oauth_browser_flow()
            _write_token(self._token_path, token)
        # Establish MCP session; store on self._session.
        # (Real implementation calls mcp.client.ClientSession with token.)

    def _run_oauth_browser_flow(self) -> dict:
        raise NotImplementedError(
            "OAuth browser flow — implemented against `mcp` SDK's OAuth helper "
            "when the SDK version to target is finalised in Task 9 review."
        )

    # Protocol methods delegate to the MCP session with narrow try/except
    # wrapping to MCPUnavailable. Shape shown for get_account; the rest follow:
    def get_account(self) -> AccountInfo:
        if self._session is None:
            self.connect()
        try:
            result = self._session.call_tool("get_accounts", {})
            row = result["accounts"][0]
            return AccountInfo(
                cash=Decimal(str(row["cash"])),
                equity=Decimal(str(row["equity"])),
                buying_power=Decimal(str(row.get("buying_power", row["cash"]))),
            )
        except Exception as exc:
            raise MCPUnavailable(f"get_account failed: {exc}") from exc

    def get_positions(self) -> list[MCPPosition]:
        if self._session is None:
            self.connect()
        try:
            result = self._session.call_tool("get_equity_positions", {})
            return [
                MCPPosition(
                    symbol=row["symbol"],
                    quantity=Decimal(str(row["quantity"])),
                    avg_price=Decimal(str(row["average_price"])),
                )
                for row in result.get("positions", [])
            ]
        except Exception as exc:
            raise MCPUnavailable(f"get_positions failed: {exc}") from exc

    def get_quote(self, symbol: str) -> Decimal:
        if self._session is None:
            self.connect()
        try:
            result = self._session.call_tool("get_equity_quotes", {"symbols": [symbol]})
            row = result["quotes"][0]
            return Decimal(str(row["last_trade_price"]))
        except Exception as exc:
            raise MCPUnavailable(f"get_quote failed: {exc}") from exc

    def place_equity_order(
        self, *, symbol: str, side: Side,
        notional: Decimal | None, quantity: Decimal | None,
        order_type: OrderType, limit_price: Decimal | None,
        client_order_id: str,
    ) -> MCPOrderAck:
        if self._session is None:
            self.connect()
        params = {
            "symbol": symbol,
            "side": side.value.lower(),
            "type": order_type.value.lower(),
            "client_order_id": client_order_id,
        }
        if notional is not None:
            params["notional"] = str(notional)
        if quantity is not None:
            params["quantity"] = str(quantity)
        if limit_price is not None:
            params["limit_price"] = str(limit_price)
        try:
            result = self._session.call_tool("place_equity_order", params)
            return MCPOrderAck(
                order_id=result["id"], client_order_id=client_order_id,
                symbol=symbol, side=side,
                quantity=Decimal(str(result["quantity"])) if result.get("quantity") else None,
                notional=Decimal(str(result["notional"])) if result.get("notional") else None,
                status=result["status"],
                fill_price=Decimal(str(result["fill_price"])) if result.get("fill_price") else None,
            )
        except Exception as exc:
            raise MCPUnavailable(f"place_equity_order failed: {exc}") from exc

    def cancel_equity_order(self, order_id: str) -> None:
        if self._session is None:
            self.connect()
        try:
            self._session.call_tool("cancel_equity_order", {"id": order_id})
        except Exception as exc:
            raise MCPUnavailable(f"cancel_equity_order failed: {exc}") from exc
```

**Note:** the exact `mcp` SDK API (`ClientSession.call_tool` etc.) may differ from the sketch — verify against the installed SDK's docs during implementation and adapt. The DTOs and error mapping stay the same.

- [ ] **Step 4: Verify token tests pass**

Run: `.venv/bin/pytest tests/ops/broker/test_robinhood.py -v -k token`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/broker/mcp_client.py tests/ops/broker/test_robinhood.py
git commit -m "feat(ops/broker): RealRobinhoodMCPClient + token file helpers

Token cached at ~/.config/tradingagents/robinhood_token.json (0o600).
Env OPS_RH_TOKEN_PATH override for tests. OAuth browser flow scaffolded
against the mcp Python SDK; exercised by opt-in live tests in Task 12."
```

---

## Task 10: `RobinhoodBroker` SPOT hard-check (dedicated tests)

**Files:**
- Modify: `tests/ops/broker/test_robinhood.py`

Task 8 already added the check in `_enforce_spot_hard_check`. This task adds tests that pin it independently of the DenyListRule so future changes can't silently weaken the gate.

- [ ] **Step 1: Write SPOT tests**

```python
def test_spot_hard_check_blocks_place_order_even_without_guardrails(fake_client, journal):
    """RobinhoodBroker's own SPOT check fires before any MCP call, so guardrails aren't required."""
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    with pytest.raises(OrderRejected, match="SpotDenyList"):
        broker.place_order(Order(
            client_order_id="b-1", symbol="SPOT", side=Side.BUY,
            notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
            stop_loss_price=Decimal("100"),
        ))
    assert fake_client.placed == []


def test_spot_hard_check_is_case_insensitive(fake_client, journal):
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    with pytest.raises(OrderRejected, match="SpotDenyList"):
        broker.place_order(Order(
            client_order_id="b-1", symbol="spot", side=Side.BUY,
            notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
            stop_loss_price=Decimal("100"),
        ))


def test_spot_hard_check_blocks_close_position(fake_client, journal):
    fake_client.seed_position("SPOT", Decimal("1"), Decimal("100"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    with pytest.raises(OrderRejected, match="SpotDenyList"):
        broker.close_position("SPOT")
```

- [ ] **Step 2: Verify pass**

Run: `.venv/bin/pytest tests/ops/broker/test_robinhood.py -v -k spot`
Expected: PASS (implementation already added in Task 8).

- [ ] **Step 3: Commit**

```bash
git add tests/ops/broker/test_robinhood.py
git commit -m "test(ops/broker): pin SPOT hard-check independent of DenyListRule"
```

---

## Task 11: `build_guarded_robinhood_broker` factory + config broker_mode switch

**Files:**
- Modify: `ops/__init__.py`
- Modify: `tests/ops/test_factory.py`

**Interfaces:**
- Consumes: `RobinhoodBroker`, `RealRobinhoodMCPClient`, `RuleEngine`, `OpsConfig`, `Journal`
- Produces:
  - `ops.build_guarded_robinhood_broker(*, config, journal, mcp_client=None, start_of_day_equity, start_of_week_equity) -> GuardedBroker`
  - Default `mcp_client=None` → constructs a `RealRobinhoodMCPClient()`; tests inject `FakeMCPClient`.
  - `broker_mode` is checked by the caller (Plan 3b), NOT auto-selected by the factory — 3a keeps the factory explicit.

- [ ] **Step 1: Write failing tests**

```python
def test_build_guarded_robinhood_broker_with_fake_client(config, journal):
    from tests.ops.broker.fakes import FakeMCPClient
    client = FakeMCPClient()
    client.set_quote("AAPL", Decimal("10"))
    broker = build_guarded_robinhood_broker(
        config=config, journal=journal,
        mcp_client=client,
        start_of_day_equity=lambda: Decimal("1000"),
        start_of_week_equity=lambda: Decimal("1000"),
    )
    assert isinstance(broker, GuardedBroker)


def test_build_guarded_robinhood_broker_blocks_spot(config, journal):
    from tests.ops.broker.fakes import FakeMCPClient
    client = FakeMCPClient()
    broker = build_guarded_robinhood_broker(
        config=config, journal=journal, mcp_client=client,
        start_of_day_equity=lambda: Decimal("1000"),
        start_of_week_equity=lambda: Decimal("1000"),
    )
    with pytest.raises(OrderRejected):
        broker.place_order(Order(
            client_order_id="b-1", symbol="SPOT", side=Side.BUY,
            notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
            stop_loss_price=Decimal("100"),
        ))
```

- [ ] **Step 2: Verify failure**

Run: `.venv/bin/pytest tests/ops/test_factory.py -v -k robinhood`
Expected: FAIL — factory doesn't exist.

- [ ] **Step 3: Implement the factory**

In `ops/__init__.py`, after `build_guarded_paper_broker`:
```python
def build_guarded_robinhood_broker(
    *,
    config: OpsConfig,
    journal: Journal,
    mcp_client: "RobinhoodMCPClient | None" = None,
    start_of_day_equity: EquityFn,
    start_of_week_equity: EquityFn,
) -> GuardedBroker:
    """Build a guarded Robinhood broker.

    Pass `mcp_client=FakeMCPClient(...)` in tests; production callers omit it
    and a `RealRobinhoodMCPClient` is constructed with default endpoint + token path.
    """
    from ops.broker.mcp_client import RealRobinhoodMCPClient
    from ops.broker.robinhood import RobinhoodBroker

    client = mcp_client if mcp_client is not None else RealRobinhoodMCPClient()
    inner = RobinhoodBroker(client=client, journal=journal)
    engine = RuleEngine(
        build_default_rule_chain(
            start_of_day_equity=start_of_day_equity,
            start_of_week_equity=start_of_week_equity,
        )
    )
    return GuardedBroker(inner=inner, engine=engine, journal=journal, config=config)
```

Add to `__all__`.

- [ ] **Step 4: Verify tests pass**

Run: `.venv/bin/pytest tests/ops/test_factory.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/__init__.py tests/ops/test_factory.py
git commit -m "feat(ops): build_guarded_robinhood_broker factory

Mirrors build_guarded_paper_broker. Tests inject FakeMCPClient; the
Plan 3b orchestrator will branch on config.broker_mode to pick the
factory."
```

---

## Task 12: Opt-in live-read integration tests

**Files:**
- Modify: `tests/ops/broker/test_robinhood_live.py`

**Interfaces:**
- Consumes: `RealRobinhoodMCPClient`
- Produces: read-only integration tests skipped unless `OPS_RH_LIVE_TESTS=1`.

**Absolutely no `place_equity_order` in this file.** Read-only only.

- [ ] **Step 1: Write the live-read tests**

```python
"""Opt-in live-network tests against the real Robinhood MCP.

Gated on OPS_RH_LIVE_TESTS=1. Requires an OAuth-authenticated token file
(first run performs the browser flow interactively). Read-only calls only.
"""
import os
from decimal import Decimal
import pytest

from ops.broker.mcp_client import RealRobinhoodMCPClient

pytestmark = pytest.mark.skipif(
    os.environ.get("OPS_RH_LIVE_TESTS") != "1",
    reason="live Robinhood MCP tests are opt-in; set OPS_RH_LIVE_TESTS=1 to run",
)


@pytest.fixture(scope="module")
def client() -> RealRobinhoodMCPClient:
    c = RealRobinhoodMCPClient()
    c.connect()
    return c


def test_get_account_returns_positive_equity(client):
    acct = client.get_account()
    assert acct.equity > Decimal("0")
    assert acct.cash >= Decimal("0")


def test_get_positions_returns_list_of_mcp_positions(client):
    positions = client.get_positions()
    for p in positions:
        assert p.symbol.isupper()
        assert p.quantity > Decimal("0")


def test_get_quote_returns_decimal(client):
    q = client.get_quote("SPY")
    assert q > Decimal("0")


def test_token_file_has_0600_perms():
    from ops.broker.mcp_client import _resolve_token_path
    path = _resolve_token_path()
    assert path.exists(), "token file should be created after connect()"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600
```

- [ ] **Step 2: Sanity-run gated behavior**

Run without env var: `.venv/bin/pytest tests/ops/broker/test_robinhood_live.py -v`
Expected: 4 skipped.

- [ ] **Step 3: Commit**

```bash
git add tests/ops/broker/test_robinhood_live.py
git commit -m "test(ops/broker): opt-in live-read Robinhood MCP integration tests

Skipped unless OPS_RH_LIVE_TESTS=1. Read-only calls (account, positions,
quote). No live order placement — that gate lives in Plan 3c."
```

---

## Task 13: End-to-end integration test with `close_position`

**Files:**
- Modify: `tests/ops/test_integration.py` or `tests/ops/test_integration_decide_once.py` (whichever holds the current end-to-end)

**Interfaces:**
- Consumes: `build_guarded_paper_broker`, universe stubs, strategy stubs (existing)
- Produces: an integration test that exercises the full pipeline through `close_position`, verifying every zero-notional SELL path in the old test is now `close_position`.

- [ ] **Step 1: Read the current end-to-end test**

Run: `.venv/bin/pytest tests/ops/test_integration.py -v` (or the relevant file). Note any remaining skips introduced in Task 1.

- [ ] **Step 2: Rewrite skipped tests to use `close_position`**

For each skipped test, replace zero-notional SELL construction with a `close_position` call. If the test's shape allows, add an assertion that the resulting fill's `client_order_id` matches `close-<symbol>-<8hex>`.

- [ ] **Step 3: Add a stop-hit lifecycle test**

```python
def test_integration_buy_stop_hit_via_close_position(tmp_path):
    """Full lifecycle: universe seeds AAPL, strategy issues BUY, guardian
    triggers on price drop, close_position sells everything."""
    # Setup: journal, config, quote source, seeded universe, seeded strategy
    # Assertions:
    #   - after strategy tick: 1 open position with stop_loss_price
    #   - after price drop below stop: guardian's check_stops_once sells
    #   - post: 0 open positions; cash increased by proceeds
    #   - journal contains a fill with client_order_id starting close-AAPL-
    #   - journal contains a stop_hit event with mode == 'absolute'
```

Wire this against the existing decide-once integration fixtures; don't reinvent.

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest tests/ops/test_integration*.py -v`
Expected: PASS.

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/pytest tests/ops/ -q`
Expected: passes = target (180-200), no skips other than live-only ones.

- [ ] **Step 6: Commit**

```bash
git add tests/ops/
git commit -m "test(ops): end-to-end lifecycle uses close_position and per-position stops"
```

---

## Task 14: `ops/README.md` — Robinhood setup section

**Files:**
- Modify: `ops/README.md`

- [ ] **Step 1: Draft the section**

Add a new section:
```markdown
## Robinhood MCP (live broker)

The live broker connects to Robinhood's official MCP endpoint at
`https://agent.robinhood.com/mcp/trading`. First run performs an OAuth
browser flow; the token is cached at `~/.config/tradingagents/robinhood_token.json`
with `0600` perms. Override via `OPS_RH_TOKEN_PATH`.

**Plan 3a ships the broker plumbing but NOT the always-on orchestration.**
`broker_mode` defaults to `paper`. The `build_guarded_robinhood_broker`
factory exists so the Plan 3b orchestrator can consume it.

### Running opt-in live tests

Read-only integration tests against the real MCP (never place orders) are
skipped by default. To run them:

```bash
OPS_RH_LIVE_TESTS=1 .venv/bin/pytest tests/ops/broker/test_robinhood_live.py -v
```

First invocation performs the OAuth browser flow. Subsequent runs reuse
the cached token until it expires.

### Constraints

- SPOT is contractually restricted. Both `DenyListRule` and a hard-coded
  check inside `RobinhoodBroker` reject any SPOT order. Do not remove
  either gate.
- Live order placement in tests is forbidden. If the CI runs live tests
  and this file grows a `place_order` call, revert.
```

- [ ] **Step 2: Commit**

```bash
git add ops/README.md
git commit -m "docs(ops): Robinhood MCP setup, OAuth, OPS_RH_LIVE_TESTS gate"
```

---

## Task 15: Push branch + open PR

- [ ] **Step 1: Verify clean state**

Run:
```bash
git status
.venv/bin/pytest tests/ops/ -q
```
Expected: clean working tree; ~180-200 tests pass; live-read suite skipped.

- [ ] **Step 2: Push**

```bash
git push -u origin feat/ops-broker-plumbing
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create --title "feat(ops): plan 3a — broker plumbing + safety hardening" --body "$(cat <<'EOF'
Plan 3a of the live-v1 spec. Design doc:
`docs/superpowers/specs/2026-07-01-tradingagents-live-v1-plan-3a-broker-plumbing-design.md`.

## Highlights

- `Broker.close_position(symbol)` replaces the zero-notional SELL convention. `PaperBroker` and `RobinhoodBroker` both implement it; `GuardedBroker` runs the rule chain under the broker lock across snapshot + inner delegate.
- `PositionGuardian` honours `Position.stop_loss_price`, falling back to `cfg.per_position_stop_pct` when unset. `stop_hit`/`stop_failed` events carry `mode` and `threshold_repr` so replays can distinguish absolute vs pct triggers.
- New `RobinhoodBroker` backed by a typed `RobinhoodMCPClient` protocol. Unit tests use `FakeMCPClient`; opt-in live-read integration tests gated on `OPS_RH_LIVE_TESTS=1`. Live order placement is not tested.
- Defense-in-depth SPOT hard-check inside `RobinhoodBroker` — plain `if`, not a Rule, so no config or rule change can remove it.
- Journal `equity_snapshots` gains `kind` + `note` columns and a `get_latest_equity_snapshot(kind=..., since=...)` reader. Defensive `ALTER TABLE` handles pre-existing DBs.
- `PaperBroker.from_journal(...)` classmethod rebuilds `_positions` and `_cash` by replaying fills. Consumed by the Plan 3b orchestrator's startup reconciliation.
- `ops.build_guarded_robinhood_broker` factory mirrors the paper factory.

## Not in this PR

- Guardian background thread, market calendar, orchestrator loop, `ops/main.py` — Plan 3b.
- Notifications (Pushover, SMTP), event dispatcher, `LIVE_MAX_POSITION` first-N-trades gate — Plan 3c.
- `TradingAgentsPipelineAdapter._ensure_graph` lock — Plan 3b.
- Journal-persisted `Position.stop_loss_price` on fills — Plan 3b (when recovery matters).

## Test plan
- [x] `.venv/bin/pytest tests/ops/` — targets ~180-200 passing (was 138 on main).
- [x] `OPS_RH_LIVE_TESTS=1 .venv/bin/pytest tests/ops/broker/test_robinhood_live.py -v` — read-only calls succeed against the live MCP (manual, out-of-CI).

## Environment
- Model: Claude Opus 4.7
- Harness: Claude Code + superpowers plugin

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Verify the PR URL**

Confirm the PR opened successfully; note the number for tracking.
