# Momentum Sleeve Part 1 (Entries + Envelope) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the daily cross-sectional momentum sleeve, generalized `Candidate`, composite universe builder with a slot-aware analysis budget, and the loosened risk envelope — so the system produces buy candidates every trading day.

**Architecture:** A new pure finder (`ops/universe/momentum.py`) mirrors `earnings.py`'s injectable-fetcher pattern and returns a full ranked leaderboard. A composite builder merges both sleeves, dedups (earnings wins, both payloads kept), excludes held/cooldown symbols, and caps at `min(daily_analysis_budget, free_slots)`. The orchestrator passes the held set and free-slot count. Spec: `docs/superpowers/specs/2026-07-05-momentum-sleeve-and-risk-envelope-design.md` Components 1–5.

**Tech Stack:** Python 3.12+, dataclasses, Decimal, yfinance (I/O boundary only), pytest.

## Global Constraints

- All new module-level code: `from __future__ import annotations`, frozen dataclasses, Decimal-at-the-boundary (`_safe_decimal`), no import-time I/O.
- Never fabricate absent data: insufficient history → skip the symbol, never zero-fill.
- Only yfinance I/O wrapped in try/except; internal logic raises (matches `filters.py`/`earnings.py`).
- Envelope values (spec Component 5): `max_open_positions=7`, `per_position_cap_pct=0.12`, `cash_reserve_pct=0.16`. Stops/kill-switches unchanged.
- Daily analysis budget: 8, gated by free slots.
- Run tests with `python -m pytest <path> -v` from the repo root (activate the project venv first — see docs/RUNBOOK-paper-golive.md).
- Commit after every task. Work on branch `feat/momentum-sleeve`.

---

### Task 1: Momentum finder (`ops/universe/momentum.py`)

**Files:**
- Create: `ops/universe/momentum.py`
- Test: `tests/ops/universe/test_momentum.py`

**Interfaces:**
- Consumes: `_safe_decimal` from `ops/universe/earnings.py:36`.
- Produces (later tasks rely on these exact names):
  - `MomentumHit` frozen dataclass: `symbol: str`, `asof_date: date`, `trailing_return_6m: Decimal`, `close: Decimal`, `sma_200: Decimal`, `avg_dollar_volume_20d: Decimal`, `rank: int` (1-based).
  - `find_momentum_leaders(members: list[str], asof_date: date, *, fetch=None) -> list[MomentumHit]` — FULL ranked list, descending 6-mo return, symbol tie-break.
  - `fetch_closes_and_volumes_from_yfinance(symbol: str) -> tuple[list[Decimal], list[Decimal]] | None`
  - Constants `RETURN_LOOKBACK_TRADING_DAYS = 126`, `SMA_WINDOW = 200`.

- [ ] **Step 1: Write the failing tests**

Create `tests/ops/universe/test_momentum.py`:

```python
from datetime import date
from decimal import Decimal

from ops.universe.momentum import find_momentum_leaders

ASOF = date(2026, 7, 2)


def _series(start: str, end: str, n: int = 210) -> list[Decimal]:
    """Linear ramp of n closes from start to end."""
    s, e = Decimal(start), Decimal(end)
    step = (e - s) / Decimal(n - 1)
    return [s + step * i for i in range(n)]


def _fake_fetch(data):
    def fetch(symbol):
        return data.get(symbol)
    return fetch


def test_ranks_by_6mo_return_descending_above_ma():
    vols = [Decimal("1000000")] * 210
    data = {
        "FAST": (_series("100", "200"), vols),
        "SLOW": (_series("100", "120"), vols),
    }
    hits = find_momentum_leaders(["SLOW", "FAST"], ASOF, fetch=_fake_fetch(data))
    assert [h.symbol for h in hits] == ["FAST", "SLOW"]
    assert [h.rank for h in hits] == [1, 2]
    assert hits[0].trailing_return_6m > hits[1].trailing_return_6m
    assert all(h.close > h.sma_200 for h in hits)
    assert all(h.asof_date == ASOF for h in hits)


def test_below_200d_ma_is_gated_out():
    # Rises for 200 bars, then collapses: last closes sit below the 200d MA.
    closes = _series("100", "200", 200) + [Decimal("90")] * 10
    data = {"FALL": (closes, [Decimal("1000000")] * 210)}
    assert find_momentum_leaders(["FALL"], ASOF, fetch=_fake_fetch(data)) == []


def test_insufficient_history_is_skipped_not_zero_filled():
    data = {"IPO": (_series("10", "50", 150), [Decimal("1000000")] * 150)}
    assert find_momentum_leaders(["IPO"], ASOF, fetch=_fake_fetch(data)) == []


def test_fetch_failure_is_skipped():
    assert find_momentum_leaders(["GONE"], ASOF, fetch=lambda s: None) == []


def test_adv_is_20day_mean_dollar_volume():
    closes = [Decimal("100")] * 210
    closes[-1] = Decimal("101")  # nudge above the flat MA so it passes the gate
    volumes = [Decimal("2000000")] * 210
    hits = find_momentum_leaders(
        ["FLAT"], ASOF, fetch=_fake_fetch({"FLAT": (closes, volumes)}),
    )
    assert len(hits) == 1
    # 19 bars at 100*2e6 + 1 bar at 101*2e6, averaged over 20
    assert hits[0].avg_dollar_volume_20d == Decimal("200100000")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/universe/test_momentum.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.universe.momentum'`

- [ ] **Step 3: Write the implementation**

Create `ops/universe/momentum.py`:

```python
"""Cross-sectional momentum sleeve: 6-month leaders above their 200-day MA.

Structured like earnings.py — a pure finder with an injectable fetcher, so it
is unit-testable with fakes and has no import-time I/O. Returns the FULL
ranked list, not a top-N slice: the composite builder takes the head for
entries and the exit engine (Part 2) looks up ranks of held names — one
computation per tick, two consumers."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable

import yfinance as yf

from ops.universe.earnings import _safe_decimal

# ~6 months of trading days for the ranking signal — deliberately between
# 3mo (noisier, higher turnover) and 12mo (sluggish). Named so it is easy
# to tune.
RETURN_LOOKBACK_TRADING_DAYS = 126
SMA_WINDOW = 200
# A 200-day SMA needs ~200 trading days ≈ 9.5 calendar months of bars.
_HISTORY_PERIOD = "10mo"


@dataclass(frozen=True)
class MomentumHit:
    symbol: str
    asof_date: date
    trailing_return_6m: Decimal
    close: Decimal
    sma_200: Decimal
    avg_dollar_volume_20d: Decimal
    rank: int  # 1-based position on the day's leaderboard


def fetch_closes_and_volumes_from_yfinance(
    symbol: str,
) -> tuple[list[Decimal], list[Decimal]] | None:
    """Chronological (closes, volumes) for ~10 months of daily bars.

    NaN rows are DROPPED, not zero-filled — a fabricated zero close would
    poison both the return and the SMA. Only the yfinance I/O is wrapped
    in try/except (same policy as filters.py)."""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period=_HISTORY_PERIOD, auto_adjust=False)
    except Exception as exc:
        print(
            f"[momentum] skipped {symbol}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    if hist is None or hist.empty:
        return None
    try:
        frame = hist.dropna(subset=["Close", "Volume"])
        closes = [_safe_decimal(c) for c in frame["Close"].tolist()]
        volumes = [_safe_decimal(v) for v in frame["Volume"].tolist()]
    except (KeyError, AttributeError) as exc:
        print(
            f"[momentum] skipped {symbol}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    return closes, volumes


def find_momentum_leaders(
    members: list[str],
    asof_date: date,
    *,
    fetch: Callable[[str], tuple[list[Decimal], list[Decimal]] | None] | None = None,
) -> list[MomentumHit]:
    fetch = fetch or fetch_closes_and_volumes_from_yfinance
    scored: list[tuple[Decimal, str, Decimal, Decimal, Decimal]] = []
    for sym in members:
        data = fetch(sym)
        if data is None:
            continue
        closes, volumes = data
        # Insufficient history: skip, never zero-fill.
        if len(closes) < max(SMA_WINDOW, RETURN_LOOKBACK_TRADING_DAYS + 1):
            continue
        last = closes[-1]
        base = closes[-(RETURN_LOOKBACK_TRADING_DAYS + 1)]
        if base == 0:
            continue
        ret = (last - base) / base
        sma = sum(closes[-SMA_WINDOW:]) / Decimal(SMA_WINDOW)
        if last <= sma:
            continue  # uptrend gate: buy strength, never catch a falling knife
        tail_c = closes[-20:]
        tail_v = volumes[-20:]
        adv = sum(c * v for c, v in zip(tail_c, tail_v)) / Decimal(len(tail_c))
        scored.append((ret, sym, last, sma, adv))
    # Descending return; symbol tie-break keeps ordering deterministic.
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [
        MomentumHit(
            symbol=sym, asof_date=asof_date, trailing_return_6m=ret,
            close=last, sma_200=sma, avg_dollar_volume_20d=adv, rank=i + 1,
        )
        for i, (ret, sym, last, sma, adv) in enumerate(scored)
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ops/universe/test_momentum.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add ops/universe/momentum.py tests/ops/universe/test_momentum.py
git commit -m "feat(universe): cross-sectional momentum finder with full ranked leaderboard"
```

---

### Task 2: Generalized `Candidate` with source + optional payloads

**Files:**
- Modify: `ops/universe/__init__.py`
- Modify: `ops/cli.py:186` (force-candidate construction)
- Modify: `tests/ops/test_integration_decide_once.py:19`, `tests/ops/test_cli_decide_once.py:21`, `tests/ops/strategy/test_post_earnings_momentum.py:19` (constructors gain `source=`)
- Test: `tests/ops/universe/test_universe.py`

**Interfaces:**
- Produces: `CandidateSource(str, Enum)` with `EARNINGS`/`MOMENTUM`; `Candidate` gains `source: CandidateSource` (second positional field) and optional `earnings: EarningsHit | None = None`, `momentum: MomentumHit | None = None`. Invariant enforced in `__post_init__`: at least one payload set, and the payload matching `source` set. Both exported in `__all__`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ops/universe/test_universe.py`:

```python
import pytest

from ops.universe import Candidate, CandidateSource
from ops.universe.momentum import MomentumHit


def _mhit(sym):
    return MomentumHit(
        symbol=sym, asof_date=date(2026, 6, 30),
        trailing_return_6m=Decimal("0.4"), close=Decimal("200"),
        sma_200=Decimal("150"), avg_dollar_volume_20d=Decimal("100000000"),
        rank=1,
    )


def test_candidate_rejects_missing_payload():
    with pytest.raises(ValueError):
        Candidate(symbol="A", source=CandidateSource.MOMENTUM,
                  last_price=Decimal("200"),
                  avg_dollar_volume_20d=Decimal("100000000"))


def test_candidate_rejects_payload_source_mismatch():
    with pytest.raises(ValueError):
        Candidate(symbol="A", source=CandidateSource.EARNINGS,
                  last_price=Decimal("200"),
                  avg_dollar_volume_20d=Decimal("100000000"),
                  momentum=_mhit("A"))


def test_candidate_allows_both_payloads_on_overlap():
    c = Candidate(symbol="A", source=CandidateSource.EARNINGS,
                  last_price=Decimal("200"),
                  avg_dollar_volume_20d=Decimal("100000000"),
                  earnings=_hit("A"), momentum=_mhit("A"))
    assert c.source is CandidateSource.EARNINGS
    assert c.momentum is not None


def test_build_universe_marks_candidates_as_earnings():
    cfg = OpsConfig()
    result = build_universe(
        asof_date=date(2026, 6, 30), config=cfg,
        members_loader=lambda: ["AAPL"],
        earnings_finder=lambda syms, asof_date, lookback_days, fetch=None: [_hit(s) for s in syms],
        metrics_fetcher=lambda sym: (Decimal("200"), Decimal("100000000")),
    )
    assert all(c.source is CandidateSource.EARNINGS for c in result)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/universe/test_universe.py -v`
Expected: FAIL — `ImportError: cannot import name 'CandidateSource'`

- [ ] **Step 3: Implement**

In `ops/universe/__init__.py`, add imports and replace the `Candidate` dataclass:

```python
from enum import Enum

from ops.universe.momentum import MomentumHit


class CandidateSource(str, Enum):
    EARNINGS = "EARNINGS"
    MOMENTUM = "MOMENTUM"


@dataclass(frozen=True)
class Candidate:
    symbol: str
    source: CandidateSource
    last_price: Decimal
    avg_dollar_volume_20d: Decimal
    # Optional means genuinely absent, never fabricated. A name that is both
    # a fresh earnings beat and a momentum leader carries BOTH payloads with
    # source == EARNINGS (the primary thesis).
    earnings: EarningsHit | None = None
    momentum: MomentumHit | None = None

    def __post_init__(self) -> None:
        if self.earnings is None and self.momentum is None:
            raise ValueError("Candidate requires at least one sleeve payload")
        if self.source is CandidateSource.EARNINGS and self.earnings is None:
            raise ValueError("EARNINGS candidate requires an earnings payload")
        if self.source is CandidateSource.MOMENTUM and self.momentum is None:
            raise ValueError("MOMENTUM candidate requires a momentum payload")
```

In `build_universe`'s list comprehension, add `source=CandidateSource.EARNINGS,` to the `Candidate(...)` construction. Update `__all__ = ["Candidate", "CandidateSource", "build_universe"]`.

Update every existing construction site to pass `source=CandidateSource.EARNINGS` (all are earnings-shaped today):
- `ops/cli.py:186` (`--force-candidate` path)
- `tests/ops/test_integration_decide_once.py:19`
- `tests/ops/test_cli_decide_once.py:21`
- `tests/ops/strategy/test_post_earnings_momentum.py:19`

- [ ] **Step 4: Run the ops suite**

Run: `python -m pytest tests/ops -v`
Expected: ALL PASS (constructor sites updated; nothing else reads the removed required field)

- [ ] **Step 5: Commit**

```bash
git add ops/universe/__init__.py ops/cli.py tests/ops
git commit -m "feat(universe): Candidate gains source + optional sleeve payloads"
```

---

### Task 3: Source-aware strategy reason string

**Files:**
- Modify: `ops/strategy/post_earnings_momentum.py`
- Test: `tests/ops/strategy/test_post_earnings_momentum.py`

**Interfaces:**
- Consumes: `CandidateSource`, `Candidate.momentum` from Task 2.
- Produces: unchanged `propose_orders` signature; reason for momentum candidates is `"6-mo momentum leader (ret {trailing_return_6m}, > 200d MA); pipeline BUY"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/ops/strategy/test_post_earnings_momentum.py` (reuse that file's existing fake pipeline/candidate helpers; `_mhit` as in Task 2's test):

```python
def test_momentum_candidate_gets_momentum_reason():
    cand = Candidate(
        symbol="NVDA", source=CandidateSource.MOMENTUM,
        last_price=Decimal("200"), avg_dollar_volume_20d=Decimal("100000000"),
        momentum=_mhit("NVDA"),
    )
    strategy = PostEarningsMomentumStrategy(config=OpsConfig())
    orders = strategy.propose_orders(
        candidates=[cand], pipeline=_fake_pipeline_buy(),
        current_equity=Decimal("1000"), asof_date=date(2026, 7, 2),
    )
    assert len(orders) == 1
    assert "6-mo momentum leader" in orders[0].reason
    assert "0.4" in orders[0].reason
```

(If the file has no BUY-pipeline fake, add one returning an object whose `.decision` is `PipelineDecision.BUY`, mirroring the file's existing fakes.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ops/strategy/test_post_earnings_momentum.py -v`
Expected: the new test FAILS with `AttributeError: 'NoneType' object has no attribute 'eps_actual'`

- [ ] **Step 3: Implement**

In `ops/strategy/post_earnings_momentum.py`, add after `_quantize_money`:

```python
def _reason_for(cand: Candidate) -> str:
    if cand.source is CandidateSource.EARNINGS:
        return (
            f"post-earnings beat (EPS {cand.earnings.eps_actual} vs "
            f"est {cand.earnings.eps_estimate}); pipeline BUY"
        )
    return (
        f"6-mo momentum leader (ret {cand.momentum.trailing_return_6m}, "
        f"> 200d MA); pipeline BUY"
    )
```

Import `CandidateSource` alongside `Candidate`, and replace the inline f-string in `propose_orders` with `reason=_reason_for(cand),`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/ops/strategy -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add ops/strategy/post_earnings_momentum.py tests/ops/strategy/test_post_earnings_momentum.py
git commit -m "feat(strategy): source-aware reason string for momentum candidates"
```

---

### Task 4: Config — risk envelope rev 2 + daily analysis budget

**Files:**
- Modify: `ops/config.py`
- Test: `tests/ops/test_config.py`

**Interfaces:**
- Produces: `OpsConfig.max_open_positions = 7`, `per_position_cap_pct = Decimal("0.12")`, `cash_reserve_pct = Decimal("0.16")`, new field `daily_analysis_budget: int = 8` (env `OPS_DAILY_ANALYSIS_BUDGET`, validated `> 0`). Later tasks read `config.daily_analysis_budget`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ops/test_config.py`:

```python
def test_envelope_rev2_defaults_and_derived_concurrency():
    cfg = OpsConfig()
    assert cfg.max_open_positions == 7
    assert cfg.per_position_cap_pct == Decimal("0.12")
    assert cfg.cash_reserve_pct == Decimal("0.16")
    # Neither dial is cosmetic: derived effective concurrency equals the cap.
    deployable = Decimal("1") - cfg.cash_reserve_pct
    assert min(cfg.max_open_positions,
               int(deployable / cfg.per_position_cap_pct)) == 7
    # Safety rails unchanged.
    assert cfg.per_position_stop_pct == Decimal("-0.08")
    assert cfg.daily_drawdown_pct == Decimal("-0.07")
    assert cfg.weekly_drawdown_pct == Decimal("-0.15")


def test_daily_analysis_budget_default_env_and_validation(monkeypatch):
    assert OpsConfig().daily_analysis_budget == 8
    monkeypatch.setenv("OPS_DAILY_ANALYSIS_BUDGET", "3")
    assert load_config().daily_analysis_budget == 3
    with pytest.raises(ValueError):
        OpsConfig(daily_analysis_budget=0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/test_config.py -v`
Expected: the two new tests FAIL (old defaults 5/0.10/0.20; missing field)

- [ ] **Step 3: Implement**

In `ops/config.py`:
- Change defaults: `per_position_cap_pct: Decimal = Decimal("0.12")`, `max_open_positions: int = 7`, `cash_reserve_pct: Decimal = Decimal("0.16")`.
- Add field after `live_fill_gate_count`: `daily_analysis_budget: int = 8` with comment `# Cost dial: max full-pipeline (LLM) analyses per day; risk is capped separately.`
- In `__post_init__`, after the `max_open_positions` check:

```python
        if self.daily_analysis_budget <= 0:
            raise ValueError(
                f"daily_analysis_budget must be > 0, got {self.daily_analysis_budget}"
            )
```

- In `load_config()`, after the `live_fill_gate_count` block:

```python
    daily_analysis_budget = _env_int("OPS_DAILY_ANALYSIS_BUDGET")
    if daily_analysis_budget is not None:
        kwargs["daily_analysis_budget"] = daily_analysis_budget
```

- [ ] **Step 4: Run the FULL ops suite and fix stale default assertions**

Run: `python -m pytest tests/ops -v`
Any failure will be a test asserting the OLD defaults (candidates: `tests/ops/test_config.py`, `tests/ops/guardrails/`). Update only literal expected values (5→7, 0.10→0.12, 0.20→0.16) — do NOT change rule logic or tests that set explicit values in a constructed `OpsConfig`. Re-run until green.

- [ ] **Step 5: Commit**

```bash
git add ops/config.py tests/ops
git commit -m "feat(config): risk envelope rev 2 (7/0.12/0.16) + daily_analysis_budget dial"
```

---

### Task 5: Composite universe builder (`ops/universe/composite.py`)

**Files:**
- Create: `ops/universe/composite.py`
- Test: `tests/ops/universe/test_composite.py`

**Interfaces:**
- Consumes: `build_universe`, `Candidate`, `CandidateSource`, `_MIN_ADV`, `_MIN_PRICE` from `ops/universe/__init__.py`; `find_momentum_leaders`, `MomentumHit` from Task 1; `apply_deny_list`, `apply_liquidity_filter` from `ops/universe/filters.py`; `config.daily_analysis_budget` from Task 4.
- Produces (orchestrator and Part 2 rely on this exact signature):

```python
build_composite_universe(
    *, asof_date: date, config: OpsConfig,
    held_symbols: frozenset[str] = frozenset(),
    free_slots: int | None = None,
    excluded_symbols: frozenset[str] = frozenset(),
    momentum_leaders: list[MomentumHit] | None = None,
    members_loader=None, earnings_finder=None,
    metrics_fetcher=None, momentum_finder=None,
) -> list[Candidate]
```

- [ ] **Step 1: Write the failing tests**

Create `tests/ops/universe/test_composite.py`:

```python
from datetime import date
from decimal import Decimal

from ops.config import OpsConfig
from ops.universe import CandidateSource
from ops.universe.composite import build_composite_universe
from ops.universe.earnings import EarningsHit
from ops.universe.momentum import MomentumHit

ASOF = date(2026, 7, 2)


def _ehit(sym):
    return EarningsHit(
        symbol=sym, report_date=ASOF, eps_actual=Decimal("1"),
        eps_estimate=Decimal("0.9"), revenue_actual=None,
        revenue_estimate=None, eps_beat=True, revenue_beat=None,
    )


def _mhit(sym, rank, close="200", adv="100000000"):
    return MomentumHit(
        symbol=sym, asof_date=ASOF,
        trailing_return_6m=Decimal("1") / Decimal(rank),
        close=Decimal(close), sma_200=Decimal("150"),
        avg_dollar_volume_20d=Decimal(adv), rank=rank,
    )


def _build(*, earnings_syms=(), leaders=(), **kwargs):
    return build_composite_universe(
        asof_date=ASOF, config=OpsConfig(),
        members_loader=lambda: ["AAPL", "MSFT", "NVDA", "AVGO", "META",
                                "AMD", "CRM", "ORCL", "NOW", "PLTR"],
        earnings_finder=lambda syms, asof_date, lookback_days, fetch=None:
            [_ehit(s) for s in syms if s in earnings_syms],
        metrics_fetcher=lambda sym: (Decimal("200"), Decimal("100000000")),
        momentum_leaders=list(leaders),
        **kwargs,
    )


def test_earnings_first_then_momentum_by_rank():
    result = _build(earnings_syms={"MSFT"},
                    leaders=[_mhit("NVDA", 1), _mhit("AMD", 2)])
    assert [c.symbol for c in result] == ["MSFT", "NVDA", "AMD"]
    assert result[0].source is CandidateSource.EARNINGS
    assert result[1].source is CandidateSource.MOMENTUM


def test_overlap_keeps_earnings_source_and_both_payloads():
    result = _build(earnings_syms={"NVDA"}, leaders=[_mhit("NVDA", 1)])
    assert len(result) == 1
    c = result[0]
    assert c.source is CandidateSource.EARNINGS
    assert c.earnings is not None and c.momentum is not None


def test_cap_is_min_of_budget_and_free_slots():
    leaders = [_mhit(s, i + 1) for i, s in enumerate(
        ["NVDA", "AMD", "AVGO", "META", "CRM", "ORCL", "NOW", "PLTR", "AAPL"])]
    assert len(_build(leaders=leaders)) == 8                    # budget caps
    assert len(_build(leaders=leaders, free_slots=2)) == 2      # slots cap
    assert _build(leaders=leaders, free_slots=0) == []          # full book -> zero LLM runs


def test_held_and_excluded_symbols_never_returned():
    result = _build(earnings_syms={"MSFT"},
                    leaders=[_mhit("NVDA", 1), _mhit("AMD", 2)],
                    held_symbols=frozenset({"NVDA"}),
                    excluded_symbols=frozenset({"MSFT"}))
    assert [c.symbol for c in result] == ["AMD"]


def test_illiquid_momentum_leader_is_dropped():
    leaders = [_mhit("NVDA", 1, adv="1000"), _mhit("AMD", 2)]
    assert [c.symbol for c in _build(leaders=leaders)] == ["AMD"]


def test_momentum_finder_used_when_no_precomputed_leaderboard():
    calls = []

    def finder(members, asof_date):
        calls.append(list(members))
        return [_mhit("NVDA", 1)]

    result = build_composite_universe(
        asof_date=ASOF, config=OpsConfig(),
        members_loader=lambda: ["NVDA", "SPOT"],
        earnings_finder=lambda syms, asof_date, lookback_days, fetch=None: [],
        metrics_fetcher=lambda sym: (Decimal("200"), Decimal("100000000")),
        momentum_finder=finder,
    )
    assert [c.symbol for c in result] == ["NVDA"]
    assert calls == [["NVDA"]]  # deny-listed SPOT never reaches the finder
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/universe/test_composite.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.universe.composite'`

- [ ] **Step 3: Implement**

Create `ops/universe/composite.py`:

```python
"""Composite universe builder: earnings sleeve + momentum sleeve, merged,
deduped (earnings wins, both payloads kept), exclusions applied, capped at
min(daily_analysis_budget, free_slots).

Every returned candidate costs a full TradingAgentsGraph (LLM) run, so with
zero free slots this returns [] and the day costs zero pipeline runs —
instead of budget-many analyses whose orders are all guaranteed rejects at
the max-open-positions guardrail."""
from __future__ import annotations

import dataclasses
from datetime import date

from ops.config import OpsConfig
from ops.universe import (
    _MIN_ADV,
    _MIN_PRICE,
    Candidate,
    CandidateSource,
    build_universe,
)
from ops.universe.filters import apply_deny_list, apply_liquidity_filter
from ops.universe.momentum import MomentumHit, find_momentum_leaders
from ops.universe.sp500 import load_sp500_members


def build_composite_universe(
    *,
    asof_date: date,
    config: OpsConfig,
    held_symbols: frozenset[str] = frozenset(),
    free_slots: int | None = None,
    excluded_symbols: frozenset[str] = frozenset(),
    momentum_leaders: list[MomentumHit] | None = None,
    members_loader=None,
    earnings_finder=None,
    metrics_fetcher=None,
    momentum_finder=None,
) -> list[Candidate]:
    members_loader = members_loader or load_sp500_members
    momentum_finder = momentum_finder or find_momentum_leaders

    # 1. Earnings sleeve (existing path, alphabetically sorted).
    earnings_candidates = build_universe(
        asof_date=asof_date, config=config,
        members_loader=members_loader, earnings_finder=earnings_finder,
        metrics_fetcher=metrics_fetcher,
    )

    # 2. Momentum sleeve. The leaderboard may be precomputed by the caller
    #    (the tick computes it once for both this builder and the exit
    #    engine); otherwise compute it here.
    if momentum_leaders is None:
        eligible = apply_deny_list(members_loader(), config.deny_list)
        momentum_leaders = momentum_finder(eligible, asof_date=asof_date)

    # 3. Shared liquidity filter, fed from data already on the hits —
    #    reuses the filter logic with zero extra I/O.
    hits_by_sym = {h.symbol: h for h in momentum_leaders}
    liquid = apply_liquidity_filter(
        [h.symbol for h in momentum_leaders],
        min_adv=_MIN_ADV, min_price=_MIN_PRICE,
        fetch_metrics=lambda s: (hits_by_sym[s].close,
                                 hits_by_sym[s].avg_dollar_volume_20d),
    )

    # 4. Merge + dedup: earnings wins on overlap and keeps both payloads.
    ineligible = held_symbols | excluded_symbols
    merged: list[Candidate] = []
    earnings_syms = set()
    for cand in earnings_candidates:
        if cand.symbol in ineligible:
            continue
        hit = hits_by_sym.get(cand.symbol)
        if hit is not None:
            cand = dataclasses.replace(cand, momentum=hit)
        merged.append(cand)
        earnings_syms.add(cand.symbol)
    for sym, price, adv in liquid:  # already in leaderboard (rank) order
        if sym in ineligible or sym in earnings_syms:
            continue
        merged.append(Candidate(
            symbol=sym, source=CandidateSource.MOMENTUM,
            last_price=price, avg_dollar_volume_20d=adv,
            momentum=hits_by_sym[sym],
        ))

    # 5. Slot-aware cap.
    cap = config.daily_analysis_budget
    if free_slots is not None:
        cap = max(0, min(cap, free_slots))
    return merged[:cap]
```

Note: `apply_liquidity_filter` preserves input order and `momentum_leaders` is rank-ordered, so momentum candidates come out by rank without re-sorting.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/ops/universe -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add ops/universe/composite.py tests/ops/universe/test_composite.py
git commit -m "feat(universe): composite builder — merge sleeves, dedup, slot-aware cap"
```

---

### Task 6: Orchestrator passes held set + free slots

**Files:**
- Modify: `ops/scheduler/orchestrator.py:43-46`
- Test: `tests/ops/scheduler/test_orchestrator.py`

**Interfaces:**
- Consumes: builder signature from Task 5.
- Produces: `_tick_impl` calls the builder with `held_symbols=frozenset(held)` and `free_slots=max(0, config.max_open_positions - len(held))`. The post-hoc `fresh_candidates` filter stays (belt-and-suspenders).

- [ ] **Step 1: Update the fake + write the failing test**

In `tests/ops/scheduler/test_orchestrator.py`, change `_fake_universe` to accept and record the new kwargs (keep returning the same candidates):

```python
def _fake_universe(symbols, seen_kwargs=None):
    def build(*, asof_date, config, **kwargs):
        if seen_kwargs is not None:
            seen_kwargs.update(kwargs)
        return [_candidate(s) for s in symbols]
    return build
```

(Adapt to the file's existing helper shape — the essential change: accept `**kwargs`, optionally record them.) Add:

```python
def test_tick_passes_held_and_free_slots_to_builder():
    seen = {}
    broker = _fake_broker_with_positions(["AAPL", "MSFT"])  # use the file's existing fake-broker helper
    orch = _make_orchestrator(  # use the file's existing construction helper/pattern
        broker=broker, universe_builder=_fake_universe([], seen),
    )
    orch.tick()
    assert seen["held_symbols"] == frozenset({"AAPL", "MSFT"})
    assert seen["free_slots"] == OpsConfig().max_open_positions - 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ops/scheduler/test_orchestrator.py -v`
Expected: new test FAILS with `KeyError: 'held_symbols'`

- [ ] **Step 3: Implement**

In `ops/scheduler/orchestrator.py` `_tick_impl`, replace lines 44-46:

```python
        held = {p.symbol for p in self._broker.get_positions()}
        free_slots = max(0, self._config.max_open_positions - len(held))
        candidates = self._universe_builder(
            asof_date=asof_date, config=self._config,
            held_symbols=frozenset(held), free_slots=free_slots,
        )
        fresh_candidates = [c for c in candidates if c.symbol not in held]
```

- [ ] **Step 4: Run scheduler tests**

Run: `python -m pytest tests/ops/scheduler -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add ops/scheduler/orchestrator.py tests/ops/scheduler/test_orchestrator.py
git commit -m "feat(scheduler): tick passes held set and free-slot count to universe builder"
```

---

### Task 7: Wire composite builder + integration test

**Files:**
- Modify: `ops/main.py:248-267` (`_wire`)
- Test: `tests/ops/test_main.py`, plus full suite

**Interfaces:**
- Consumes: `build_composite_universe` from Task 5.

- [ ] **Step 1: Write the failing test**

Append to `tests/ops/test_main.py` (if that file already has a `_wire` test with its own broker/journal fixtures, reuse those instead of the MagicMock):

```python
def test_wire_uses_composite_universe_builder(tmp_path):
    from unittest.mock import MagicMock

    from ops.journal import Journal
    from ops.main import _wire
    from ops.universe.composite import build_composite_universe

    journal = Journal(str(tmp_path / "j.sqlite"))
    orchestrator, _guardian, _calendar = _wire(MagicMock(), journal, OpsConfig())
    assert orchestrator._universe_builder is build_composite_universe
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ops/test_main.py -v`
Expected: new test FAILS (`is build_universe`, not composite)

- [ ] **Step 3: Implement**

In `ops/main.py` `_wire`, replace `from ops.universe import build_universe` with `from ops.universe.composite import build_composite_universe` and `universe_builder=build_universe` with `universe_builder=build_composite_universe`.

- [ ] **Step 4: Run the FULL suite**

Run: `python -m pytest tests/ops -v`
Expected: ALL PASS. Also verify no other caller depends on the bare builder positionally: `grep -rn "build_universe" ops/ | grep -v universe/` — expected: no orchestration call sites remain (the CLI force-candidate path constructs candidates directly).

- [ ] **Step 5: Commit**

```bash
git add ops/main.py tests/ops/test_main.py
git commit -m "feat(ops): wire composite universe builder into the service"
```

**Part 1 complete:** the system now generates up to `min(8, free_slots)` candidates every trading day from two sleeves, under the rev-2 envelope. Part 2 (exit engine) makes the book turn over.
