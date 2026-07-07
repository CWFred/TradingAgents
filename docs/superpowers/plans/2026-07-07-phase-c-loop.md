# Phase C: The Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Positions and memos are watched mechanically — a falsifier trip, a −30% drawdown, a lapsed holding period, or a lapsed hard-dated catalyst reaches the user's phone without any human polling, escalations queue a re-research hit for the existing brain, and delisted baseline names write themselves off.

**Architecture:** A pure metric-evaluation module (`ops/research/metrics.py`) maps the memo schema's free-text falsifier metric names onto real computations (prices via `PriceContext`, fundamentals via XBRL facts) with stateless consecutive-period semantics. A monitor orchestrator (`ops/research/monitor.py`) walks `MemoStore.open_memos()` once per trading day, journals typed events for trips/dues/escalations (deduped against the journal itself — no side state), and enqueues re-research hits through a new `ScreenStore.enqueue_hit`. It is wired into the always-on daemon's APScheduler (post-close, alongside the daily summary) and exposed as `ops research monitor` for manual runs. Baseline delisting automation lives in `ops/research/baseline.py`: per-run quote-failure events + write-off after 3 consecutive failing runs, derived entirely from the journal. **There is no LLM anywhere in this phase** — monitoring is 100% mechanical (spec decision); escalation queues work for the existing Phase B brain, never invokes it inline.

**Tech Stack:** Python 3.10+, stdlib, APScheduler (already a daemon dep), Pydantic memo schema (read-only — never modified), SQLite stores already in the repo, pytest (all network and all quotes mocked/injected).

**Spec:** `docs/superpowers/specs/2026-07-06-finish-research-system-design.md`, section "Phase C — The loop". Companion: `docs/long_horizon_research.md` build-order step 6. Read both once before starting.

## Global Constraints

- Work happens in `/Users/frednick/Code/TradingAgents` on a new branch `feat/phase-c-loop` cut from **updated** `origin/main` (≥ f4acbcd). GOTCHA: `main` is checked out in the `~/Code/TradingAgents-live` deploy worktree — never `git checkout main` here; use `git fetch origin && git checkout -b feat/phase-c-loop origin/main`. Never commit to `main` directly.
- The working tree may contain unrelated user-modified files (`main.py` at repo root, `tradingagents/dataflows/reddit.py`) — NEVER stage them; always stage explicit file lists, never `git add -A` or `git add .`.
- Lint: `ruff check <files you touched>` must pass (line-length 100, py310+). Pre-existing errors in untouched files (4 in `ops/cli.py`, 1 in `ops/scheduler/orchestrator.py`) are not yours to fix.
- Tests: pytest; new test modules set `pytestmark = pytest.mark.unit`; ALL network, quotes, and EDGAR fetches mocked/injected. Full suite green before every commit: `pytest tests/ -q` (baseline before this plan: **1392 passed, 13 skipped**, 69 subtests). Always use `.venv/bin/python -m pytest`.
- Money math in `Decimal`; **monitoring metric values are `float`** (they are calibration/monitoring data, not money — same rule as the Memo schema's float fields).
- **No LLM calls in this phase.** No code path added here may import or invoke an LLM client, and no code path may require an API key or `SEC_EDGAR_USER_AGENT` to *import or test* (live fetches degrade gracefully at runtime).
- Journal event-kind strings are frozen once journaled — pick names carefully; every new kind MUST be registered in `ops/events.py` `BUILDERS` and in exactly one of `ops/notify/policy.py` `POLICY` or `ops/events.py` `AUDIT_ONLY` (the enforcement test in `tests/ops/notify/test_policy.py` fails otherwise).
- **Do not modify** `tradingagents/memos/schema.py` (the Memo/Falsifier schema is frozen), `ops/research/brain.py`, or the notify dispatcher/transports.
- Never run `launchctl`. `install-*` commands render plists and print the bootstrap command; loading is always the user's action.
- **Escalation rule for the implementer:** if an instruction contradicts what you find in the code (a function signature differs, a fixture doesn't exist), STOP and report BLOCKED with details. Do not improvise around it.

## File structure (what this plan touches)

| File | Task | Responsibility |
|---|---|---|
| `ops/research/metrics.py` (new) | 1 | falsifier metric evaluators + trip logic (pure, stateless) |
| `ops/research/store.py` | 2 | `ScreenStore.enqueue_hit` — single-symbol re-research queueing |
| `ops/events.py`, `ops/notify/policy.py` | 3 | 8 new event kinds + payload builders + notify policy |
| `ops/research/monitor.py` (new) | 4 | the daily monitor: falsifiers, drawdown, catalysts, due-for-resolution, escalation |
| `ops/main.py`, `ops/cli.py` | 5 | daemon post-close job + `ops research monitor` CLI |
| `ops/research/baseline.py`, `ops/research/run.py`, `ops/cli.py` | 6 | quote-failure journaling + auto write-off after 3 failing runs |
| `ops/cli.py`, `ops/deploy/__init__.py`, `ops/deploy/com.tradingagents.research.plist.template` (new) | 7 | `ops research run --notify` + Saturday launchd job |
| `docs/research_monitor.md` (new), `docs/long_horizon_research.md`, `docs/research_brain.md` | 8 | runbook, build-order checkmark, PR |

## Key repo facts (verified 2026-07-07 @ f4acbcd — trust these, but re-verify signatures you depend on before coding)

- `tradingagents/memos/schema.py`: `Falsifier(description, check_type: Literal["fundamental","event","price"], metric: str|None, operator: Literal["<","<=",">",">="]|None, threshold: float|None, consecutive_periods: int>=1)` — machine-checkable iff metric+operator+threshold all set. `Catalyst(description, expected_date: date|None, hard_date: bool)`. `Memo` fields used here: `memo_id` (32-hex), `ticker`, `thesis_type`, `entry_price_ref: float`, `as_of_date: date`, `created_at: datetime`, `expected_holding_months`, `price_target_low/high`, `must_be_true: list[str]`, `falsifiers`, `catalysts`, `event_block.key_dates: list[Catalyst]` (SEPARATE from `memo.catalysts` — check both), `status`.
- `tradingagents/memos/store.py`: `MemoStore.open_memos() -> list[Memo]` (status="open"); `due_for_resolution(as_of: datetime|None=None) -> list[Memo]` (open+passed where `(now - created_at).days >= expected_holding_months*30`); `resolve(memo_id, resolution)`; `list(ticker=,status=,thesis_type=)`.
- `ops/research/prices.py`: `PriceContext(closes: dict[date, Decimal], splits: dict[date, Decimal])` with `recent_closes(*, asof, days=60) -> list[Decimal]` (**oldest-first**), `close_on_or_before(when, *, max_gap_days=10) -> Decimal|None`, `split_factor_after(anchor: date) -> Decimal`, `unadjusted_close_on_or_before(when, *, max_gap_days=10, era_end=None)`. `fetch_price_context(symbol) -> PriceContext|None` (6y, paced, None on failure).
- `tradingagents/dataflows/edgar_facts.py`: `get_company_facts(ticker) -> dict` (throttled; raises `EdgarNotConfiguredError` — a `ValueError` subtype from `tradingagents/dataflows/edgar.py` — when `SEC_EDGAR_USER_AGENT` unset); `annual_series(facts, concepts, *, asof, unit="USD", max_years=5) -> list[FactPoint]` where `FactPoint(concept, value: Decimal, unit, end: date, start, form)`.
- `tradingagents/dataflows/fundamentals.py`: `compute_fundamentals(ticker, facts, *, asof) -> Fundamentals(ticker, asof, ebit, ebitda, total_debt, cash, fcf, eps_history, roic_history, gross_margin_history)`; histories are `tuple[YearValue(fiscal_year_end: date, value: Decimal), ...]`; `gross_margin_history` values are **ratios** (0.35), not percent; `REVENUE_CONCEPTS` is a module constant (tuple of XBRL tags).
- `ops/journal.py`: `record_event(kind: str, payload: dict, *, at: datetime|None=None)`; `has_event_today(kind, *, now=None)`; `count_events(kind, *, since: datetime|None=None, payload_equals: dict[str,str]|None=None) -> int` (json_extract match — **payload values used for matching must be stored as strings**); `read_events() -> list[dict]` (each has `"kind"`, `"payload"`); `last_buy_fill_for(symbol) -> dict|None` (has `"price"`, `"filled_at"`); tz-aware datetimes required.
- `ops/events.py`: flat `KIND_* = "snake_case"` constants; one `<kind>_payload(*, ...) -> dict` builder per kind (Decimals stringified, datetimes isoformat at this boundary); `BUILDERS` dict registry; `AUDIT_ONLY` frozenset. `ops/notify/policy.py`: `POLICY: dict[str, PolicyEntry]`, `PolicyEntry(channels: tuple, urgency: str, cooldown_seconds: int|None)`, presets `_PUSH_ONLY = PolicyEntry(("push",), "normal", None)`; high-priority pattern from `KIND_UNIVERSE_BLIND: PolicyEntry(("push",), "high", None)`.
- `ops/main.py`: `_start_full_scheduler(orchestrator, guardian, dispatcher, journal, broker, calendar=None, heartbeat_job=None) -> BackgroundScheduler` registers `daily_summary` at `CronTrigger(hour=16, minute=5, day_of_week="mon-fri")` with `max_instances=1, misfire_grace_time=300`; the wrapper pattern is `_daily_summary_tick(journal, broker, calendar=None)` — try/except into `journal.record_event(events.KIND_DAILY_SUMMARY_ERROR, ...)` because raising kills the APScheduler job. `tests/ops/test_main.py::test_daily_summary_job_callable_does_not_name_error` shows the wiring test pattern (`sched.get_job("daily_summary").func()` must not raise).
- `ops/research/store.py`: `screen_hits(id, run_id, symbol, asof, status, payload, created_at, UNIQUE(run_id, symbol))`; `record_run` dedupes new hits against `status='pending'` only; `pending_hits()` oldest-first; `mark_researched/mark_failed/mark_expired`. `ops/research/brain.py` `_screen_summary` bracket-indexes payload keys `symbol, asof, passed, cheap, quality, market_cap, ev_ebit` and `.get`s `valuation_bars/quality_bars/triggers` (trigger dicts need `kind`, `date`, `description`) — any synthetic payload MUST carry all bracket-accessed keys.
- `ops/research/baseline.py`: `update_baseline_portfolio(*, broker, journal, passers, asof, now=None) -> {"buys","exits","skipped"}`; `write_off_position(*, journal, symbol, price, starting_cash, note=None) -> dict` (rebuilds broker with `_no_quotes`, synthetic SELL via `journal.record_order` + `journal.record_fill`, journals `KIND_BASELINE_WRITEOFF` — AUDIT_ONLY); `_equity_with_fallback(broker, journal)`. `ops/broker/base.py`: `Broker.get_quote(symbol) -> Decimal` raises `QuoteUnavailable`; `PaperBroker.from_journal(journal=, quote_source=, starting_cash=)`.
- `ops/research/run.py` `run_screen(config=, asof=, dry_run=, limit=, quote_source=None)`: baseline block wraps `Journal(config.baseline_journal_path)` + `PaperBroker.from_journal(quote_source=quote_source or make_yfinance_quote_source())` + `update_baseline_portfolio` in try/except (control never takes down a screen); returns `ScreenRunSummary(..., baseline=dict|None, coverage=...)`.
- `ops/cli.py`: `screen --notify` implements the A7 push pattern (direct `build_push_transport(load_notify_config()).send(NotifyMessage(...))` — the batch job has no dispatcher); `research` group has `write-off` and `run`; `install-screen-service` (fail-fast on missing `SEC_EDGAR_USER_AGENT`, renders `ops/deploy/com.tradingagents.screen.plist.template` via `render_screen_plist`, `_install_plist` writes + prints launchctl commands, never runs them). `ops/deploy/__init__.py`: `_render(template_path, substitutions)` raises on leftover `{{...}}`.
- `OpsConfig` already has `journal_path`, `baseline_journal_path`, `screen_store_path`, `memo_store_path`, `baseline_starting_cash` — **no new config fields are needed in this plan**; thresholds are constants.

---

### Task 1: Falsifier metric evaluators (`ops/research/metrics.py`)

**Files:**
- Create: `ops/research/metrics.py`
- Test: `tests/ops/research/test_metrics.py`

**Interfaces:**
- Produces (Task 4 relies on these exact names):
  - `@dataclass(frozen=True) MetricContext: entry_price_ref: float; asof: date; entry_era: date; price_ctx: PriceContext | None = None; fundamentals: Fundamentals | None = None; facts: dict | None = None` — `entry_era` is the memo's `as_of_date` (the share-count era `entry_price_ref` was quoted in; drawdown must undo splits dated after it).
  - `@dataclass(frozen=True) FalsifierCheck: status: str` (`"tripped" | "ok" | "unevaluable"`) `; observed: float | None; detail: str`
  - `observations(metric: str, ctx: MetricContext) -> list[float] | None` — most-recent-first observation series; `None` = metric unknown or inputs missing.
  - `evaluate_falsifier(falsifier, ctx: MetricContext) -> FalsifierCheck`
  - `drawdown_pct(ctx: MetricContext) -> float | None` — the implicit −30% escalation check (also the `drawdown_from_cost_pct` metric's latest observation).
  - `SUPPORTED_METRICS: frozenset[str]` = `{"drawdown_from_cost_pct", "gross_margin_pct", "revenue_yoy_pct", "net_debt_to_ebitda"}`

**Metric semantics (binding):**

| metric | source | observation series (most-recent-first) | period unit |
|---|---|---|---|
| `drawdown_from_cost_pct` | prices | `((close_in_entry_era − entry_price_ref) / entry_price_ref) × 100` per trading day; `close_in_entry_era = adjusted_close × split_factor_after(entry_era)` | trading days |
| `gross_margin_pct` | fundamentals | `gross_margin_history` values × 100, sorted by `fiscal_year_end` descending | fiscal years |
| `revenue_yoy_pct` | facts | `((rev[y] / rev[y−1]) − 1) × 100` between consecutive fiscal years from `annual_series(facts, REVENUE_CONCEPTS, asof=ctx.asof, max_years=6)` | fiscal years |
| `net_debt_to_ebitda` | fundamentals | single value `[(total_debt − cash) / ebitda]`; `cash=None` → 0; `total_debt=None` or `ebitda` None/≤0 → unevaluable | latest year |

**Trip rule (binding):** a machine-checkable falsifier trips iff the first `consecutive_periods` observations ALL satisfy `value OP threshold`. Fewer observations than `consecutive_periods` → `unevaluable`. Not machine-checkable (any of metric/operator/threshold unset) → `unevaluable` with detail `"not machine-checkable"`. Unknown metric → `unevaluable` naming the metric. All comparisons on floats.

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for falsifier metric evaluation (pure, no network)."""

from datetime import date
from decimal import Decimal

import pytest

from ops.research.metrics import (
    SUPPORTED_METRICS,
    FalsifierCheck,
    MetricContext,
    drawdown_pct,
    evaluate_falsifier,
    observations,
)
from ops.research.prices import PriceContext
from tradingagents.dataflows.fundamentals import Fundamentals, YearValue
from tradingagents.memos.schema import Falsifier

pytestmark = pytest.mark.unit

ASOF = date(2026, 7, 7)
ERA = date(2026, 1, 5)


def _ctx(**overrides):
    kwargs = dict(
        entry_price_ref=10.0,
        asof=ASOF,
        entry_era=ERA,
        price_ctx=PriceContext(closes={
            date(2026, 7, 2): Decimal("8"),
            date(2026, 7, 6): Decimal("7"),
            date(2026, 7, 7): Decimal("6.5"),
        }),
    )
    kwargs.update(overrides)
    return MetricContext(**kwargs)


def _fundamentals(**overrides):
    kwargs = dict(
        ticker="WIDG", asof=ASOF, ebit=Decimal("10"), ebitda=Decimal("20"),
        total_debt=Decimal("50"), cash=Decimal("10"), fcf=Decimal("5"),
        eps_history=(), roic_history=(),
        gross_margin_history=(
            YearValue(date(2024, 12, 31), Decimal("0.40")),
            YearValue(date(2025, 12, 31), Decimal("0.28")),
        ),
    )
    kwargs.update(overrides)
    return Fundamentals(**kwargs)


def _falsifier(**overrides):
    kwargs = dict(
        description="drawdown", check_type="price",
        metric="drawdown_from_cost_pct", operator="<", threshold=-25.0,
        consecutive_periods=1,
    )
    kwargs.update(overrides)
    return Falsifier(**kwargs)


def test_drawdown_observations_most_recent_first_in_entry_era():
    obs = observations("drawdown_from_cost_pct", _ctx())
    # closes 6.5, 7, 8 (most recent first) against entry 10 -> -35%, -30%, -20%
    assert obs == pytest.approx([-35.0, -30.0, -20.0])
    assert drawdown_pct(_ctx()) == pytest.approx(-35.0)


def test_drawdown_undoes_splits_after_entry_era():
    # 2-for-1 split after entry: Yahoo-adjusted 6.5 is 13.0 in entry-era shares.
    ctx = _ctx(price_ctx=PriceContext(
        closes={date(2026, 7, 7): Decimal("6.5")},
        splits={date(2026, 6, 1): Decimal("2")},
    ))
    assert drawdown_pct(ctx) == pytest.approx(30.0)  # (13 - 10) / 10


def test_gross_margin_pct_sorted_descending_and_scaled():
    obs = observations("gross_margin_pct", _ctx(fundamentals=_fundamentals()))
    assert obs == pytest.approx([28.0, 40.0])  # FY2025 first


def test_net_debt_to_ebitda_single_observation():
    obs = observations("net_debt_to_ebitda", _ctx(fundamentals=_fundamentals()))
    assert obs == pytest.approx([2.0])  # (50 - 10) / 20


def test_net_debt_to_ebitda_unprofitable_is_unevaluable():
    f = _fundamentals(ebitda=Decimal("0"))
    assert observations("net_debt_to_ebitda", _ctx(fundamentals=f)) is None


def test_revenue_yoy_from_facts(monkeypatch):
    from ops.research import metrics

    def fake_annual_series(facts, concepts, *, asof, unit="USD", max_years=5):
        from tradingagents.dataflows.edgar_facts import FactPoint
        return [
            FactPoint("Revenues", Decimal("110"), "USD", date(2024, 12, 31), None, "10-K"),
            FactPoint("Revenues", Decimal("88"), "USD", date(2025, 12, 31), None, "10-K"),
        ]

    monkeypatch.setattr(metrics, "annual_series", fake_annual_series)
    obs = observations("revenue_yoy_pct", _ctx(facts={"facts": {}}))
    assert obs == pytest.approx([-20.0])  # 88 vs 110


def test_unknown_metric_and_missing_inputs_return_none():
    assert observations("free_cash_flow_conversion", _ctx()) is None
    assert observations("gross_margin_pct", _ctx(fundamentals=None)) is None
    assert observations("drawdown_from_cost_pct", _ctx(price_ctx=None)) is None


def test_evaluate_falsifier_trips_on_threshold():
    check = evaluate_falsifier(_falsifier(threshold=-30.0), _ctx())
    assert check.status == "tripped"
    assert check.observed == pytest.approx(-35.0)


def test_evaluate_falsifier_consecutive_periods():
    # -35, -30, -20: two most recent both < -25 -> trips at periods=2 ...
    check = evaluate_falsifier(_falsifier(consecutive_periods=2), _ctx())
    assert check.status == "tripped"
    # ... but not at periods=3 (the -20 observation breaks the streak).
    check = evaluate_falsifier(_falsifier(consecutive_periods=3), _ctx())
    assert check.status == "ok"


def test_evaluate_falsifier_insufficient_history_unevaluable():
    check = evaluate_falsifier(_falsifier(consecutive_periods=9), _ctx())
    assert check.status == "unevaluable"


def test_prose_only_falsifier_unevaluable():
    prose = Falsifier(description="thesis stops working", check_type="fundamental")
    check = evaluate_falsifier(prose, _ctx())
    assert check.status == "unevaluable"
    assert "machine-checkable" in check.detail


def test_supported_metrics_frozen():
    assert SUPPORTED_METRICS == {
        "drawdown_from_cost_pct", "gross_margin_pct",
        "revenue_yoy_pct", "net_debt_to_ebitda",
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/ops/research/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.research.metrics'`

- [ ] **Step 3: Write the implementation**

```python
"""Falsifier metric evaluation — the mechanical half of the monitoring loop.

The memo schema's ``Falsifier.metric`` is a free-text name; this module is
the registry that makes those names mean something. Everything here is pure
and stateless: ``consecutive_periods`` is evaluated against the observation
HISTORY (last N trading days for price metrics, last N fiscal years for
fundamental ones), never against persisted counter state — the journal is
the only state store in ops, and it doesn't need to be involved here.

Metric values are floats: monitoring/calibration data, not money.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ops.research.prices import PriceContext
from tradingagents.dataflows.edgar_facts import annual_series
from tradingagents.dataflows.fundamentals import REVENUE_CONCEPTS, Fundamentals

SUPPORTED_METRICS = frozenset({
    "drawdown_from_cost_pct",
    "gross_margin_pct",
    "revenue_yoy_pct",
    "net_debt_to_ebitda",
})

_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


@dataclass(frozen=True)
class MetricContext:
    """Everything a metric evaluator may draw on. Fetching happens upstream
    (the monitor decides what to fetch per memo); evaluators only read."""

    entry_price_ref: float
    asof: date
    entry_era: date  # memo.as_of_date: the share-count era of entry_price_ref
    price_ctx: PriceContext | None = None
    fundamentals: Fundamentals | None = None
    facts: dict | None = None


@dataclass(frozen=True)
class FalsifierCheck:
    status: str  # "tripped" | "ok" | "unevaluable"
    observed: float | None
    detail: str


def _drawdown_series(ctx: MetricContext) -> list[float] | None:
    if ctx.price_ctx is None or ctx.entry_price_ref <= 0:
        return None
    closes = ctx.price_ctx.recent_closes(asof=ctx.asof, days=60)  # oldest-first
    if not closes:
        return None
    # Yahoo closes are split-adjusted to TODAY's share count; entry_price_ref
    # was quoted in the entry era's. Undo every split after the entry era so
    # the comparison is apples-to-apples (Phase A split machinery).
    factor = ctx.price_ctx.split_factor_after(ctx.entry_era)
    entry = ctx.entry_price_ref
    return [
        (float(close * factor) - entry) / entry * 100.0
        for close in reversed(closes)  # most-recent-first
    ]


def _gross_margin_series(ctx: MetricContext) -> list[float] | None:
    f = ctx.fundamentals
    if f is None or not f.gross_margin_history:
        return None
    ordered = sorted(f.gross_margin_history, key=lambda yv: yv.fiscal_year_end,
                     reverse=True)
    return [float(yv.value) * 100.0 for yv in ordered]


def _revenue_yoy_series(ctx: MetricContext) -> list[float] | None:
    if ctx.facts is None:
        return None
    points = annual_series(ctx.facts, REVENUE_CONCEPTS, asof=ctx.asof, max_years=6)
    by_year = sorted(points, key=lambda p: p.end)  # oldest-first
    if len(by_year) < 2:
        return None
    yoy = []
    for prev, cur in zip(by_year, by_year[1:]):
        if prev.value == 0:
            continue
        yoy.append((float(cur.value) / float(prev.value) - 1.0) * 100.0)
    return list(reversed(yoy)) or None  # most-recent-first


def _net_debt_to_ebitda(ctx: MetricContext) -> list[float] | None:
    f = ctx.fundamentals
    if f is None or f.total_debt is None or f.ebitda is None or f.ebitda <= 0:
        return None
    cash = f.cash if f.cash is not None else 0
    return [float((f.total_debt - cash) / f.ebitda)]


_EVALUATORS = {
    "drawdown_from_cost_pct": _drawdown_series,
    "gross_margin_pct": _gross_margin_series,
    "revenue_yoy_pct": _revenue_yoy_series,
    "net_debt_to_ebitda": _net_debt_to_ebitda,
}


def observations(metric: str, ctx: MetricContext) -> list[float] | None:
    """Most-recent-first observation series for a metric; None = unevaluable."""
    evaluator = _EVALUATORS.get(metric)
    return evaluator(ctx) if evaluator else None


def drawdown_pct(ctx: MetricContext) -> float | None:
    """Latest drawdown vs entry_price_ref — the implicit escalation check."""
    series = _drawdown_series(ctx)
    return series[0] if series else None


def evaluate_falsifier(falsifier, ctx: MetricContext) -> FalsifierCheck:
    """Trip iff the most recent ``consecutive_periods`` observations ALL
    satisfy ``value OP threshold``. Anything unanswerable is 'unevaluable' —
    honest uncertainty, surfaced in the run summary, never a silent pass."""
    if not falsifier.metric or falsifier.operator is None or falsifier.threshold is None:
        return FalsifierCheck("unevaluable", None, "not machine-checkable")
    series = observations(falsifier.metric, ctx)
    if series is None:
        return FalsifierCheck(
            "unevaluable", None,
            f"metric {falsifier.metric!r} not evaluable (unknown or inputs missing)",
        )
    need = falsifier.consecutive_periods
    if len(series) < need:
        return FalsifierCheck(
            "unevaluable", series[0],
            f"only {len(series)} observation(s), need {need}",
        )
    op = _OPS[falsifier.operator]
    window = series[:need]
    tripped = all(op(v, falsifier.threshold) for v in window)
    detail = (
        f"{falsifier.metric} {falsifier.operator} {falsifier.threshold}: "
        f"last {need} = {[round(v, 2) for v in window]}"
    )
    return FalsifierCheck("tripped" if tripped else "ok", series[0], detail)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/ops/research/test_metrics.py -v` — Expected: 12 passed.
Note: `test_revenue_yoy_from_facts` monkeypatches `metrics.annual_series` — the module-level `from ... import annual_series` binding makes that work; keep the import at module top exactly as written.

- [ ] **Step 5: Full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ -q
ruff check ops/research/metrics.py tests/ops/research/test_metrics.py
git add ops/research/metrics.py tests/ops/research/test_metrics.py
git commit -m "feat(research): falsifier metric evaluators — stateless consecutive-period trip logic"
```

---

### Task 2: `ScreenStore.enqueue_hit`

**Files:**
- Modify: `ops/research/store.py`
- Test: extend `tests/ops/research/test_store.py`

**Interfaces:**
- Produces (Task 4 relies on this exact name):
  - `ScreenStore.enqueue_hit(symbol: str, *, asof: date, payload: dict, source: str = "monitor") -> int | None` — inserts one hit with `status="pending"`, `run_id=f"{source}-{asof.isoformat()}-{uuid4().hex[:8]}"`; returns the new hit id, or `None` (no insert) when a pending hit for the symbol already exists (same dedupe rule as `record_run`). `payload` is stored as JSON verbatim — the CALLER owns making it `_screen_summary`-compatible.

- [ ] **Step 1: Failing tests.** Read `tests/ops/research/test_store.py` first; reuse its fixtures/helpers (adapt `_result`/`ASOF` spellings to the file). Append:

```python
def test_enqueue_hit_queues_and_dedupes(store):
    payload = {"symbol": "AAA", "asof": "2026-07-07", "passed": True}
    hit_id = store.enqueue_hit("AAA", asof=date(2026, 7, 7), payload=payload)
    assert hit_id is not None
    hits = store.pending_hits()
    assert [h["symbol"] for h in hits] == ["AAA"]
    assert hits[0]["payload"] == payload
    assert hits[0]["run_id"].startswith("monitor-2026-07-07-")
    # Second enqueue while pending: dedupe, no new row.
    assert store.enqueue_hit("AAA", asof=date(2026, 7, 7), payload=payload) is None
    assert len(store.pending_hits()) == 1


def test_enqueue_hit_requeues_after_terminal_status(store):
    payload = {"symbol": "AAA", "asof": "2026-07-07", "passed": True}
    hit_id = store.enqueue_hit("AAA", asof=date(2026, 7, 7), payload=payload)
    store.mark_researched(hit_id)
    assert store.enqueue_hit("AAA", asof=date(2026, 7, 8), payload=payload) is not None
```

Run: `.venv/bin/python -m pytest tests/ops/research/test_store.py -v` — Expected: new tests FAIL (`AttributeError: ... 'enqueue_hit'`).

- [ ] **Step 2: Implement.** In `ops/research/store.py`, next to `record_run` (read it first and mirror its insert/dedupe SQL exactly — same column list, same `json.dumps(payload)`… note `record_run` serializes with `default=str`; use plain `json.dumps(payload)` here since the caller passes JSON-ready dicts):

```python
    def enqueue_hit(
        self, symbol: str, *, asof: date, payload: dict, source: str = "monitor",
    ) -> int | None:
        """Queue one ad-hoc research hit (monitoring escalation path).

        Same dedupe rule as record_run: a symbol already pending is not
        re-queued. The payload must be _screen_summary-compatible — the
        caller (ops/research/monitor.py) owns that contract.
        """
        symbol = symbol.upper()
        with self._connect() as conn:
            pending = conn.execute(
                "SELECT 1 FROM screen_hits WHERE symbol = ? AND status = 'pending'",
                (symbol,),
            ).fetchone()
            if pending:
                return None
            run_id = f"{source}-{asof.isoformat()}-{uuid4().hex[:8]}"
            cur = conn.execute(
                """
                INSERT INTO screen_hits (run_id, symbol, asof, status, payload, created_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (run_id, symbol, asof.isoformat(), json.dumps(payload),
                 datetime.now(timezone.utc).isoformat()),
            )
            return cur.lastrowid
```

Adapt the INSERT column list/`created_at` expression to what `record_run` actually does (read it first — if it uses a helper for now-ISO or different columns, mirror that). Add any missing imports (`uuid4`, `datetime`/`timezone`) only if not already present.

- [ ] **Step 3: Run tests, full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ops/research/test_store.py -v && .venv/bin/python -m pytest tests/ -q
ruff check ops/research/store.py tests/ops/research/test_store.py
git add ops/research/store.py tests/ops/research/test_store.py
git commit -m "feat(research): ScreenStore.enqueue_hit — single-symbol re-research queueing"
```

---

### Task 3: Monitoring event kinds + notify policy

**Files:**
- Modify: `ops/events.py`, `ops/notify/policy.py`
- Test: extend `tests/ops/test_events.py` if it exists (check; else the policy enforcement test in `tests/ops/notify/test_policy.py` is the safety net — read it to confirm it walks BUILDERS/POLICY/AUDIT_ONLY automatically) plus the explicit policy assertions below.

**Interfaces (Tasks 4–6 rely on these exact names):**

| constant | value | policy | payload builder kwargs |
|---|---|---|---|
| `KIND_FALSIFIER_TRIPPED` | `"falsifier_tripped"` | POLICY: `PolicyEntry(("push",), "high", None)` | `memo_id, ticker, falsifier_index: str, description, metric, observed: str, threshold: str, consecutive_periods: int` |
| `KIND_RESOLUTION_DUE` | `"resolution_due"` | POLICY: `_PUSH_ONLY` (push/normal) | `memo_id, ticker, thesis_type, status, expected_holding_months: int, elapsed_days: int, checklist: str` |
| `KIND_CATALYST_DUE` | `"catalyst_due"` | POLICY: `_PUSH_ONLY` | `memo_id, ticker, catalyst_index: str, description, expected_date: str` |
| `KIND_RESEARCH_ESCALATION` | `"research_escalation"` | POLICY: `PolicyEntry(("push",), "high", None)` | `ticker, memo_id, reason, hit_id: int \| None` |
| `KIND_RESEARCH_MONITOR_RUN` | `"research_monitor_run"` | AUDIT_ONLY | `asof, memos_checked: int, falsifiers_evaluated: int, tripped: int, unevaluable: int, escalations: int, resolution_due: int, catalyst_due: int, errors: list[str]` |
| `KIND_RESEARCH_MONITOR_ERROR` | `"research_monitor_error"` | AUDIT_ONLY | `error: str` |
| `KIND_BASELINE_QUOTE_FAILURE` | `"baseline_quote_failure"` | AUDIT_ONLY | `symbol, asof: str, error: str` |
| `KIND_BASELINE_AUTO_WRITEOFF` | `"baseline_auto_writeoff"` | AUDIT_ONLY (surfaced through the screen job's `--notify` summary — the weekly batch has no dispatcher; mirrors how `baseline_writeoff` stays audit-only) | `symbol, quantity: str, price: str, failing_runs: int, note: str` |

**Dedupe-critical detail:** `falsifier_index`, `catalyst_index` are **strings** in payloads (e.g. `"0"`), and `memo_id`/`symbol`/`asof` are strings — `Journal.count_events(payload_equals=...)` matches with bound string parameters, so the monitor's 7-day dedupe queries only work if the stored values are strings.

- [ ] **Step 1: Read first.** Read `ops/events.py` fully (constants block, one payload builder, `AUDIT_ONLY`, `BUILDERS`) and `ops/notify/policy.py` (`PolicyEntry`, presets, `POLICY`). Read `tests/ops/notify/test_policy.py` to see exactly what the enforcement test requires.

- [ ] **Step 2: Failing test.** Append to the most fitting existing test file (the policy test file, or `tests/ops/test_events.py` if present — follow the repo's precedent):

```python
def test_phase_c_monitoring_kinds_registered():
    from ops import events
    from ops.notify.policy import POLICY

    assert POLICY[events.KIND_FALSIFIER_TRIPPED].urgency == "high"
    assert POLICY[events.KIND_RESEARCH_ESCALATION].urgency == "high"
    assert POLICY[events.KIND_RESOLUTION_DUE].urgency == "normal"
    assert POLICY[events.KIND_CATALYST_DUE].urgency == "normal"
    for kind in (
        events.KIND_RESEARCH_MONITOR_RUN, events.KIND_RESEARCH_MONITOR_ERROR,
        events.KIND_BASELINE_QUOTE_FAILURE, events.KIND_BASELINE_AUTO_WRITEOFF,
    ):
        assert kind in events.AUDIT_ONLY
        assert kind not in POLICY
    # Every new kind has a registered payload builder.
    for kind in (
        events.KIND_FALSIFIER_TRIPPED, events.KIND_RESOLUTION_DUE,
        events.KIND_CATALYST_DUE, events.KIND_RESEARCH_ESCALATION,
        events.KIND_RESEARCH_MONITOR_RUN, events.KIND_RESEARCH_MONITOR_ERROR,
        events.KIND_BASELINE_QUOTE_FAILURE, events.KIND_BASELINE_AUTO_WRITEOFF,
    ):
        assert kind in events.BUILDERS
```

Run it — Expected: FAIL (`AttributeError: ... KIND_FALSIFIER_TRIPPED`).

- [ ] **Step 3: Implement.** In `ops/events.py`, add the constants in a new commented group (`# --- Research monitoring (Phase C) ---`), the eight payload builders following the file's exact style (keyword-only args; Decimals/floats stringified where the table says `str`; example below), the `BUILDERS` entries, and the four AUDIT_ONLY additions. Example builder shape:

```python
def falsifier_tripped_payload(
    *, memo_id: str, ticker: str, falsifier_index: str, description: str,
    metric: str, observed: str, threshold: str, consecutive_periods: int,
) -> dict[str, Any]:
    return {
        "memo_id": memo_id, "ticker": ticker, "falsifier_index": falsifier_index,
        "description": description, "metric": metric, "observed": observed,
        "threshold": threshold, "consecutive_periods": consecutive_periods,
    }
```

In `ops/notify/policy.py`, add the four POLICY entries (with a one-line comment each explaining the priority choice, matching the file's commenting style).

- [ ] **Step 4: Run tests, full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ops/notify/ -v && .venv/bin/python -m pytest tests/ -q
ruff check ops/events.py ops/notify/policy.py
git add ops/events.py ops/notify/policy.py tests/ops/notify/test_policy.py
git commit -m "feat(ops): Phase C monitoring event kinds + notify policy"
```

(Adjust the staged test path to wherever Step 2's test actually landed.)

---

### Task 4: The monitor (`ops/research/monitor.py`)

**Files:**
- Create: `ops/research/monitor.py`
- Test: `tests/ops/research/test_monitor.py`

**Interfaces:**
- Consumes: `MetricContext`, `evaluate_falsifier`, `drawdown_pct`, `FalsifierCheck` (Task 1); `ScreenStore.enqueue_hit` (Task 2); event kinds + builders (Task 3); `MemoStore.open_memos`/`due_for_resolution`; `Journal.record_event`/`count_events`; `fetch_price_context`; `get_company_facts` + `compute_fundamentals`.
- Produces (Task 5 relies on these exact names):
  - `DRAWDOWN_ESCALATION_PCT = -30.0` (spec's −30% drawdown escalation)
  - `RENOTIFY_DAYS = 7` (a trip/due already notified within this window is not re-notified)
  - `@dataclass MonitorOutcome: asof: str; memos_checked: int = 0; falsifiers_evaluated: int = 0; tripped: int = 0; unevaluable: int = 0; escalations: int = 0; resolution_due: int = 0; catalyst_due: int = 0; errors: list[str] = field(default_factory=list)`
  - `monitor_memos(*, memo_store, screen_store, journal, price_fetcher=None, facts_fetcher=None, today: date | None = None, now: datetime | None = None) -> MonitorOutcome`

**Behavior (binding, maps 1:1 to the spec bullet):**

1. For every memo in `memo_store.open_memos()` (per-memo try/except — one bad ticker must never kill the loop; append to `outcome.errors` and continue):
   - Fetch `price_ctx = price_fetcher(ticker)` once. Build `MetricContext(entry_price_ref=memo.entry_price_ref, asof=today, entry_era=memo.as_of_date, price_ctx=price_ctx)`.
   - Fetch facts/fundamentals **lazily** — only if the memo has ≥1 machine-checkable falsifier with `check_type == "fundamental"`; wrap the fetch: on ANY exception (including `EdgarNotConfiguredError` when the daemon env lacks `SEC_EDGAR_USER_AGENT`), leave `facts`/`fundamentals` as `None` (those falsifiers become `unevaluable`) and record one note in `outcome.errors`. Fundamental falsifiers degrade; price falsifiers and the drawdown check must still run.
   - Evaluate EVERY falsifier via `evaluate_falsifier`; count `tripped`/`unevaluable`. For each **tripped** falsifier: journal `KIND_FALSIFIER_TRIPPED` (payload per Task 3; `observed`/`threshold` stringified, `falsifier_index=str(i)`) **unless** `journal.count_events(KIND_FALSIFIER_TRIPPED, since=now - timedelta(days=RENOTIFY_DAYS), payload_equals={"memo_id": memo.memo_id, "falsifier_index": str(i)}) > 0`.
   - Drawdown: `dd = drawdown_pct(ctx)`; `dd is not None and dd <= DRAWDOWN_ESCALATION_PCT` is an escalation reason (`f"drawdown {dd:.1f}% <= {DRAWDOWN_ESCALATION_PCT}%"`) even with zero falsifiers tripped.
   - **Escalation** (any falsifier tripped OR drawdown breach): `hit_id = screen_store.enqueue_hit(memo.ticker, asof=today, payload=_escalation_payload(memo.ticker, today, reason))`; if `hit_id` is not `None` (i.e. not already queued), journal `KIND_RESEARCH_ESCALATION` and increment `escalations`. The enqueue-dedupe doubles as notification dedupe.
   - **Catalysts:** for `thesis_type == "event"` memos, walk `memo.catalysts + (memo.event_block.key_dates if memo.event_block else [])` (a single combined list; index over it). For each with `hard_date and expected_date and expected_date <= today`: journal `KIND_CATALYST_DUE` (`catalyst_index=str(combined_index)`, `expected_date=isoformat`) with the same 7-day `count_events` dedupe on `{"memo_id": ..., "catalyst_index": ...}`; increment `catalyst_due`.
2. For every memo in `memo_store.due_for_resolution(as_of=now)`: journal `KIND_RESOLUTION_DUE` with 7-day dedupe on `{"memo_id": ...}`; `checklist` = a newline-joined string: each falsifier's description, `targets: low X / high Y`, then each `must_be_true` line prefixed `must-be-true: `. Increment `resolution_due`.
3. Always finish by journaling `KIND_RESEARCH_MONITOR_RUN` with the outcome counts (`at=now`) — this event is also the daemon's once-per-day gate (Task 5).
4. Defaults: `price_fetcher = fetch_price_context`, `facts_fetcher = get_company_facts` (both imported lazily inside the function bodies so importing the module costs nothing), `today = date.today()`, `now = datetime.now(timezone.utc)`.

`_escalation_payload(symbol, asof, reason) -> dict` (module-private but stable enough to test): a `_screen_summary`-compatible dict — every bracket-accessed key present:

```python
def _escalation_payload(symbol: str, asof: date, reason: str) -> dict:
    """A ScreenResult-shaped payload for a monitoring escalation hit.

    The brain's _screen_summary bracket-indexes these exact keys — keep in
    lockstep with ops/research/screener.py's ScreenResult serialization.
    """
    return {
        "symbol": symbol, "asof": asof.isoformat(),
        "passed": True, "cheap": False, "quality": False,
        "valuation_bars": [], "quality_bars": [],
        "triggers": [{
            "kind": "monitor_escalation", "description": reason,
            "date": asof.isoformat(), "source": "monitor",
        }],
        "market_cap": None, "ev_ebit": None,
    }
```

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the memo monitoring loop (no network; real stores on tmp)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ops import events
from ops.journal import Journal
from ops.research.monitor import (
    DRAWDOWN_ESCALATION_PCT,
    MonitorOutcome,
    monitor_memos,
)
from ops.research.prices import PriceContext
from ops.research.store import ScreenStore
from tradingagents.memos.schema import (
    Catalyst, EventThesis, EvidenceItem, Falsifier, Memo, ValueThesis,
)
from tradingagents.memos.store import MemoStore

pytestmark = pytest.mark.unit

TODAY = date(2026, 7, 7)
NOW = datetime(2026, 7, 7, 20, 30, tzinfo=timezone.utc)


def _memo(ticker="WIDG", *, thesis_type="value", entry=10.0, falsifiers=None,
          catalysts=None, key_dates=None, months=12, created_at=None):
    kwargs = dict(
        ticker=ticker, as_of_date=date(2026, 1, 5), thesis_type=thesis_type,
        thesis="Mispriced on distributor loss.",
        evidence=[EvidenceItem(claim="c", source_type="filing", source_ref="a:mdna")],
        conviction_tier="starter", entry_price_ref=entry,
        price_target_low=15.0, price_target_high=20.0,
        expected_holding_months=months,
        must_be_true=["volume replaced"],
        falsifiers=falsifiers or [Falsifier(
            description="drawdown breach", check_type="price",
            metric="drawdown_from_cost_pct", operator="<", threshold=-25.0,
        )],
        catalysts=catalysts or [],
    )
    if thesis_type == "value":
        kwargs["value_block"] = ValueThesis(
            why_cheap="lost distributor", change_trigger="selloff",
            normalized_earnings_view="$1.20", quality_assessment="net cash",
        )
    else:
        kwargs["event_block"] = EventThesis(
            event_type="spinoff", forced_seller="index funds",
            mechanism="index deletion", key_dates=key_dates or [],
        )
    if created_at is not None:
        kwargs["created_at"] = created_at
    return Memo(**kwargs)


@pytest.fixture
def stores(tmp_path):
    return (
        MemoStore(tmp_path / "memos.sqlite"),
        ScreenStore(tmp_path / "screen.sqlite"),
        Journal(str(tmp_path / "journal.sqlite")),
    )


def _prices(close):
    return lambda symbol: PriceContext(closes={TODAY: Decimal(str(close))})


def _run(stores, *, close=9.5, facts_fetcher=None):
    memo_store, screen_store, journal = stores
    return monitor_memos(
        memo_store=memo_store, screen_store=screen_store, journal=journal,
        price_fetcher=_prices(close),
        facts_fetcher=facts_fetcher or (lambda t: (_ for _ in ()).throw(AssertionError("no facts needed"))),
        today=TODAY, now=NOW,
    )


def _events_of(journal, kind):
    return [e for e in journal.read_events() if e["kind"] == kind]


def test_quiet_memo_produces_only_run_summary(stores):
    memo_store, _, journal = stores
    memo_store.save(_memo())  # entry 10, close 9.5 -> -5%, nothing trips
    outcome = _run(stores)
    assert outcome.memos_checked == 1
    assert outcome.tripped == 0 and outcome.escalations == 0
    kinds = [e["kind"] for e in journal.read_events()]
    assert kinds == [events.KIND_RESEARCH_MONITOR_RUN]


def test_falsifier_trip_notifies_and_escalates(stores):
    memo_store, screen_store, journal = stores
    memo_store.save(_memo())
    outcome = _run(stores, close=7.0)  # -30% < -25 threshold
    assert outcome.tripped == 1
    assert outcome.escalations == 1
    tripped = _events_of(journal, events.KIND_FALSIFIER_TRIPPED)
    assert len(tripped) == 1
    assert tripped[0]["payload"]["falsifier_index"] == "0"
    assert [h["symbol"] for h in screen_store.pending_hits()] == ["WIDG"]
    payload = screen_store.pending_hits()[0]["payload"]
    # _screen_summary-compatible: bracket-accessed keys all present.
    for key in ("symbol", "asof", "passed", "cheap", "quality", "market_cap", "ev_ebit"):
        assert key in payload
    assert payload["triggers"][0]["kind"] == "monitor_escalation"
    assert len(_events_of(journal, events.KIND_RESEARCH_ESCALATION)) == 1


def test_renotify_dedupe_within_window(stores):
    memo_store, screen_store, journal = stores
    memo_store.save(_memo())
    _run(stores, close=7.0)
    outcome2 = _run(stores, close=7.0)  # same day re-run: still tripped...
    assert outcome2.tripped == 1
    # ...but no second notification and no second escalation (hit still pending).
    assert len(_events_of(journal, events.KIND_FALSIFIER_TRIPPED)) == 1
    assert len(_events_of(journal, events.KIND_RESEARCH_ESCALATION)) == 1
    assert len(screen_store.pending_hits()) == 1


def test_drawdown_escalates_without_any_falsifier_trip(stores):
    memo_store, screen_store, journal = stores
    memo_store.save(_memo(falsifiers=[Falsifier(
        description="margin", check_type="fundamental",
        metric="gross_margin_pct", operator="<", threshold=30.0,
    )]))
    # No facts fetchable -> fundamental falsifier unevaluable; close 6.5 = -35%.
    outcome = _run(
        stores, close=6.5,
        facts_fetcher=lambda t: (_ for _ in ()).throw(RuntimeError("EDGAR down")),
    )
    assert outcome.tripped == 0
    assert outcome.unevaluable == 1
    assert outcome.escalations == 1
    assert DRAWDOWN_ESCALATION_PCT == -30.0
    esc = _events_of(journal, events.KIND_RESEARCH_ESCALATION)[0]
    assert "drawdown" in esc["payload"]["reason"]
    # The facts failure was recorded, not fatal.
    assert any("EDGAR down" in e for e in outcome.errors)


def test_lapsed_hard_catalyst_surfaces_for_event_memo(stores):
    memo_store, _, journal = stores
    memo_store.save(_memo(
        ticker="SPIN", thesis_type="event",
        key_dates=[Catalyst(description="distribution date",
                            expected_date=date(2026, 6, 30), hard_date=True)],
    ))
    outcome = _run(stores)
    assert outcome.catalyst_due == 1
    due = _events_of(journal, events.KIND_CATALYST_DUE)
    assert len(due) == 1 and due[0]["payload"]["ticker"] == "SPIN"
    # Soft/future dates never fire: re-run dedupes too.
    assert _run(stores).catalyst_due == 0


def test_resolution_due_with_checklist(stores):
    memo_store, _, journal = stores
    old = NOW - timedelta(days=400)
    memo_store.save(_memo(months=12, created_at=old))
    outcome = _run(stores)
    assert outcome.resolution_due == 1
    due = _events_of(journal, events.KIND_RESOLUTION_DUE)[0]
    checklist = due["payload"]["checklist"]
    assert "drawdown breach" in checklist
    assert "must-be-true: volume replaced" in checklist
    assert "15.0" in checklist and "20.0" in checklist
    # Dedupe on re-run.
    assert _run(stores).resolution_due == 0


def test_bad_ticker_does_not_kill_the_loop(stores):
    memo_store, _, journal = stores
    memo_store.save(_memo(ticker="BAD1"))
    memo_store.save(_memo(ticker="GOOD"))

    def flaky_prices(symbol):
        if symbol == "BAD1":
            raise RuntimeError("yahoo exploded")
        return PriceContext(closes={TODAY: Decimal("9.5")})

    outcome = monitor_memos(
        memo_store=memo_store, screen_store=stores[1], journal=journal,
        price_fetcher=flaky_prices, facts_fetcher=lambda t: {},
        today=TODAY, now=NOW,
    )
    assert outcome.memos_checked == 2
    assert any("BAD1" in e for e in outcome.errors)
    assert len(_events_of(journal, events.KIND_RESEARCH_MONITOR_RUN)) == 1


def test_empty_store_is_a_clean_noop(stores):
    outcome = _run(stores)
    assert isinstance(outcome, MonitorOutcome)
    assert outcome.memos_checked == 0
    # The run summary is still journaled (it is the daemon's daily gate).
    assert len(_events_of(stores[2], events.KIND_RESEARCH_MONITOR_RUN)) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/ops/research/test_monitor.py -v` — Expected: FAIL (`ModuleNotFoundError`). Note: the fixture passes `created_at` as a constructor kwarg (the schema field has a default_factory, so an explicit value overrides it — verify against `tradingagents/memos/schema.py`; if `created_at` were not accepted as a kwarg, STOP → BLOCKED).

- [ ] **Step 3: Implement** — module docstring explaining the spec mapping, then:

```python
"""The daily memo monitor (Phase C, build-order step 6).

Positions and memos are watched MECHANICALLY; humans get exceptions:

  - machine-checkable falsifiers evaluated against fresh prices/facts
    (ops/research/metrics.py — stateless, journal is the only memory);
  - a -30% drawdown escalates even when no falsifier trips;
  - lapsed hard-dated catalysts surface for event-sleeve memos;
  - due_for_resolution memos push the memo's exit checklist;
  - every escalation queues a re-research hit for the Phase B brain
    (ops research run picks it up) — the monitor NEVER invokes an LLM.

Notifications dedupe against the journal itself (count_events over a
RENOTIFY_DAYS window) so a tripped falsifier nags weekly, not daily.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ops import events
from ops.research.metrics import MetricContext, drawdown_pct, evaluate_falsifier

DRAWDOWN_ESCALATION_PCT = -30.0
RENOTIFY_DAYS = 7


@dataclass
class MonitorOutcome:
    asof: str
    memos_checked: int = 0
    falsifiers_evaluated: int = 0
    tripped: int = 0
    unevaluable: int = 0
    escalations: int = 0
    resolution_due: int = 0
    catalyst_due: int = 0
    errors: list[str] = field(default_factory=list)
```

then `_escalation_payload` (given above under Interfaces) and the rest of the module. Every monitor `record_event` call passes `at=now` — deterministic timestamps make the dedupe window testable and restart-honest (the journal gained the `at=` param in the Phase B pre-plan fix). Keep ALL journaling at this layer — `ops/research/metrics.py` stays pure:

```python
def _recently_notified(journal, kind: str, *, now: datetime, **payload_keys: str) -> bool:
    """Already journaled within the re-notify window? The journal IS the
    dedupe state — no counters to lose on restart."""
    since = now - timedelta(days=RENOTIFY_DAYS)
    return journal.count_events(kind, since=since, payload_equals=payload_keys) > 0


def _checklist(memo) -> str:
    lines = [f"falsifier: {f.description}" for f in memo.falsifiers]
    lines.append(f"targets: low {memo.price_target_low} / high {memo.price_target_high}")
    lines.extend(f"must-be-true: {item}" for item in memo.must_be_true)
    return "\n".join(lines)


def _build_context(memo, *, today, price_fetcher, facts_fetcher, errors) -> MetricContext:
    """Fetch what this memo's falsifiers actually need — facts only when a
    machine-checkable fundamental falsifier exists, and never fatally."""
    price_ctx = price_fetcher(memo.ticker)
    fundamentals = None
    facts = None
    needs_facts = any(
        f.check_type == "fundamental" and f.metric and f.operator is not None
        and f.threshold is not None
        for f in memo.falsifiers
    )
    if needs_facts:
        try:
            from tradingagents.dataflows.fundamentals import compute_fundamentals

            facts = facts_fetcher(memo.ticker)
            fundamentals = compute_fundamentals(memo.ticker, facts, asof=today)
        except Exception as exc:  # degrade: fundamental checks go unevaluable
            errors.append(f"{memo.ticker}: facts unavailable ({exc})")
    return MetricContext(
        entry_price_ref=memo.entry_price_ref, asof=today,
        entry_era=memo.as_of_date, price_ctx=price_ctx,
        fundamentals=fundamentals, facts=facts,
    )


def _check_memo(memo, ctx, *, journal, screen_store, today, now, outcome) -> None:
    escalation_reasons: list[str] = []
    for i, falsifier in enumerate(memo.falsifiers):
        check = evaluate_falsifier(falsifier, ctx)
        outcome.falsifiers_evaluated += 1
        if check.status == "unevaluable":
            outcome.unevaluable += 1
            continue
        if check.status != "tripped":
            continue
        outcome.tripped += 1
        escalation_reasons.append(f"falsifier tripped: {check.detail}")
        if not _recently_notified(
            journal, events.KIND_FALSIFIER_TRIPPED, now=now,
            memo_id=memo.memo_id, falsifier_index=str(i),
        ):
            journal.record_event(
                events.KIND_FALSIFIER_TRIPPED,
                events.falsifier_tripped_payload(
                    memo_id=memo.memo_id, ticker=memo.ticker,
                    falsifier_index=str(i), description=falsifier.description,
                    metric=falsifier.metric or "",
                    observed=str(check.observed), threshold=str(falsifier.threshold),
                    consecutive_periods=falsifier.consecutive_periods,
                ),
                at=now,
            )

    dd = drawdown_pct(ctx)
    if dd is not None and dd <= DRAWDOWN_ESCALATION_PCT:
        escalation_reasons.append(f"drawdown {dd:.1f}% <= {DRAWDOWN_ESCALATION_PCT}%")

    if escalation_reasons:
        reason = "; ".join(escalation_reasons)
        hit_id = screen_store.enqueue_hit(
            memo.ticker, asof=today,
            payload=_escalation_payload(memo.ticker, today, reason),
        )
        if hit_id is not None:  # enqueue-dedupe doubles as notify-dedupe
            outcome.escalations += 1
            journal.record_event(
                events.KIND_RESEARCH_ESCALATION,
                events.research_escalation_payload(
                    ticker=memo.ticker, memo_id=memo.memo_id,
                    reason=reason, hit_id=hit_id,
                ),
                at=now,
            )

    if memo.thesis_type == "event":
        catalysts = list(memo.catalysts)
        if memo.event_block is not None:
            catalysts += list(memo.event_block.key_dates)
        for i, catalyst in enumerate(catalysts):
            if not (catalyst.hard_date and catalyst.expected_date
                    and catalyst.expected_date <= today):
                continue
            if _recently_notified(
                journal, events.KIND_CATALYST_DUE, now=now,
                memo_id=memo.memo_id, catalyst_index=str(i),
            ):
                continue
            outcome.catalyst_due += 1
            journal.record_event(
                events.KIND_CATALYST_DUE,
                events.catalyst_due_payload(
                    memo_id=memo.memo_id, ticker=memo.ticker,
                    catalyst_index=str(i), description=catalyst.description,
                    expected_date=catalyst.expected_date.isoformat(),
                ),
                at=now,
            )


def monitor_memos(
    *,
    memo_store,
    screen_store,
    journal,
    price_fetcher=None,
    facts_fetcher=None,
    today: date | None = None,
    now: datetime | None = None,
) -> MonitorOutcome:
    """One post-close pass over the open-memo book. Per-memo failures are
    recorded and skipped — one bad ticker must never blind the whole watch."""
    if price_fetcher is None:
        from ops.research.prices import fetch_price_context

        price_fetcher = fetch_price_context
    if facts_fetcher is None:
        from tradingagents.dataflows.edgar_facts import get_company_facts

        facts_fetcher = get_company_facts
    today = today or date.today()
    now = now or datetime.now(timezone.utc)
    outcome = MonitorOutcome(asof=today.isoformat())

    for memo in memo_store.open_memos():
        outcome.memos_checked += 1
        try:
            ctx = _build_context(
                memo, today=today, price_fetcher=price_fetcher,
                facts_fetcher=facts_fetcher, errors=outcome.errors,
            )
            _check_memo(memo, ctx, journal=journal, screen_store=screen_store,
                        today=today, now=now, outcome=outcome)
        except Exception as exc:  # noqa: BLE001 — one name never kills the loop
            outcome.errors.append(f"{memo.ticker}: {type(exc).__name__}: {exc}")

    for memo in memo_store.due_for_resolution(as_of=now):
        if _recently_notified(journal, events.KIND_RESOLUTION_DUE, now=now,
                              memo_id=memo.memo_id):
            continue
        outcome.resolution_due += 1
        elapsed = (now - memo.created_at).days
        journal.record_event(
            events.KIND_RESOLUTION_DUE,
            events.resolution_due_payload(
                memo_id=memo.memo_id, ticker=memo.ticker,
                thesis_type=memo.thesis_type, status=memo.status,
                expected_holding_months=memo.expected_holding_months,
                elapsed_days=elapsed, checklist=_checklist(memo),
            ),
            at=now,
        )

    journal.record_event(
        events.KIND_RESEARCH_MONITOR_RUN,
        events.research_monitor_run_payload(
            asof=outcome.asof, memos_checked=outcome.memos_checked,
            falsifiers_evaluated=outcome.falsifiers_evaluated,
            tripped=outcome.tripped, unevaluable=outcome.unevaluable,
            escalations=outcome.escalations, resolution_due=outcome.resolution_due,
            catalyst_due=outcome.catalyst_due, errors=outcome.errors,
        ),
        at=now,
    )
    return outcome
```

Note `memo.created_at` subtraction: `MemoStore.due_for_resolution` already does `(now - memo.created_at).days`, so the schema's default is tz-aware and compatible; if a test failure proves otherwise, STOP → BLOCKED.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/ops/research/test_monitor.py -v` — Expected: 8 passed.

- [ ] **Step 5: Full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ -q
ruff check ops/research/monitor.py tests/ops/research/test_monitor.py
git add ops/research/monitor.py tests/ops/research/test_monitor.py
git commit -m "feat(research): daily memo monitor — falsifiers, drawdown, catalysts, resolution surfacing"
```

---

### Task 5: Daemon post-close job + `ops research monitor` CLI

**Files:**
- Modify: `ops/main.py`, `ops/cli.py`
- Test: extend `tests/ops/test_main.py`; create `tests/ops/test_cli_research_monitor.py`

**Interfaces:**
- Consumes: `monitor_memos`, `MonitorOutcome`, `KIND_RESEARCH_MONITOR_RUN/_ERROR` (Tasks 3–4); `_start_full_scheduler` and the `_daily_summary_tick` wrapper pattern in `ops/main.py`.
- Produces:
  - `_research_monitor_tick(journal, config) -> None` in `ops/main.py` — gate on `journal.has_event_today(events.KIND_RESEARCH_MONITOR_RUN)` (return early), else build `MemoStore(config.memo_store_path)` + `ScreenStore(config.screen_store_path)` and call `monitor_memos(...)`; catch ALL exceptions into `journal.record_event(events.KIND_RESEARCH_MONITOR_ERROR, ...)` exactly like `_daily_summary_tick` (raising kills the APScheduler job).
  - `_start_full_scheduler(..., config=None)` — new keyword-only-position final param, default `None`; when a config is passed, register job id `"research_monitor"` at `CronTrigger(hour=16, minute=20, day_of_week="mon-fri")`, `max_instances=1, misfire_grace_time=300`. When `config is None` (existing tests), the job is not registered. `run()` passes its loaded config.
  - CLI: `ops research monitor` — manual/debug entry point; builds `Journal(config.journal_path)`, both stores, calls `monitor_memos` (real default fetchers), echoes one summary line + each error; exit 0 always (an empty store is a clean no-op). If a monitor run already happened today it still runs (manual = explicit) but echoes a note.

- [ ] **Step 1: Read first.** `ops/main.py` `_daily_summary_tick`, `_start_full_scheduler`, and `run()`'s call site; `tests/ops/test_main.py::test_daily_summary_job_callable_does_not_name_error`; `tests/ops/test_cli_research_run.py` (the lazy-import monkeypatch pattern for CLI tests).

- [ ] **Step 2: Failing tests.** Append to `tests/ops/test_main.py`:

```python
def test_research_monitor_job_registered_and_callable(tmp_path):
    """The Phase C monitor job mirrors daily_summary: registered only when a
    config is supplied, and its callable must be invokable with no args."""
    from unittest.mock import MagicMock
    from ops.main import _start_full_scheduler

    journal = MagicMock()
    journal.has_event_today.return_value = True  # monitor idempotent no-op
    config = MagicMock()
    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), MagicMock(), journal, MagicMock(), config=config,
    )
    try:
        job = sched.get_job("research_monitor")
        assert job is not None
        job.func()  # gate returns early; must not raise
    finally:
        sched.shutdown(wait=False)


def test_research_monitor_job_absent_without_config():
    from unittest.mock import MagicMock
    from ops.main import _start_full_scheduler

    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
    )
    try:
        assert sched.get_job("research_monitor") is None
    finally:
        sched.shutdown(wait=False)


def test_research_monitor_tick_records_error_instead_of_raising(tmp_path):
    from unittest.mock import MagicMock
    from ops import events
    from ops.main import _research_monitor_tick

    journal = MagicMock()
    journal.has_event_today.return_value = False
    config = MagicMock()
    config.memo_store_path = str(tmp_path / "nope" / "memos.sqlite")
    # Force a failure inside: monitor_memos will blow up on a MagicMock path
    # or the stores will; either way the tick must swallow and journal it.
    config.screen_store_path = object()  # guaranteed TypeError downstream
    _research_monitor_tick(journal, config)  # must not raise
    kinds = [c.args[0] for c in journal.record_event.call_args_list]
    assert events.KIND_RESEARCH_MONITOR_ERROR in kinds
```

Create `tests/ops/test_cli_research_monitor.py`:

```python
"""Unit tests for `ops research monitor` (monitor core faked)."""

import pytest
from click.testing import CliRunner

import ops.cli as cli_mod
from ops.research.monitor import MonitorOutcome

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "journal.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    return tmp_path


def test_monitor_echoes_summary(env, monkeypatch):
    outcome = MonitorOutcome(
        asof="2026-07-07", memos_checked=3, falsifiers_evaluated=5,
        tripped=1, unevaluable=1, escalations=1, resolution_due=1,
        catalyst_due=0, errors=["WIDG: yahoo exploded"],
    )
    monkeypatch.setattr("ops.research.monitor.monitor_memos", lambda **kw: outcome)
    result = CliRunner().invoke(cli_mod.cli, ["research", "monitor"])
    assert result.exit_code == 0, result.output
    assert "3 memos" in result.output
    assert "1 tripped" in result.output
    assert "yahoo exploded" in result.output


def test_monitor_empty_stores_clean_exit(env):
    # Real stores, real (empty) journal, no fakes: must be a quiet no-op.
    result = CliRunner().invoke(cli_mod.cli, ["research", "monitor"])
    assert result.exit_code == 0, result.output
```

Check the exact env var name for the journal path in `ops/config.py` (`OPS_JOURNAL_PATH` — verify; adapt if different). Run both files — Expected: FAIL.

- [ ] **Step 3: Implement `ops/main.py`.** Next to `_daily_summary_tick`:

```python
def _research_monitor_tick(journal: Journal, config) -> None:
    """Scheduler-safe wrapper around the Phase C memo monitor: gate on the
    run-summary event (restart-safe once-per-day, same pattern as the
    orchestrator's daily_cycle_run), and record errors as events rather than
    raising — raising would kill the APScheduler job."""
    try:
        if journal.has_event_today(events.KIND_RESEARCH_MONITOR_RUN):
            return
        from ops.research.monitor import monitor_memos
        from ops.research.store import ScreenStore
        from tradingagents.memos.store import MemoStore

        monitor_memos(
            memo_store=MemoStore(config.memo_store_path),
            screen_store=ScreenStore(config.screen_store_path),
            journal=journal,
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
        journal.record_event(
            events.KIND_RESEARCH_MONITOR_ERROR,
            events.research_monitor_error_payload(
                error=f"{type(exc).__name__}: {exc}",
            ),
        )
```

In `_start_full_scheduler`, add `config=None` as the final keyword parameter and, after the `daily_summary` registration:

```python
    if config is not None:
        sched.add_job(
            lambda: _research_monitor_tick(journal, config),
            CronTrigger(hour=16, minute=20, day_of_week="mon-fri"),
            id="research_monitor", max_instances=1, misfire_grace_time=300,
        )
```

In `run()`, pass `config=config` at the `_start_full_scheduler` call site (read the call site; the loaded config object is in scope — verify its variable name). Do NOT add the job to `_start_guardian_only` — a reconcile halt stops trading, and monitoring pushes on top of a halted book would be noise while the human is already investigating.

- [ ] **Step 4: Implement the CLI command.** In `ops/cli.py` under the `research` group (lazy imports inside the body, matching `research run`):

```python
@research.command("monitor")
def research_monitor() -> None:
    """Run the daily memo monitor once (falsifiers, drawdown, resolution due)."""
    from ops.journal import Journal
    from ops.research.monitor import monitor_memos
    from ops.research.store import ScreenStore
    from tradingagents.memos.store import MemoStore

    config = load_config()
    with Journal(config.journal_path) as journal:
        from ops import events as ops_events

        if journal.has_event_today(ops_events.KIND_RESEARCH_MONITOR_RUN):
            click.echo("note: a monitor run was already recorded today; running again")
        outcome = monitor_memos(
            memo_store=MemoStore(config.memo_store_path),
            screen_store=ScreenStore(config.screen_store_path),
            journal=journal,
        )
    click.echo(
        f"monitor {outcome.asof}: {outcome.memos_checked} memos, "
        f"{outcome.falsifiers_evaluated} falsifiers ({outcome.tripped} tripped, "
        f"{outcome.unevaluable} unevaluable), {outcome.escalations} escalations, "
        f"{outcome.resolution_due} due for resolution, {outcome.catalyst_due} catalysts due"
    )
    for err in outcome.errors:
        click.echo(f"  error: {err}")
```

Verify `Journal` supports the context-manager form here (it has `__enter__`; `run_screen` uses `with Journal(...)`). Verify `config.journal_path` is the right field name for the daemon journal (read `ops/config.py`).

- [ ] **Step 5: Run tests, full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ops/test_main.py tests/ops/test_cli_research_monitor.py -v && .venv/bin/python -m pytest tests/ -q
ruff check ops/main.py ops/cli.py tests/ops/test_main.py tests/ops/test_cli_research_monitor.py
git add ops/main.py ops/cli.py tests/ops/test_main.py tests/ops/test_cli_research_monitor.py
git commit -m "feat(ops): post-close research-monitor job in the daemon + ops research monitor CLI"
```

---

### Task 6: Automated delisted-position write-off (baseline)

**Files:**
- Modify: `ops/research/baseline.py`, `ops/research/run.py`, `ops/cli.py` (screen output/notify lines only)
- Test: extend `tests/ops/research/test_baseline.py` (read it first — reuse its broker/journal fixtures)

**Interfaces:**
- Consumes: `KIND_BASELINE_QUOTE_FAILURE`, `KIND_BASELINE_AUTO_WRITEOFF` (Task 3); `Journal.count_events`/`read_events`/`last_buy_fill_for`; `PaperBroker.from_journal`; `QuoteUnavailable`.
- Produces:
  - `DELIST_WRITEOFF_RUNS = 3` in `ops/research/baseline.py` — a position is written off after quote failures on this many **consecutive baseline runs** (today's + the 2 most recent prior run asofs), all derived from the journal (no side state).
  - `auto_write_off_delisted(*, journal: Journal, quote_source, starting_cash: Decimal, asof: date, now: datetime | None = None) -> list[dict]` — each dict `{"symbol", "quantity", "price", "failing_runs"}`. Runs BEFORE the main baseline broker is constructed in `run_screen` (the replay then includes the synthetic SELLs — the fresh broker never sees the dead position).
  - `_journal_synthetic_sell(journal, position, price, *, now, coid_prefix) -> None` — the extracted order+fill core shared by `write_off_position` (manual, unchanged behavior + event kind) and the auto path.
  - `run_screen`'s `baseline_summary` dict gains a `"writeoffs": list[str]` key (symbols); the `ops screen` CLI echoes it and appends `", N written off"` to the `--notify` completion body when nonzero.

**Detection rule (binding):** during `auto_write_off_delisted`, every held position gets one `broker.get_quote(symbol)` probe. On `QuoteUnavailable`: journal `KIND_BASELINE_QUOTE_FAILURE` (payload `symbol`, `asof=asof.isoformat()`, `error`). Then, with `prior_asofs` = the payload `asof` values of the last `DELIST_WRITEOFF_RUNS - 1` `KIND_BASELINE_SCREEN_RUN` events (via `journal.read_events()`, filtered by kind, ordered as stored, take the last 2 distinct): if the symbol has a `KIND_BASELINE_QUOTE_FAILURE` event for EVERY prior asof (`count_events(kind, payload_equals={"symbol": s, "asof": prior_asof}) > 0` per asof) — i.e. it failed today and on both prior runs — write it off at the fallback price: `journal.last_buy_fill_for(symbol)["price"]`, else `position.avg_entry_price`. Journal `KIND_BASELINE_AUTO_WRITEOFF` (quantity/price stringified, `failing_runs=DELIST_WRITEOFF_RUNS`, note naming the rule). Fewer than `DELIST_WRITEOFF_RUNS - 1` prior baseline runs → never write off (a brand-new baseline can't have a streak).

The manual `ops research write-off` command keeps working unchanged (spec: "the command remains as override").

- [ ] **Step 1: Read `ops/research/baseline.py` and `tests/ops/research/test_baseline.py` completely.** Confirm: `write_off_position`'s synthetic-SELL block (record_order + record_fill + coid format), `Position.avg_entry_price` and `.quantity` attribute names (check `ops/broker/base.py`), and the test file's fixture style for journals/brokers/quote sources. If the SELL block differs materially from what Task 6 assumes, adapt the extraction — the contract is "manual path behavior unchanged".

- [ ] **Step 2: Failing tests** (append; adapt fixture/helper names to the file):

```python
def test_quote_failure_journaled_and_writeoff_after_three_runs(tmp_path):
    """A position that stops quoting is written off on the 3rd consecutive
    failing run, at the last buy-fill price, with the auto event journaled."""
    journal = Journal(str(tmp_path / "b.sqlite"))
    quotes = {"AAA": Decimal("10"), "DEAD": Decimal("4")}

    def quote_source(symbol):
        if symbol not in quotes:
            raise QuoteUnavailable(symbol)
        return quotes[symbol]

    broker = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("100000"),
    )
    update_baseline_portfolio(broker=broker, journal=journal,
                              passers=["AAA", "DEAD"], asof=date(2026, 6, 20))
    del quotes["DEAD"]  # delisted between runs

    for i, asof in enumerate([date(2026, 6, 27), date(2026, 7, 4), date(2026, 7, 11)]):
        writeoffs = auto_write_off_delisted(
            journal=journal, quote_source=quote_source,
            starting_cash=Decimal("100000"), asof=asof,
        )
        if i < 2:
            assert writeoffs == []
            # keep the baseline-run cadence: each cycle records a screen-run event
            broker = PaperBroker.from_journal(
                journal=journal, quote_source=quote_source,
                starting_cash=Decimal("100000"),
            )
            update_baseline_portfolio(broker=broker, journal=journal,
                                      passers=["AAA"], asof=asof)
    assert [w["symbol"] for w in writeoffs] == ["DEAD"]
    # Fallback price = last buy fill price for DEAD.
    last_buy = journal.last_buy_fill_for("DEAD")
    assert Decimal(writeoffs[0]["price"]) == last_buy["price"]
    kinds = [e["kind"] for e in journal.read_events()]
    assert kinds.count("baseline_quote_failure") == 3
    assert kinds.count("baseline_auto_writeoff") == 1
    # The position is gone from a fresh replay.
    broker = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("100000"),
    )
    assert "DEAD" not in {p.symbol for p in broker.get_positions()}


def test_transient_failure_does_not_write_off(tmp_path):
    """One failing run followed by a healthy run resets nothing permanent —
    the streak rule needs failures on EVERY one of the last 3 run asofs."""
    journal = Journal(str(tmp_path / "b.sqlite"))
    quotes = {"AAA": Decimal("10"), "FLKY": Decimal("4")}

    def quote_source(symbol):
        if symbol not in quotes:
            raise QuoteUnavailable(symbol)
        return quotes[symbol]

    broker = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("100000"),
    )
    update_baseline_portfolio(broker=broker, journal=journal,
                              passers=["AAA", "FLKY"], asof=date(2026, 6, 20))

    del quotes["FLKY"]  # fails on run 2
    assert auto_write_off_delisted(journal=journal, quote_source=quote_source,
                                   starting_cash=Decimal("100000"),
                                   asof=date(2026, 6, 27)) == []
    broker = PaperBroker.from_journal(journal=journal, quote_source=quote_source,
                                      starting_cash=Decimal("100000"))
    update_baseline_portfolio(broker=broker, journal=journal, passers=["AAA"],
                              asof=date(2026, 6, 27))

    quotes["FLKY"] = Decimal("3.5")  # back on run 3: healthy, no failure event
    assert auto_write_off_delisted(journal=journal, quote_source=quote_source,
                                   starting_cash=Decimal("100000"),
                                   asof=date(2026, 7, 4)) == []
    broker = PaperBroker.from_journal(journal=journal, quote_source=quote_source,
                                      starting_cash=Decimal("100000"))
    update_baseline_portfolio(broker=broker, journal=journal, passers=["AAA"],
                              asof=date(2026, 7, 4))

    del quotes["FLKY"]  # fails again on run 4 — but run 3 was healthy
    assert auto_write_off_delisted(journal=journal, quote_source=quote_source,
                                   starting_cash=Decimal("100000"),
                                   asof=date(2026, 7, 11)) == []
```

Add the imports the test file needs (`auto_write_off_delisted`, `QuoteUnavailable`, `PaperBroker`, `Journal`, `update_baseline_portfolio`, `Decimal`, `date`) following the file's existing import block. Run — Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement in `ops/research/baseline.py`.**
  1. Extract the synthetic-SELL core out of `write_off_position` into `_journal_synthetic_sell(journal, position, price, *, now, coid_prefix)` (order row + fill row, coid `f"{coid_prefix}-{now.date().isoformat()}-{position.symbol}-{uuid4().hex[:8]}"`). `write_off_position` calls it with `coid_prefix="baseline-writeoff"` and journals `KIND_BASELINE_WRITEOFF` exactly as before (its tests must stay green untouched).
  2. Add:

```python
DELIST_WRITEOFF_RUNS = 3


def auto_write_off_delisted(
    *,
    journal: Journal,
    quote_source,
    starting_cash: Decimal,
    asof: date,
    now: datetime | None = None,
) -> list[dict]:
    """Write off positions that failed to quote on DELIST_WRITEOFF_RUNS
    consecutive baseline runs (spec Phase C). Consecutiveness is derived
    from the journal — a failure event per failing run asof, checked against
    the last N-1 baseline_screen_run asofs — so there is no counter state to
    lose. Runs BEFORE the main baseline broker is built: the replay after
    this call no longer contains the dead position.
    """
    now = now or datetime.now(timezone.utc)
    broker = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=starting_cash,
    )
    failing: list = []
    for pos in broker.get_positions():
        try:
            broker.get_quote(pos.symbol)
        except QuoteUnavailable as exc:
            journal.record_event(
                events.KIND_BASELINE_QUOTE_FAILURE,
                events.baseline_quote_failure_payload(
                    symbol=pos.symbol, asof=asof.isoformat(), error=str(exc),
                ),
            )
            failing.append(pos)

    if not failing:
        return []
    run_asofs = [
        e["payload"]["asof"]
        for e in journal.read_events()
        if e["kind"] == events.KIND_BASELINE_SCREEN_RUN
    ]
    prior = list(dict.fromkeys(run_asofs))[-(DELIST_WRITEOFF_RUNS - 1):]
    if len(prior) < DELIST_WRITEOFF_RUNS - 1:
        return []  # not enough history for a streak

    written_off: list[dict] = []
    for pos in failing:
        streak = all(
            journal.count_events(
                events.KIND_BASELINE_QUOTE_FAILURE,
                payload_equals={"symbol": pos.symbol, "asof": prior_asof},
            ) > 0
            for prior_asof in prior
        )
        if not streak:
            continue
        last_buy = journal.last_buy_fill_for(pos.symbol)
        price = last_buy["price"] if last_buy else pos.avg_entry_price
        _journal_synthetic_sell(journal, pos, price, now=now,
                                coid_prefix="baseline-auto-writeoff")
        journal.record_event(
            events.KIND_BASELINE_AUTO_WRITEOFF,
            events.baseline_auto_writeoff_payload(
                symbol=pos.symbol, quantity=str(pos.quantity), price=str(price),
                failing_runs=DELIST_WRITEOFF_RUNS,
                note=f"quote failures on {DELIST_WRITEOFF_RUNS} consecutive baseline runs",
            ),
        )
        written_off.append({
            "symbol": pos.symbol, "quantity": str(pos.quantity),
            "price": str(price), "failing_runs": DELIST_WRITEOFF_RUNS,
        })
    return written_off
```

Adapt attribute names (`pos.avg_entry_price`, `pos.quantity`) to the real `Position` dataclass — verify in `ops/broker/base.py` first. Edge case the test pins: today's failure event is journaled BEFORE the streak check, but the streak is decided by the PRIOR runs' events plus today's probe — do not double-count today.

  3. Wire into `run_screen` (`ops/research/run.py`), inside the existing baseline `try` block, before `PaperBroker.from_journal`:

```python
                qs = quote_source or make_yfinance_quote_source()
                writeoffs = auto_write_off_delisted(
                    journal=baseline_journal, quote_source=qs,
                    starting_cash=config.baseline_starting_cash, asof=asof,
                )
                broker = PaperBroker.from_journal(
                    journal=baseline_journal, quote_source=qs,
                    starting_cash=config.baseline_starting_cash,
                )
                baseline_summary = update_baseline_portfolio(
                    broker=broker, journal=baseline_journal,
                    passers=list(passed), asof=asof,
                )
                baseline_summary["writeoffs"] = [w["symbol"] for w in writeoffs]
```

  4. In `ops/cli.py`'s `screen` command: extend the baseline echo line with `f", {len(summary.baseline['writeoffs'])} written off"` (use `.get('writeoffs', [])` defensively) and, in the `--notify` completion body, append `f", {n} written off"` when `n > 0`.

- [ ] **Step 4: Run tests, full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ops/research/test_baseline.py tests/ops/research/test_run.py -v && .venv/bin/python -m pytest tests/ -q
ruff check ops/research/baseline.py ops/research/run.py ops/cli.py tests/ops/research/test_baseline.py
git add ops/research/baseline.py ops/research/run.py ops/cli.py tests/ops/research/test_baseline.py
git commit -m "feat(research): automated delisted-position write-off after 3 failing baseline runs"
```

---

### Task 7: Scheduled research run (`--notify` + Saturday launchd job)

Spec Phase B deferred this to "Phase C job or cron": the weekly screen (Saturday 10:00) fills the pending queue; this job drains it (Saturday 12:00).

**Files:**
- Modify: `ops/cli.py` (`research run` gains `--notify`; new `install-research-service` command), `ops/deploy/__init__.py`
- Create: `ops/deploy/com.tradingagents.research.plist.template`
- Test: extend `tests/ops/test_cli_research_run.py`; extend the deploy render tests (find them: grep `render_screen_plist` under `tests/`)

**Interfaces:**
- `ops research run --notify`: after a completed batch, push a normal-urgency summary (`title="research run complete"`, body `"N researched, M failed, K still pending"`) via the same direct-transport pattern as `screen --notify`; on a batch-aborting exception push `title="research run FAILED"` high-urgency then re-raise. **When there were no pending hits, send nothing** (a quiet week must not push).
- `render_research_plist(*, python_path, repo_dir, log_dir, sec_edgar_user_agent, managed_backend="") -> str` in `ops/deploy/__init__.py`.
- CLI `ops install-research-service` mirroring `install-screen-service`: fail fast if `SEC_EDGAR_USER_AGENT` unset; render with `managed_backend=os.environ.get("OPS_LLM_MANAGED_BACKEND", "")`; default output `~/Library/LaunchAgents/com.tradingagents.research.plist`; print (never run) the launchctl bootstrap/bootout commands.

- [ ] **Step 1: Template.** Copy the screen template's structure exactly (same DOCTYPE/keys/log-path shape), with: label `com.tradingagents.research`, ProgramArguments `{{VENV_PYTHON}} -m ops.cli research run --max-names 3 --notify`, `StartCalendarInterval` Weekday 6 / Hour 12 / Minute 0, comment `<!-- Batch job: NOT a service — no KeepAlive. Saturday 12:00, after the 10:00 screen fills the queue. -->`, env block with `OPS_NOTIFY_ENABLED=1`, `SEC_EDGAR_USER_AGENT={{SEC_EDGAR_USER_AGENT}}`, `OPS_LLM_MANAGED_BACKEND={{OPS_LLM_MANAGED_BACKEND}}`, logs `research.out.log`/`research.err.log`. **Verify first** that `ops/llm_backend.py`'s `load_managed_backend_config` treats an EMPTY `OPS_LLM_MANAGED_BACKEND` string as unmanaged (`kind="none"`); if it raises on empty string instead, render the env key only when `managed_backend` is non-empty (conditional template line — mirror how the repo handles optional plist entries, or assemble the env block in the renderer). STOP → BLOCKED only if neither works cleanly.

- [ ] **Step 2: Failing tests.** For the renderer (in the deploy test file):

```python
def test_render_research_plist_substitutes_everything():
    from ops.deploy import render_research_plist

    rendered = render_research_plist(
        python_path="/venv/bin/python", repo_dir="/repo", log_dir="/logs",
        sec_edgar_user_agent="Fred fred@example.com", managed_backend="ds4",
    )
    assert "com.tradingagents.research" in rendered
    assert "research.out.log" in rendered
    assert "ds4" in rendered
    assert "{{" not in rendered
```

For `--notify` (append to `tests/ops/test_cli_research_run.py`, reusing its `env`/`_seed_hits` fixtures and the `ops.research.brain.research_hit` monkeypatch pattern):

```python
def test_notify_sends_summary_after_batch(env, monkeypatch):
    _seed_hits(env, ["AAA"])
    monkeypatch.setattr(
        "ops.research.brain.research_hit",
        lambda hit, **kw: ResearchOutcome(
            symbol=hit["symbol"], hit_id=hit["id"], status="researched",
            memo_id="m-AAA", recommendation="buy",
        ),
    )
    sent = []

    class FakeTransport:
        def send(self, message):
            sent.append(message)

    monkeypatch.setattr("ops.notify.push.build_push_transport", lambda cfg: FakeTransport())
    result = CliRunner().invoke(cli_mod.cli, ["research", "run", "--notify"])
    assert result.exit_code == 0, result.output
    assert len(sent) == 1
    assert "1 researched" in sent[0].body


def test_notify_silent_when_no_pending_hits(env, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "ops.notify.push.build_push_transport",
        lambda cfg: type("T", (), {"send": lambda self, m: sent.append(m)})(),
    )
    result = CliRunner().invoke(cli_mod.cli, ["research", "run", "--notify"])
    assert result.exit_code == 0
    assert sent == []
```

(The `screen --notify` code imports `build_push_transport` inside the command body — patching the SOURCE module attribute works for the same reason as the brain patches. Confirm `SEC_EDGAR_USER_AGENT` is set by the shared `env` fixture — Task 9 of Phase B added that.) Run — Expected: FAIL.

- [ ] **Step 3: Implement.** Renderer in `ops/deploy/__init__.py` (mirror `render_screen_plist`, add the template-path constant). `--notify` in `research_run`: wrap the existing body's completion with the push (reuse the exact import trio from `screen`'s notify blocks); the failure push goes in an `except Exception` around the batch section that re-raises — read `screen`'s try/except shape and mirror it. `install-research-service` command: copy `install_screen_service`, adjust names/labels/env additions.

- [ ] **Step 4: Run tests, full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ops/test_cli_research_run.py -v && .venv/bin/python -m pytest tests/ -q
ruff check ops/cli.py ops/deploy/__init__.py tests/ops/test_cli_research_run.py
git add ops/cli.py ops/deploy/__init__.py ops/deploy/com.tradingagents.research.plist.template tests/ops/test_cli_research_run.py
# plus the deploy render test file you extended in Step 2 (the file you found via `grep -rl render_screen_plist tests/`) — stage it by its real path
git commit -m "feat(research): --notify on research run + Saturday launchd job (install-research-service)"
```

---

### Task 8: Docs, final review, PR

- [ ] **Step 1: Write `docs/research_monitor.md`:**

```markdown
# Research Monitor Runbook (Phase C — the loop)

Phase C of docs/superpowers/specs/2026-07-06-finish-research-system-design.md.
Open memos are watched mechanically; humans get exceptions. No LLM runs in
the monitor — escalations queue re-research hits for `ops research run`.

## What runs when

| job | where | when | what |
|---|---|---|---|
| research_monitor | ops daemon (APScheduler) | 16:20 ET mon-fri | falsifiers, drawdown, catalysts, resolution-due |
| screen | launchd com.tradingagents.screen | Sat 10:00 | fills the pending queue; auto write-off of delisted baseline names |
| research run | launchd com.tradingagents.research | Sat 12:00 | drains the pending queue into memos |

Manual: `ops research monitor` (safe anywhere; empty stores are a no-op).

## What gets pushed

| event | urgency | trigger |
|---|---|---|
| falsifier_tripped | high | a machine-checkable falsifier held for its consecutive_periods |
| research_escalation | high | falsifier trip or drawdown <= -30% queued a re-research hit |
| resolution_due | normal | expected_holding_months elapsed (memo exit checklist in the body) |
| catalyst_due | normal | a hard-dated event-sleeve catalyst date passed |

Re-notification is deduped per memo/falsifier over a 7-day window (journal
count_events — no side state). Escalations dedupe naturally: a symbol with a
hit already pending is not re-queued.

## Falsifier metrics evaluable today

drawdown_from_cost_pct (split-era-corrected vs entry_price_ref),
gross_margin_pct, revenue_yoy_pct, net_debt_to_ebitda. Anything else is
"unevaluable" — counted in the research_monitor_run summary event, never a
silent pass. consecutive_periods = trading days for price metrics, fiscal
years for fundamental ones.

## Requirements in the daemon environment

Fundamental falsifier checks need SEC_EDGAR_USER_AGENT in the ops daemon
plist env (re-render gotcha: install-service resets the env block — re-merge
creds per RUNBOOK). Without it, price checks still run; fundamental checks
degrade to unevaluable with a note in the run summary.

## Delisted baseline names

Each weekly screen probes every held baseline position once; a quote failure
journals baseline_quote_failure. Three consecutive failing runs write the
position off at the last buy-fill price (baseline_auto_writeoff, surfaced in
the screen's --notify summary). Manual override remains:
`ops research write-off SYMBOL --price P`.

## Inspecting

    sqlite3 ~/.local/state/tradingagents/ops_journal.sqlite \
      "SELECT at, kind, payload FROM events WHERE kind LIKE 'research_%' OR kind LIKE '%falsifier%' ORDER BY id DESC LIMIT 20"
```

Adjust the journal filename in the sqlite example to `OpsConfig.journal_path`'s real default (read `ops/config.py`).

- [ ] **Step 2: Update `docs/long_horizon_research.md`** — mark build-order step 6 done (`6. ✅ Monitoring loop ...` matching the file's checkmark style, adjusting wording to what was built). Add one line to `docs/research_brain.md`'s Running section: escalation hits from the monitor appear in the same pending queue and are researched the same way.

- [ ] **Step 3: Full suite, lint, commit, push, PR**

```bash
.venv/bin/python -m pytest tests/ -q
git add docs/research_monitor.md docs/long_horizon_research.md docs/research_brain.md
git commit -m "docs(research): monitor runbook + build-order step 6 checkmark"
git push -u origin feat/phase-c-loop
gh pr create --repo CWFred/TradingAgents --base main --head feat/phase-c-loop \
  --title "feat(research): phase C — the loop (memo monitor, escalation, auto write-off, scheduled research run)" \
  --body "Implements Phase C of docs/superpowers/specs/2026-07-06-finish-research-system-design.md: falsifier metric evaluators, daily post-close memo monitor in the daemon (+ ops research monitor CLI), re-research escalation queueing, monitoring notify events, automated delisted-baseline write-off, and the Saturday research-run launchd job.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Report the PR URL and WAIT for user review.

- [ ] **Step 4 (USER GATE — deployment).** After merge, the user (not the implementer) redeploys the live worktree and re-renders/loads the new plist; remind them: `install-*` re-renders RESET plist env blocks (Pushover/heartbeat/LLM creds must be re-merged — RUNBOOK Deploy section), and the ops daemon plist needs `SEC_EDGAR_USER_AGENT` added for fundamental falsifier checks.

---

## Verification checklist (after all tasks)

1. `pytest tests/ -q` green on `feat/phase-c-loop` (expect ~30–40 new tests over the 1392 baseline).
2. `ops research monitor` with empty stores prints a zero-count summary, exit 0, and journals exactly one `research_monitor_run` event (safe to run anywhere).
3. Grep discipline: no LLM imports in the monitor path — `grep -rn "llm\|bind_structured\|invoke" ops/research/monitor.py ops/research/metrics.py` returns nothing LLM-related.
4. Notify enforcement test green: every new kind in BUILDERS + exactly one of POLICY/AUDIT_ONLY; falsifier_tripped and research_escalation are push/high, resolution_due and catalyst_due push/normal (spec's exact priorities).
5. The daemon test proves `research_monitor` registers at 16:20 with the has_event_today gate; `_start_guardian_only` does NOT run the monitor.
6. `write_off_position` (manual) behavior unchanged — its pre-existing tests untouched and green.
7. Spec coverage: daily post-close job (T4+T5), falsifiers vs latest facts/prices (T1+T4), calendar catalysts (T4), due_for_resolution surfacing with exit checklist (T4), escalation = queue re-research hit + notify on trip or −30% drawdown (T2+T4), delisted auto write-off replacing the manual default (T6), three spec'd events registered with spec'd priorities (T3), research run scheduled (T7).
8. Phase D hooks intact: `MemoStore.resolve` untouched and ready for the sizing/calibration phase; monitor events carry `memo_id` for future position linkage.
