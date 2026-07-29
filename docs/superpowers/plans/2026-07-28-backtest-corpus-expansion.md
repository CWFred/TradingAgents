# Backtest Corpus Expansion v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Grow the reconstruction corpus from 40 to 80 cases over the same June 2025–March 2026 window — denser date sampling (5 sessions), near-miss control cases, top-up prepare that respects the existing 40 — plus wider sealed price history and a point-in-time-universe feasibility spike.

**Architecture:** All changes extend the existing reconstruction path (`service._reconstruction_prepare_cases`, `historical_source.ReconstructionScreenerFetcher`, `price_backfill.backfill_prices`) without touching the recorded-live path or replay. The 40 existing cases and their frozen memos are preserved; expansion adds new `(symbol, asof)` pairs only.

**Tech Stack:** Python 3, pytest, Click, SQLite (`backtest.sqlite`), yfinance, EDGAR.

## Global Constraints

- **Cutoff `2025-06-01` is untouchable** (`MIN_BACKTEST_CUTOFF`, `enforce_cutoff`); no override, ever.
- **Existing cases/memos are immutable** — expansion must never modify or duplicate an existing `(symbol, asof)` case; frozen memos stay cached.
- **Reconstruction stays labeled** `exploratory/current-universe-reconstruction`; near-miss controls are additionally distinguishable via `trigger["kind"] == "near_miss_control"`.
- **Zero-network tests** with injected fakes, mirroring `tests/ops/backtest/` conventions.
- **Fail closed** on missing prices at manifest sealing (guard added 2026-07-27; do not weaken).
- Run `python -m ruff check` on touched files before each commit.

---

### Task 1: Widen the case-count ceiling to 100

**Files:**
- Modify: `ops/backtest/service.py` (`_validate_window`, ~line 208)
- Test: `tests/ops/backtest/test_generate.py`

**Interfaces:**
- Produces: `_validate_window` accepting `30 <= case_count <= 100` (was 50). Later tasks rely on `case_count=80` passing validation.

- [ ] **Step 1: Write the failing test**

```python
def test_window_allows_up_to_100_cases():
    from ops.backtest.service import InvalidBacktestRequest, _validate_window
    _validate_window(start=date(2025, 6, 2), end=date(2026, 3, 31),
                     today=date(2026, 7, 28), cutoff=date(2025, 6, 1),
                     case_count=80)          # must not raise
    with pytest.raises(InvalidBacktestRequest):
        _validate_window(start=date(2025, 6, 2), end=date(2026, 3, 31),
                         today=date(2026, 7, 28), cutoff=date(2025, 6, 1),
                         case_count=101)
```

- [ ] **Step 2: Run it** — `python -m pytest tests/ops/backtest/test_generate.py -k allows_up_to_100 -v` → FAIL (80 raises today).
- [ ] **Step 3: Implement** — in `_validate_window`, change the upper bound check and its message from `50` to `100`. Read the existing check first and keep its exact error-message style.
- [ ] **Step 4: Run** the whole file — `python -m pytest tests/ops/backtest/test_generate.py -q` → PASS.
- [ ] **Step 5: Commit** — `feat(backtest): allow up to 100 cases per window`

---

### Task 2: `--spacing` option threaded to session sampling

**Files:**
- Modify: `ops/backtest/service.py` (`generate_cases`, `_reconstruction_prepare_cases`), `ops/cli.py` (`backtest generate`)
- Test: `tests/ops/backtest/test_cli.py`, `tests/ops/backtest/test_prepare.py`

**Interfaces:**
- Consumes: `_reconstruction_prepare_cases(..., spacing_sessions=10)` (already parameterized).
- Produces: `generate_cases(..., spacing_sessions: int = 10)` forwarding to the reconstruction preparer only; CLI `--spacing N` (default 10). Task 7 runs with `--spacing 5`.

- [ ] **Step 1: Write the failing tests**

```python
# test_cli.py — mirror the existing generate-CLI patch pattern
def test_generate_cli_passes_spacing(monkeypatch):
    captured = {}
    monkeypatch.setattr("ops.backtest.service.generate_cases",
                        lambda **kw: captured.update(kw) or _EMPTY_RESULT)
    _invoke_cli(["backtest", "generate", "--source", "reconstruction",
                 "--start", "2025-06-02", "--end", "2026-03-31",
                 "--spacing", "5"])
    assert captured["spacing_sessions"] == 5
```

```python
# test_prepare.py / test_generate.py — generate_cases forwards spacing to the preparer
def test_generate_cases_forwards_spacing_to_preparer(tmp_path):
    seen = {}
    def preparer(**kw):
        seen.update(kw); raise SystemExit  # stop before generation planning
    with pytest.raises(SystemExit):
        generate_cases(config=_config(tmp_path), sleeve="research",
                       start=date(2025, 6, 2), end=date(2026, 3, 31),
                       case_count=40, today=date(2026, 7, 28),
                       source="reconstruction", preparer=preparer,
                       spacing_sessions=5)
    assert seen["spacing_sessions"] == 5
```

- [ ] **Step 2: Run, verify FAIL** (unknown kwarg / option).
- [ ] **Step 3: Implement** — add `spacing_sessions: int = 10` to `generate_cases`; pass it in the `prepare(...)` call (harmless extra kwarg for `_default_prepare_cases`? NO — recorded path does not accept it; forward it only when `source == "reconstruction"`, i.e. build `prepare_kwargs = {"spacing_sessions": spacing_sessions} if source == "reconstruction" or preparer is not None else {}` and splat it). Add the Click option `--spacing` with `type=int, default=10, show_default=True` and thread `spacing_sessions=spacing`.
- [ ] **Step 4: Run** both test files → PASS.
- [ ] **Step 5: Commit** — `feat(backtest): --spacing option for reconstruction date density`

---

### Task 3: Top-up prepare (`--append`) with (symbol, asof) dedupe

**Files:**
- Modify: `ops/backtest/service.py` (`generate_cases`, `_reconstruction_prepare_cases`), `ops/cli.py`
- Test: `tests/ops/backtest/test_prepare.py`, `tests/ops/backtest/test_cli.py`

**Interfaces:**
- Consumes: `store.list_cases(sleeve=)`, `cases.select_candidates(candidates, target_count=, per_date_cap=)`.
- Produces: `generate_cases(..., append: bool = False)`: when `append=True` and the window already holds cases, the reconstruction preparer runs with `case_count = requested - existing` and skips any candidate whose `(symbol, asof)` already exists. CLI flag `--append`. Existing behavior (skip prepare when cases exist) unchanged when `append=False`.

- [ ] **Step 1: Write the failing test**

```python
def test_append_prepare_tops_up_and_never_duplicates(tmp_path):
    # seed store with 2 cases via existing fixtures (symbols AAA/BBB @ 2025-06-16)
    # fake fetcher offers AAA (duplicate) and CCC (new) on the same date
    cases = _reconstruction_prepare_cases(
        store=store, config=config, sleeve="research",
        start=date(2025, 6, 2), end=date(2025, 7, 1), case_count=1,
        spacing_sessions=10, universe=_fake_universe(["AAA", "CCC"]),
        fetcher=_fake_fetcher, existing=(("AAA", date(2025, 6, 16)),),
    )
    assert [c.symbol for c in cases] == ["CCC"]     # duplicate skipped
```

- [ ] **Step 2: Run, verify FAIL** (`existing` kwarg unknown).
- [ ] **Step 3: Implement** — `_reconstruction_prepare_cases` gains `existing: Collection[tuple[str, date]] = ()`; after `collect_candidates`, filter `candidates = [c for c in candidates if (c.normalized_symbol(), c.asof) not in set(existing)]` before `select_candidates`. In `generate_cases`, replace the `if not available:` gate with:

```python
        need_prepare = (not available) or (append and len(available) < case_count)
        if need_prepare:
            ...
            prepare(store=store, config=config, sleeve=sleeve,
                    start=start, end=end,
                    case_count=case_count - len(available),
                    existing=tuple((c.symbol, c.asof) for c in available),
                    **prepare_kwargs)
```

(`_default_prepare_cases` keeps its signature; guard as in Task 2 so `existing`/`spacing_sessions` only flow to the reconstruction preparer. `append` with `source="recorded"` raises `InvalidBacktestRequest("append is reconstruction-only")`.) Add the `--append` Click flag and thread it.
- [ ] **Step 4: Run** `python -m pytest tests/ops/backtest/test_prepare.py tests/ops/backtest/test_cli.py tests/ops/backtest/test_generate.py -q` → PASS.
- [ ] **Step 5: Commit** — `feat(backtest): --append top-up prepare with (symbol,asof) dedupe`

---

### Task 4: Near-miss control cases

**Files:**
- Modify: `ops/backtest/historical_source.py`, `ops/backtest/service.py`, `ops/cli.py`
- Test: `tests/ops/backtest/test_historical_source.py`, `tests/ops/backtest/test_prepare.py`, `tests/ops/backtest/test_cli.py`

**Interfaces:**
- Consumes: `ScreenResult` fields `passed, cheap, quality` and `NameInputs.triggers` (a name passes when `cheap AND quality AND len(triggers) >= 1` — see `ops/research/screener.py:197`).
- Produces: `ReconstructionScreenerFetcher(..., include_near_misses: bool = False)`; when enabled, `__call__` ALSO emits candidates for names failing **exactly one** of the three conditions, with `trigger={"kind": "near_miss_control", "asof": ..., "failed": "<cheap|quality|trigger>"}`, `source_ref=f"nearmiss:{asof}:{symbol}"`, and the full `_result_payload`. `_reconstruction_prepare_cases(..., controls_count: int = 0)` selects passers and controls **separately** (controls never crowd out passers); CLI `--controls N` (default 0).

- [ ] **Step 1: Write the failing fetcher test**

```python
def test_near_miss_controls_emitted_and_flagged():
    def fake_screen(universe, *, asof, facts_fetcher, triggers_finder,
                    price_context_fetcher):
        return (
            (_FakeInputs("PASS1", triggers=("t",)), _FakeResult(passed=True)),
            # cheap+quality but zero triggers -> near miss (failed=trigger)
            (_FakeInputs("NM1"), _FakeResult(passed=False)),
            # fails two conditions -> NOT a near miss
            (_FakeInputs("FAR"), _FakeResult(passed=False, cheap=False, quality=False)),
        )
    fetcher = ReconstructionScreenerFetcher(
        universe=("PASS1", "NM1", "FAR"),
        facts_fetcher=lambda s: {}, price_context_fetcher=lambda s: {},
        triggers_finder=lambda s, *, asof: [], screen=fake_screen,
        include_near_misses=True,
    )
    out = fetcher(date(2025, 6, 16))
    kinds = {c.symbol: c.trigger["kind"] for c in out}
    assert kinds == {"PASS1": "historical_screener_replay",
                     "NM1": "near_miss_control"}
    nm = next(c for c in out if c.symbol == "NM1")
    assert nm.trigger["failed"] == "trigger"
    assert nm.source_ref == "nearmiss:2025-06-16:NM1"
```

- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement the fetcher** — in `__call__`, replace the `if not result.passed: continue` loop body:

```python
        for inputs, result in pairs:
            conditions = {
                "cheap": bool(result.cheap),
                "quality": bool(result.quality),
                "trigger": len(inputs.triggers) >= 1,
            }
            if result.passed:
                out.append(self._candidate(inputs, result, asof,
                                           kind="historical_screener_replay"))
            elif self.include_near_misses and sum(conditions.values()) == 2:
                failed = next(k for k, ok in conditions.items() if not ok)
                out.append(self._candidate(inputs, result, asof,
                                           kind="near_miss_control", failed=failed))
```

with a `_candidate(self, inputs, result, asof, *, kind, failed=None)` helper building the `CaseCandidate` (score `Decimal(len(inputs.triggers) or 1)`, trigger dict `{"kind": kind, "asof": asof.isoformat()}` plus `{"failed": failed}` when set, `source_ref` prefix `reconstruction:`/`nearmiss:` by kind, payload `_result_payload(result, inputs.symbol, asof)`).
- [ ] **Step 4: Write the failing preparer test** — `_reconstruction_prepare_cases(..., controls_count=2, fetcher=<fake emitting 3 passers + 3 controls per date>)` returns `case_count` passers plus exactly 2 cases whose `trigger["kind"] == "near_miss_control"`; selection of passers is unaffected by controls.
- [ ] **Step 5: Implement the preparer** — split candidates by `trigger["kind"]`; run `select_candidates` twice (passers → `target_count=case_count`; controls → `target_count=controls_count`); insert both through the same `construct_case`/manifest/store loop. Also filter both against `existing` (Task 3). Thread `controls_count` through `generate_cases` (reconstruction-only, like `spacing_sessions`) and add CLI `--controls` (`type=int, default=0`). The fetcher is constructed with `include_near_misses=controls_count > 0`.
- [ ] **Step 6: Run** all four test files → PASS.
- [ ] **Step 7: Commit** — `feat(backtest): near-miss control cases (--controls N)`

---

### Task 5: Wider sealed price history (400-day lookback)

**Files:**
- Modify: `ops/backtest/price_backfill.py` (window start `min_asof - 10 days` → `min_asof - 400 days`)
- Test: `tests/ops/backtest/test_price_backfill.py`

**Interfaces:**
- Consumes: `backfill_prices(config, store, *, sleeve, start, end, fetcher=, today=)` and its per-symbol window computation.
- Produces: per-symbol fetch windows starting `min_asof - 400 days` so manifests sealed afterwards carry ~13 months of closes (live memos see 6y but only consume the reference price; 400d future-proofs momentum/drawdown prompt additions without 6y of storage).

- [ ] **Step 1: Write the failing test** — extend the existing window-assertion test (read it first; it asserts the fetch start): expected start becomes `min_asof - timedelta(days=400)`.
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement** — change the `timedelta(days=10)` constant to `timedelta(days=400)`, rename any local like `PRE_ASOF_PAD = timedelta(days=400)` with a comment: sealed manifests inherit whatever the cache holds before asof; 400d ≈ 13 months of context.
- [ ] **Step 4: Run** the file → PASS.
- [ ] **Step 5: Commit** — `feat(backtest): 400-day pre-asof price lookback for richer sealed context`

---

### Task 6: PIT-universe feasibility spike (research, no production code)

**Files:**
- Create: `docs/superpowers/specs/2026-07-28-pit-universe-feasibility.md` (findings memo)
- Create: `/private/tmp/.../scratchpad` scripts only — nothing in `ops/`.

Survivorship context motivating this: US delistings run ~7–10%/yr (higher in small caps); a cheapness screen preferentially catches the distressed slice of them, so the missing names are disproportionately losers.

- [ ] **Step 1:** Build a list of ≥15 US small-caps delisted between 2025-06-01 and today (bankruptcies + take-privates + compliance delistings; sources: SEC 25/25-NSE filings via EDGAR full-text search, news).
- [ ] **Step 2:** For each, test `yfinance` daily-bar coverage of the pre-delisting period (the exact `yf.Ticker(t).history(start=..., end=..., auto_adjust=False, actions=True)` call `price_fetch.py` uses). Record: full history / truncated / absent.
- [ ] **Step 3:** For each, test EDGAR data presence (CIK resolvable? companyfacts JSON alive? filings listable?) — EDGAR retains dead companies; confirm the screener's inputs would have been buildable.
- [ ] **Step 4:** Write the findings memo: (a) % of delisted names with usable prices+facts, (b) the implied corpus fix (e.g. "EDGAR-derived membership + yfinance where covered + `PriceCache.mark_state(TERMINAL)` for the rest"), (c) effort estimate, (d) recommendation: build `HistoricalCaseSource` now / later / never.
- [ ] **Step 5: Commit** the memo — `docs: PIT-universe feasibility findings`

---

### Task 7: Operational expansion run (after Tasks 1–5 merge)

**Files:** none (runbook).

- [ ] **Step 1:** Full suite green: `python -m pytest tests/ops/backtest tests/ops/research -q`; merge the branch to main (PR; user merges) so daemon and CLI converge.
- [ ] **Step 2:** Expansion sweep (screening only, ~14h — schedule overnight; per-symbol cache makes densification cheaper than run 1):

```bash
SEC_EDGAR_USER_AGENT="<ops plist UA>" python -m ops.cli backtest generate \
  --source reconstruction --start 2025-06-01 --end 2026-03-31 \
  --cases 80 --spacing 5 --controls 20 --append --enqueue
```

Expected: ~40 new cases inserted (top-up to 80: ~20 new passers + 20 controls), 40 existing untouched, ~40 new jobs enqueued.
- [ ] **Step 3:** `python -m ops.cli backtest prices --start 2025-06-01 --end 2026-03-31` (now also backfills the 400d lookback for all symbols — expect a much larger bar count).
- [ ] **Step 4:** Drain memos (daemon overnight, or foreground `--execute` off-hours), then `backtest run` and compare the new verdict + calibration against run `backtest-2026-07-28-feae69ce647f`. Passers and controls are separable in analysis via `trigger.kind`.

## Self-review notes

- User directives covered: spacing 5 (Task 2/7), near-miss controls (Task 4/7), 80 cases (Tasks 1/3/7), corpus-before-Phase-1 (this plan is standalone), survivorship fix path (Task 6 spike; full `HistoricalCaseSource` deliberately deferred to its own plan pending findings).
- Backtest↔live alignment: payload parity landed 2026-07-27; Task 5 closes the remaining price-context gap for anything beyond reference price. Full alignment item remaining after this plan: live-import (roadmap Phase 2).
- Type consistency: `spacing_sessions`, `existing`, `controls_count` names match across Tasks 2–4; `prepare_kwargs` guard pattern shared by Tasks 2–3.
