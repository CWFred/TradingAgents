# Phase A: Consolidate, Verify, Deploy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One reconciled codebase running both sleeves; the daemon isolated from the dev checkout; data-feed failures loud instead of silent; data coverage measured; the research screen running on a schedule.

**Architecture:** First a semantic merge of `origin/main` (momentum sleeve + exit engine) into the research branch (screener + ds4 backend lifecycle), then seven additive hardening tasks on a fresh branch, then an operational deploy/calibration task. The one shared new mechanism is `ops/universe/yf_pacing.py` — a process-global pace/retry/counter choke point that every batch yfinance fetcher routes through; the blindness alarm and coverage telemetry both read from it or from per-bar results.

**Tech Stack:** Python 3.10+, stdlib, yfinance, click, pytest (all network mocked), launchd (macOS).

**Spec:** `docs/superpowers/specs/2026-07-06-finish-research-system-design.md` (Phase A sections A1–A8). Read it once before starting.

## Global Constraints

- Work happens in `/Users/frednick/Code/TradingAgents`. Task 1 works on branch `claude/smallcap-research-coverage-dervpt` and ends with PR #12 merged into `main`. Tasks 2–9 happen on a new branch `feat/phase-a-hardening` cut from the updated `main`. Never commit to `main` directly.
- The working tree has unrelated user-modified files (`main.py` at repo root, `tradingagents/dataflows/reddit.py`) — NEVER `git add` them; always stage explicit file lists, never `git add -A` or `git add .`.
- Lint: `ruff check <files you touched>` must pass (line-length 100, py310+). `ruff check ops/` has ~26 PRE-EXISTING errors in untouched files — those are not yours to fix.
- Tests: pytest, new test modules set `pytestmark = pytest.mark.unit`, ALL network mocked/injected. Full suite must be green before every commit that claims completion: `pytest tests/ -q` (baseline after merge: expect ≥1146 passed; skips are pre-existing opt-in live tests).
- Money math in `Decimal`; convert at I/O boundaries with `Decimal(str(x))`.
- New event kinds MUST be added to `ops/events.py` `BUILDERS` and to either the notify `POLICY` (`ops/notify/policy.py`) or `AUDIT_ONLY` — `tests/ops/notify/test_policy.py` enforces this and will fail otherwise.
- Never run `launchctl` commands without explicit user confirmation in that step (Task 9 only).
- **Escalation rule for the implementer:** if a merge conflict appears in a file NOT listed in Task 1, or any instruction contradicts what you find in the code, STOP and report BLOCKED with the details. Do not improvise.

## File structure (what this plan touches)

| File | Task | Responsibility |
|---|---|---|
| (merge of 8 both-sides files) | 1 | one codebase, two sleeves |
| `ops/universe/yf_pacing.py` (new) | 2 | global pace + retry + ok/failed counters for batch yfinance I/O |
| `ops/universe/earnings.py`, `ops/universe/momentum.py`, `ops/universe/filters.py`, `ops/research/prices.py` | 2 | route their yfinance calls through the pacing helper |
| `ops/events.py`, `ops/notify/policy.py`, `ops/scheduler/orchestrator.py` | 3 | `universe_diagnostics` + `universe_blind` events, emitted per daily cycle |
| `ops/research/prices.py`, `ops/research/run.py` | 4 | split-unadjusted year-end closes for the P/E-history bar |
| `ops/research/run.py`, `ops/research/store.py`, `ops/cli.py` | 5 | per-bar coverage telemetry + `--notify` on `ops screen` |
| `ops/research/baseline.py`, `ops/cli.py`, `ops/events.py` | 6 | manual delisted write-off command |
| `ops/deploy/`, `ops/cli.py` | 7 | weekly screen launchd job |
| `ops/cli.py` | 8 | decide-once uses the composite universe |
| `~/Code/TradingAgents-live`, plists, `docs/RUNBOOK-paper-golive.md`, `docs/research_screener.md` | 9 | deploy isolation, calibration run, docs |

---

### Task 1: Merge main into the research branch, merge PR #12

**Files (conflicts expected in exactly these — anything else: STOP, report BLOCKED):**
- `ops/scheduler/orchestrator.py` (semantic: main rewrote the tick; research wrapped analysis in a backend session)
- `ops/main.py` (semantic: main swapped in the composite universe; research added managed-backend wiring)
- `ops/config.py`, `ops/events.py`, `ops/cli.py`, `tests/ops/test_config.py`, `tests/ops/test_main.py`, `tests/ops/scheduler/test_orchestrator.py` (mechanical: both sides ADDED disjoint things; resolution = union, keep both)

**Context you must know:** `main` gained the momentum sleeve (PR #11): once-daily cycle gate, momentum leaderboard, exit engine, composite universe, 7 new event kinds. The research branch gained: the whole `ops/research/` package (no conflicts — main never touched it), 3 config fields + 2 event kinds + the `screen` CLI command, and (user commit e3c21de) the ds4 managed-backend lifecycle: `ops/llm_backend.py`, `TradingAgentsPipelineAdapter(backend=...)` with a `.session()` context manager, orchestrator wrapping its analysis batch in that session, `_wire`/`_startup`/`run()` threading a `backend` object through.

- [ ] **Step 1: Start the merge**

```bash
cd /Users/frednick/Code/TradingAgents
git checkout claude/smallcap-research-coverage-dervpt
git fetch origin main
git merge origin/main
```

Expected: conflict list naming (a subset of) the 8 files above.

- [ ] **Step 2: Resolve `ops/scheduler/orchestrator.py`**

Take MAIN's version of the whole file as the base (it was heavily rewritten: daily-cycle gate, leaderboard, exits, composite universe, `position_opened` events), then re-apply the research side's single change: wrap the analysis batch in the pipeline session. In main's `_tick_impl`, the tail currently reads:

```python
        fresh_candidates = [c for c in candidates if c.symbol not in held]
        current_equity = self._broker.get_equity()
        live_cap = self._compute_live_cap()
        proposals = self._strategy.propose_orders(
            ...
```

Replace from `proposals = self._strategy.propose_orders(` through the end of the `for proposal in proposals:` loop (INCLUDING the `position_opened` journaling inside that loop) with the same code indented one level under a `with` block, preceded by the research side's comment:

```python
        # Bracket the analysis batch: a managed local model backend (e.g. ds4)
        # is torn down when the session exits, freeing its resident memory
        # between ticks. Bringing it up is lazy inside propagate().
        with self._pipeline_adapter.session():
            proposals = self._strategy.propose_orders(
                candidates=fresh_candidates,
                pipeline=self._pipeline_adapter,
                current_equity=current_equity,
                asof_date=asof_date,
                live_max_position_cap=live_cap,
            )
            for proposal in proposals:
                try:
                    self._broker.place_order(proposal.order)
                except OrderRejected:
                    continue
                except BrokerError:
                    break
                cand = proposal.candidate
                self._journal.record_event(
                    events.KIND_POSITION_OPENED,
                    events.position_opened_payload(
                        symbol=cand.symbol,
                        source=cand.source.value,
                        entry_date=asof_date,
                        client_order_id=proposal.order.client_order_id,
                        entry_rank=cand.momentum.rank if cand.momentum else None,
                    ),
                )
```

The exits/leaderboard code stays OUTSIDE the session (it uses yfinance, not the LLM). Everything else in the file is main's version verbatim.

- [ ] **Step 3: Resolve `ops/main.py`**

Take the RESEARCH side's version as the base (backend threading: `_wire(broker, journal, config, *, backend=None)` returning 4 values, `_startup` returning 6, `run()`'s `backend = None` + `finally: backend.shutdown()`), then apply main's only change inside `_wire`: the universe builder swap. In `_wire`, change:

```python
    from ops.universe import build_universe
```
to
```python
    from ops.universe.composite import build_composite_universe
```
and
```python
        universe_builder=build_universe,
```
to
```python
        universe_builder=build_composite_universe,
```
Everything else (backend construction, `TradingAgentsPipelineAdapter(backend=backend)`, 4-tuple return) is the research side verbatim.

- [ ] **Step 4: Resolve the mechanical union files**

For each of `ops/config.py`, `ops/events.py`, `ops/cli.py`, `tests/ops/test_config.py`, `tests/ops/test_main.py`, `tests/ops/scheduler/test_orchestrator.py`: keep BOTH sides' additions. Specifics:
- `ops/config.py`: main added momentum/exit fields (e.g. `stopout_reentry_cooldown_days`, `daily_analysis_budget`, envelope values) and their env overrides/validation; research added `baseline_journal_path`, `baseline_starting_cash`, `screen_store_path` + env overrides + validation. Final file has ALL of them.
- `ops/events.py`: main added `KIND_POSITION_OPENED`, `KIND_EXIT_DECISION`, `KIND_EXIT_ORDER_PLACED`, `KIND_EXIT_SKIPPED_MISSING_DATA`, `KIND_EXIT_CHECK_ERROR`, `KIND_EXIT_UNKNOWN_PROVENANCE`, `KIND_DAILY_CYCLE_RUN` (+ payload builders + BUILDERS + AUDIT_ONLY entries); research added `KIND_BASELINE_SCREEN_RUN`, `KIND_BASELINE_EXIT` (same registration pattern). Union of all.
- `ops/cli.py`: main changed `decide_once` (adds `CandidateSource` import and `source=CandidateSource.EARNINGS,` in the forced-candidate `Candidate(...)`); research added the whole `screen` command. Both.
- Test files: union of both sides' added tests; no test deleted, none modified beyond conflict markers.

- [ ] **Step 5: Verify, commit the merge, push, merge PR #12**

```bash
pytest tests/ -q          # expect ~1146+ (research) + ~130 (momentum-side additions) all green
ruff check ops/scheduler/orchestrator.py ops/main.py ops/config.py ops/events.py ops/cli.py
git add ops/ tests/ docs/
git status                # MUST NOT list main.py (repo root) or tradingagents/dataflows/reddit.py as staged
git commit --no-edit      # keep git's default merge message (no editor)
git push origin claude/smallcap-research-coverage-dervpt
gh pr view 12 --repo CWFred/TradingAgents --json mergeable -q .mergeable   # expect MERGEABLE
gh pr merge 12 --repo CWFred/TradingAgents --merge
git checkout main && git pull
git checkout -b feat/phase-a-hardening
```

If the suite fails on tests touching the orchestrator session (e.g. a fake pipeline adapter without `.session()`): main-side orchestrator tests construct fake adapters; give the fake the same context manager the research side's tests use (`@contextmanager def session(self): yield`) — that pattern already exists in the research side's `tests/ops/scheduler/test_orchestrator.py` additions. Copy it, don't invent.

---

### Task 2: yfinance pacing + retry choke point

**Files:**
- Create: `ops/universe/yf_pacing.py`
- Modify: `ops/universe/earnings.py` (inside `_fetch_from_yfinance`), `ops/universe/momentum.py` (inside `fetch_closes_and_volumes_from_yfinance`), `ops/universe/filters.py` (inside `fetch_price_and_adv_from_yfinance`), `ops/research/prices.py` (inside `fetch_price_context`)
- Test: `tests/ops/universe/test_yf_pacing.py`

**Interfaces:**
- Produces (Task 3 relies on these exact names): `call_paced(fn: Callable[[], T], *, label: str, sleep=time.sleep, monotonic=time.monotonic) -> T`; `snapshot_and_reset() -> dict[str, dict[str, int]]` (label → `{"ok": int, "failed": int}`); constants `MIN_INTERVAL_SECONDS = 0.15`, `BACKOFF_SECONDS = (5.0, 25.0)`.

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the yfinance pacing/retry choke point (no real sleeping)."""

import pytest

from ops.universe import yf_pacing

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(yf_pacing, "_last_call_at", 0.0)
    yf_pacing.snapshot_and_reset()


def test_retries_transient_failure_then_succeeds():
    sleeps = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise KeyError("['Earnings Date']")
        return "data"

    result = yf_pacing.call_paced(
        flaky, label="earnings", sleep=sleeps.append, monotonic=lambda: 0.0,
    )
    assert result == "data"
    assert calls["n"] == 3
    # Two backoff sleeps happened (5s then 25s); throttle sleeps may interleave.
    assert [s for s in sleeps if s in yf_pacing.BACKOFF_SECONDS] == [5.0, 25.0]
    assert yf_pacing.snapshot_and_reset() == {"earnings": {"ok": 1, "failed": 0}}


def test_exhausted_retries_reraise_and_count_failure():
    def dead():
        raise KeyError("['Earnings Date']")

    with pytest.raises(KeyError):
        yf_pacing.call_paced(dead, label="momentum", sleep=lambda s: None, monotonic=lambda: 0.0)
    assert yf_pacing.snapshot_and_reset() == {"momentum": {"ok": 0, "failed": 1}}


def test_global_min_interval_between_calls():
    sleeps = []
    clock = {"t": 100.0}
    yf_pacing.call_paced(lambda: 1, label="x", sleep=sleeps.append, monotonic=lambda: clock["t"])
    # Second call at the same instant must wait out the interval.
    yf_pacing.call_paced(lambda: 2, label="x", sleep=sleeps.append, monotonic=lambda: clock["t"])
    assert any(0 < s <= yf_pacing.MIN_INTERVAL_SECONDS for s in sleeps)


def test_snapshot_resets():
    yf_pacing.call_paced(lambda: 1, label="adv", sleep=lambda s: None, monotonic=lambda: 0.0)
    assert yf_pacing.snapshot_and_reset() == {"adv": {"ok": 1, "failed": 0}}
    assert yf_pacing.snapshot_and_reset() == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ops/universe/test_yf_pacing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.universe.yf_pacing'`

- [ ] **Step 3: Write the implementation**

```python
"""Pacing + retry + failure counting for batch yfinance I/O.

On 2026-07-06 a transient Yahoo degradation (rate-limiting under ~500 rapid
calls at the open) made yfinance raise KeyError for every symbol; the
per-name skip handlers correctly ate the errors and the day's universe was
silently empty. This module is the single choke point that fixes all three
aspects: a process-global minimum interval keeps sweeps under Yahoo's
rate limits, transient failures retry with backoff, and ok/failed counters
feed the universe_diagnostics journal event so blindness becomes a
measurable, alertable condition instead of stderr noise.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

MIN_INTERVAL_SECONDS = 0.15
BACKOFF_SECONDS = (5.0, 25.0)

_lock = threading.Lock()
_last_call_at = 0.0
_counters: dict[str, list[int]] = {}  # label -> [ok, failed]


def call_paced(
    fn: Callable[[], T],
    *,
    label: str,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> T:
    """Run ``fn`` under the global pace; retry transient failures.

    Re-raises the last exception once retries are exhausted. Counts one
    ok/failed per COMPLETED call — a retry that eventually succeeds is ok.
    """
    global _last_call_at
    attempts = len(BACKOFF_SECONDS) + 1
    last_exc: Exception | None = None
    for attempt in range(attempts):
        with _lock:
            wait = MIN_INTERVAL_SECONDS - (monotonic() - _last_call_at)
        if wait > 0:
            sleep(wait)
        with _lock:
            _last_call_at = monotonic()
        try:
            result = fn()
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                sleep(BACKOFF_SECONDS[attempt])
                continue
            _count(label, ok=False)
            raise
        _count(label, ok=True)
        return result
    raise last_exc  # pragma: no cover - loop always returns or raises


def _count(label: str, *, ok: bool) -> None:
    with _lock:
        bucket = _counters.setdefault(label, [0, 0])
        bucket[0 if ok else 1] += 1


def snapshot_and_reset() -> dict[str, dict[str, int]]:
    """Counters since the last snapshot, then cleared — one cycle's worth."""
    with _lock:
        snap = {k: {"ok": v[0], "failed": v[1]} for k, v in _counters.items()}
        _counters.clear()
    return snap
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ops/universe/test_yf_pacing.py -v` — Expected: 4 passed

- [ ] **Step 5: Route the four fetchers through it**

Each change is inside the existing `try:` that wraps the yfinance I/O; the surrounding error handling stays byte-identical. Add `from ops.universe.yf_pacing import call_paced` to each file's imports.

`ops/universe/earnings.py`, in `_fetch_from_yfinance`, replace:
```python
        t = yf.Ticker(symbol)
        df = getattr(t, "earnings_dates", None)
```
with:
```python
        df = call_paced(
            lambda: getattr(yf.Ticker(symbol), "earnings_dates", None),
            label="earnings",
        )
```

`ops/universe/momentum.py`, in `fetch_closes_and_volumes_from_yfinance`, replace:
```python
        t = yf.Ticker(symbol)
        hist = t.history(period=_HISTORY_PERIOD, auto_adjust=False)
```
with:
```python
        hist = call_paced(
            lambda: yf.Ticker(symbol).history(period=_HISTORY_PERIOD, auto_adjust=False),
            label="momentum",
        )
```

`ops/universe/filters.py`, in `fetch_price_and_adv_from_yfinance`, replace:
```python
        t = yf.Ticker(symbol)
        hist = t.history(period="20d", auto_adjust=False)
```
with:
```python
        hist = call_paced(
            lambda: yf.Ticker(symbol).history(period="20d", auto_adjust=False),
            label="adv",
        )
```

`ops/research/prices.py`, in `fetch_price_context`, replace:
```python
        hist = yf.Ticker(symbol).history(period="6y", auto_adjust=False)
```
with:
```python
        hist = call_paced(
            lambda: yf.Ticker(symbol).history(period="6y", auto_adjust=False),
            label="prices",
        )
```

Add one regression test at the end of `tests/ops/universe/test_yf_pacing.py` proving a fetcher retries (uses the earnings fetcher with a monkeypatched yfinance that fails once then succeeds):

```python
def test_earnings_fetcher_survives_one_transient_failure(monkeypatch):
    import pandas as pd

    from ops.universe import earnings

    calls = {"n": 0}

    class FakeTicker:
        def __init__(self, symbol):
            pass

        @property
        def earnings_dates(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyError("['Earnings Date']")
            return pd.DataFrame()  # empty -> fetcher returns None cleanly

    monkeypatch.setattr(earnings.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(yf_pacing, "MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(yf_pacing, "BACKOFF_SECONDS", (0.0,))
    assert earnings._fetch_from_yfinance("AAPL") is None
    assert calls["n"] == 2  # retried once, then clean empty
```

- [ ] **Step 6: Full suite, lint, commit**

```bash
pytest tests/ -q
ruff check ops/universe/yf_pacing.py ops/universe/earnings.py ops/universe/momentum.py ops/universe/filters.py ops/research/prices.py tests/ops/universe/test_yf_pacing.py
git add ops/universe/yf_pacing.py ops/universe/earnings.py ops/universe/momentum.py ops/universe/filters.py ops/research/prices.py tests/ops/universe/test_yf_pacing.py
git commit -m "feat(universe): global yfinance pacing, retry, and failure counters"
```

---

### Task 3: Universe diagnostics + blindness alarm

**Files:**
- Modify: `ops/events.py` (2 kinds + payloads + registration), `ops/notify/policy.py` (1 POLICY entry), `ops/scheduler/orchestrator.py` (emit in the daily cycle)
- Test: extend `tests/ops/scheduler/test_orchestrator.py`; `tests/ops/notify/test_policy.py` must stay green

**Interfaces:**
- Consumes: `yf_pacing.snapshot_and_reset()` (Task 2).
- Produces: journal kinds `"universe_diagnostics"` (AUDIT_ONLY) and `"universe_blind"` (POLICY: push, high). Blind rule: `candidates == 0 AND fetch_failed * 2 > (fetch_ok + fetch_failed) AND (fetch_ok + fetch_failed) > 0`.

- [ ] **Step 1: Events.** In `ops/events.py` add (next to the other kind groups):

```python
# Universe data-feed health (A3): "found nothing" must be distinguishable
# from "could not see".
KIND_UNIVERSE_DIAGNOSTICS = "universe_diagnostics"
KIND_UNIVERSE_BLIND = "universe_blind"
```

Payload builders (next to the other builders):

```python
def universe_diagnostics_payload(
    *, asof_date, candidates: int, fetch_ok: int, fetch_failed: int,
    by_label: dict[str, dict[str, int]],
) -> dict[str, Any]:
    return {
        "asof_date": str(asof_date), "candidates": candidates,
        "fetch_ok": fetch_ok, "fetch_failed": fetch_failed,
        "by_label": by_label,
    }


def universe_blind_payload(
    *, asof_date, fetch_ok: int, fetch_failed: int, detail: str,
) -> dict[str, Any]:
    return {
        "asof_date": str(asof_date), "fetch_ok": fetch_ok,
        "fetch_failed": fetch_failed, "detail": detail,
    }
```

Register `KIND_UNIVERSE_DIAGNOSTICS: universe_diagnostics_payload` and `KIND_UNIVERSE_BLIND: universe_blind_payload` in `BUILDERS`; add `KIND_UNIVERSE_DIAGNOSTICS` to `AUDIT_ONLY` (NOT universe_blind — that one notifies).

- [ ] **Step 2: Policy.** In `ops/notify/policy.py`, add to `POLICY` (near KIND_DAILY_HALT):

```python
    # The universe came back empty because the data feed was failing, not
    # because the market was quiet (2026-07-06 incident) — worth a push.
    events.KIND_UNIVERSE_BLIND: PolicyEntry(("push",), "high", None),
```

Run: `pytest tests/ops/notify/test_policy.py -v` — Expected: PASS (proves registration + rendering).

- [ ] **Step 3: Orchestrator emission.** In `ops/scheduler/orchestrator.py`: add import `from ops.universe import yf_pacing`. Immediately AFTER the `KIND_DAILY_CYCLE_RUN` record_event call, add:

```python
        # Discard fetch counters accumulated outside this cycle so the
        # diagnostics below describe exactly one day's sweep.
        yf_pacing.snapshot_and_reset()
```

Immediately AFTER `candidates = self._universe_builder(...)` (before `fresh_candidates = ...`), add:

```python
        self._emit_universe_diagnostics(asof_date, len(candidates))
```

Add the method (after `_run_exits`):

```python
    def _emit_universe_diagnostics(self, asof_date, candidate_count: int) -> None:
        stats = yf_pacing.snapshot_and_reset()
        fetch_ok = sum(s["ok"] for s in stats.values())
        fetch_failed = sum(s["failed"] for s in stats.values())
        self._journal.record_event(
            events.KIND_UNIVERSE_DIAGNOSTICS,
            events.universe_diagnostics_payload(
                asof_date=asof_date, candidates=candidate_count,
                fetch_ok=fetch_ok, fetch_failed=fetch_failed, by_label=stats,
            ),
        )
        total = fetch_ok + fetch_failed
        if candidate_count == 0 and total > 0 and fetch_failed * 2 > total:
            self._journal.record_event(
                events.KIND_UNIVERSE_BLIND,
                events.universe_blind_payload(
                    asof_date=asof_date, fetch_ok=fetch_ok,
                    fetch_failed=fetch_failed,
                    detail="empty universe with majority fetch failures",
                ),
            )
```

- [ ] **Step 4: Tests.** Append to `tests/ops/scheduler/test_orchestrator.py`, reusing that file's existing fixtures/fakes for constructing an orchestrator whose universe_builder returns `[]` (read the file first; the momentum-era tests already build orchestrators with fake calendars/journals — copy the newest construction pattern in the file):

```python
def _seed_pacing_failures(n: int) -> None:
    from ops.universe import yf_pacing

    for _ in range(n):
        yf_pacing._count("earnings", ok=False)


def test_daily_cycle_emits_universe_diagnostics(...existing fixture args...):
    # build orchestrator with universe_builder returning [] and tick once
    # (copy the construction used by the newest daily-cycle test in this file)
    from ops.universe import yf_pacing

    yf_pacing.snapshot_and_reset()
    _seed_pacing_failures(10)
    orchestrator.tick()
    kinds = [e["kind"] for e in journal.read_events()]
    assert events.KIND_UNIVERSE_DIAGNOSTICS in kinds
    assert events.KIND_UNIVERSE_BLIND in kinds  # 0 candidates, 100% failures


def test_no_blind_alarm_when_feed_healthy(...):
    from ops.universe import yf_pacing

    yf_pacing.snapshot_and_reset()
    yf_pacing._count("earnings", ok=True)   # healthy fetches, quiet day
    orchestrator.tick()
    kinds = [e["kind"] for e in journal.read_events()]
    assert events.KIND_UNIVERSE_DIAGNOSTICS in kinds
    assert events.KIND_UNIVERSE_BLIND not in kinds
```

(The `...` in the signatures means: use the same fixture parameters the neighboring tests in that file use — this is the ONE place you adapt to the file's local conventions rather than transcribe. The assertions are the contract; do not weaken them. Note: seeding failures must happen AFTER the cycle-start reset would run — seed counters, then tick; the cycle-start reset runs inside tick BEFORE the universe builder, so seed via a universe_builder side effect instead if the plain version fails: make the fake universe_builder call `_seed_pacing_failures(10)` and then return `[]`.)

Use the side-effect variant from the start — it is deterministic:

```python
    def blind_universe_builder(**kwargs):
        _seed_pacing_failures(10)
        return []
```

- [ ] **Step 5: Full suite, lint, commit**

```bash
pytest tests/ops/scheduler/test_orchestrator.py tests/ops/notify/test_policy.py -q && pytest tests/ -q
ruff check ops/events.py ops/notify/policy.py ops/scheduler/orchestrator.py tests/ops/scheduler/test_orchestrator.py
git add ops/events.py ops/notify/policy.py ops/scheduler/orchestrator.py tests/ops/scheduler/test_orchestrator.py
git commit -m "feat(ops): universe diagnostics event + blindness push alarm"
```

---

### Task 4: Split-unadjusted year-end closes (P/E-history bar fix)

**Files:**
- Modify: `ops/research/prices.py`, `ops/research/run.py` (one line), `docs/research_screener.md` (remove the split-bias caveat if present; note the fix)
- Test: extend `tests/ops/research/test_prices.py`, one test in `tests/ops/research/test_run.py`

**Interfaces:**
- Produces: `PriceContext.splits: dict[date, Decimal]` (split date → ratio, e.g. `Decimal("10")` for 10:1 forward, `Decimal("0.1")` for 1:10 reverse); `PriceContext.unadjusted_close_on_or_before(when: date, *, max_gap_days: int = 10) -> Decimal | None`.
- Math: Yahoo closes are split-adjusted; the as-traded close on day d = adjusted close(d) × Π(ratios of splits dated strictly after d). XBRL EPS is as-reported (old share terms), so the P/E-history bar must use as-traded prices.

- [ ] **Step 1: Failing tests** (append to `tests/ops/research/test_prices.py`):

```python
def test_unadjusted_close_reverses_forward_split():
    # 10:1 forward split on 2026-06-01. Yahoo shows pre-split closes divided
    # by 10; as-traded price was 10x the adjusted figure.
    ctx = PriceContext(
        closes={date(2026, 5, 15): Decimal("10"), date(2026, 6, 15): Decimal("95")},
        splits={date(2026, 6, 1): Decimal("10")},
    )
    assert ctx.unadjusted_close_on_or_before(date(2026, 5, 15)) == Decimal("100")
    # After the split there is nothing to undo.
    assert ctx.unadjusted_close_on_or_before(date(2026, 6, 15)) == Decimal("95")


def test_unadjusted_close_reverses_reverse_split():
    # 1:10 reverse split (ratio 0.1): busted small cap trading at $1 becomes
    # $10; Yahoo scales history UP; as-traded was the low price.
    ctx = PriceContext(
        closes={date(2026, 5, 15): Decimal("10")},
        splits={date(2026, 6, 1): Decimal("0.1")},
    )
    assert ctx.unadjusted_close_on_or_before(date(2026, 5, 15)) == Decimal("1.0")


def test_unadjusted_equals_adjusted_without_splits():
    ctx = PriceContext(closes={date(2026, 5, 15): Decimal("42")})
    assert ctx.unadjusted_close_on_or_before(date(2026, 5, 15)) == Decimal("42")


def test_multiple_future_splits_compound():
    ctx = PriceContext(
        closes={date(2024, 12, 31): Decimal("5")},
        splits={date(2025, 6, 1): Decimal("2"), date(2026, 6, 1): Decimal("3")},
    )
    assert ctx.unadjusted_close_on_or_before(date(2024, 12, 31)) == Decimal("30")
```

Run: `pytest tests/ops/research/test_prices.py -v` — Expected: new tests FAIL (`TypeError: unexpected keyword argument 'splits'`).

- [ ] **Step 2: Implement in `ops/research/prices.py`.**

Dataclass gains a default-empty split map (frozen dataclass → `field(default_factory=dict)`; add `field` to the dataclasses import):

```python
@dataclass(frozen=True)
class PriceContext:
    closes: dict[date, Decimal]            # trading day -> close (split-ADJUSTED, from Yahoo)
    splits: dict[date, Decimal] = field(default_factory=dict)  # split date -> ratio
```

New method after `close_on_or_before`:

```python
    def unadjusted_close_on_or_before(
        self, when: date, *, max_gap_days: int = 10,
    ) -> Decimal | None:
        """As-traded close: Yahoo back-adjusts splits into Close, but XBRL EPS
        is as-reported in the era's share count, so P/E history must undo the
        adjustment — multiply by every split ratio dated after the hit day."""
        for offset in range(max_gap_days + 1):
            d = when - timedelta(days=offset)
            if d in self.closes:
                factor = Decimal("1")
                for split_date, ratio in self.splits.items():
                    if split_date > d and ratio > 0:
                        factor *= ratio
                return self.closes[d] * factor
        return None
```

In `fetch_price_context`, request actions and harvest the split column (the history call is already wrapped by `call_paced` from Task 2 — keep that wrapper, just add the parameter):

```python
        hist = call_paced(
            lambda: yf.Ticker(symbol).history(period="6y", auto_adjust=False, actions=True),
            label="prices",
        )
```

and after the closes loop:

```python
    splits: dict[date, Decimal] = {}
    if "Stock Splits" in hist:
        for ts, ratio in hist["Stock Splits"].items():
            value = _safe_decimal(ratio)
            if value > 0:
                splits[ts.date()] = value
    return PriceContext(closes=closes, splits=splits) if closes else None
```

Run: `pytest tests/ops/research/test_prices.py -v` — Expected: all pass.

- [ ] **Step 3: Use it in the screener sweep.** In `ops/research/run.py`, `_name_inputs`, change the year-end price lookup only (the current/asof price stays adjusted — no future splits exist for "today"):

```python
    year_end_prices = {
        yv.fiscal_year_end: px
        for yv in fundamentals.eps_history
        if (px := ctx.unadjusted_close_on_or_before(yv.fiscal_year_end)) is not None
    }
```

Add to `tests/ops/research/test_run.py` (reuse that file's existing `_price_ctx` helper, giving it a splits argument — extend the helper with `splits=None` default):

```python
def test_year_end_prices_are_split_unadjusted(config):
    from ops.research.run import _name_inputs

    ctx = _price_ctx()
    # Rebuild with a 10:1 forward split newer than every fiscal year end.
    ctx = PriceContext(closes=ctx.closes, splits={ASOF: Decimal("10")})
    ni = _name_inputs(
        _name("GOOD"), asof=ASOF,
        facts_fetcher=lambda t: _facts_for_passer(),
        triggers_finder=lambda t, *, asof, lookback_days=90, list_filings=None: [],
        price_context_fetcher=lambda s: ctx,
    )
    assert ni is not None
    # Every year-end price is 10x the adjusted 20 -> 200.
    assert all(px == Decimal("200") for px in ni.year_end_prices.values())
```

(If `_name_inputs`'s keyword names differ in the file, match the file — the contract is: year_end_prices values are unadjusted.)

- [ ] **Step 4: Docs.** In `docs/research_screener.md`, add under a `## Data notes` (or the Form 4 note section): "P/E-history prices are split-unadjusted (as-traded) to match as-reported XBRL EPS; Yahoo's split-adjusted closes are corrected using the split actions from the same history call."

- [ ] **Step 5: Full suite, lint, commit; then one-time queue re-check note.**

```bash
pytest tests/ops/research/ -q && pytest tests/ -q
ruff check ops/research/prices.py ops/research/run.py tests/ops/research/test_prices.py tests/ops/research/test_run.py
git add ops/research/prices.py ops/research/run.py docs/research_screener.md tests/ops/research/test_prices.py tests/ops/research/test_run.py
git commit -m "fix(research): P/E-history bar compares as-traded prices with as-reported EPS"
```

The spec's "one-time re-check of accumulated pending hits" is operational: it happens automatically in Task 9's calibration run (any reverse-split false positives simply stop passing; existing pending hits get re-screened by the next real run and superseded). No code.

---

### Task 5: Screen coverage telemetry + `--notify`

**Files:**
- Modify: `ops/research/run.py` (coverage aggregation into the summary), `ops/research/store.py` (persist coverage on the run row), `ops/cli.py` (print coverage; `--notify` flag; blind exit code)
- Test: extend `tests/ops/research/test_run.py`, `tests/ops/research/test_store.py`

**Interfaces:**
- Produces: `ScreenRunSummary.coverage: dict[str, dict[str, int]]` (bar name → `{"computed": int, "missing": int}`); `ScreenStore.record_run(..., coverage: dict | None = None)`; run rows gain a nullable `coverage` TEXT column (JSON); CLI `ops screen --notify`.
- Blind rule (screen side): `universe_size > 0 and len(errors) * 2 > universe_size` → high-urgency notification + exit code 2.
- Notification API (already in the repo): `from ops.notify.config import load_notify_config`, `from ops.notify.push import build_push_transport`, `from ops.notify.transport import NotifyMessage` — `build_push_transport(load_notify_config()).send(NotifyMessage(title=..., body=..., urgency="normal"|"high"))`; the transport self-disables when creds/enabled are missing, so sending is always safe.

- [ ] **Step 1: Failing tests.**

Append to `tests/ops/research/test_run.py` (reuse existing fixtures):

```python
def test_summary_carries_per_bar_coverage(config):
    summary = _run(config)
    assert summary.coverage  # six bar names
    assert summary.coverage["fcf_yield"]["computed"] >= 1
    assert set(summary.coverage) == {
        "ev_ebit_vs_sector", "fcf_yield", "pe_vs_own_history",
        "roic_5y", "debt_to_ebitda", "gross_margin_stability",
    }
```

Append to `tests/ops/research/test_store.py`:

```python
def test_record_run_persists_coverage(store):
    coverage = {"fcf_yield": {"computed": 5, "missing": 1}}
    store.record_run(asof=ASOF, universe_size=6, results=[_result("AAA")], coverage=coverage)
    run = store.last_run()
    assert run["coverage"] == coverage
```

Run both files — Expected: FAIL (`coverage` unknown).

- [ ] **Step 2: Implement.**

`ops/research/run.py` — add field `coverage: dict[str, dict[str, int]]` to `ScreenRunSummary` (after `errors`; note `baseline` stays last if it currently is — match the file). After `results = screen_universe(...)`:

```python
    coverage: dict[str, dict[str, int]] = {}
    for result in results:
        for bar in (*result.valuation_bars, *result.quality_bars):
            slot = coverage.setdefault(bar.name, {"computed": 0, "missing": 0})
            slot["missing" if bar.detail.startswith("missing:") else "computed"] += 1
```

Pass `coverage=coverage` into `store.record_run(...)` and into the returned `ScreenRunSummary`.

`ops/research/store.py` — defensive migration in `__init__` (same pattern as `ops/journal.py` uses for late columns), after `executescript(_SCHEMA)`:

```python
            cols = {row[1] for row in conn.execute("PRAGMA table_info(screen_runs)")}
            if "coverage" not in cols:
                conn.execute("ALTER TABLE screen_runs ADD COLUMN coverage TEXT")
```

`record_run` gains `coverage: dict | None = None` keyword; the INSERT stores `json.dumps(coverage) if coverage is not None else None` in the new column. `last_run()` decodes: after `row` fetch, build the dict and set `d["coverage"] = json.loads(d["coverage"]) if d["coverage"] else None`.

`ops/cli.py` — in `screen`: add option

```python
@click.option("--notify", "do_notify", is_flag=True,
              help="Send a Pushover summary (or a high-urgency alert on a blind sweep).")
```

After printing the existing summary lines, print coverage:

```python
    for bar_name, counts in sorted(summary.coverage.items()):
        total = counts["computed"] + counts["missing"]
        pct = (100 * counts["computed"] // total) if total else 0
        click.echo(f"  coverage {bar_name}: {counts['computed']}/{total} ({pct}%)")
```

Blindness + notify at the end of the command:

```python
    blind = summary.universe_size > 0 and len(summary.errors) * 2 > summary.universe_size
    if do_notify:
        from ops.notify.config import load_notify_config
        from ops.notify.push import build_push_transport
        from ops.notify.transport import NotifyMessage

        transport = build_push_transport(load_notify_config())
        if blind:
            transport.send(NotifyMessage(
                title="screen BLIND",
                body=(f"{len(summary.errors)}/{summary.universe_size} names errored; "
                      "results unusable"),
                urgency="high",
            ))
        else:
            transport.send(NotifyMessage(
                title="screen complete",
                body=(f"asof {summary.asof}: {len(summary.passed)} passed / "
                      f"{summary.screened} screened / {len(summary.errors)} errors"),
                urgency="normal",
            ))
    if blind:
        raise SystemExit(2)
```

- [ ] **Step 3: CLI test.** Append to `tests/ops/research/test_run.py` a blind-exit test using click's runner only if the file already tests the CLI; otherwise test the blind rule at the summary level (universe of 6 with `price_context_fetcher=lambda s: None` gives 6 errors → callers can compute blind) and leave CLI wiring to manual verification in Task 9. Do NOT build a new CLI test harness for this.

- [ ] **Step 4: Full suite, lint, commit**

```bash
pytest tests/ops/research/ -q && pytest tests/ -q
ruff check ops/research/run.py ops/research/store.py ops/cli.py tests/ops/research/test_run.py tests/ops/research/test_store.py
git add ops/research/run.py ops/research/store.py ops/cli.py tests/ops/research/test_run.py tests/ops/research/test_store.py
git commit -m "feat(research): per-bar coverage telemetry + screen --notify with blind alarm"
```

---

### Task 6: Manual delisted write-off command

**Files:**
- Modify: `ops/research/baseline.py` (write-off function), `ops/events.py` (1 kind), `ops/cli.py` (research command group)
- Test: extend `tests/ops/research/test_baseline.py`

**Interfaces:**
- Produces: `write_off_position(*, journal: Journal, symbol: str, price: Decimal, starting_cash: Decimal, note: str | None = None) -> dict` returning `{"symbol", "quantity": str, "price": str, "proceeds": str}`; event kind `"baseline_writeoff"` (AUDIT_ONLY); CLI `ops research write-off SYMBOL --price P [--note ...]`. The CLI `research` GROUP is created here — Phase B/D add `run`/`report` subcommands to it later.

- [ ] **Step 1: Failing tests** (append to `tests/ops/research/test_baseline.py`):

```python
def test_write_off_closes_position_at_given_price(journal):
    from ops.research.baseline import update_baseline_portfolio, write_off_position

    broker = _broker(journal)
    update_baseline_portfolio(
        broker=broker, journal=journal, passers=["DEAD"], asof=ASOF, now=NOW,
    )
    result = write_off_position(
        journal=journal, symbol="DEAD", price=Decimal("2.50"),
        starting_cash=Decimal("100000"), note="acquired at $2.50",
    )
    assert result["symbol"] == "DEAD"
    # Replay must show the position gone and cash credited at the write-off price.
    rebuilt = PaperBroker.from_journal(
        journal=journal, quote_source=lambda s: Decimal("20"),
        starting_cash=Decimal("100000"),
    )
    assert all(p.symbol != "DEAD" for p in rebuilt.get_positions())
    kinds = [e["kind"] for e in journal.read_events()]
    assert "baseline_writeoff" in kinds


def test_write_off_unknown_symbol_raises(journal):
    from ops.broker.base import NoSuchPosition
    from ops.research.baseline import write_off_position

    with pytest.raises(NoSuchPosition):
        write_off_position(
            journal=journal, symbol="GHOST", price=Decimal("1"),
            starting_cash=Decimal("100000"),
        )
```

(Ensure `pytest` and `PaperBroker` are already imported in that file — they are.)

Run: FAIL with `ImportError: cannot import name 'write_off_position'`.

- [ ] **Step 2: Event.** In `ops/events.py`:

```python
KIND_BASELINE_WRITEOFF = "baseline_writeoff"


def baseline_writeoff_payload(
    *, symbol: str, quantity: Decimal, price: Decimal, note: str | None,
) -> dict[str, Any]:
    return {"symbol": symbol, "quantity": str(quantity), "price": str(price), "note": note}
```

Register in `BUILDERS` and add `KIND_BASELINE_WRITEOFF` to `AUDIT_ONLY`. Run `pytest tests/ops/notify/test_policy.py -q` — PASS.

- [ ] **Step 3: Implement in `ops/research/baseline.py`** (imports: add `uuid4` if not present, `NoSuchPosition` from `ops.broker.base`, `Side` from `ops.broker.types`, `datetime`/`timezone` already there):

```python
def write_off_position(
    *,
    journal: Journal,
    symbol: str,
    price: Decimal,
    starting_cash: Decimal,
    note: str | None = None,
) -> dict:
    """Manually resolve a position the broker can no longer quote (delisted:
    tender, acquisition, bankruptcy) by journaling a synthetic SELL at the
    known settlement price. PaperBroker.close_position would quote and fail,
    so the order+fill are written directly — replay reconstructs the cash.
    """
    from ops.broker.paper import PaperBroker

    broker = PaperBroker.from_journal(
        journal=journal,
        quote_source=_no_quotes,
        starting_cash=starting_cash,
    )
    position = next((p for p in broker.get_positions() if p.symbol == symbol.upper()), None)
    if position is None:
        raise NoSuchPosition(f"no baseline position in {symbol!r}")
    proceeds = position.quantity * price
    now = datetime.now(timezone.utc)
    coid = f"baseline-writeoff-{now.date().isoformat()}-{symbol.upper()}-{uuid4().hex[:8]}"
    journal.record_order(
        client_order_id=coid, symbol=symbol.upper(), side=Side.SELL.value,
        notional_dollars=proceeds, stop_loss_price=None,
    )
    journal.record_fill(
        order_id=str(uuid4()), client_order_id=coid, symbol=symbol.upper(),
        side=Side.SELL.value, quantity=position.quantity, price=price, filled_at=now,
    )
    journal.record_event(
        events.KIND_BASELINE_WRITEOFF,
        events.baseline_writeoff_payload(
            symbol=symbol.upper(), quantity=position.quantity, price=price, note=note,
        ),
    )
    return {
        "symbol": symbol.upper(), "quantity": str(position.quantity),
        "price": str(price), "proceeds": str(proceeds),
    }


def _no_quotes(symbol: str) -> Decimal:
    raise AssertionError("write-off must never quote")
```

- [ ] **Step 4: CLI group.** In `ops/cli.py`:

```python
@cli.group()
def research() -> None:
    """Long-horizon research sleeve commands."""


@research.command("write-off")
@click.argument("symbol")
@click.option("--price", required=True,
              help="Settlement price per share (deal price or last trade).")
@click.option("--note", default=None, help="Why (e.g. 'acquired 2026-08-01 at $12.50').")
def research_write_off(symbol: str, price: str, note: str | None) -> None:
    """Resolve a delisted baseline position at a known price."""
    from ops.research.baseline import write_off_position

    config = load_config()
    journal = Journal(config.baseline_journal_path)
    try:
        result = write_off_position(
            journal=journal, symbol=symbol, price=Decimal(price),
            starting_cash=config.baseline_starting_cash, note=note,
        )
    finally:
        journal.close()
    click.echo(
        f"wrote off {result['quantity']} {result['symbol']} at {result['price']} "
        f"(proceeds {result['proceeds']})"
    )
```

- [ ] **Step 5: Full suite, lint, commit**

```bash
pytest tests/ops/research/test_baseline.py tests/ops/notify/test_policy.py -q && pytest tests/ -q
ruff check ops/research/baseline.py ops/events.py ops/cli.py tests/ops/research/test_baseline.py
git add ops/research/baseline.py ops/events.py ops/cli.py tests/ops/research/test_baseline.py
git commit -m "feat(research): manual write-off command for delisted baseline positions"
```

---

### Task 7: Weekly screen launchd job

**Files:**
- Create: `ops/deploy/com.tradingagents.screen.plist.template`
- Modify: `ops/deploy/__init__.py`, `ops/cli.py`
- Test: extend `tests/ops/test_deploy.py`

**Interfaces:**
- Produces: `render_screen_plist(*, python_path: str, repo_dir: str, log_dir: str) -> str` in `ops/deploy`; CLI `ops install-screen-service [--output PATH] [--log-dir DIR]` writing `~/Library/LaunchAgents/com.tradingagents.screen.plist`, never invoking launchctl.

- [ ] **Step 1: Read the existing pattern.** Read `ops/deploy/__init__.py`, `ops/deploy/com.tradingagents.ops.plist.template`, the `install_service` command in `ops/cli.py`, and `tests/ops/test_deploy.py` COMPLETELY before writing anything. Mirror them exactly: same placeholder style, same env-block handling, same "write file + print load command, never launchctl" contract.

- [ ] **Step 2: Template.** `com.tradingagents.screen.plist.template` — copy the ops template's structure (same env passthrough approach) with these differences: Label `com.tradingagents.screen`; ProgramArguments run `{python_path} -m ops.cli screen --notify`; NO `KeepAlive` (batch job, not a service); scheduling block:

```xml
	<key>StartCalendarInterval</key>
	<dict>
		<key>Weekday</key>
		<integer>6</integer>
		<key>Hour</key>
		<integer>10</integer>
		<key>Minute</key>
		<integer>0</integer>
	</dict>
```

(Weekday 6 = Saturday; market closed, SEC quiet.) Stdout/err to `{log_dir}/screen.out.log` / `{log_dir}/screen.err.log`. Include `SEC_EDGAR_USER_AGENT` in the env block the same way the ops template handles its env vars (read how it does OPS_* passthrough and follow it).

- [ ] **Step 3: Renderer + CLI + tests.** Add `render_screen_plist` next to `render_launchd_plist` with the same signature style; add `install-screen-service` mirroring `install_service` (default output `~/Library/LaunchAgents/com.tradingagents.screen.plist`). Tests: mirror the existing `test_deploy.py` assertions (placeholders substituted, no `{` left, label correct, `screen --notify` present, `StartCalendarInterval` present, `KeepAlive` absent).

- [ ] **Step 4: Full suite, lint, commit**

```bash
pytest tests/ops/test_deploy.py -q && pytest tests/ -q
ruff check ops/deploy/__init__.py ops/cli.py tests/ops/test_deploy.py
git add ops/deploy/ ops/cli.py tests/ops/test_deploy.py
git commit -m "feat(deploy): weekly screen launchd job + install-screen-service"
```

---

### Task 8: decide-once composite parity

**Files:**
- Modify: `ops/cli.py` (`decide_once`, non-forced path only)
- Test: `tests/ops/test_cli_decide_once.py`

**Interfaces:**
- Consumes: `build_composite_universe(*, asof_date, config, held_symbols=frozenset(), free_slots=None, excluded_symbols=frozenset(), momentum_leaders=None, members_loader=None, earnings_finder=None, metrics_fetcher=None, momentum_finder=None) -> list[Candidate]` (exists on main since PR #11).

- [ ] **Step 1: Read first.** Read `decide_once` in `ops/cli.py` and `tests/ops/test_cli_decide_once.py` completely. The non-forced path currently calls `build_universe(asof_date=..., config=...)` (earnings-only) — the documented momentum-merge limitation.

- [ ] **Step 2: Swap the builder.** In the non-forced branch, replace the `build_universe(...)` call with:

```python
        held = {p.symbol for p in broker.get_positions()}
        candidates = build_composite_universe(
            asof_date=asof_date, config=config,
            held_symbols=frozenset(held),
            free_slots=max(0, config.max_open_positions - len(held)),
        )
```

(matching the orchestrator's call minus the leaderboard precompute — `momentum_leaders=None` makes the composite compute it itself). Import `build_composite_universe` from `ops.universe.composite` alongside the existing universe imports. If `decide_once`'s structure makes `broker` unavailable at that point, move the candidates build after broker construction — read the function; do not restructure anything else.

- [ ] **Step 3: Tests.** Update `tests/ops/test_cli_decide_once.py`: whatever the file monkeypatches for `build_universe` on the non-forced path must now target `build_composite_universe` (patch it where `ops.cli` imports it). Keep the existing assertions; add one:

```python
def test_decide_once_uses_composite_universe(...existing fixture args...):
    # patch ops.cli.build_composite_universe with a recorder returning []
    # run the command non-forced; assert the recorder was called once with
    # held_symbols and free_slots kwargs present.
```

Write it concretely against the file's existing harness (it already invokes the command with a runner and fakes; follow the newest test in the file). The assertion contract: composite builder called; earnings-only `build_universe` NOT called on the non-forced path.

- [ ] **Step 4: Full suite, lint, commit**

```bash
pytest tests/ops/test_cli_decide_once.py -q && pytest tests/ -q
ruff check ops/cli.py tests/ops/test_cli_decide_once.py
git add ops/cli.py tests/ops/test_cli_decide_once.py
git commit -m "fix(cli): decide-once exercises the composite universe like the daemon"
```

---

### Task 9: Deploy isolation, calibration run, docs, PR

This task is operational + docs; it has explicit USER GATES. Do the steps in order.

- [ ] **Step 1: Push and open the PR**

```bash
git push -u origin feat/phase-a-hardening
gh pr create --repo CWFred/TradingAgents --base main --head feat/phase-a-hardening \
  --title "feat(ops): phase A hardening — pacing, blindness alarms, coverage, weekly screen, write-off" \
  --body "Implements Phase A of docs/superpowers/specs/2026-07-06-finish-research-system-design.md (A3–A8). A1 merged as PR #12; A2 is the deploy swap below.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Report the PR URL and WAIT for the user to review/merge (or explicit instruction to merge it yourself).

- [ ] **Step 2 (after PR merged): Create the live worktree**

```bash
cd /Users/frednick/Code/TradingAgents
git worktree add ~/Code/TradingAgents-live main
cd ~/Code/TradingAgents-live
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/ops --help    # sanity: CLI loads from the live venv
```

- [ ] **Step 3: Render both plists FROM the live worktree paths**

```bash
cd ~/Code/TradingAgents-live
.venv/bin/ops install-service --output ~/Library/LaunchAgents/com.tradingagents.ops.plist
.venv/bin/ops install-screen-service --output ~/Library/LaunchAgents/com.tradingagents.screen.plist
grep -n "TradingAgents-live" ~/Library/LaunchAgents/com.tradingagents.ops.plist  # MUST match
```

If the rendered plist still points at the dev checkout, the renderer derives paths from CWD/sys.executable — run it from the live worktree's venv as shown; if it hardcodes the dev path, STOP and report BLOCKED with the template line.

- [ ] **Step 4: USER GATE — service swap.** Print exactly this and wait for explicit user confirmation before running anything:

```
Ready to swap the daemon to the live worktree:
  launchctl unload ~/Library/LaunchAgents/com.tradingagents.ops.plist
  launchctl load ~/Library/LaunchAgents/com.tradingagents.ops.plist
  launchctl load ~/Library/LaunchAgents/com.tradingagents.screen.plist
This restarts the trading service (paper). Confirm?
```

After confirmation and the swap: `launchctl list | grep tradingagents` must show both jobs; `tail -5 ~/.local/state/tradingagents/logs/ops.err.log` should show a fresh start; the journal should record a new `service_started`.

- [ ] **Step 5: Live calibration run** (network; needs `SEC_EDGAR_USER_AGENT` — ask the user for the value or source it from their `.env`; never commit it):

```bash
cd ~/Code/TradingAgents-live
set -a; source /Users/frednick/Code/TradingAgents/.env 2>/dev/null; set +a
.venv/bin/ops screen --limit 200 --dry-run
```

Expected: some `[screen] skipped ...` stderr lines (designed failure path), a summary with the coverage table. Record the coverage table verbatim in `docs/research_screener.md` under `## Calibration runs` with the date. **Gate:** if `ev_ebit_vs_sector` or `fcf_yield` coverage < 60%, file a follow-up (add a `## Follow-ups` bullet in the runbook naming the worst-covered XBRL concepts from the run's stderr) — do NOT start tuning fallback chains in this plan.

- [ ] **Step 6: Docs.** Update `docs/RUNBOOK-paper-golive.md` (or create `docs/RUNBOOK-deploy.md` if the former doesn't exist) with a `## Deploy` section:

```markdown
## Deploy (live worktree)

The daemon and the weekly screen run from ~/Code/TradingAgents-live (pinned
to main) — NEVER from the dev checkout. Redeploy after merging to main:

    git -C ~/Code/TradingAgents-live pull --ff-only
    ~/Code/TradingAgents-live/.venv/bin/pip install -e ~/Code/TradingAgents-live   # only if deps changed
    launchctl kickstart -k gui/$(id -u)/com.tradingagents.ops

The screen job (com.tradingagents.screen) picks up new code on its next
Saturday run automatically; kickstart it manually to run early.

Momentum sunset review due ~2026-08-30 (8-week paper gate): keep / pause /
retire on its track record. See docs/superpowers/specs/2026-07-06-finish-research-system-design.md.
```

Commit docs to the branch (or a small follow-up PR if the main PR already merged):

```bash
git add docs/
git commit -m "docs(ops): live-worktree deploy recipe + calibration results"
git push
```

- [ ] **Step 7: Report.** Final message must include: PR URL + merge state, worktree path, both launchd jobs' status lines, the calibration coverage table, and whether the 60% gate passed.

---

## Verification checklist (after all tasks)

1. `pytest tests/ -q` green on the merged branch and on `feat/phase-a-hardening`.
2. `gh pr view 12 --json state` → MERGED; Phase A PR open or merged.
3. Daemon: `launchctl list | grep tradingagents` shows `com.tradingagents.ops` (live worktree) and `com.tradingagents.screen`.
4. Kill test for the alarm: temporarily unplug network? NO — do not do this; the unit tests cover the blind path. Verify instead that `sqlite3 ~/.local/state/tradingagents/ops_journal.sqlite "SELECT kind FROM events WHERE kind='universe_diagnostics' ORDER BY id DESC LIMIT 1"` returns a row after the next trading-day cycle.
5. Coverage table recorded in `docs/research_screener.md`; 60% gate outcome noted.
