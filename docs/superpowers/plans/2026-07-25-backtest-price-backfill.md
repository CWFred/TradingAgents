# Backtest Price Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate the backtest `price_bars` cache with historical daily bars for each case's symbol (and the SPY benchmark), so the replay can actually price and score cases instead of reporting every one "unpriceable." Delivered as a dedicated `ops backtest prices` step (generate → **prices** → run).

**Architecture:** `ops/backtest/prices.py::PriceCache.update(symbol, *, start, end, fetcher)` already fetches-and-persists bars but has **no caller** wiring a real price fetcher. This plan adds (1) a yfinance→`PriceBarLike` daily-bar fetcher, (2) a backfill driver that iterates cases and computes each symbol's window, and (3) a `backtest prices` CLI command. The replay stays "cached-prices, zero-network"; all fetching lives in this new step.

**Tech Stack:** Python 3, pytest; yfinance (reuse `tradingagents/dataflows/y_finance.py` patterns); SQLite (`price_bars`); `ops/scheduler/market_calendar.py`.

## Global Constraints

- **Money/prices are `Decimal`** — never float. yfinance floats convert via `Decimal(str(x))`.
- **`PriceBarLike` contract** the fetcher MUST satisfy (each bar): `symbol:str, session:date, open/high/low/close:Decimal, adjusted_open/adjusted_high/adjusted_low/adjusted_close:Decimal, volume:Decimal, dividend:Decimal (>=0), split_ratio:Decimal (>0), provider:str`. `PriceCache.update` rejects any bar whose symbol mismatches or whose `session` falls outside the requested `[start,end]`.
- **Idempotent:** `upsert_bars` uses `ON CONFLICT(symbol,session) DO UPDATE`, so re-running the step is safe and refreshes forward data as horizons mature.
- **Never die on one name:** a symbol whose fetch fails is recorded and skipped; the sweep continues (mirrors the screener's per-name isolation).
- **The replay stays zero-network** — do NOT add fetching to `run_cached_backtest`.
- **Benchmark** is `config.backtest_benchmark` (default `SPY`); max horizon is `max(config.backtest_horizons)` (default 126 sessions).
- No new dependencies (yfinance is already used).

---

### Task 1: yfinance daily-bar fetcher → `PriceBarLike`

**Files:**
- Create: `ops/backtest/price_fetch.py`
- Test: `tests/ops/backtest/test_price_fetch.py`

**Interfaces:**
- Produces: frozen dataclass `YfBar` implementing `PriceBarLike`; and `yfinance_bar_fetcher(symbol, start, end, *, history_fn=None) -> tuple[YfBar, ...]`. `history_fn(symbol, start, end) -> pandas.DataFrame` is an injectable seam (default: real yfinance) so tests need no network. Signature is `Callable[[str, date, date], Iterable[PriceBarLike]]`-compatible for `PriceCache.update` (bind `history_fn` via `functools.partial` or a default).

Mapping from a yfinance daily frame (columns `Open, High, Low, Close, Adj Close, Volume, Dividends, Stock Splits`, `DatetimeIndex`):
- `open/high/low/close` ← raw OHLC (`Decimal(str(v))`).
- adjusted OHLC ← raw × `(Adj Close / Close)` (the split/div adjustment ratio; Adj Close is provided, Adj O/H/L are not — derive them with the same ratio). If `Close == 0`, skip the row.
- `volume` ← `Decimal(str(int(Volume)))`.
- `dividend` ← `Dividends` (0 if absent/NaN).
- `split_ratio` ← `Stock Splits` if `> 0` else `Decimal(1)` (yfinance emits 0 on non-split days; the cache requires `> 0`).
- `provider` ← `"yfinance"`.
- `session` ← the index date (`.date()`).

- [ ] **Step 1: Write failing test** — build a small fake DataFrame (2 rows, one with a dividend, one with a 2:1 split) via `history_fn`, assert the mapping:

```python
import pandas as pd
from datetime import date
from decimal import Decimal
from ops.backtest.price_fetch import yfinance_bar_fetcher

def _frame():
    idx = pd.to_datetime(["2025-06-02", "2025-06-03"])
    return pd.DataFrame({
        "Open":[10.0,20.0],"High":[11.0,21.0],"Low":[9.0,19.0],"Close":[10.0,20.0],
        "Adj Close":[5.0,20.0],"Volume":[100,200],
        "Dividends":[0.0,0.5],"Stock Splits":[0.0,2.0],
    }, index=idx)

def test_maps_yfinance_frame_to_pricebars():
    bars = yfinance_bar_fetcher("ACMR", date(2025,6,2), date(2025,6,3),
                                history_fn=lambda s,a,b: _frame())
    assert [b.session for b in bars] == [date(2025,6,2), date(2025,6,3)]
    b0 = bars[0]
    assert b0.symbol == "ACMR" and b0.provider == "yfinance"
    assert b0.close == Decimal("10") and b0.adjusted_close == Decimal("5")
    # adj ratio 5/10 applied to OHLC:
    assert b0.adjusted_open == Decimal("5") and b0.adjusted_high == Decimal("5.5")
    assert b0.split_ratio == Decimal("1")               # 0 -> 1
    assert bars[1].dividend == Decimal("0.5")
    assert bars[1].split_ratio == Decimal("2")
```

- [ ] **Step 2: Run, verify fail** — `python -m pytest tests/ops/backtest/test_price_fetch.py -v` → module missing.

- [ ] **Step 3: Implement** `ops/backtest/price_fetch.py` — the `YfBar` dataclass and `yfinance_bar_fetcher` with the mapping above. Default `history_fn` calls yfinance (reuse `tradingagents/dataflows/y_finance.py::yf_retry` + `yf.Ticker(symbol).history(start=, end=, auto_adjust=False, actions=True)`); the `end` passed to yfinance is exclusive, so add one day. Guard NaN (`pandas.isna`) → 0 for dividends/splits; skip rows with NaN/0 close.

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit** — `feat(backtest): yfinance daily-bar fetcher mapping to PriceBarLike`.

---

### Task 2: Backfill driver over cases + benchmark

**Files:**
- Create: `ops/backtest/price_backfill.py`
- Test: `tests/ops/backtest/test_price_backfill.py`

**Interfaces:**
- Consumes: `PriceCache` (`ops/backtest/prices.py`), `BacktestStore.list_cases`, `yfinance_bar_fetcher` (Task 1), `MarketCalendar` (`sessions_between`), `config.backtest_benchmark`, `config.backtest_horizons`.
- Produces: `@dataclass BackfillSummary(symbols:int, bars:int, failures:tuple[tuple[str,str],...])`; `backfill_prices(config, store, *, sleeve, start, end, fetcher=None, calendar=None, today=None) -> BackfillSummary`.

Behavior:
1. `cases = store.list_cases(sleeve=sleeve)` filtered to `start <= case.asof <= end`.
2. For each **distinct** symbol, window = `[min_asof_for_symbol - 10 days, min(today, max_asof_for_symbol + horizon_calendar_days)]` where `horizon_calendar_days` covers `max(config.backtest_horizons)` sessions (use `MarketCalendar` to find the Nth session after the as-of, or a safe `ceil(max_horizon * 1.6) + 5` calendar-day pad). `today = today or date.today()`.
3. Also backfill `config.backtest_benchmark` (SPY) once over the **global** `[min asof - 10d, min(today, max asof + horizon days)]` union.
4. Each fetch: `PriceCache(config.backtest_store_path).update(symbol, start=w0, end=w1, fetcher=fetcher)`. Wrap per symbol in try/except → append `(symbol, error)` to failures, continue.
5. Return counts.

- [ ] **Step 1: Write failing test** — seed a store with 2 cases (2 symbols, June-2025 asofs) via the existing `test_prepare.py`/`test_store.py` fixtures; inject a fake `fetcher` returning a known bar set and a fake `today`; assert `summary.symbols == 3` (2 + SPY), bars persisted (`PriceCache(...).bars(symbol, ...)` non-empty), and that a fetcher raising for one symbol yields a `failures` entry while others still persist. NO network.

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement** `ops/backtest/price_backfill.py` per the behavior above.

- [ ] **Step 4: Run, verify pass** — include the per-symbol-failure-isolation case.

- [ ] **Step 5: Commit** — `feat(backtest): price backfill driver (cases + benchmark, per-symbol isolation)`.

---

### Task 3: `backtest prices` CLI command

**Files:**
- Modify: `ops/cli.py`
- Test: `tests/ops/backtest/test_cli.py`

**Interfaces:**
- Produces: `@backtest.command("prices")` with `--sleeve` (default `research`, `click.Choice(["research"])`), `--start` (required, YYYY-MM-DD), `--end` (default `today`). Resolves the window via the existing `_backtest_window`, calls `backfill_prices(config, store, sleeve=, start=, end=)`, echoes the summary (symbols, bars, failures). Opens `BacktestStore` like the other backtest CLI commands.

- [ ] **Step 1: Write failing test** — patch `ops.cli.backfill_prices` (or the service import) and assert `backtest prices --start 2025-06-02 --end 2025-07-15` calls it with the resolved dates + sleeve, and prints the returned summary. Mirror the existing `backtest generate`/`run` CLI test patterns.

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement** the command (mirror `backtest_run`/`backtest_generate` structure: `_backtest_window`, `load_config`, `BacktestStore` context, `_backtest_error` wrapping).

- [ ] **Step 4: Run, verify pass** — full `tests/ops/backtest/test_cli.py`.

- [ ] **Step 5: Commit** — `feat(backtest): backtest prices CLI step`.

---

### Task 4: Real end-to-end validation (generate → prices → run)

**Files:** none (operational).

- [ ] **Step 1: Full suites green** — `python -m pytest tests/ops/backtest/ tests/ops/research/test_run.py tests/ops/scheduler/test_market_calendar.py -q`.

- [ ] **Step 2: Real backfill + replay into a scratch store.** Using an isolated `XDG_STATE_HOME` (tmp) and `SEC_EDGAR_USER_AGENT` (from the ops plist), reproduce a small matured corpus and price it:
```bash
XDG_STATE_HOME=$TMP/state SEC_EDGAR_USER_AGENT="<from plist>" \
  python -m ops.cli backtest generate --source reconstruction \
    --start 2025-06-02 --end 2025-07-15 --cases 30
XDG_STATE_HOME=$TMP/state \
  python -m ops.cli backtest prices --start 2025-06-02 --end 2025-07-15
XDG_STATE_HOME=$TMP/state \
  python -m ops.cli backtest run --start 2025-06-02 --end 2025-07-15
```
Expected: after `prices`, `price_bars` is non-empty; the `run` report shows cases now **priceable** (price_state `ready`, not `unpriceable`), with 5/21-day horizon outcomes populated for the oldest cases (63/126 may still be `pending` — acceptable, they mature later). Capture the verdict + one priced case row as evidence. If generation is too slow/heavy, bound the universe as in the prior validation script and price the resulting cases directly via `backfill_prices`.

- [ ] **Step 3: Commit** any validation-support tweak.

---

## Notes for the executor

- **This does NOT populate 63/126-day outcomes for recent cases** — those horizons haven't elapsed. That is expected; the step is re-run over time (idempotent upsert) and matures the corpus. Success here = cases become **priceable** and short horizons score.
- **Keep the replay zero-network.** All fetching is in this step only.
- Confirm the exact yfinance `history` kwargs and column names against `tradingagents/dataflows/y_finance.py` before writing Task 1 (reuse `yf_retry`). Confirm `MarketCalendar` has a "Nth session after" helper or use `sessions_between` + slice.
- Reuse existing test fixtures in `tests/ops/backtest/test_store.py` / `test_prepare.py` for seeding cases; do not invent new store fixtures.
