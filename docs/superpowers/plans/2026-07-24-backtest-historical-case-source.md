# Backtest Historical Case Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the backtest generate a **matured** case corpus by running the screener at sampled historical dates (June 2025 → ~3 months ago), instead of reading only recorded live screen hits (July 2026+, which never mature). Fixes the root cause of the perpetual `INSUFFICIENT — 0 mature cases` verdict.

**Architecture:** The infrastructure already exists in `ops/backtest/cases.py` (`CaseSourceProtocol`, `HistoricalCaseSource`, `CurrentUniverseReconstructionSource`, `sample_sessions`, `collect_candidates`, `select_candidates`). The one missing piece is a `fetch(asof) -> Sequence[CaseCandidate]` that runs the point-in-time screener at a given date. The per-name screener inputs (`ops/research/run.py::_build_screen_inputs` → `ops/research/screener.py::screen_universe`) are already fully as-of parameterized and proven to time-travel to 2025 (fundamentals via EDGAR gate correctly to `asof`; prices via yfinance). We wire that fetcher into a new preparer and select it from `generate_cases` / the CLI.

**Tech Stack:** Python 3, pytest; SQLite (`backtest.sqlite`); yfinance (prices) + EDGAR (fundamentals); `pandas_market_calendars` via `ops/scheduler/market_calendar.py`.

## Global Constraints

- **Strict post-cutoff only.** No case may have `asof < 2025-06-01` (`config.backtest_cutoff` / `MIN_BACKTEST_CUTOFF`). The existing cutoff gate (`ops/backtest/cases.py::validate_cutoff` → `enforce_cutoff`) stays authoritative; never add an override.
- **Point-in-time discipline.** Every per-name input must be dated on-or-before the case `asof`. The screener input builder already enforces this (`compute_fundamentals(..., asof=)` excludes fiscal ends after `asof`; `unadjusted_close_on_or_before(asof)`). Do not weaken it.
- **Universe membership is NOT point-in-time.** `build_smallcap_universe` returns today's members. Screening a 2025 date over it is the spec's **reconstruction** mode. Every case/run produced this way MUST carry `source_mode = RECONSTRUCTION_SOURCE_MODE ("exploratory/current-universe-reconstruction")` so reports never render it as a clean historical screen. This is load-bearing, not cosmetic.
- **Money is `Decimal`** end to end; scores may be `Decimal`.
- **A sweep must never die on one name.** Mirror `_build_screen_inputs`' per-name try/except: a single bad symbol contributes nothing, never raises.
- **No new dependencies.** Reuse existing fetchers and stores.

---

### Task 1: Expose a reusable point-in-time screen over injected inputs

**Files:**
- Modify: `ops/research/run.py`
- Test: `tests/ops/research/test_run.py` (or the existing screen test module — match what's there)

**Interfaces:**
- Consumes: existing `_build_screen_inputs(universe, *, asof, facts_fetcher, triggers_finder, price_context_fetcher)`, `screen_universe(inputs, *, asof)`.
- Produces: `screen_inputs_and_results(universe, *, asof, facts_fetcher, triggers_finder, price_context_fetcher) -> tuple[tuple[NameInputs, ScreenResult], ...]` — a public wrapper returning each name's inputs paired with its screen result, so the backtest module never reaches into a private helper.

- [ ] **Step 1: Write the failing test**

In the research screen test module, add (use a fake 2-name universe + fake fetchers, mirroring existing screen tests — read them first for the fixtures):

```python
def test_screen_inputs_and_results_pairs_inputs_with_results():
    from ops.research.run import screen_inputs_and_results
    universe = _fake_universe(["AAA", "BBB"])          # existing test helper
    pairs = screen_inputs_and_results(
        universe, asof=date(2025, 6, 16),
        facts_fetcher=_fake_facts, triggers_finder=_fake_triggers,
        price_context_fetcher=_fake_price_ctx,
    )
    assert [ni.symbol for ni, _ in pairs] == ["AAA", "BBB"]
    assert all(res.asof == date(2025, 6, 16) for _, res in pairs)
```

- [ ] **Step 2: Run it, verify it fails**

Run: `python -m pytest tests/ops/research/test_run.py -k screen_inputs_and_results -v`
Expected: FAIL — `cannot import name 'screen_inputs_and_results'`.

- [ ] **Step 3: Implement the wrapper**

In `ops/research/run.py`, add:

```python
def screen_inputs_and_results(
    universe, *, asof, facts_fetcher, triggers_finder, price_context_fetcher,
):
    """Public PIT screen returning (NameInputs, ScreenResult) pairs, without
    touching the screen store. Shared by the live screen and the backtest
    historical case source so both use identical input assembly."""
    inputs, _errors = _build_screen_inputs(
        universe, asof=asof, facts_fetcher=facts_fetcher,
        triggers_finder=triggers_finder,
        price_context_fetcher=price_context_fetcher,
    )
    results = screen_universe(inputs, asof=asof)
    return tuple(zip(inputs, results, strict=True))
```

(`screen_universe` returns results in input order; `strict=True` fails loudly if that ever changes.)

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/ops/research/test_run.py -k screen_inputs_and_results -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/research/run.py tests/ops/research/test_run.py
git commit -m "feat(research): public screen_inputs_and_results wrapper for reuse"
```

---

### Task 2: Historical screener candidate fetcher (with per-symbol caching)

**Files:**
- Create: `ops/backtest/historical_source.py`
- Test: `tests/ops/backtest/test_historical_source.py`

**Interfaces:**
- Consumes: `screen_inputs_and_results` (Task 1); `ops.backtest.cases.CaseCandidate`, `RECONSTRUCTION_SOURCE_MODE`; `ops.research.run` fetcher defaults (`get_company_facts`, `find_triggers`, `fetch_price_context`), `ops.universe.smallcap.build_smallcap_universe`.
- Produces: `class ReconstructionScreenerFetcher` with `__call__(self, asof: date) -> tuple[CaseCandidate, ...]`. Fetches each symbol's facts + price context **once** (cached on the instance) and reuses them across every sampled `asof` — so a 26-date sweep over ~1500 names costs ~1500 network fetches total, not 39,000. Returns one `CaseCandidate` per **passing** name, `score = len(inputs.triggers)` (mirrors the live hit score `payload.get("score", len(triggers) or 1)`), `trigger = {"kind": "historical_screener_replay", "asof": asof.isoformat()}`, `source_ref = f"reconstruction:{asof}:{symbol}"`, `screen_payload = {"passed": True, "cheap": r.cheap, "quality": r.quality}`.

- [ ] **Step 1: Write the failing test**

Create `tests/ops/backtest/test_historical_source.py`. Inject a fake universe + fake per-symbol data so there is **no network**:

```python
from datetime import date
from decimal import Decimal

from ops.backtest.cases import RECONSTRUCTION_SOURCE_MODE
from ops.backtest.historical_source import ReconstructionScreenerFetcher


def test_fetcher_returns_passing_candidates_and_caches_per_symbol(monkeypatch):
    calls = {"facts": 0, "price": 0}

    def fake_facts(sym):
        calls["facts"] += 1
        return {"symbol": sym}

    def fake_price_ctx(sym):
        calls["price"] += 1
        return _fake_ctx(sym)              # helper: closes reachable on/before asof

    # a screen that passes only AAA
    def fake_screen(universe, *, asof, facts_fetcher, triggers_finder,
                    price_context_fetcher):
        return _pairs_where_pass(universe, asof, passing={"AAA"})

    fetcher = ReconstructionScreenerFetcher(
        universe=_fake_universe(["AAA", "BBB"]),
        facts_fetcher=fake_facts, price_context_fetcher=fake_price_ctx,
        triggers_finder=lambda s, *, asof: [],
        screen=fake_screen,
    )
    a = fetcher(date(2025, 6, 16))
    b = fetcher(date(2025, 6, 30))

    assert [c.symbol for c in a] == ["AAA"]
    assert a[0].trigger["kind"] == "historical_screener_replay"
    assert a[0].asof == date(2025, 6, 16)
    # per-symbol data fetched once total, reused across both asofs:
    assert calls["facts"] == 2 and calls["price"] == 2  # AAA + BBB, not x2 dates
    assert b[0].asof == date(2025, 6, 30)
```

- [ ] **Step 2: Run it, verify it fails**

Run: `python -m pytest tests/ops/backtest/test_historical_source.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

Create `ops/backtest/historical_source.py`:

```python
"""Reconstruction case source: run the PIT screener at sampled historical
dates over today's universe membership. Labeled RECONSTRUCTION_SOURCE_MODE —
survivorship-biased and never rendered as a clean point-in-time screen.
Per-name facts/prices are fetched once and reused across every sampled asof."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from ops.backtest.cases import CaseCandidate, RECONSTRUCTION_SOURCE_MODE
from ops.research.run import screen_inputs_and_results


@dataclass
class ReconstructionScreenerFetcher:
    universe: Any
    facts_fetcher: Callable[[str], Any]
    price_context_fetcher: Callable[[str], Any]
    triggers_finder: Callable[..., Any]
    screen: Callable[..., Any] = screen_inputs_and_results
    source_mode: str = RECONSTRUCTION_SOURCE_MODE
    _facts: dict[str, Any] = field(default_factory=dict, init=False)
    _ctx: dict[str, Any] = field(default_factory=dict, init=False)

    def _cached_facts(self, symbol: str) -> Any:
        if symbol not in self._facts:
            self._facts[symbol] = self.facts_fetcher(symbol)
        return self._facts[symbol]

    def _cached_ctx(self, symbol: str) -> Any:
        if symbol not in self._ctx:
            self._ctx[symbol] = self.price_context_fetcher(symbol)
        return self._ctx[symbol]

    def __call__(self, asof: date) -> tuple[CaseCandidate, ...]:
        pairs = self.screen(
            self.universe, asof=asof,
            facts_fetcher=self._cached_facts,
            triggers_finder=self.triggers_finder,
            price_context_fetcher=self._cached_ctx,
        )
        out: list[CaseCandidate] = []
        for inputs, result in pairs:
            if not result.passed:
                continue
            out.append(CaseCandidate(
                symbol=inputs.symbol,
                asof=asof,
                score=Decimal(len(inputs.triggers) or 1),
                trigger={"kind": "historical_screener_replay",
                         "asof": asof.isoformat()},
                screen_payload={"passed": True, "cheap": result.cheap,
                                "quality": result.quality},
                source_ref=f"reconstruction:{asof.isoformat()}:{inputs.symbol}",
            ))
        return tuple(out)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/ops/backtest/test_historical_source.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/backtest/historical_source.py tests/ops/backtest/test_historical_source.py
git commit -m "feat(backtest): reconstruction screener case-source fetcher (cached per symbol)"
```

---

### Task 3: Reconstruction preparer wired through the existing case pipeline

**Files:**
- Modify: `ops/backtest/service.py`
- Test: `tests/ops/backtest/test_prepare.py`

**Interfaces:**
- Consumes: `ReconstructionScreenerFetcher` (Task 2); existing `ops.backtest.cases.sample_sessions`, `collect_candidates`, `select_candidates`, `construct_case`, `CaseSource`; `ops.backtest.cases.CurrentUniverseReconstructionSource`; `ops.scheduler.market_calendar.MarketCalendar`; existing `_sealed_context_builder`.
- Produces: `_reconstruction_prepare_cases(*, store, config, sleeve, start, end, case_count, spacing_sessions=10, universe=None, fetcher=None) -> tuple[BacktestCase, ...]` — mirrors `_default_prepare_cases` but sources candidates by sampling sessions in `[start, end]` and running the reconstruction fetcher at each; stamps `CaseSource.RECONSTRUCTION` (add this enum member) on every case. Uses the same `_sealed_context_builder`.

- [ ] **Step 1: Add the `CaseSource.RECONSTRUCTION` enum member**

In `ops/backtest/models.py`, add to `CaseSource`:
```python
    RECONSTRUCTION = "reconstruction"
```
(Keep `LIVE_IMPORT` for the recorded-live path.)

- [ ] **Step 2: Write the failing test**

In `tests/ops/backtest/test_prepare.py` (read its existing fixtures for `store`, fake context builder, and a fake fetcher pattern), add a test that injects a fake fetcher returning known candidates for two sampled dates and asserts cases are inserted with `source == CaseSource.RECONSTRUCTION` and as-of dates within the window:

```python
def test_reconstruction_prepare_inserts_matured_cases(tmp_path, monkeypatch):
    # fake fetcher: 2 passing names per sampled date
    # fake sessions calendar: sessions every day in window
    cases = _reconstruction_prepare_cases(
        store=store, config=config, sleeve="research",
        start=date(2025, 6, 2), end=date(2025, 7, 1), case_count=4,
        spacing_sessions=10, universe=_fake_universe([...]),
        fetcher=_fake_fetcher,           # instance with __call__(asof)
    )
    assert len(cases) == 4
    assert {c.source for c in cases} == {CaseSource.RECONSTRUCTION}
    assert all(date(2025, 6, 2) <= c.asof <= date(2025, 7, 1) for c in cases)
```

- [ ] **Step 3: Run it, verify it fails**

Run: `python -m pytest tests/ops/backtest/test_prepare.py -k reconstruction -v`
Expected: FAIL — `_reconstruction_prepare_cases` not defined.

- [ ] **Step 4: Implement**

In `ops/backtest/service.py`, add (place beside `_default_prepare_cases`):

```python
def _reconstruction_prepare_cases(
    *, store, config, sleeve, start, end, case_count,
    spacing_sessions=10, universe=None, fetcher=None,
):
    from ops.backtest.cases import (
        CurrentUniverseReconstructionSource, collect_candidates, sample_sessions,
        select_candidates,
    )
    from ops.backtest.historical_source import ReconstructionScreenerFetcher
    from ops.scheduler.market_calendar import MarketCalendar

    if fetcher is None:
        from tradingagents.dataflows import edgar
        from tradingagents.dataflows.edgar_facts import get_company_facts
        from ops.research.run import fetch_price_context
        from ops.research.triggers import find_triggers
        from ops.universe.smallcap import build_smallcap_universe

        edgar.get_user_agent()  # fail fast, same as run_screen
        universe = universe if universe is not None else build_smallcap_universe()
        fetcher = ReconstructionScreenerFetcher(
            universe=universe, facts_fetcher=get_company_facts,
            price_context_fetcher=fetch_price_context,
            triggers_finder=find_triggers,
        )

    sessions = MarketCalendar().sessions_between(start, end)   # see Step 4a
    sampled = sample_sessions(sessions, start=start, end=end,
                              spacing_sessions=spacing_sessions)
    source = CurrentUniverseReconstructionSource(fetch=fetcher)
    candidates = collect_candidates(source, sampled)
    selected = select_candidates(
        candidates, target_count=case_count,
        per_date_cap=max(1, case_count // max(1, len(sampled))),
    )
    prepared = []
    context_builder = _sealed_context_builder(config)
    for candidate in selected:
        case = construct_case(candidate, sleeve=sleeve,
                              cutoff=store.effective_cutoff,
                              source=CaseSource.RECONSTRUCTION)
        manifest = context_builder(case, candidate)
        store.insert_case(case)
        store.save_context_manifest(manifest)
        prepared.append(case)
    if not prepared:
        raise MissingBacktestArtifacts(
            f"reconstruction screen produced no passing cases in {start}..{end}")
    return tuple(prepared)
```

- [ ] **Step 4a: Ensure `MarketCalendar.sessions_between(start, end) -> list[date]` exists**

If `ops/scheduler/market_calendar.py` lacks it, add it (it already wraps `self._cal.schedule(start_date=, end_date=)`):
```python
    def sessions_between(self, start: date, end: date) -> list[date]:
        sched = self._cal.schedule(start_date=start, end_date=end)
        return [ts.date() for ts in sched.index]
```
Add a focused test in `tests/ops/scheduler/test_market_calendar.py` asserting it returns trading days only (excludes a known weekend/holiday).

- [ ] **Step 5: Run tests, verify pass**

Run: `python -m pytest tests/ops/backtest/test_prepare.py -k reconstruction tests/ops/scheduler/test_market_calendar.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ops/backtest/service.py ops/backtest/models.py ops/scheduler/market_calendar.py tests/ops/backtest/test_prepare.py tests/ops/scheduler/test_market_calendar.py
git commit -m "feat(backtest): reconstruction preparer over sampled historical sessions"
```

---

### Task 4: Select the source from `generate_cases` and the CLI

**Files:**
- Modify: `ops/backtest/service.py` (`generate_cases`)
- Modify: `ops/cli.py` (`backtest generate`)
- Test: `tests/ops/backtest/test_generate.py`, `tests/ops/backtest/test_cli.py`

**Interfaces:**
- Produces: `generate_cases(..., source: str = "recorded")` selecting the preparer; a `--source [recorded|reconstruction]` CLI option on `backtest generate` (default `recorded` to preserve current behavior; `reconstruction` invokes the new path). The chosen `source_mode` is recorded so downstream (run metadata/reports) can label it.

- [ ] **Step 1: Write failing tests**

`test_generate.py`: `generate_cases(..., source="reconstruction", preparer=<fake>)` calls the reconstruction preparer, not `_default_prepare_cases`. `test_cli.py`: `backtest generate --source reconstruction --start 2025-06-02 --end 2025-10-01` reaches `generate_cases` with `source="reconstruction"` (patch `generate_cases`, assert kwarg). Read both test files for their existing invocation/patch patterns first.

- [ ] **Step 2: Run, verify fail**

Run: `python -m pytest tests/ops/backtest/test_generate.py tests/ops/backtest/test_cli.py -k "reconstruction or source" -v`
Expected: FAIL (unknown `source` kwarg / option).

- [ ] **Step 3: Implement**

In `generate_cases`, add `source: str = "recorded"` and select the preparer:
```python
        if not available:
            if preparer is not None:
                prepare = preparer
            elif source == "reconstruction":
                prepare = _reconstruction_prepare_cases
            elif source == "recorded":
                prepare = _default_prepare_cases
            else:
                raise InvalidBacktestRequest(f"unknown case source {source!r}")
            prepare(store=store, config=config, sleeve=sleeve,
                    start=start, end=end, case_count=case_count)
```
In the CLI `backtest generate`, add:
```python
@click.option("--source", type=click.Choice(["recorded", "reconstruction"]),
              default="recorded", show_default=True,
              help="recorded live hits (default) or historical reconstruction screen.")
```
and thread `source=source` into `generate_cases(...)`.

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/ops/backtest/test_generate.py tests/ops/backtest/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/backtest/service.py ops/cli.py tests/ops/backtest/test_generate.py tests/ops/backtest/test_cli.py
git commit -m "feat(backtest): --source recorded|reconstruction selector for generate"
```

---

### Task 5: Small live validation sweep + full-suite gate

**Files:** none (operational); may add one `--limit`-style guard if missing.

- [ ] **Step 1: Full backtest + research suites green**

Run: `python -m pytest tests/ops/backtest/ tests/ops/research/test_run.py tests/ops/scheduler/test_market_calendar.py -q`
Expected: all PASS.

- [ ] **Step 2: Tiny real reconstruction sweep (bounded universe)**

Confirm `build_smallcap_universe` supports a bounded run for validation (the `run_screen` path already accepts a `limit`). If `_reconstruction_prepare_cases` needs a `--limit`, pass a small `universe=build_smallcap_universe()[:25]` via a one-off. With `SEC_EDGAR_USER_AGENT` set (from the ops plist), run a **single sampled date** reconstruction over ~25 names into a scratch `backtest.sqlite` (never the live store — point `OPS_BACKTEST_STORE`/config at a tmp path):

```bash
SEC_EDGAR_USER_AGENT="<from ops plist>" \
  OPS_BACKTEST_STORE=/tmp/bt-validate.sqlite \
  python -m ops.cli backtest generate --source reconstruction \
    --start 2025-06-02 --end 2025-06-16 --cases 30
```
Expected: cases inserted with `asof` in June 2025, `source=reconstruction`. Then:
```bash
OPS_BACKTEST_STORE=/tmp/bt-validate.sqlite \
  python -m ops.cli backtest run --start 2025-06-02 --end 2025-06-16
```
Expected: report renders with cases now **priceable and (for the 5/21-day horizons) maturing** — i.e., no longer uniformly `unpriceable/pending`. Capture the verdict line as evidence.

- [ ] **Step 3: Commit any validation-support change**

```bash
git add -A && git commit -m "test(backtest): bounded reconstruction validation support"
```

---

## Notes for the executor

- **Survivorship caveat is intentional and must stay visible.** These cases use today's universe membership; that is why `source_mode` is `RECONSTRUCTION_SOURCE_MODE` and `CaseSource.RECONSTRUCTION`. Do not relabel to point-in-time. A future task can add a true historical-membership source (needs a dated universe snapshot we do not have yet); it would slot in as a `HistoricalCaseSource` with `source_mode = HISTORICAL_SOURCE_MODE`.
- **Efficiency:** per-symbol facts/prices are cached on the fetcher instance and reused across all sampled dates — do not re-create the fetcher per date.
- **Do not touch** the recorded-live path (`_screen_hit_source` / `_default_prepare_cases`); it stays as the `recorded` source for forward accumulation.
- **The run phase is still manual** (`ops backtest run`); automating it is out of scope for this fix (separate follow-up).
- Confirm exact fixture/helper names in `tests/ops/backtest/test_prepare.py`, `test_generate.py`, `test_cli.py`, and the research screen test module before writing tests — reuse their existing fakes rather than inventing new ones.
