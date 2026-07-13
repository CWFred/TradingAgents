# Short Sleeve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fourth paper sleeve that shorts EDGAR-red-flag names, with its own journal, memo store, screen queue, and $10k bucket.

**Architecture:** Mirrors the research sleeve stage-for-stage with inverted semantics: a `ShortPaperBroker` (new, short-native, isolated to the short journal), a `thesis_type="short"` memo, an inverted screen (expensive + red flag), the same brain/vet/drain overnight machinery pointed at short-specific stores, and a mechanical trade step. Spec: `docs/superpowers/specs/2026-07-13-short-and-insider-sleeves-design.md`.

**Tech Stack:** Python 3.11+, stdlib sqlite3, Pydantic v2, APScheduler, pytest. No new dependencies.

## Global Constraints

- Money is `Decimal`, always. Metric/calibration values are floats.
- Every scheduler wrapper journals `*_error` events; it never raises (a raise kills the APScheduler job).
- Per-name failures are logged to stderr and skipped — a sweep never dies on name #937.
- ds4 (the research LLM backend) may only be spun up inside the overnight window's managed-backend bracket in `_research_overnight_tick`. Never from a post-close tick.
- The short sleeve touches ONLY `short_journal_path`, `short_memo_store_path`, `short_screen_store_path`. The main journal is touched for reading falsifier trips and writing run-summary/gate events only.
- LLM-stated probabilities are never sizing inputs. The only research signal sizing reads is `conviction_tier`.
- Run tests with `.venv/bin/pytest`. Note: `tests/test_main.py` has 11 pre-existing failures on main — do not chase them; scope test runs to the files you touch plus the suites named in each task.
- Commit after every task with the message given in the task.

## File Map

| file | role |
|---|---|
| `ops/broker/types.py` (modify) | `Side.SHORT` / `Side.COVER` |
| `ops/broker/short_paper.py` (create) | short-native paper broker + replay |
| `tradingagents/memos/schema.py` (modify) | `thesis_type="short"`, `ShortThesis` |
| `ops/research/short_screen.py` (create) | inverted bars |
| `ops/research/short_triggers.py` (create) | red-flag triggers |
| `ops/research/short_brain.py` (create) | short memo authoring |
| `ops/research/vetting.py` (modify) | pluggable confirm-tier map |
| `ops/research/short_sizing.py` (create) | short fences |
| `ops/research/short_trading.py` (create) | trade step |
| `ops/research/metrics.py`, `monitor.py` (modify) | direction-aware drawdown |
| `ops/events.py` (modify) | new event kinds |
| `ops/config.py` (modify) | paths + starting cash |
| `ops/main.py` (modify) | overnight stage + trade tick |

---

### Task 1: `Side.SHORT` / `Side.COVER` + long-only `stop_pct`

**Files:**
- Modify: `ops/broker/types.py`
- Test: `tests/ops/broker/test_types_short.py` (create)

**Interfaces:**
- Produces: `Side.SHORT` (`"SHORT"`), `Side.COVER` (`"COVER"`); `Order` rejects `stop_pct` on SHORT/COVER sides.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/broker/test_types_short.py
from decimal import Decimal

import pytest

from ops.broker.types import Order, OrderType, Side


def _order(side, **kw):
    return Order(
        client_order_id="t-1", symbol="XYZ", side=side,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET, **kw,
    )


def test_short_and_cover_sides_exist():
    assert Side.SHORT.value == "SHORT"
    assert Side.COVER.value == "COVER"


def test_short_order_without_stop_is_valid():
    assert _order(Side.SHORT).side is Side.SHORT


def test_stop_pct_rejected_on_short_and_cover():
    for side in (Side.SHORT, Side.COVER):
        with pytest.raises(ValueError, match="SHORT/COVER"):
            _order(side, stop_pct=Decimal("-0.08"))


def test_buy_stop_pct_unchanged():
    assert _order(Side.BUY, stop_pct=Decimal("-0.08")).stop_pct == Decimal("-0.08")
    with pytest.raises(ValueError):
        _order(Side.BUY, stop_pct=Decimal("0.08"))
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/broker/test_types_short.py -v`
Expected: FAIL — `AttributeError: SHORT`

- [ ] **Step 3: Implement**

In `ops/broker/types.py`, extend the enum:

```python
class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    SHORT = "SHORT"    # sell-to-open; ShortPaperBroker only
    COVER = "COVER"    # buy-to-close; ShortPaperBroker only
```

In `Order.__post_init__`, replace the existing `stop_pct` check with:

```python
        if self.stop_pct is not None:
            if self.side in (Side.SHORT, Side.COVER):
                raise ValueError(
                    "stop_pct is not supported on SHORT/COVER orders; the short "
                    "trade step enforces stops from average entry price"
                )
            if self.stop_pct >= 0:
                raise ValueError("stop_pct must be negative (entry-relative, e.g. -0.08)")
```

- [ ] **Step 4: Run new tests + existing broker tests**

Run: `.venv/bin/pytest tests/ops/broker/ -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/broker/types.py tests/ops/broker/test_types_short.py
git commit -m "feat(short): SHORT/COVER order sides, stop_pct stays long-only"
```

---

### Task 2: `ShortPaperBroker` — open, cover, equity

**Files:**
- Create: `ops/broker/short_paper.py`
- Test: `tests/ops/broker/test_short_paper.py` (create)

**Interfaces:**
- Consumes: `Side.SHORT`/`Side.COVER` (Task 1), `ops.journal.Journal`, `ops.broker.base.Broker/NoSuchPosition`, `ops.broker.types.Order/Position/Fill`.
- Produces: `ShortPaperBroker(journal=, quote_source=, starting_cash=)` with `place_order(order) -> Fill` (SHORT/COVER only), `close_position(symbol, *, client_order_id=None) -> Fill`, `get_cash()`, `get_equity()`, `get_positions()` (positive quantities; the whole book is short by construction), `get_quote(symbol)`. Class method `from_journal(...)` arrives in Task 3.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/broker/test_short_paper.py
from decimal import Decimal

import pytest

from ops.broker.base import NoSuchPosition
from ops.broker.short_paper import ShortPaperBroker
from ops.broker.types import Order, OrderType, Side
from ops.journal import Journal


@pytest.fixture
def journal(tmp_path):
    with Journal(str(tmp_path / "short.sqlite")) as j:
        yield j


def _broker(journal, prices: dict[str, Decimal], cash="10000"):
    return ShortPaperBroker(
        journal=journal, quote_source=lambda s: prices[s],
        starting_cash=Decimal(cash),
    )


def _short(symbol="XYZ", notional="400"):
    return Order(client_order_id=f"s-{symbol}", symbol=symbol, side=Side.SHORT,
                 notional_dollars=Decimal(notional), order_type=OrderType.MARKET)


def test_short_credits_proceeds_and_opens_position(journal):
    b = _broker(journal, {"XYZ": Decimal("10")})
    fill = b.place_order(_short())
    assert fill.side is Side.SHORT and fill.quantity == Decimal("40")
    assert b.get_cash() == Decimal("10400")
    (pos,) = b.get_positions()
    assert pos.quantity == Decimal("40") and pos.avg_entry_price == Decimal("10")


def test_equity_is_cash_minus_liability(journal):
    prices = {"XYZ": Decimal("10")}
    b = _broker(journal, prices)
    b.place_order(_short())
    assert b.get_equity() == Decimal("10000")   # unchanged at entry
    prices["XYZ"] = Decimal("9")                # -10%: short profits
    assert b.get_equity() == Decimal("10040")
    prices["XYZ"] = Decimal("12")               # +20%: short loses
    assert b.get_equity() == Decimal("9920")


def test_close_position_covers_all_at_market(journal):
    prices = {"XYZ": Decimal("10")}
    b = _broker(journal, prices)
    b.place_order(_short())
    prices["XYZ"] = Decimal("8")
    fill = b.close_position("XYZ")
    assert fill.side is Side.COVER
    assert b.get_positions() == []
    assert b.get_cash() == Decimal("10400") - Decimal("320")  # 40 * 8


def test_close_position_allows_negative_cash(journal):
    # A blown-up short must still be coverable — forced cover, damage shows in equity.
    prices = {"XYZ": Decimal("10")}
    b = _broker(journal, prices, cash="150")
    b.place_order(_short(notional="400"))
    prices["XYZ"] = Decimal("20")
    b.close_position("XYZ")
    assert b.get_cash() == Decimal("550") - Decimal("800")


def test_rejects_buy_and_sell_sides(journal):
    b = _broker(journal, {"XYZ": Decimal("10")})
    for side in (Side.BUY, Side.SELL):
        with pytest.raises(ValueError, match="SHORT/COVER"):
            b.place_order(Order(client_order_id="x", symbol="XYZ", side=side,
                                notional_dollars=Decimal("100"),
                                order_type=OrderType.MARKET))


def test_cover_unknown_symbol_raises(journal):
    b = _broker(journal, {"XYZ": Decimal("10")})
    with pytest.raises(NoSuchPosition):
        b.close_position("XYZ")


def test_short_adds_average_entry(journal):
    prices = {"XYZ": Decimal("10")}
    b = _broker(journal, prices)
    b.place_order(_short(notional="400"))
    prices["XYZ"] = Decimal("20")
    b.place_order(_short(notional="400"))   # 20 more shares at 20
    (pos,) = b.get_positions()
    assert pos.quantity == Decimal("60")
    assert pos.avg_entry_price == Decimal("40") * 10 / 60 + Decimal("20") * 20 / 60
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/broker/test_short_paper.py -v`
Expected: FAIL — `ModuleNotFoundError: ops.broker.short_paper`

- [ ] **Step 3: Implement `ops/broker/short_paper.py`**

```python
"""Short-native in-memory paper broker, isolated to the short journal.

Deliberately NOT a mode of PaperBroker: retrofitting signed quantities into
the long broker risks the replay correctness of four live paper books.
Positions here carry positive quantities; the whole book is short by
construction. Equity = cash - Σ(qty × current price). Short proceeds are
credited to cash at fill; covering debits qty × price and MAY drive cash
negative (a forced cover must always succeed — the damage shows in equity,
never as a refused exit).

Paper-fidelity caveats (recorded in the spec): no borrow cost, no locate,
no squeeze modeling. Exposure discipline lives in ops/research/short_sizing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from ops.broker.base import Broker, NoSuchPosition
from ops.broker.types import Fill, Order, Position, Side

_EPSILON = Decimal("0.0000001")


class ShortPaperBroker(Broker):
    def __init__(self, *, journal, quote_source, starting_cash: Decimal):
        self._journal = journal
        self._quote = quote_source
        self._cash = Decimal(starting_cash)
        self._positions: dict[str, Position] = {}

    def get_cash(self) -> Decimal:
        return self._cash

    def get_quote(self, symbol: str) -> Decimal:
        return self._quote(symbol)

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_equity(self) -> Decimal:
        total = self._cash
        for pos in self._positions.values():
            total -= pos.quantity * self._quote(pos.symbol)
        return total

    def place_order(self, order: Order) -> Fill:
        if order.side not in (Side.SHORT, Side.COVER):
            raise ValueError(
                f"ShortPaperBroker accepts SHORT/COVER only, got {order.side}"
            )
        self._journal.record_order(
            client_order_id=order.client_order_id, symbol=order.symbol,
            side=order.side.value, notional_dollars=order.notional_dollars,
            stop_loss_price=None,
        )
        price = self._quote(order.symbol)
        if order.side == Side.SHORT:
            return self._fill_short(order, price)
        return self._fill_cover(order, price)

    def _fill_short(self, order: Order, price: Decimal) -> Fill:
        qty = order.notional_dollars / price
        self._cash += order.notional_dollars
        existing = self._positions.get(order.symbol)
        if existing is None:
            pos = Position(symbol=order.symbol, quantity=qty,
                           avg_entry_price=price, stop_loss_price=None)
        else:
            total = existing.quantity + qty
            avg = ((existing.avg_entry_price * existing.quantity) + price * qty) / total
            pos = Position(symbol=order.symbol, quantity=total,
                           avg_entry_price=avg, stop_loss_price=None)
        self._positions[order.symbol] = pos
        return self._make_fill(order.client_order_id, order.symbol, Side.SHORT, qty, price)

    def _fill_cover(self, order: Order, price: Decimal) -> Fill:
        existing = self._positions.get(order.symbol)
        if existing is None:
            raise NoSuchPosition(f"no short position in {order.symbol}")
        qty = order.notional_dollars / price
        if qty > existing.quantity + _EPSILON:
            raise NoSuchPosition(
                f"cover qty {qty} exceeds short position {existing.quantity}"
            )
        self._cash -= order.notional_dollars
        remaining = existing.quantity - qty
        if remaining > _EPSILON:
            self._positions[order.symbol] = Position(
                symbol=existing.symbol, quantity=remaining,
                avg_entry_price=existing.avg_entry_price, stop_loss_price=None,
            )
        else:
            del self._positions[order.symbol]
        return self._make_fill(order.client_order_id, order.symbol, Side.COVER, qty, price)

    def close_position(self, symbol: str, *, client_order_id: str | None = None) -> Fill:
        existing = self._positions.get(symbol)
        if existing is None:
            raise NoSuchPosition(f"no short position in {symbol}")
        price = self._quote(symbol)
        qty = existing.quantity
        cost = qty * price
        order_id = client_order_id or f"cover-{symbol}-{uuid4().hex[:8]}"
        self._journal.record_order(
            client_order_id=order_id, symbol=symbol, side=Side.COVER.value,
            notional_dollars=cost, stop_loss_price=None,
        )
        self._cash -= cost
        del self._positions[symbol]
        return self._make_fill(order_id, symbol, Side.COVER, qty, price)

    def _make_fill(self, client_order_id, symbol, side, qty, price) -> Fill:
        fill = Fill(
            order_id=str(uuid4()), client_order_id=client_order_id, symbol=symbol,
            side=side, quantity=qty, price=price,
            filled_at=datetime.now(timezone.utc),
        )
        self._journal.record_fill(
            order_id=fill.order_id, client_order_id=fill.client_order_id,
            symbol=fill.symbol, side=fill.side.value, quantity=fill.quantity,
            price=fill.price, filled_at=fill.filled_at,
        )
        return fill
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/ops/broker/test_short_paper.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/broker/short_paper.py tests/ops/broker/test_short_paper.py
git commit -m "feat(short): ShortPaperBroker — short-native paper fills, isolated journal"
```

---

### Task 3: `ShortPaperBroker.from_journal` replay

**Files:**
- Modify: `ops/broker/short_paper.py`, `ops/events.py`
- Test: `tests/ops/broker/test_short_paper.py` (extend)

**Interfaces:**
- Produces: `ShortPaperBroker.from_journal(*, journal, quote_source, starting_cash) -> ShortPaperBroker`; event kind `KIND_JOURNAL_REPLAY_ORPHAN_COVER = "journal_replay_orphan_cover"` in `ops/events.py` (payload via the existing `journal_replay_orphan_sell_payload` builder — same shape).

- [ ] **Step 1: Write the failing tests** (append to the test file)

```python
def test_from_journal_rebuilds_shorts(journal):
    prices = {"XYZ": Decimal("10"), "ABC": Decimal("5")}
    b = _broker(journal, prices)
    b.place_order(_short("XYZ", "400"))
    b.place_order(_short("ABC", "200"))
    prices["ABC"] = Decimal("4")
    b.close_position("ABC")
    replayed = ShortPaperBroker.from_journal(
        journal=journal, quote_source=lambda s: prices[s],
        starting_cash=Decimal("10000"),
    )
    assert replayed.get_cash() == b.get_cash()
    assert {p.symbol for p in replayed.get_positions()} == {"XYZ"}
    (pos,) = replayed.get_positions()
    assert pos.quantity == Decimal("40") and pos.avg_entry_price == Decimal("10")


def test_replay_orphan_cover_is_journaled_and_skipped(journal):
    journal.record_order(client_order_id="c1", symbol="GHO", side="COVER",
                         notional_dollars=Decimal("100"), stop_loss_price=None)
    journal.record_fill(order_id="f1", client_order_id="c1", symbol="GHO",
                        side="COVER", quantity=Decimal("10"), price=Decimal("10"),
                        filled_at=__import__("datetime").datetime.now(
                            __import__("datetime").timezone.utc))
    replayed = ShortPaperBroker.from_journal(
        journal=journal, quote_source=lambda s: Decimal("10"),
        starting_cash=Decimal("10000"),
    )
    assert replayed.get_cash() == Decimal("10000")
    from ops import events
    assert journal.count_events(events.KIND_JOURNAL_REPLAY_ORPHAN_COVER) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/broker/test_short_paper.py -v -k replay`
Expected: FAIL — no attribute `from_journal`

- [ ] **Step 3: Implement**

In `ops/events.py`, next to `KIND_JOURNAL_REPLAY_ORPHAN_SELL`, add:

```python
KIND_JOURNAL_REPLAY_ORPHAN_COVER = "journal_replay_orphan_cover"
```

(and add it to the audit-only kinds frozenset alongside the orphan-sell kind).
In `ops/broker/short_paper.py`, add (mirrors `PaperBroker.from_journal`'s
notional-from-order-row discipline):

```python
    @classmethod
    def from_journal(cls, *, journal, quote_source, starting_cash: Decimal) -> "ShortPaperBroker":
        from ops import events

        broker = cls(journal=journal, quote_source=quote_source,
                     starting_cash=starting_cash)
        for adj in journal.read_cash_adjustments():
            broker._cash += adj["amount"]
        orders_by_id = {o["client_order_id"]: o for o in journal.read_orders()}
        for f in journal.read_fills():
            symbol, side, qty, price = f["symbol"], f["side"], f["quantity"], f["price"]
            order = orders_by_id.get(f["client_order_id"])
            notional = order["notional_dollars"] if order is not None else qty * price
            if order is None:
                journal.record_event(
                    events.KIND_JOURNAL_REPLAY_FALLBACK,
                    events.journal_replay_fallback_payload(
                        client_order_id=f["client_order_id"], symbol=symbol,
                        side=side, reason="no matching order row; falling back to qty*price",
                    ),
                )
            if side == Side.SHORT.value:
                broker._cash += notional
                existing = broker._positions.get(symbol)
                if existing is None:
                    broker._positions[symbol] = Position(
                        symbol=symbol, quantity=qty, avg_entry_price=price,
                        stop_loss_price=None,
                    )
                else:
                    total = existing.quantity + qty
                    avg = ((existing.avg_entry_price * existing.quantity) + price * qty) / total
                    broker._positions[symbol] = Position(
                        symbol=symbol, quantity=total, avg_entry_price=avg,
                        stop_loss_price=None,
                    )
            elif side == Side.COVER.value:
                existing = broker._positions.get(symbol)
                if existing is None:
                    journal.record_event(
                        events.KIND_JOURNAL_REPLAY_ORPHAN_COVER,
                        events.journal_replay_orphan_sell_payload(
                            client_order_id=f["client_order_id"], symbol=symbol,
                            quantity=qty, price=price,
                            reason="COVER replayed with no matching prior SHORT position",
                        ),
                    )
                    continue
                broker._cash -= notional
                remaining = existing.quantity - qty
                if remaining > _EPSILON:
                    broker._positions[symbol] = Position(
                        symbol=symbol, quantity=remaining,
                        avg_entry_price=existing.avg_entry_price, stop_loss_price=None,
                    )
                else:
                    del broker._positions[symbol]
        return broker
```

- [ ] **Step 4: Run the broker suite**

Run: `.venv/bin/pytest tests/ops/broker/ -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/broker/short_paper.py ops/events.py tests/ops/broker/test_short_paper.py
git commit -m "feat(short): ShortPaperBroker.from_journal replay + orphan-cover event"
```

---

### Task 4: memo schema — `thesis_type="short"` + `ShortThesis`

**Files:**
- Modify: `tradingagents/memos/schema.py`
- Test: `tests/` — extend the existing memo schema test module (find it with `grep -rl block_matches_type tests/`)

**Interfaces:**
- Produces: `ThesisType = Literal["value", "event", "short"]`; `class ShortThesis(BaseModel)` with fields `overvaluation_mechanism: str`, `red_flags: list[str]` (min_length=1), `why_now: str`, `squeeze_risk: str`, `downside_scenario: str`; `Memo.short_block: ShortThesis | None = None`; `block_matches_type()` true iff exactly the block matching `thesis_type` is set.

- [ ] **Step 1: Write the failing tests** (in the existing memo schema test module; reuse its memo-factory helper if one exists, else build a minimal valid Memo inline as below)

```python
def _short_thesis():
    from tradingagents.memos.schema import ShortThesis
    return ShortThesis(
        overvaluation_mechanism="story stock priced on a segment in decline",
        red_flags=["8-K 4.02 non-reliance filed 2026-06-30"],
        why_now="restatement forces guidance reset within two quarters",
        squeeze_risk="12% of float short; borrow assumed available (paper)",
        downside_scenario="rerates to 8x normalized EBIT, ~-40%",
    )


def test_short_memo_block_matches(memo_kwargs):
    from tradingagents.memos.schema import Memo
    memo = Memo(**{**memo_kwargs, "thesis_type": "short",
                   "value_block": None, "event_block": None,
                   "short_block": _short_thesis()})
    assert memo.block_matches_type()


def test_short_memo_with_value_block_mismatches(memo_kwargs, value_block):
    from tradingagents.memos.schema import Memo
    memo = Memo(**{**memo_kwargs, "thesis_type": "short",
                   "value_block": value_block, "event_block": None,
                   "short_block": _short_thesis()})
    assert not memo.block_matches_type()
```

(Adapt fixture names to the module's existing helpers; the two assertions are
the deliverable.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ -k "short_memo" -v`
Expected: FAIL — `ImportError: ShortThesis`

- [ ] **Step 3: Implement**

In `tradingagents/memos/schema.py`:

```python
ThesisType = Literal["value", "event", "short"]


class ShortThesis(BaseModel):
    """Type-specific block for a short (negative-catalyst) thesis."""

    overvaluation_mechanism: str = Field(
        description=(
            "Why the market prices it too HIGH — a specific, named reason "
            "(the mirror of ValueThesis.why_cheap)."
        )
    )
    red_flags: list[str] = Field(
        min_length=1,
        description="The disclosures driving the thesis; each must also appear "
                    "in evidence with an accession citation.",
    )
    why_now: str = Field(
        description="The trigger. Shorts bleed carry — timing is part of the thesis."
    )
    squeeze_risk: str = Field(
        description="Crowded-short / low-float assessment (prose; paper has no borrow data)."
    )
    downside_scenario: str = Field(
        description="What the stock is worth if the thesis plays out."
    )
```

On `Memo`, add `short_block: ShortThesis | None = None` next to the other
blocks, and replace `block_matches_type` with:

```python
    def block_matches_type(self) -> bool:
        """True when exactly the block matching ``thesis_type`` is set."""
        blocks = {"value": self.value_block, "event": self.event_block,
                  "short": self.short_block}
        want = blocks.pop(self.thesis_type)
        return want is not None and all(b is None for b in blocks.values())
```

Also update the `MemoStore.save` error message in
`tradingagents/memos/store.py` ("exactly one of
value_block/event_block/short_block, matching the type, must be set").

- [ ] **Step 4: Run the full memo + research suites** (schema is load-bearing)

Run: `.venv/bin/pytest tests/ -k "memo or research" -v`
Expected: all PASS (fix any test asserting the old two-block error message)

- [ ] **Step 5: Commit**

```bash
git add tradingagents/memos/schema.py tradingagents/memos/store.py tests/
git commit -m "feat(short): thesis_type=short + ShortThesis memo block"
```

---

### Task 5: short screen — inverted bars

**Files:**
- Create: `ops/research/short_screen.py`
- Test: `tests/ops/research/test_short_screen.py` (create)

**Interfaces:**
- Consumes: `ops.research.screener.NameInputs`, `Bar`, `_ev_ebit`; `Fundamentals` fields `gross_margin_history`, `total_debt`, `cash`, `ebitda` (exactly as `ops/research/metrics.py` reads them); `Trigger` from Task 6 (screen only checks `trigger.kind` membership — build test triggers directly).
- Produces: `SHORT_TRIGGER_KINDS = frozenset({"red_flag_8k", "insider_sell_cluster", "going_concern"})`; `ShortScreenResult(symbol, asof, passed, bars, red_flags, market_cap, ev_ebit)` (frozen dataclass; `bars: tuple[Bar, ...]`, `red_flags: tuple[Trigger, ...]`); `screen_short_universe(inputs: list[NameInputs], *, asof: date) -> list[ShortScreenResult]`. Constants: `EV_EBIT_EXPENSIVE = Decimal("20")`, `NEGATIVE_EBIT_MIN_CAP = Decimal("500000000")`, `NET_DEBT_EBITDA_HIGH = Decimal("4")`, `GROSS_MARGIN_DECLINE_PP = Decimal("0.03")`, `MIN_BARS = 2`.

- [ ] **Step 1: Write the failing tests** — mirror the fixture style of the existing `tests/ops/research/` screener tests (canned `Fundamentals`, no network). Cover: (a) expensive EV/EBIT + high net-debt + red-flag trigger → passed; (b) two bars but NO red-flag trigger → not passed; (c) red-flag trigger but only one bar → not passed; (d) EBIT ≤ 0 with cap > $500M counts as the expensive bar; (e) margin-decline bar trips on a ≥3pp YoY drop in `gross_margin_history`.

```python
# tests/ops/research/test_short_screen.py — core case (adapt fixtures from
# the existing screener tests for Fundamentals/NameInputs construction)
from datetime import date
from decimal import Decimal

from ops.research.short_screen import screen_short_universe
from ops.research.triggers import Trigger


RED_FLAG = Trigger(kind="red_flag_8k", description="4.02 non-reliance",
                   date=date(2026, 7, 1), source="0001-26-000001")


def test_expensive_plus_red_flag_passes(expensive_name_inputs):
    inputs = expensive_name_inputs(triggers=(RED_FLAG,))
    (res,) = screen_short_universe([inputs], asof=date(2026, 7, 13))
    assert res.passed and res.red_flags == (RED_FLAG,)


def test_no_red_flag_never_passes(expensive_name_inputs):
    inputs = expensive_name_inputs(triggers=())
    (res,) = screen_short_universe([inputs], asof=date(2026, 7, 13))
    assert not res.passed
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/ops/research/test_short_screen.py -v` → `ModuleNotFoundError`

- [ ] **Step 3: Implement `ops/research/short_screen.py`**

```python
"""Inverted screen for the short sleeve: expensive/deteriorating AND red flag.

The mirror of ops/research/screener.py's "cheap AND change trigger": a name
is a short candidate only when >= MIN_BARS of the expensive/deteriorating
bars fire AND at least one red-flag trigger (SHORT_TRIGGER_KINDS, produced
by ops/research/short_triggers.py) is present. Thresholds are tunable
constants, same convention as the long screen.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ops.research.screener import Bar, NameInputs, _ev_ebit
from ops.research.triggers import Trigger

EV_EBIT_EXPENSIVE = Decimal("20")
NEGATIVE_EBIT_MIN_CAP = Decimal("500000000")
NET_DEBT_EBITDA_HIGH = Decimal("4")
GROSS_MARGIN_DECLINE_PP = Decimal("0.03")
MIN_BARS = 2

SHORT_TRIGGER_KINDS = frozenset({"red_flag_8k", "insider_sell_cluster", "going_concern"})


@dataclass(frozen=True)
class ShortScreenResult:
    symbol: str
    asof: date
    passed: bool
    bars: tuple[Bar, ...]
    red_flags: tuple[Trigger, ...]
    market_cap: Decimal
    ev_ebit: Decimal | None


def _expensive_bar(inputs: NameInputs) -> Bar:
    ev = _ev_ebit(inputs)
    if ev is None:  # EBIT <= 0 (or inputs missing): expensive iff a real cap rides on no earnings
        passed = inputs.market_cap > NEGATIVE_EBIT_MIN_CAP
        return Bar("ev_ebit_expensive", passed,
                   f"EBIT <= 0 with market cap {inputs.market_cap}")
    return Bar("ev_ebit_expensive", ev > EV_EBIT_EXPENSIVE,
               f"EV/EBIT {ev:.1f} vs {EV_EBIT_EXPENSIVE}")


def _net_debt_bar(inputs: NameInputs) -> Bar:
    f = inputs.fundamentals
    name = "net_debt_ebitda_high"
    if f.total_debt is None or f.ebitda is None or f.ebitda <= 0:
        return Bar(name, False, "debt/EBITDA unavailable")
    cash = f.cash if f.cash is not None else Decimal("0")
    ratio = (f.total_debt - cash) / f.ebitda
    return Bar(name, ratio > NET_DEBT_EBITDA_HIGH,
               f"net debt/EBITDA {ratio:.2f} vs {NET_DEBT_EBITDA_HIGH}")


def _margin_decline_bar(inputs: NameInputs) -> Bar:
    hist = sorted(inputs.fundamentals.gross_margin_history,
                  key=lambda yv: yv.fiscal_year_end, reverse=True)
    name = "gross_margin_declining"
    if len(hist) < 2:
        return Bar(name, False, "insufficient margin history")
    decline = hist[1].value - hist[0].value
    return Bar(name, decline >= GROSS_MARGIN_DECLINE_PP,
               f"gross margin {-decline * 100:.1f}pp YoY vs -{GROSS_MARGIN_DECLINE_PP * 100}pp")


def screen_short_universe(
    inputs: list[NameInputs], *, asof: date,
) -> list[ShortScreenResult]:
    out = []
    for name in inputs:
        bars = (_expensive_bar(name), _net_debt_bar(name), _margin_decline_bar(name))
        red_flags = tuple(t for t in name.triggers if t.kind in SHORT_TRIGGER_KINDS)
        fired = sum(1 for b in bars if b.passed)
        out.append(ShortScreenResult(
            symbol=name.symbol, asof=asof,
            passed=fired >= MIN_BARS and bool(red_flags),
            bars=bars, red_flags=red_flags,
            market_cap=name.market_cap, ev_ebit=_ev_ebit(name),
        ))
    return out
```

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/ops/research/test_short_screen.py -v` → all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/research/short_screen.py tests/ops/research/test_short_screen.py
git commit -m "feat(short): inverted screen — expensive/deteriorating + red flag"
```

---

### Task 6: red-flag triggers

**Files:**
- Create: `ops/research/short_triggers.py`
- Test: `tests/ops/research/test_short_triggers.py` (create)

**Interfaces:**
- Consumes: `tradingagents.dataflows.edgar` (`list_filings`, `full_text_search`, `get_cik`, `fetch_filing_text`, `Filing.notable_8k_items()`), `tradingagents.dataflows.form4.get_insider_transactions` (`InsiderTransaction` fields: `insider_name`, `code`, `ten_b5_1`, `transaction_date`), `ops.research.triggers.Trigger`.
- Produces: `find_short_triggers(ticker, *, asof, lookback_days=90, list_filings=None, transactions_fetcher=None, full_text_search=None, fetch_text=None, cik_resolver=None) -> list[Trigger]` emitting kinds `red_flag_8k` (items 4.02/1.03/3.01, plus 5.02 whose 8-K text mentions the CFO), `insider_sell_cluster` (≥ `INSIDER_SELL_CLUSTER_MIN = 3` distinct insiders, `code == "S"`, non-10b5-1, in window), `going_concern` (EDGAR full-text hit for the ticker's CIK on `'"substantial doubt" "going concern"'` in 10-K/10-Q). Every callable is injectable so tests run with zero network (same convention as `ops/research/triggers.py`).

- [ ] **Step 1: Write the failing tests** — canned `Filing` objects / transaction lists / FTS hit dicts, mirroring `tests/ops/research/` conventions for the existing trigger tests. Cover: 4.02 8-K → `red_flag_8k`; 5.02 8-K with "Chief Financial Officer" in fetched text → trigger, without it → no trigger; 3 distinct code-S sellers → `insider_sell_cluster`, 2 → none, 3 but 10b5-1 → none; FTS hit whose `_source.ciks` contains the ticker's CIK → `going_concern`; FTS raising → other triggers still returned (degrade, never die).

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`

- [ ] **Step 3: Implement `ops/research/short_triggers.py`**

```python
"""Red-flag trigger detection for the short sleeve — the reason to look NOW.

Mirror of ops/research/triggers.py. Kinds map to
short_screen.SHORT_TRIGGER_KINDS. Every source degrades independently: a
full-text-search failure must not suppress an 8-K red flag.
"""
from __future__ import annotations

from datetime import date, timedelta

from ops.research.triggers import TRIGGER_LOOKBACK_DAYS, Trigger
from tradingagents.dataflows import edgar

SHORT_8K_ITEMS = {"4.02", "1.03", "3.01"}
CFO_ITEM = "5.02"
INSIDER_SELL_CLUSTER_MIN = 3
GOING_CONCERN_QUERY = '"substantial doubt" "going concern"'


def find_short_triggers(
    ticker: str,
    *,
    asof: date,
    lookback_days: int = TRIGGER_LOOKBACK_DAYS,
    list_filings=None,
    transactions_fetcher=None,
    full_text_search=None,
    fetch_text=None,
    cik_resolver=None,
) -> list[Trigger]:
    list_filings = list_filings or edgar.list_filings
    full_text_search = full_text_search or edgar.full_text_search
    fetch_text = fetch_text or edgar.fetch_filing_text
    cik_resolver = cik_resolver or edgar.get_cik
    since = asof - timedelta(days=lookback_days)
    out: list[Trigger] = []

    out += _red_flag_8ks(ticker, since=since, asof=asof,
                         list_filings=list_filings, fetch_text=fetch_text)
    cluster = _insider_sell_cluster(ticker, since=since, asof=asof,
                                    transactions_fetcher=transactions_fetcher)
    if cluster is not None:
        out.append(cluster)
    gc = _going_concern(ticker, since=since, asof=asof,
                        full_text_search=full_text_search, cik_resolver=cik_resolver)
    if gc is not None:
        out.append(gc)
    return out


def _red_flag_8ks(ticker, *, since, asof, list_filings, fetch_text) -> list[Trigger]:
    # Filing.items carries the raw 8-K item numbers (tuple[str, ...]).
    out = []
    for f in list_filings(ticker, forms={"8-K"}, since=since):
        if f.filing_date is None or f.filing_date > asof:
            continue
        hit_items = set(f.items) & SHORT_8K_ITEMS
        if hit_items:
            out.append(Trigger(kind="red_flag_8k", description=", ".join(sorted(hit_items)),
                               date=f.filing_date, source=f.accession_number))
            continue
        if CFO_ITEM in f.items:
            try:
                text = fetch_text(f)
            except Exception:
                continue  # degrade: unreadable 8-K is not a trigger
            if "Chief Financial Officer" in text or "principal financial officer" in text.lower():
                out.append(Trigger(kind="red_flag_8k", description="CFO departure (5.02)",
                                   date=f.filing_date, source=f.accession_number))
    return out


def _insider_sell_cluster(ticker, *, since, asof, transactions_fetcher) -> Trigger | None:
    from tradingagents.dataflows.form4 import get_insider_transactions

    fetch = transactions_fetcher or get_insider_transactions
    txns = fetch(ticker, since=since)
    sells = [t for t in txns
             if t.code == "S" and not t.ten_b5_1
             and t.transaction_date is not None and since <= t.transaction_date <= asof]
    sellers = {t.insider_name for t in sells}
    if len(sellers) < INSIDER_SELL_CLUSTER_MIN:
        return None
    latest = max(sells, key=lambda t: t.transaction_date)
    return Trigger(kind="insider_sell_cluster",
                   description=f"{len(sellers)} insiders sold (non-10b5-1) in window",
                   date=latest.transaction_date, source=latest.accession)


def _going_concern(ticker, *, since, asof, full_text_search, cik_resolver) -> Trigger | None:
    try:
        cik = cik_resolver(ticker)
        hits = full_text_search(GOING_CONCERN_QUERY, forms={"10-K", "10-Q"},
                                start=since, end=asof)
    except Exception:
        return None  # degrade: FTS/CIK failure must not suppress other flags
    for hit in hits:
        src = hit.get("_source", {})
        ciks = {int(c) for c in src.get("ciks", []) if str(c).isdigit()}
        if cik in ciks:
            return Trigger(kind="going_concern",
                           description="going-concern language in 10-K/10-Q",
                           date=asof, source=hit.get("_id", "fts"))
    return None
```

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/ops/research/test_short_triggers.py -v` → all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/research/short_triggers.py tests/ops/research/test_short_triggers.py
git commit -m "feat(short): red-flag triggers — 8-K items, insider sell cluster, going concern"
```

---

### Task 7: short brain — memo authoring

**Files:**
- Create: `ops/research/short_brain.py`
- Test: `tests/ops/research/test_short_brain.py` (create)

**Interfaces:**
- Consumes: `ops.research.brain` internals (`_build_reading_plan`, `_run_evidence_stage`, `_screen_summary`, `_evidence_bullets`, `ResearchOutcome`, `ResearchError`, `MIN_EVIDENCE_ITEMS`), `resolve_evidence` + `validate_memo` from `ops/research/memo_validation.py`, `ShortThesis` (Task 4), `bind_structured`.
- Produces: `research_short_hit(hit, *, evidence_llm, thesis_llm, memo_store, list_filings=None, fetch_text=None, price_fetcher=None, today=None, thesis_model_spec=None) -> ResearchOutcome` — same contract as `brain.research_hit` but: draft model is `ShortMemoDraft` (`thesis_type: Literal["short"]`, `short_block: ShortThesis`, `recommendation: Literal["short", "pass"]`, all other MemoDraft fields), the counter-argument stage is a **bull defending the stock** (`DEFENSE_PROMPT`), and the memo prompt (`SHORT_MEMO_PROMPT`) states the inverted target semantics: *price_target_low is the cover target (profit); price_target_high is the thesis-wrong level; at least one falsifier machine-checkable; falsifiers describe IMPROVEMENT (the thesis breaking), e.g. `gross_margin_pct > X`*.

- [ ] **Step 1: Write the failing tests** — stub LLMs exactly as the existing `tests/ops/research/` brain tests do (canned structured outputs, injected `list_filings`/`fetch_text`/`price_fetcher`). Cover: a "short" draft saves a `pending_vetting` memo with `short_block` set into the given store; a "pass" draft marks it passed; insufficient evidence → `status="failed"`, nothing saved.

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`

- [ ] **Step 3: Implement** — copy `brain.research_hit`'s flow (it is ~100 lines; the shared helpers are imported, not duplicated), swapping: `BEAR_PROMPT` → `DEFENSE_PROMPT` ("You are the BULL defending {ticker} against a short thesis… name the strongest specific reasons the shorts are wrong"), `MEMO_PROMPT` → `SHORT_MEMO_PROMPT` (rules exactly as in Interfaces above, plus: `red_flags` in `short_block` must each be backed by a cited evidence item), `MemoDraft` → `ShortMemoDraft`, and the save path setting `thesis_type="short"`, `short_block=draft.short_block`, `status="pending_vetting"`, `mark_passed` on `recommendation == "pass"`.

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/ops/research/test_short_brain.py -v` → all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/research/short_brain.py tests/ops/research/test_short_brain.py
git commit -m "feat(short): short brain — bear authors, bull defends, short memo drafts"
```

---

### Task 8: vetting — pluggable confirm-tier map

**Files:**
- Modify: `ops/research/vetting.py`
- Test: extend the existing vetting test module (`grep -rl CONFIRM_TIERS tests/`)

**Interfaces:**
- Produces: `SHORT_CONFIRM_TIERS: dict[str, ConvictionTier] = {"Sell": "high", "Underweight": "medium"}`; `vet_memo(..., confirm_tiers: dict[str, ConvictionTier] = CONFIRM_TIERS)` and `vet_pending(..., confirm_tiers=CONFIRM_TIERS)` threading it through. Existing callers unchanged (default preserves behavior).

- [ ] **Step 1: Write the failing tests** — with the existing stub adapter: rating "Sell" + `confirm_tiers=SHORT_CONFIRM_TIERS` → confirm/high; rating "Buy" + short map → reject; default map behavior unchanged (existing tests already pin it).

- [ ] **Step 2: Run to verify failure** — `TypeError: unexpected keyword argument 'confirm_tiers'`

- [ ] **Step 3: Implement** — add the constant next to `CONFIRM_TIERS`; change `tier = CONFIRM_TIERS.get(rating)` to `tier = confirm_tiers.get(rating)`; add the parameter to both signatures with the default.

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/ -k vetting -v` → all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/research/vetting.py tests/
git commit -m "feat(short): inverted vetting map — graph Sell confirms a short"
```

---

### Task 9: short sizing fences

**Files:**
- Create: `ops/research/short_sizing.py`
- Test: `tests/ops/research/test_short_sizing.py` (create)

**Interfaces:**
- Consumes: `SizingDecision` from `ops/research/sizing.py`.
- Produces: `size_short_entry(*, tier, equity, exposure_by_symbol: dict[str, Decimal], symbol, sector, exposure_by_sector: dict[str, Decimal], gross_short_exposure: Decimal, adv_20d: Decimal | None) -> SizingDecision`. Constants: `SHORT_TIER_SIZING = {"starter": Decimal("0.01"), "medium": Decimal("0.02"), "high": Decimal("0.03")}`, `NAME_CAP_PCT = Decimal("0.05")`, `SECTOR_CAP_PCT = Decimal("0.15")`, `GROSS_EXPOSURE_CAP_PCT = Decimal("0.50")`, `ADV_CAP_PCT = Decimal("0.02")`, `MIN_ORDER_DOLLARS = Decimal("100")`. Exposure = current market value of the short (qty × current price), not cost — shorts grow as they go wrong, so caps must read live exposure.

- [ ] **Step 1: Write the failing tests** — mirror `tests/ops/research/test_sizing.py` style. Cover: happy path (1% of $10k = $100 for starter); each fence rejecting with its name in `rejected` (name cap, sector cap, gross-exposure cap, ADV cap, unknown tier); notional clamped by remaining gross-exposure room; sub-$100 room → reject.

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`

- [ ] **Step 3: Implement** — same shape as `sizing.size_entry`: start from `equity * SHORT_TIER_SIZING[tier]`, then clamp/reject in order: name room (`NAME_CAP_PCT * equity - exposure_by_symbol.get(symbol, 0)`), sector room, gross room (`GROSS_EXPOSURE_CAP_PCT * equity - gross_short_exposure`), ADV (`ADV_CAP_PCT * adv_20d`; None → reject "adv unavailable"), final `< MIN_ORDER_DOLLARS` → reject. Quantize money to cents as `sizing._quantize_money` does. No cash clamp: shorting *adds* cash; the gross-exposure cap is the margin discipline.

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/ops/research/test_short_sizing.py -v` → all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/research/short_sizing.py tests/ops/research/test_short_sizing.py
git commit -m "feat(short): sizing fences — half tiers, live-exposure caps, 50% gross cap"
```

---

### Task 10: short trade step + events

**Files:**
- Create: `ops/research/short_trading.py`
- Modify: `ops/events.py`
- Test: `tests/ops/research/test_short_trading.py` (create)

**Interfaces:**
- Consumes: `ShortPaperBroker` (Tasks 2–3), `size_short_entry` (Task 9), memo store with short memos (Task 4), `events` kinds (below).
- Produces: `trade_short_sleeve(*, memo_store, short_journal, main_journal, quote_source, starting_cash, deny_list: frozenset[str], asof, now=None, sector_lookup=None, adv_fetcher=None) -> TradeOutcome` (reuse `TradeOutcome` from `ops/research/trading.py`). The caller passes `config.deny_list`. New event kinds in `ops/events.py`, mirroring the research set with payload builders of identical shape: `KIND_SHORT_POSITION_OPENED = "short_position_opened"`, `KIND_SHORT_POSITION_CLOSED = "short_position_closed"`, `KIND_SHORT_TRADE_RUN = "short_trade_run"`, `KIND_SHORT_TRADE_ERROR = "short_trade_error"` (run/error kinds live on the MAIN journal as the daily gate; opened/closed on the short journal). Constants: `HARD_STOP_PCT = Decimal("0.25")`, `MAX_HOLD_MONTHS = 9`.

- [ ] **Step 1: Write the failing tests** — build on `tests/ops/research/test_trading.py` fixtures (in-memory journals, dict quote source, canned memo store). Exit reasons each get a test, first-match-wins order pinned: (1) memo missing, (2) memo resolved, (3) falsifier tripped (`events.KIND_FALSIFIER_TRIPPED` with the memo_id on the MAIN journal), (4) hard stop (quote ≥ entry × 1.25), (5) target hit (quote ≤ `price_target_low`), (6) time stop (entry_date older than `min(expected_holding_months, 9)` months — compute via 30-day months on the `short_position_opened` payload's `entry_date`). Entries: open short memo → SHORT order sized by tier; a deny-listed ticker is skipped with `"deny-listed"` in the reason (spec decision 4 — never short a blackout name); closed-memo guard (a memo with a `short_position_closed` event never re-enters); exits run before entries; a symbol exited this run is not re-entered; summary event + equity snapshot (`kind="short_run"`) recorded.

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`

- [ ] **Step 3: Implement** — transcribe `ops/research/trading.py` with these deltas (the module docstring must state each):
  - broker: `ShortPaperBroker.from_journal(journal=short_journal, ...)`
  - `_exit_reason` first-match order as pinned in Step 1; hard stop reads `pos.avg_entry_price`; target compares `quote <= Decimal(str(memo.price_target_low))`; time stop parses `entry_date` from the `short_position_opened` provenance payload (`short_journal.latest_event_payload_by_symbol(events.KIND_SHORT_POSITION_OPENED)`)
  - `_entry_pass`: skip `memo.ticker in deny_list` first; exposure maps built from live quotes (`{p.symbol: p.quantity * broker.get_quote(p.symbol)}`, falling back per-symbol to `avg_entry_price` on `QuoteUnavailable`); gross = their sum; `size_short_entry(...)`; orders `Side.SHORT`
  - equity fallback: mirror `_equity_with_fallback` but SUBTRACT position value, marking unquotable names at last SHORT fill price — conservative note in docstring: a stale mark understates neither side systematically; `journal.last_buy_fill_for` only reads BUY fills, so add the tiny query inline via `read_fills()` filtering `side == "SHORT"` per symbol
  - summary event `KIND_SHORT_TRADE_RUN` on main journal; snapshot `kind="short_run"` on short journal

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/ops/research/test_short_trading.py tests/ops/research/test_trading.py -v` → all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/research/short_trading.py ops/events.py tests/ops/research/test_short_trading.py
git commit -m "feat(short): short trade step — inverted exits, live-exposure sizing"
```

---

### Task 11: direction-aware drawdown (metrics + monitor)

**Files:**
- Modify: `ops/research/metrics.py`, `ops/research/monitor.py`
- Test: extend `tests/ops/research/` metrics/monitor test modules

**Interfaces:**
- Produces: `MetricContext` gains `direction: str = "long"`; `_drawdown_series` (and therefore `drawdown_pct` and the `drawdown_from_cost_pct` falsifier metric) returns the **adverse move as negative** for shorts: for `direction="short"`, each observation is `(entry - close*factor) / entry * 100` (price up ⇒ negative ⇒ loss). `monitor._build_context` sets `direction="short"` when `memo.thesis_type == "short"`. `DRAWDOWN_ESCALATION_PCT` then applies unchanged to both directions.

- [ ] **Step 1: Write the failing tests** — a short-direction context where price rose 35% from entry yields `drawdown_pct(ctx) == pytest.approx(-35.0)` and trips a `drawdown_from_cost_pct <= -30` falsifier; long-direction behavior unchanged (existing tests pin it).

- [ ] **Step 2: Run to verify failure** — assertion error (positive value returned)

- [ ] **Step 3: Implement** — in `_drawdown_series`, compute `move = (float(close * factor) - entry) / entry * 100.0` then `move if ctx.direction == "long" else -move`. Add the field with default `"long"` so every existing constructor call stands. In `monitor._build_context`, pass `direction=("short" if memo.thesis_type == "short" else "long")`.

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/ops/research/ -v` → all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/research/metrics.py ops/research/monitor.py tests/
git commit -m "feat(short): direction-aware drawdown — -30% escalation covers shorts"
```

---

### Task 12: config — paths, cash, env overrides

**Files:**
- Modify: `ops/config.py`
- Test: extend the existing config test module (`grep -rl research_journal_path tests/`)

**Interfaces:**
- Produces: `OpsConfig` fields `short_journal_path` (default `~/.local/state/tradingagents/short_journal.sqlite` via a `_default_short_journal_path()` mirroring the research one, XDG-aware), `short_memo_store_path` (`.../short_memos.sqlite`), `short_screen_store_path` (`.../short_screen.sqlite`), `short_starting_cash: Decimal = Decimal("10000")` (validated > 0 in `__post_init__`). Env overrides in `load_config`: `OPS_SHORT_JOURNAL_PATH`, `OPS_SHORT_MEMO_STORE_PATH`, `OPS_SHORT_SCREEN_STORE_PATH`, `OPS_SHORT_STARTING_CASH` — same pattern as the research ones at `ops/config.py:297`.

- [ ] **Step 1: Write the failing tests** — defaults end with the expected filenames; each env var overrides; `short_starting_cash=Decimal("0")` raises `ValueError`.
- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement** — copy the `research_journal_path` default-fn/field/env/validation pattern four times with the new names.
- [ ] **Step 4: Run** — `.venv/bin/pytest tests/ -k config -v` → all PASS
- [ ] **Step 5: Commit**

```bash
git add ops/config.py tests/
git commit -m "feat(short): config — short journal/memo/screen paths + starting cash"
```

---

### Task 13: daemon wiring — overnight stage + trade tick

**Files:**
- Modify: `ops/main.py`, `ops/events.py`
- Test: `tests/ops/test_main_short.py` (create), following the existing `_research_trade_tick`-style tests

**Interfaces:**
- Consumes: everything above.
- Produces: `_short_trade_tick(journal, config)` (gate: `journal.has_event_today(events.KIND_SHORT_TRADE_RUN)`; errors → `KIND_SHORT_TRADE_ERROR`; registered `CronTrigger(hour=16, minute=27, day_of_week="mon-fri")`). Short overnight work runs INSIDE `_research_overnight_tick`'s existing while-loop as a stage invoked AFTER the research vet/drain stages each iteration (research — the proven sleeve — gets the window first; shorts get the remainder under the same `deadline`/`stop`/`backend` bracket, which is the sole ds4-contention guard). The stage: (a) short screen if due per `config.research_screen_interval_days` against the short screen store's `last_run()` — build `NameInputs` exactly as `run_screen` does but with `triggers=find_short_triggers(...)`, then `screen_short_universe` → store hits; (b) drain pending short hits via `research_short_hit` (cap: `config.research_drain_nightly_cap`); (c) vet the short store's queue via `vet_pending(..., confirm_tiers=SHORT_CONFIRM_TIERS)`. Aggregated per-tick events on the main journal: `KIND_SHORT_DRAIN_RUN = "short_drain_run"`, `KIND_SHORT_VETTING_RUN = "short_vetting_run"`, `KIND_SHORT_DRAIN_ERROR = "short_drain_error"` (payload builders mirror the research ones; add all to the audit frozenset). The stage contributes to the loop's `progress` counter so an all-empty night still exits promptly, and a stage failure disables further short iterations that night without touching the research stages.

- [ ] **Step 1: Write the failing tests** — `_short_trade_tick`: gates on today's run event; journals `KIND_SHORT_TRADE_ERROR` (not raises) when the trade step blows up (inject a quote source that raises). Overnight stage: with empty short queues it contributes zero progress and records nothing; with a canned pending hit + stub LLMs/adapter it records one `short_drain_run`.
- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement** — `_short_trade_tick` transcribes `_research_trade_tick` (`ops/main.py:455-482`) with short names/stores. Extract the short overnight work into `_short_overnight_stage(journal, config, *, backend, deadline, should_stop, tick_now, adapter_factory=None) -> int` (returns progress) and call it inside the while-loop after the research drain block, guarding with the same `stop()`/deadline checks; wrap in try/except journaling `KIND_SHORT_DRAIN_ERROR` and setting a `short_errored` flag mirroring `vet_errored`.
- [ ] **Step 4: Run** — `.venv/bin/pytest tests/ops/test_main_short.py -v` plus the existing overnight-tick tests (`.venv/bin/pytest tests/ops/ -k overnight -v`) → all PASS
- [ ] **Step 5: Commit**

```bash
git add ops/main.py ops/events.py tests/ops/test_main_short.py
git commit -m "feat(short): daemon wiring — overnight short stage + 16:27 trade tick"
```

---

### Task 14: overview section + docs

**Files:**
- Modify: `ops/notify/overview.py`, `docs/research_trading.md`
- Create: `docs/short_sleeve.md`
- Test: extend `tests/ops/notify/test_overview.py`

**Interfaces:**
- Produces: `build_daily_overview(...)` accepts `short_journal` (optional, `None` ⇒ section says the sleeve isn't configured — day-one empty-journal discipline per the module docstring) and the report gains a short-sleeve section: today's `short_trade_run` summary (entered/exited/skipped), latest `short_run` equity snapshot, any `short_*_error` events. Journal-only — no network, no store reads, mirroring `_research_section`'s pattern (`_day_slice` + `read_equity_snapshots`).

- [ ] **Step 1: Write the failing tests** — an overview built with a short journal containing one trade-run event + snapshot mentions the sleeve's equity and entries; with `short_journal=None` the section reports not-configured; an error event surfaces.
- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement** — add `_short_section(...)` next to the research section, thread the journal through `build_daily_overview` and its `ops/main.py` call site (`Journal(config.short_journal_path)` in the overview tick's context stack).
- [ ] **Step 4: Run** — `.venv/bin/pytest tests/ops/notify/ -v` → all PASS
- [ ] **Step 5: Write `docs/short_sleeve.md`** — one page: what runs when (overnight stage order, 16:27 trade tick), the fences table from the spec, exit-reason order, paper-fidelity caveats, and manual commands. Add a row for `short_trade` to the "What runs when" table in `docs/research_trading.md`.
- [ ] **Step 6: Full-suite check + commit**

Run: `.venv/bin/pytest tests/ --ignore=tests/test_main.py -q`
Expected: all PASS (test_main.py's 11 failures pre-date this work)

```bash
git add ops/notify/overview.py ops/main.py docs/short_sleeve.md docs/research_trading.md tests/
git commit -m "feat(short): daily-overview section + short-sleeve runbook"
```
