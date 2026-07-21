# Dashboard Per-Stock P&L + Decimal Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a toggleable ($/%) per-stock P&L view to the ops dashboard's sleeve drawer and all-positions panel, colored green/red, and enforce 2-decimal formatting everywhere.

**Architecture:** A new isolated `GET /api/pnl?sleeve=<name>` endpoint fetches quotes (via the existing yfinance quote source) and computes unrealized P&L per open position server-side in `Decimal`. `build_snapshot` stays journal-only and untouched. The React frontend fetches P&L per sleeve on demand and renders a toggleable P&L column.

**Tech Stack:** Python 3 (stdlib `http.server`, `sqlite3`, `decimal`), pytest; React + TypeScript + Vite, vitest.

## Global Constraints

- **Money never touches IEEE float.** All money math is `Decimal` (Python) or string arithmetic (JS `fmtMoney`/`fmt2`). Ratios/percentages may use float.
- **`build_snapshot` stays journal-only** — no broker, no quotes, no network. The new `/api/pnl` route is the ONLY dashboard code path allowed to fetch quotes.
- **Server bind stays `127.0.0.1`** (loopback-only) and read-only (no mutating routes).
- **Per-symbol degradation:** one symbol's quote failure must not fail the whole response — that row gets `null` P&L + an `error` string; other rows resolve.
- **Short sleeve** (`short`) journals positive magnitudes; its P&L sign is inverted (profit when price falls).
- Follow existing code style: no new dependencies, mirror existing test file patterns.

---

### Task 1: `fmtQty` rounds to 2 dp + `fmt2` bare-number helper

**Files:**
- Modify: `dashboard-ui/src/lib/format.ts`
- Test: `dashboard-ui/src/lib/format.test.ts`

**Interfaces:**
- Consumes: existing `fmtMoney(value, dp)`.
- Produces: `fmtQty(q: string): string` (now rounds to ≤2 dp, strips trailing zeros); `fmt2(value: string | null | undefined): string` (2 dp, no `$`, `—` for empty).

- [ ] **Step 1: Write failing tests**

Add to `dashboard-ui/src/lib/format.test.ts` inside the existing `describe("fmtQty", ...)` block and a new `describe` for `fmt2`. Also add `fmt2` to the import on line 2.

```ts
// inside describe("fmtQty", ...)
  it("rounds long quantities to 2dp", () => {
    expect(fmtQty("12.3456789")).toBe("12.35");
    expect(fmtQty("100.005")).toBe("100.01");
    expect(fmtQty("7")).toBe("7");
    expect(fmtQty("-30.128")).toBe("-30.13");
  });

// new top-level describe
describe("fmt2 (bare 2dp number)", () => {
  it("formats decimal strings to 2dp without a currency symbol", () => {
    expect(fmt2("142.3")).toBe("142.30");
    expect(fmt2("12.3456")).toBe("12.35");
    expect(fmt2("-5.005")).toBe("−5.01");
    expect(fmt2(null)).toBe("—");
    expect(fmt2("")).toBe("—");
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd dashboard-ui && npx vitest run src/lib/format.test.ts`
Expected: FAIL — `fmtQty("12.3456789")` returns `"12.3456789"`; `fmt2` is not exported.

- [ ] **Step 3: Implement**

In `dashboard-ui/src/lib/format.ts`, replace the `fmtQty` function (lines 47-50) and add `fmt2`:

```ts
export function fmtQty(q: string): string {
  // Round paper-fill precision noise to at most 2dp, then strip trailing
  // zeros so whole shares read "12" not "12.00".
  const bare = fmt2(q);
  if (bare === "—") return q;
  return bare.replace(/\.?0+$/, "");
}

// Bare 2dp number (no currency symbol) reusing fmtMoney's string-safe,
// float-free rounding. For prices/entries/stops that are decimal strings.
export function fmt2(value: string | null | undefined): string {
  const m = fmtMoney(value, 2);
  return m.startsWith("$") ? m.slice(1)
    : m.startsWith("−$") ? "−" + m.slice(2)
    : m;
}
```

Note: `fmt2("-5.005")` → `fmtMoney` gives `"−$5.01"` → `"−5.01"`. Negative-zero suppression is inherited from `fmtMoney`. `fmtQty`'s trailing-zero strip: `"12.35"`→`"12.35"`, `"12.00"`→`"12"`, `"12.50"`→`"12.5"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd dashboard-ui && npx vitest run src/lib/format.test.ts`
Expected: PASS (existing `fmtQty` cases `"12.0000"→"12"`, `"0.5000"→"0.5"`, `"-30"→"-30"` still pass).

- [ ] **Step 5: Commit**

```bash
git add dashboard-ui/src/lib/format.ts dashboard-ui/src/lib/format.test.ts
git commit -m "feat(dashboard): round fmtQty to 2dp, add fmt2 bare-number helper"
```

---

### Task 2: Pure P&L math — `position_pnl`

**Files:**
- Create: `ops/dashboard/pnl.py`
- Test: `tests/ops/dashboard/test_pnl.py`

**Interfaces:**
- Produces: `position_pnl(entry: Decimal | None, quantity: Decimal, price: Decimal | None, *, is_short: bool) -> tuple[Decimal | None, Decimal | None]` returning `(pnl_dollar, pnl_pct)`. `pnl_dollar` is `None` when `price` is `None`. `pnl_pct` is `None` when `price` is `None`, or `entry` is `None`/`0`.

- [ ] **Step 1: Write failing tests**

Create `tests/ops/dashboard/test_pnl.py`:

```python
"""Pure per-position P&L math: long/short sign, guards."""
from decimal import Decimal

from ops.dashboard.pnl import position_pnl


def test_long_gain():
    d, p = position_pnl(Decimal("100"), Decimal("10"), Decimal("110"),
                        is_short=False)
    assert d == Decimal("100")            # (110-100)*10
    assert p == Decimal("0.1")            # (110-100)/100


def test_long_loss():
    d, p = position_pnl(Decimal("100"), Decimal("10"), Decimal("90"),
                        is_short=False)
    assert d == Decimal("-100")
    assert p == Decimal("-0.1")


def test_short_gain_when_price_falls():
    # short journals positive magnitude qty; profit when price drops
    d, p = position_pnl(Decimal("100"), Decimal("10"), Decimal("90"),
                        is_short=True)
    assert d == Decimal("100")            # (100-90)*10
    assert p == Decimal("0.1")


def test_none_price_yields_none():
    assert position_pnl(Decimal("100"), Decimal("10"), None,
                        is_short=False) == (None, None)


def test_zero_or_none_entry_guards_pct_only():
    d, p = position_pnl(Decimal("0"), Decimal("10"), Decimal("50"),
                        is_short=False)
    assert d == Decimal("500")            # dollar still computable
    assert p is None                      # pct guarded (divide-by-zero)
    d2, p2 = position_pnl(None, Decimal("10"), Decimal("50"), is_short=False)
    assert d2 is None and p2 is None      # no entry basis at all
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/dashboard/test_pnl.py -v`
Expected: FAIL — `ModuleNotFoundError: ops.dashboard.pnl`.

- [ ] **Step 3: Implement**

Create `ops/dashboard/pnl.py`:

```python
"""Per-position unrealized P&L math (pure; no I/O).

Money is Decimal end to end (never float). The short sleeve journals
positive-magnitude quantities, so its sign is inverted here: a short
position profits when the price falls.
"""
from __future__ import annotations

from decimal import Decimal


def position_pnl(
    entry: Decimal | None,
    quantity: Decimal,
    price: Decimal | None,
    *,
    is_short: bool,
) -> tuple[Decimal | None, Decimal | None]:
    """Return (pnl_dollar, pnl_pct) for one open position.

    - pnl_dollar is None only when price is unavailable.
    - pnl_pct is None when price is unavailable, or entry is None/0 (no
      basis / divide-by-zero guard); pnl_dollar can still be computed
      from entry in the entry==0 case.
    """
    if price is None:
        return None, None
    if entry is None:
        return None, None
    move = (entry - price) if is_short else (price - entry)
    pnl_dollar = move * quantity
    pnl_pct = (move / entry) if entry != 0 else None
    return pnl_dollar, pnl_pct
```

Note: when `entry == 0` and `price` present, `pnl_dollar = price * quantity` (or `-price*quantity` short), `pnl_pct = None` — matches the test.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ops/dashboard/test_pnl.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add ops/dashboard/pnl.py tests/ops/dashboard/test_pnl.py
git commit -m "feat(dashboard): pure per-position P&L math with short sign inversion"
```

---

### Task 3: Sleeve P&L builder — replay positions + quote + compute

**Files:**
- Modify: `ops/dashboard/snapshot.py` (extract a reusable position-replay helper)
- Modify: `ops/dashboard/pnl.py` (add `build_sleeve_pnl`)
- Test: `tests/ops/dashboard/test_pnl.py`

**Interfaces:**
- Consumes: `position_pnl` (Task 2); `ops.broker.paper.PaperBroker`, `ops.broker.short_paper.ShortPaperBroker`; `ops.journal.Journal`; `ops.dashboard.snapshot._refuse_quotes`.
- Produces:
  - In `snapshot.py`: `replay_positions(path: str, *, broker_cls=None) -> list[dict]` returning `[{"symbol", "quantity" (Decimal), "entry" (Decimal|None), "stop"}]`. `_one_sleeve` is refactored to use it.
  - In `pnl.py`: `build_sleeve_pnl(path: str, *, is_short: bool, quote_source: Callable[[str], Decimal], broker_cls=None) -> dict` returning `{"positions": [{"symbol", "price": str|None, "pnl_dollar": str|None, "pnl_pct": str|None, "error": str?}]}`. Money fields are `str` (jsonable), matching the snapshot's Decimal→str convention.

- [ ] **Step 1: Extract `replay_positions` in snapshot.py**

In `ops/dashboard/snapshot.py`, add a module-level helper (place it above `_one_sleeve`):

```python
def replay_positions(path: str, *, broker_cls=None) -> list[dict[str, Any]]:
    """Open positions for one ledger via journal replay (readonly,
    refuse-quotes guard). Shared by the snapshot's _one_sleeve and the
    /api/pnl builder so both see identical positions."""
    if broker_cls is None:
        from ops.broker.paper import PaperBroker as broker_cls
    with Journal(path, readonly=True) as j:
        replay = broker_cls.from_journal(
            journal=j, quote_source=_refuse_quotes, starting_cash=Decimal("0"))
        return [
            {"symbol": p.symbol, "quantity": p.quantity,
             "entry": p.avg_entry_price, "stop": p.stop_loss_price}
            for p in replay.get_positions()
        ]
```

Then in `_one_sleeve`, replace the inline `positions = [...]` list comprehension (the block building `positions` from `replay.get_positions()`) with a call. Keep the existing `replay`/`replay_cash` logic intact — `_one_sleeve` still needs `replay.get_cash()`, so only the `positions` list is swapped:

```python
        positions = [
            {"symbol": p["symbol"], "quantity": p["quantity"],
             "entry": p["entry"], "stop": p["stop"]}
            for p in replay_positions(path, broker_cls=broker_cls)
        ]
```

(This opens the journal a second time but keeps one clear helper; the readonly cost is negligible and correctness/DRY win. `jsonable()` at the section boundary still converts the Decimals to strings.)

- [ ] **Step 2: Verify existing snapshot tests still pass**

Run: `python -m pytest tests/ops/dashboard/test_snapshot_sleeves.py -v`
Expected: PASS (refactor is behavior-preserving).

- [ ] **Step 3: Write failing tests for `build_sleeve_pnl`**

Add to `tests/ops/dashboard/test_pnl.py` (add imports at top):

```python
from ops.broker.paper import PaperBroker
from ops.broker.short_paper import ShortPaperBroker
from ops.broker.base import QuoteUnavailable
from ops.dashboard.pnl import build_sleeve_pnl
from ops.journal import Journal


def _seed_long(path):
    with Journal(path) as j:
        j.record_cash_adjustment(kind="seed", amount=Decimal("10000"),
                                 note="t")
        j.record_order(symbol="BAH", side="BUY", quantity=Decimal("10"),
                       price=Decimal("100"), order_id="o1")
        j.record_fill(symbol="BAH", side="BUY", quantity=Decimal("10"),
                      price=Decimal("100"), order_id="o1")


def test_build_sleeve_pnl_long(tmp_path):
    path = str(tmp_path / "s.sqlite")
    _seed_long(path)
    quotes = lambda s: {"BAH": Decimal("110")}[s]
    out = build_sleeve_pnl(path, is_short=False, quote_source=quotes)
    row = out["positions"][0]
    assert row["symbol"] == "BAH"
    assert row["price"] == "110"
    assert row["pnl_dollar"] == "100"        # (110-100)*10
    assert row["pnl_pct"] == "0.1"
    assert "error" not in row


def test_build_sleeve_pnl_per_symbol_quote_failure(tmp_path):
    path = str(tmp_path / "s.sqlite")
    _seed_long(path)
    def quotes(s):
        raise QuoteUnavailable("boom")
    out = build_sleeve_pnl(path, is_short=False, quote_source=quotes)
    row = out["positions"][0]
    assert row["price"] is None
    assert row["pnl_dollar"] is None and row["pnl_pct"] is None
    assert "boom" in row["error"]
```

If `record_order`/`record_fill` signatures differ, mirror the exact calls used in `tests/ops/dashboard/test_snapshot_sleeves.py::_seed_momentum` (read it first — it seeds a BUY order + fill the replay accepts).

- [ ] **Step 4: Run tests to verify they fail**

Run: `python -m pytest tests/ops/dashboard/test_pnl.py -v`
Expected: FAIL — `build_sleeve_pnl` not defined.

- [ ] **Step 5: Implement `build_sleeve_pnl`**

Add to `ops/dashboard/pnl.py` (add imports):

```python
from collections.abc import Callable
from typing import Any

from ops.broker.base import QuoteUnavailable


def build_sleeve_pnl(
    path: str,
    *,
    is_short: bool,
    quote_source: Callable[[str], Decimal],
    broker_cls=None,
) -> dict[str, Any]:
    """Per-position P&L for one sleeve ledger. Replays positions
    (journal-only), then marks each with a live quote. A quote failure
    degrades that row only (null P&L + error); every other row resolves."""
    from ops.dashboard.snapshot import replay_positions

    rows: list[dict[str, Any]] = []
    for pos in replay_positions(path, broker_cls=broker_cls):
        symbol = pos["symbol"]
        row: dict[str, Any] = {"symbol": symbol}
        try:
            price = quote_source(symbol)
        except QuoteUnavailable as exc:
            row.update(price=None, pnl_dollar=None, pnl_pct=None,
                       error=str(exc))
            rows.append(row)
            continue
        d, p = position_pnl(pos["entry"], pos["quantity"], price,
                            is_short=is_short)
        row.update(
            price=str(price),
            pnl_dollar=None if d is None else str(d),
            pnl_pct=None if p is None else str(p),
        )
        rows.append(row)
    return {"positions": rows}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/ops/dashboard/test_pnl.py -v`
Expected: PASS (7 tests).

- [ ] **Step 7: Commit**

```bash
git add ops/dashboard/pnl.py ops/dashboard/snapshot.py tests/ops/dashboard/test_pnl.py
git commit -m "feat(dashboard): build_sleeve_pnl marks replayed positions with quotes"
```

---

### Task 4: `/api/pnl` route

**Files:**
- Modify: `ops/dashboard/server.py`
- Test: `tests/ops/dashboard/test_server.py`

**Interfaces:**
- Consumes: `build_sleeve_pnl` (Task 3); `ops.quotes.make_yfinance_quote_source`; `ops.broker.short_paper.ShortPaperBroker`.
- Produces: route `GET /api/pnl?sleeve=<name>` → `200` `{"sleeve", "positions":[...]}`; unknown/missing sleeve → `400`; total failure → `{"error": ...}`.

- [ ] **Step 1: Write failing tests**

Add to `tests/ops/dashboard/test_server.py`. The route hits yfinance, so tests inject a fake quote source by monkeypatching `ops.dashboard.server.make_yfinance_quote_source`. Add a parametrized fixture variant or a standalone test that seeds a position first.

```python
def test_pnl_unknown_sleeve_400(base_url):
    import urllib.error
    try:
        _get(base_url + "/api/pnl?sleeve=nope")
        assert False, "expected HTTP 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_pnl_route_returns_rows(cfg, monkeypatch):
    import threading
    from decimal import Decimal
    from ops.dashboard.server import make_server
    from ops.journal import Journal
    # seed one momentum position
    with Journal(cfg.journal_path) as j:
        j.record_event("service_started", {"pid": 1})
        j.record_cash_adjustment(kind="seed", amount=Decimal("10000"), note="t")
        j.record_order(symbol="BAH", side="BUY", quantity=Decimal("10"),
                       price=Decimal("100"), order_id="o1")
        j.record_fill(symbol="BAH", side="BUY", quantity=Decimal("10"),
                      price=Decimal("100"), order_id="o1")
    monkeypatch.setattr(
        "ops.dashboard.server.make_yfinance_quote_source",
        lambda **_: (lambda s: Decimal("110")))
    server = make_server(cfg, port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
    host, port = server.server_address
    try:
        status, body = _get(f"http://127.0.0.1:{port}/api/pnl?sleeve=momentum")
        import json
        data = json.loads(body)
        assert status == 200
        assert data["sleeve"] == "momentum"
        assert data["positions"][0]["symbol"] == "BAH"
        assert data["positions"][0]["pnl_dollar"] == "100"
    finally:
        server.shutdown(); server.server_close()
```

Match `record_order`/`record_fill`/`record_cash_adjustment` signatures to `_seed_momentum` in `test_snapshot_sleeves.py` if these differ.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/dashboard/test_server.py -k pnl -v`
Expected: FAIL — `/api/pnl` returns 404.

- [ ] **Step 3: Implement the route**

In `ops/dashboard/server.py`:

Add imports near the top:
```python
from ops.dashboard.pnl import build_sleeve_pnl
from ops.quotes import make_yfinance_quote_source
```

In `do_GET`, add a branch before the `startswith("/api/")` catch-all (after the `/api/logs` branch):
```python
            elif parsed.path == "/api/pnl":
                self._api_pnl(query)
```

Add the handler method:
```python
    # Sleeve name -> (journal path, short?). The one dashboard code path
    # allowed to fetch quotes; build_snapshot stays journal-only.
    def _api_pnl(self, query) -> None:
        from ops.broker.short_paper import ShortPaperBroker
        sleeves = {
            "momentum": (self.config.journal_path, False, None),
            "research": (self.config.research_journal_path, False, None),
            "baseline": (self.config.baseline_journal_path, False, None),
            "short": (self.config.short_journal_path, True, ShortPaperBroker),
            "insider": (self.config.insider_journal_path, False, None),
        }
        name = query.get("sleeve", [""])[0]
        if name not in sleeves:
            self._send_json(
                {"error": f"sleeve must be one of {sorted(sleeves)}"},
                status=400)
            return
        path, is_short, broker_cls = sleeves[name]
        quote_source = make_yfinance_quote_source()
        result = build_sleeve_pnl(
            path, is_short=is_short, quote_source=quote_source,
            broker_cls=broker_cls)
        self._send_json({"sleeve": name, **result})
```

The existing `do_GET` try/except already turns a total failure (e.g. missing journal) into a `500 {"error": ...}`; the frontend treats any non-200 as "no P&L available".

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ops/dashboard/test_server.py -k pnl -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Full server test file + commit**

Run: `python -m pytest tests/ops/dashboard/test_server.py -v`
Expected: PASS (all).

```bash
git add ops/dashboard/server.py tests/ops/dashboard/test_server.py
git commit -m "feat(dashboard): /api/pnl route (isolated quote fetch)"
```

---

### Task 5: Frontend data layer — types, fetchPnl, usePnl, usePnlMode

**Files:**
- Modify: `dashboard-ui/src/data/types.ts`
- Modify: `dashboard-ui/src/data/api.ts`
- Create: `dashboard-ui/src/data/pnl.ts`
- Test: `dashboard-ui/src/data/pnl.test.ts`

**Interfaces:**
- Produces:
  - Types `PnlRow { symbol: string; price: string | null; pnl_dollar: string | null; pnl_pct: string | null; error?: string }` and `PnlResponse { sleeve: string; positions: PnlRow[] }`.
  - `fetchPnl(sleeve: string): Promise<PnlResponse>`.
  - Hook `usePnl(sleeve: string, active: boolean, intervalMs?: number): { rows: Record<string, PnlRow>; loading: boolean }` — fetches when `active`, re-fetches on interval, maps `symbol -> PnlRow`.
  - Hook `usePnlMode(): ["dollar" | "pct", () => void]` — localStorage-backed (`key "pnlMode"`, default `"pct"`), shared across components.

- [ ] **Step 1: Add types**

In `dashboard-ui/src/data/types.ts`, append:
```ts
export interface PnlRow {
  symbol: string; price: string | null;
  pnl_dollar: string | null; pnl_pct: string | null; error?: string;
}
export interface PnlResponse { sleeve: string; positions: PnlRow[] }
```

- [ ] **Step 2: Add fetchPnl**

In `dashboard-ui/src/data/api.ts`, add to the import and a new export:
```ts
import type { EventItem, PnlResponse, Snapshot } from "./types";
// ...
export const fetchPnl = (sleeve: string) =>
  getJson<PnlResponse>(`/api/pnl?sleeve=${encodeURIComponent(sleeve)}`);
```

- [ ] **Step 3: Write failing tests for the hooks**

Create `dashboard-ui/src/data/pnl.test.ts`:
```ts
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { usePnl, usePnlMode } from "./pnl";
import * as api from "./api";

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe("usePnl", () => {
  it("fetches when active and maps rows by symbol", async () => {
    vi.spyOn(api, "fetchPnl").mockResolvedValue({
      sleeve: "momentum",
      positions: [{ symbol: "BAH", price: "110", pnl_dollar: "100", pnl_pct: "0.1" }],
    });
    const { result } = renderHook(() => usePnl("momentum", true, 999999));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.rows.BAH.pnl_dollar).toBe("100");
  });

  it("does not fetch when inactive", () => {
    const spy = vi.spyOn(api, "fetchPnl").mockResolvedValue({ sleeve: "x", positions: [] });
    renderHook(() => usePnl("momentum", false, 999999));
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("usePnlMode", () => {
  it("defaults to pct and toggles, persisting to localStorage", () => {
    const { result } = renderHook(() => usePnlMode());
    expect(result.current[0]).toBe("pct");
    act(() => result.current[1]());
    expect(result.current[0]).toBe("dollar");
    expect(localStorage.getItem("pnlMode")).toBe("dollar");
  });
});
```

If `@testing-library/react` is absent, check `dashboard-ui/package.json`; add it as a devDependency (`npm i -D @testing-library/react`) — this is the only new dependency and is test-only.

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd dashboard-ui && npx vitest run src/data/pnl.test.ts`
Expected: FAIL — `./pnl` module missing.

- [ ] **Step 5: Implement**

Create `dashboard-ui/src/data/pnl.ts`:
```ts
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchPnl } from "./api";
import type { PnlRow } from "./types";

export function usePnl(sleeve: string, active: boolean, intervalMs = 5000) {
  const [rows, setRows] = useState<Record<string, PnlRow>>({});
  const [loading, setLoading] = useState(false);
  const inFlight = useRef(false);

  useEffect(() => {
    if (!active) return;
    let alive = true;
    const tick = async () => {
      if (inFlight.current) return;
      inFlight.current = true;
      setLoading(true);
      try {
        const res = await fetchPnl(sleeve);
        if (!alive) return;
        const map: Record<string, PnlRow> = {};
        for (const r of res.positions) map[r.symbol] = r;
        setRows(map);
      } catch {
        /* leave last-good rows; the column shows — until next success */
      } finally {
        if (alive) setLoading(false);
        inFlight.current = false;
      }
    };
    void tick();
    const id = setInterval(() => void tick(), intervalMs);
    return () => { alive = false; clearInterval(id); };
  }, [sleeve, active, intervalMs]);

  return { rows, loading };
}

export type PnlMode = "dollar" | "pct";

export function usePnlMode(): [PnlMode, () => void] {
  const [mode, setMode] = useState<PnlMode>(
    () => (localStorage.getItem("pnlMode") as PnlMode) || "pct");
  const toggle = useCallback(() => {
    setMode((m) => {
      const next = m === "pct" ? "dollar" : "pct";
      localStorage.setItem("pnlMode", next);
      return next;
    });
  }, []);
  return [mode, toggle];
}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd dashboard-ui && npx vitest run src/data/pnl.test.ts`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add dashboard-ui/src/data/types.ts dashboard-ui/src/data/api.ts dashboard-ui/src/data/pnl.ts dashboard-ui/src/data/pnl.test.ts dashboard-ui/package.json dashboard-ui/package-lock.json
git commit -m "feat(dashboard): usePnl + usePnlMode hooks and fetchPnl"
```

---

### Task 6: Shared P&L cell + drawer integration + widen drawer

**Files:**
- Create: `dashboard-ui/src/components/PnlCell.tsx`
- Modify: `dashboard-ui/src/components/SleeveDrillDrawer.tsx`
- Modify: `dashboard-ui/src/app.css`

**Interfaces:**
- Consumes: `PnlRow` (types), `usePnl`/`usePnlMode` (Task 5), `fmt2` (Task 1), `fmtMoney`/`fmtPct` (format).
- Produces: `PnlCell({ row, mode }: { row: PnlRow | undefined; mode: PnlMode })` — a `<td className="num ...">` rendering `$`-signed dollars or signed percent, green/red/flat, `…` while unknown; `PnlHeader({ mode, onToggle })` — a clickable `<th>` showing `P&L $`/`P&L %`.

- [ ] **Step 1: Create the shared cell + header**

Create `dashboard-ui/src/components/PnlCell.tsx`:
```tsx
import { fmtMoney, fmtPct } from "../lib/format";
import type { PnlMode } from "../data/pnl";
import type { PnlRow } from "../data/types";

export function PnlHeader({ mode, onToggle }: { mode: PnlMode; onToggle: () => void }) {
  return (
    <th className="num pnl-h">
      <button type="button" className="pnl-toggle" onClick={onToggle}
        title="toggle dollars / percent">
        P&L {mode === "dollar" ? "$" : "%"}
      </button>
    </th>
  );
}

export function PnlCell({ row, mode }: { row: PnlRow | undefined; mode: PnlMode }) {
  if (!row || (row.pnl_dollar == null && row.pnl_pct == null)) {
    return <td className="num" style={{ color: "var(--tx3)" }}>…</td>;
  }
  if (mode === "dollar") {
    const v = row.pnl_dollar;
    const neg = v != null && v.startsWith("-");
    const cls = v == null ? "" : neg ? "neg" : /[1-9]/.test(v) ? "pos" : "flat";
    // fmtMoney handles the sign; strip its leading "-" source since it signs itself
    return <td className={`num ${cls}`}>{fmtMoney(v, 2)}</td>;
  }
  const p = fmtPct(row.pnl_pct);
  return <td className={`num ${p.cls}`}>{p.text}</td>;
}
```

Note: `fmtMoney("-100", 2)` → `"−$100.00"` (it signs negatives itself), `fmtMoney("100", 2)` → `"$100.00"`. Class is chosen from the raw string sign.

- [ ] **Step 2: Wire into the drawer**

In `dashboard-ui/src/components/SleeveDrillDrawer.tsx`:
- Add imports: `import { usePnl, usePnlMode } from "../data/pnl";` `import { PnlCell, PnlHeader } from "./PnlCell";` and add `fmt2` to the format import.
- Inside the component body (before `body()`), add hooks. The drawer only renders when a sleeve is open, so `active` is `!!sleeve && !isErr(sleeve)`:
```tsx
  const [mode, toggleMode] = usePnlMode();
  const active = !!sleeve && !isErr(sleeve);
  const { rows: pnl } = usePnl(name, active);
```
- Change the positions table header to 5 columns:
```tsx
            <thead><tr>
              <th>symbol</th><th className="num">qty</th>
              <th className="num">entry</th><th className="num">stop</th>
              <PnlHeader mode={mode} onToggle={toggleMode} />
            </tr></thead>
```
- Change the row body: format entry/stop with `fmt2`, add the P&L cell:
```tsx
                <tr key={p.symbol}>
                  <td className="sym">{p.symbol}</td>
                  <td className={`num ${shortSleeve || p.quantity.startsWith("-") ? "neg" : ""}`}>{fmtQty(p.quantity)}</td>
                  <td className="num">{p.entry ? fmt2(p.entry) : "—"}</td>
                  <td className="num" style={{ color: "var(--tx3)" }}>{p.stop ? fmt2(p.stop) : "—"}</td>
                  <PnlCell row={pnl[p.symbol]} mode={mode} />
                </tr>
```
- Also format the fills price with `fmt2`: change `<td className="num" style={{ color: "var(--tx)" }}>{f.price}</td>` to `{fmt2(f.price)}`.

- [ ] **Step 3: Widen the drawer + toggle styles in app.css**

In `dashboard-ui/src/app.css`, change `.drawer` width (line 214) from `width: 480px;` to `width: min(760px, 92vw);` and update the `Sparkline` width in the drawer to match — in `SleeveDrillDrawer.tsx` change `<Sparkline ... w={520} ...>` to `w={700}`.

Append toggle styles:
```css
.pnl-h { padding: 0; }
.pnl-toggle {
  font: inherit; color: var(--tx3); background: none; border: none;
  cursor: pointer; padding: 6px 8px; text-transform: none;
}
.pnl-toggle:hover { color: var(--tx); }
```

- [ ] **Step 4: Typecheck + build**

Run: `cd dashboard-ui && npx tsc --noEmit && npx vitest run`
Expected: PASS (typecheck clean, all tests green).

- [ ] **Step 5: Commit**

```bash
git add dashboard-ui/src/components/PnlCell.tsx dashboard-ui/src/components/SleeveDrillDrawer.tsx dashboard-ui/src/app.css
git commit -m "feat(dashboard): P&L column + $/% toggle in sleeve drawer, wider drawer"
```

---

### Task 7: All-positions panel P&L + remaining decimal fixes

**Files:**
- Modify: `dashboard-ui/src/components/PositionsPanel.tsx`
- Modify: `dashboard-ui/src/components/FillsPanel.tsx`
- Modify: `dashboard-ui/src/components/SleeveCards.tsx`

**Interfaces:**
- Consumes: `usePnl`/`usePnlMode` (Task 5), `PnlCell`/`PnlHeader` (Task 6), `fmt2` (Task 1).

- [ ] **Step 1: PositionsPanel — P&L per group + 2dp entry/stop**

In `dashboard-ui/src/components/PositionsPanel.tsx`:
- Imports: add `import { usePnl, usePnlMode } from "../data/pnl";` `import { PnlCell, PnlHeader } from "./PnlCell";` and `fmt2` to the format import.
- The `Group` component fetches P&L only when open. Add near the top of `Group`:
```tsx
  const { rows: pnl } = usePnl(name, open);
```
- `Group` needs the shared mode. Thread `mode`/`onToggleMode` as props from `PositionsPanel` (single source so drawer + panel agree). Add to `Group`'s props: `mode: PnlMode; onToggleMode: () => void;` (import `PnlMode` type).
- Header → 5 columns:
```tsx
              <thead><tr>
                <th>symbol</th><th className="num">qty</th>
                <th className="num">entry</th><th className="num">stop</th>
                <PnlHeader mode={mode} onToggle={onToggleMode} />
              </tr></thead>
```
- Row → 2dp entry/stop + P&L cell:
```tsx
                  <tr key={r.symbol}>
                    <td className="sym">{r.symbol}</td>
                    <td className={`num ${short || r.quantity.startsWith("-") ? "neg" : ""}`}>
                      {fmtQty(r.quantity)}
                    </td>
                    <td className="num">{r.entry ? fmt2(r.entry) : "—"}</td>
                    <td className="num" style={{ color: "var(--tx3)" }}>{r.stop ? fmt2(r.stop) : "—"}</td>
                    <PnlCell row={pnl[r.symbol]} mode={mode} />
                  </tr>
```
- In `PositionsPanel`, call `const [mode, toggleMode] = usePnlMode();` and pass `mode={mode} onToggleMode={toggleMode}` to each `<Group>`.

- [ ] **Step 2: FillsPanel — 2dp price**

In `dashboard-ui/src/components/FillsPanel.tsx`, add `fmt2` to the import and change `<td className="num" style={{ color: "var(--tx)" }}>{f.price}</td>` to render `{fmt2(f.price)}`.

- [ ] **Step 3: SleeveCards — cash 2dp**

In `dashboard-ui/src/components/SleeveCards.tsx`, change `fmtMoney(sleeve.cash, 0)` (the `<span>cash ...` line) to `fmtMoney(sleeve.cash, 2)`.

- [ ] **Step 4: Grep for any remaining raw numbers**

Run: `cd dashboard-ui/src && grep -rn "\.price}\|\.entry ??\|\.stop ??\|, 0)" components/ | grep -v ".test."`
Expected: no raw `{x.price}`/`{x.entry ?? "—"}` without `fmt2`, and no stray `fmtMoney(..., 0)` remaining. Wrap any stragglers with `fmt2`/2dp.

- [ ] **Step 5: Typecheck + build + tests**

Run: `cd dashboard-ui && npx tsc --noEmit && npx vitest run`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add dashboard-ui/src/components/PositionsPanel.tsx dashboard-ui/src/components/FillsPanel.tsx dashboard-ui/src/components/SleeveCards.tsx
git commit -m "feat(dashboard): P&L column in positions panel; 2dp price/cash audit"
```

---

### Task 8: Build the frontend + end-to-end verification

**Files:**
- Modify: `ops/dashboard/static/*` (Vite build output — check `.gitignore` / how the built bundle is deployed; mirror the existing build step)

- [ ] **Step 1: Full Python + JS test suites**

Run: `python -m pytest tests/ops/dashboard/ -q && cd dashboard-ui && npx vitest run && npx tsc --noEmit`
Expected: all PASS.

- [ ] **Step 2: Build the frontend bundle**

Run: `cd dashboard-ui && npm run build`
Expected: build succeeds; output lands where `ops/dashboard/static` is served from (confirm the build target in `vite.config.ts` / `README.md`).

- [ ] **Step 3: End-to-end verification via the `verify` skill**

Invoke the `verify` skill (drives server + built React). Verify:
- Clicking a sleeve opens a **wider** drawer.
- Positions table shows **symbol · qty · entry · stop · P&L**; qty/entry/stop show ≤2 decimals.
- The **P&L header toggles** between `$` and `%`; gains render green, losses red.
- The all-positions panel shows the same P&L column when a group is expanded.
- A momentary quote failure shows `…`/`—`, not a blank panel (the main snapshot keeps rendering).

- [ ] **Step 4: Final commit (built assets, if tracked)**

```bash
git add ops/dashboard/static
git commit -m "build(dashboard): rebuild bundle with per-stock P&L view"
```

(Skip if `ops/dashboard/static` build output is gitignored / built at deploy time.)

---

## Notes for the executor

- **Confirm journal seed signatures** (`record_order`, `record_fill`, `record_cash_adjustment`) against `tests/ops/dashboard/test_snapshot_sleeves.py::_seed_momentum` before writing Task 3/4 tests — that file is the source of truth for a replay-accepted BUY.
- **`fmtMoney` sign behavior:** it signs negatives itself (`"−$100.00"`) and suppresses negative-zero. `PnlCell` derives the color class from the raw string, not the formatted output.
- The `short` sleeve is the only `is_short=True` sleeve; its P&L sign is inverted in `position_pnl`.
