# Dashboard per-stock P&L + decimal audit — design

**Date:** 2026-07-21
**Status:** Approved (design), pending implementation plan

## Goal

Give each stock in every sleeve/portfolio a P&L view on the ops dashboard,
toggleable between **percent** and **dollar** gain/loss, colored green for
gains and red for losses. Simultaneously enforce **2-decimal-place formatting
universally** across the dashboard, fixing the over-long `qty`, `entry`, `stop`,
and `price` values currently rendered as raw journal decimal strings.

## Context & the load-bearing constraint

`ops/dashboard/snapshot.py::build_snapshot` is **journal-only by hard
guarantee** — no broker, no quotes, no network. Per-position P&L requires a
*current price per symbol*, and the journal stores none:

- Positions (from `PaperBroker.from_journal` replay) carry: `symbol`,
  `quantity`, `avg_entry_price` (entry), `stop`.
- Equity snapshots store only **total equity + cash** per sleeve — no
  per-position marks.

**Decision (approved): Approach B — the dashboard fetches quotes**, but in a
*new, isolated endpoint only*. `build_snapshot` is NOT modified and keeps its
journal-only guarantee intact. Network touches the dashboard in exactly one
place, documented as such.

## Architecture

### 1. New endpoint: `GET /api/pnl?sleeve=<name>`

Added to `ops/dashboard/server.py`. Read-only (no mutation), so the server's
read-only guarantee still holds; only the *journal-only* guarantee is scoped —
it applies to `build_snapshot`, not this route.

Behavior:

1. Validate `sleeve` against the known sleeve map (same map as `_api_events`);
   unknown → `400`.
2. Replay that sleeve's positions exactly as `_one_sleeve` does (readonly
   journal, `ShortPaperBroker` for `short`, refuse-quotes guard during replay).
3. For each open position, fetch a quote via the shared
   `ops.quotes.make_yfinance_quote_source` (yfinance, 60s TTL cache) — the same
   provider the trading service uses.
4. Compute P&L **server-side in `Decimal`** (money never touches float — global
   constraint):
   - **Long sleeves:** `pnl_dollar = (price − entry) × qty`,
     `pnl_pct = (price − entry) / entry`
   - **Short sleeve** (`short`, positive-magnitude journal): inverted —
     `pnl_dollar = (entry − price) × qty`, `pnl_pct = (entry − price) / entry`
5. Return JSON:
   ```json
   { "sleeve": "research",
     "positions": [
       { "symbol": "BAH", "price": "142.30", "pnl_dollar": "128.40", "pnl_pct": "0.0213" },
       { "symbol": "CRC", "price": null, "pnl_dollar": null, "pnl_pct": null, "error": "QuoteUnavailable: ..." }
     ] }
   ```

**Degradation is per-symbol.** A `QuoteUnavailable` for one ticker → that row
gets `null` price/P&L plus an `error` string; every other row still resolves.
A total failure (e.g. journal missing) → `{ "error": ... }` for the whole
route, and the client falls back to entry/stop with no P&L. The `/api/snapshot`
payload is unaffected in all cases.

**Guards:** `entry` of `0`/`null` → `pnl_pct = null` (avoid divide-by-zero),
`pnl_dollar` still computable if price present. Positions with `null` entry →
P&L null.

**Isolation of the pure math:** the compute step is a pure function
`position_pnl(entry, qty, price, *, is_short) -> (pnl_dollar, pnl_pct)` living
next to the endpoint (e.g. `ops/dashboard/pnl.py`), unit-tested with a fake
quote source — no network in tests.

### 2. Frontend: shared P&L fetch + display

- **Data layer:** a `fetchPnl(sleeve)` in `src/data/api.ts` and a small hook
  `usePnl(sleeve, active)` (`src/data/pnl.ts`) that fetches on mount/when
  `active`, and re-fetches on the existing dashboard poll interval. Returns a
  `symbol → { price, pnl_dollar, pnl_pct, error }` map plus a loading flag.
- **Types:** add `PnlRow` / `PnlResponse` to `src/data/types.ts`.
- **Toggle:** a `$ ⇄ %` mode, **default `%`**, shared across the drawer and the
  positions panel (persisted via `localStorage` so both agree). Clicking the
  **P&L column header** flips the mode.
- **Rendering:** shared cell helper — green (`pos`) for gain, red (`neg`) for
  loss, `flat` for zero; `…` placeholder while loading; `—` on per-symbol
  error. Percent via `fmtPct`; dollars via a signed `fmtMoney` at 2 dp.

#### 2a. `SleeveDrillDrawer.tsx`
Positions table becomes **symbol · qty · entry · stop · P&L** (stop retained).
Drawer calls `usePnl(name, /* active */ true)` on open.

**Widen the drawer.** The current drawer is cramped (~520px). Widen it
substantially (target ~40–50% of viewport, e.g. `min(760px, 92vw)`, with a
sensible max) so the 5-column table + sparkline breathe. Adjust the `.drawer`
width in `app.css` and bump the `Sparkline` `w` to match the new content width.
Keep it responsive — never exceed viewport width on narrow screens.

#### 2b. `PositionsPanel.tsx`
Each sleeve `Group` becomes **symbol · qty · entry · stop · P&L**. A group calls
`usePnl(name, open)` so quotes are fetched only for expanded groups.

### 3. Universal 2-decimal audit

| Location | Field | Current | Fix |
|---|---|---|---|
| `format.ts` | `fmtQty` | strips trailing zeros only → long qty | round to 2 dp |
| `SleeveDrillDrawer` | `entry`, `stop`, fill `price` | raw journal string | 2 dp format |
| `PositionsPanel` | `entry`, `stop` | raw journal string | 2 dp format |
| `FillsPanel` | fill `price` | raw journal string | 2 dp format |
| `SleeveCards` | `cash` | `fmtMoney(cash, 0)` | `fmtMoney(cash, 2)` |

`entry`/`stop`/`price` are decimal strings → format with `fmtMoney(v, 2)` minus
the `$` where a bare number reads better (a small `fmt2(v)` helper in
`format.ts` that reuses `fmtMoney`'s string-safe rounding without the currency
symbol). A final grep of the built UI catches any raw number missed.

## Testing

- **`ops/dashboard/pnl.py`** (pytest): long P&L, short P&L sign inversion,
  zero/`null` entry guard, `null` price passthrough. Pure function, fake quotes.
- **Endpoint** (pytest): valid sleeve returns per-symbol rows; per-symbol quote
  failure degrades that row only; unknown sleeve → 400; a total failure →
  `{error}`. Quote source injected/monkeypatched — no network.
- **`format.test.ts`**: `fmtQty` rounds to 2 dp; `fmt2` matches `fmtMoney`
  rounding behavior.
- **Frontend**: `usePnl` hook — loading → data, per-symbol error handling.
- **End-to-end**: `verify` skill against the running dashboard (server + built
  React), open a sleeve, confirm P&L renders and the $/% toggle works.

## Non-goals

- Realized P&L on closed positions (this is unrealized P&L on open positions).
- Streaming/websocket quotes — poll-interval refresh is sufficient.
- Changing `build_snapshot`'s journal-only contract.
- The separate "recovery attempt reuse" bug (tracked as a follow-up task).
