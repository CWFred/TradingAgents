# Pipeline Activity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The ops dashboard shows what the model is working on right now, a recent-runs history with reason/outcome/duration, and a gate-aware forecast of the next real ds4 work.

**Architecture:** The ops service journals `activity_started`/`activity_finished` breadcrumb events (job scope = a whole ds4-holding window; item scope = one symbol/memo inside it) via a small `ActivityReporter`. The read-only dashboard snapshot derives `current` + `recent_runs` from those events and computes `next_work` with a pure forecast function over the journals/stores/market calendar. Two new React components (NowStrip, RunsPanel) render the section.

**Tech Stack:** Python 3 stdlib + sqlite (existing `ops/` patterns), pytest; React + TypeScript + vite/vitest (existing `dashboard-ui/` patterns).

**Spec:** `docs/superpowers/specs/2026-07-14-pipeline-activity-design.md`

## Global Constraints

- Dashboard reads are read-only: sqlite `mode=ro` URIs via `ops.dashboard.snapshot.ro_conn`, or `Journal(path, readonly=True)`. No broker, no network, no LLM.
- Money is Decimal/string end to end; never float. (Activity events carry no money — durations as float seconds are fine.)
- The reporter must NEVER alter control flow of the work it wraps: exceptions re-raise, journal-write failures inside the reporter print to stderr and are swallowed.
- Journal `record_event` writes only to the MAIN ops journal (`config.journal_path`) for activity events — same journal the run-summary events use.
- Job scopes are exactly `"daily_cycle"` and `"overnight"`. Item stages are `"analyzing"`, `"vetting"`, `"researching"`, `"authoring_memo"`.
- New tests must not touch `tests/ops/test_main.py` (11 pre-existing failures on main are out of scope — do not fix or worsen them).
- Run Python tests with `python -m pytest <path> -v` from the repo root; UI tests with `npm test` (vitest) from `dashboard-ui/`.
- Commit after every task with the message given in the task.

---

### Task 1: Activity event kinds + payload helpers

**Files:**
- Modify: `ops/events.py` (add two KIND constants after `KIND_DAILY_OVERVIEW_ERROR` ~line 153, and two payload helpers at the end of the file)
- Test: `tests/ops/test_events_activity.py` (create)

**Interfaces:**
- Produces: `events.KIND_ACTIVITY_STARTED = "activity_started"`, `events.KIND_ACTIVITY_FINISHED = "activity_finished"`, `events.activity_started_payload(*, scope, job, stage=None, symbol=None, seq=None, reason=None) -> dict`, `events.activity_finished_payload(*, scope, job, ok, duration_s, stage=None, symbol=None, seq=None, outcome=None) -> dict`. Optional fields are OMITTED from the payload when None (matches the repo's byte-stable payload convention).

- [ ] **Step 1: Write the failing test**

```python
"""Activity breadcrumb events: kind constants + payload builders."""
from ops import events


def test_activity_kind_constants():
    assert events.KIND_ACTIVITY_STARTED == "activity_started"
    assert events.KIND_ACTIVITY_FINISHED == "activity_finished"


def test_started_payload_full():
    p = events.activity_started_payload(
        scope="item", job="overnight", stage="vetting",
        symbol="CRC", seq="2/5", reason=None,
    )
    assert p == {
        "scope": "item", "job": "overnight", "stage": "vetting",
        "symbol": "CRC", "seq": "2/5",
    }


def test_started_payload_omits_none_fields():
    p = events.activity_started_payload(
        scope="job", job="daily_cycle", reason="attempt 1 of 3",
    )
    assert p == {"scope": "job", "job": "daily_cycle", "reason": "attempt 1 of 3"}


def test_finished_payload():
    p = events.activity_finished_payload(
        scope="job", job="overnight", ok=True, duration_s=12.5,
        outcome="researched 4, vetted 2",
    )
    assert p == {
        "scope": "job", "job": "overnight", "ok": True,
        "duration_s": 12.5, "outcome": "researched 4, vetted 2",
    }


def test_finished_payload_failure_omits_outcome():
    p = events.activity_finished_payload(
        scope="item", job="daily_cycle", stage="analyzing", symbol="BAH",
        ok=False, duration_s=3.0,
    )
    assert p == {
        "scope": "item", "job": "daily_cycle", "stage": "analyzing",
        "symbol": "BAH", "ok": False, "duration_s": 3.0,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ops/test_events_activity.py -v`
Expected: FAIL with `AttributeError: module 'ops.events' has no attribute 'KIND_ACTIVITY_STARTED'`

- [ ] **Step 3: Implement**

In `ops/events.py`, after the `KIND_DAILY_OVERVIEW_ERROR` line add:

```python
# Activity breadcrumbs: what the ds4-holding pipeline is doing right now.
# scope="job" brackets a whole window (daily cycle / overnight); scope="item"
# brackets one unit inside it (a symbol analysis, a memo vetting, a drain
# name). The dashboard derives "now working on", the recent-runs list, and
# durations from these pairs.
KIND_ACTIVITY_STARTED = "activity_started"
KIND_ACTIVITY_FINISHED = "activity_finished"
```

At the end of `ops/events.py` add:

```python
def activity_started_payload(
    *, scope: str, job: str, stage: str | None = None,
    symbol: str | None = None, seq: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """None-valued fields are omitted (byte-stable payload convention)."""
    payload: dict[str, Any] = {"scope": scope, "job": job}
    if stage is not None:
        payload["stage"] = stage
    if symbol is not None:
        payload["symbol"] = symbol
    if seq is not None:
        payload["seq"] = seq
    if reason is not None:
        payload["reason"] = reason
    return payload


def activity_finished_payload(
    *, scope: str, job: str, ok: bool, duration_s: float,
    stage: str | None = None, symbol: str | None = None,
    seq: str | None = None, outcome: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"scope": scope, "job": job}
    if stage is not None:
        payload["stage"] = stage
    if symbol is not None:
        payload["symbol"] = symbol
    if seq is not None:
        payload["seq"] = seq
    payload["ok"] = ok
    payload["duration_s"] = duration_s
    if outcome is not None:
        payload["outcome"] = outcome
    return payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/ops/test_events_activity.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add ops/events.py tests/ops/test_events_activity.py
git commit -m "feat(activity): activity_started/finished event kinds + payload helpers"
```

---

### Task 2: ActivityReporter + NullReporter

**Files:**
- Create: `ops/activity.py`
- Test: `tests/ops/test_activity.py` (create)

**Interfaces:**
- Consumes: Task 1's kinds/payload helpers; `ops.journal.Journal.record_event(kind, payload)`.
- Produces (all later service tasks depend on these exact names):
  - `class ActivityReporter: __init__(self, journal)`
  - `ActivityReporter.job(job: str, *, reason: str | None = None)` — context manager yielding a handle with a writable `.outcome: str | None` attribute.
  - `ActivityReporter.item(job: str, *, stage: str, symbol: str | None = None, seq: str | None = None)` — context manager yielding the same kind of handle.
  - `class NullReporter:` same two context managers, no-ops, yields a handle.
  - Both emit `activity_finished` with `ok=False` on exception and RE-RAISE.

- [ ] **Step 1: Write the failing test**

```python
"""ActivityReporter: journal-backed breadcrumb pairs; NullReporter no-ops."""
import pytest

from ops import events
from ops.activity import ActivityReporter, NullReporter
from ops.journal import Journal


@pytest.fixture()
def journal(tmp_path):
    j = Journal(str(tmp_path / "j.db"))
    yield j
    j.close()


def _activity_events(journal):
    return [e for e in journal.read_events()
            if e["kind"] in (events.KIND_ACTIVITY_STARTED,
                             events.KIND_ACTIVITY_FINISHED)]


def test_job_emits_start_and_ok_finish_with_outcome(journal):
    reporter = ActivityReporter(journal)
    with reporter.job("daily_cycle", reason="attempt 1 of 3") as h:
        h.outcome = "analyzed 2, placed 1"
    evs = _activity_events(journal)
    assert [e["kind"] for e in evs] == ["activity_started", "activity_finished"]
    assert evs[0]["payload"] == {
        "scope": "job", "job": "daily_cycle", "reason": "attempt 1 of 3"}
    fin = evs[1]["payload"]
    assert fin["scope"] == "job" and fin["job"] == "daily_cycle"
    assert fin["ok"] is True
    assert fin["outcome"] == "analyzed 2, placed 1"
    assert fin["duration_s"] >= 0


def test_item_emits_pair_with_stage_symbol_seq(journal):
    reporter = ActivityReporter(journal)
    with reporter.item("overnight", stage="vetting", symbol="CRC", seq="2/5"):
        pass
    evs = _activity_events(journal)
    assert evs[0]["payload"] == {
        "scope": "item", "job": "overnight", "stage": "vetting",
        "symbol": "CRC", "seq": "2/5"}
    assert evs[1]["payload"]["ok"] is True


def test_exception_finishes_not_ok_and_reraises(journal):
    reporter = ActivityReporter(journal)
    with pytest.raises(ValueError):
        with reporter.job("overnight"):
            raise ValueError("boom")
    evs = _activity_events(journal)
    assert evs[1]["payload"]["ok"] is False


def test_reporter_swallows_journal_write_failure(journal, capsys):
    reporter = ActivityReporter(journal)
    journal.close()  # every record_event now raises
    with reporter.job("daily_cycle"):
        pass  # must not raise despite emit failures
    assert "activity emit failed" in capsys.readouterr().err


def test_null_reporter_noops():
    reporter = NullReporter()
    with reporter.job("daily_cycle", reason="x") as h:
        h.outcome = "ignored"
    with reporter.item("overnight", stage="vetting", symbol="A"):
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ops/test_activity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.activity'`

- [ ] **Step 3: Implement `ops/activity.py`**

```python
"""ActivityReporter — journal-backed breadcrumbs for the live dashboard.

Emits activity_started/activity_finished pairs around ds4-holding work.
Deliberately best-effort: a breadcrumb must never break the work it
describes, so emit failures print to stderr and are swallowed; exceptions
from the wrapped body always re-raise after the ok=False finish."""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager

from ops import events


class ActivityHandle:
    """Mutable handle a job/item body can set an outcome summary on."""

    def __init__(self) -> None:
        self.outcome: str | None = None


class ActivityReporter:
    def __init__(self, journal) -> None:
        self._journal = journal

    def _emit(self, kind: str, payload: dict) -> None:
        try:
            self._journal.record_event(kind, payload)
        except Exception as exc:  # noqa: BLE001 - breadcrumbs are best-effort
            print(f"activity emit failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)

    @contextmanager
    def _scope(self, *, scope: str, job: str, stage: str | None = None,
               symbol: str | None = None, seq: str | None = None,
               reason: str | None = None):
        self._emit(events.KIND_ACTIVITY_STARTED, events.activity_started_payload(
            scope=scope, job=job, stage=stage, symbol=symbol, seq=seq,
            reason=reason,
        ))
        handle = ActivityHandle()
        t0 = time.monotonic()

        def finish(ok: bool) -> None:
            self._emit(
                events.KIND_ACTIVITY_FINISHED,
                events.activity_finished_payload(
                    scope=scope, job=job, stage=stage, symbol=symbol, seq=seq,
                    ok=ok, duration_s=round(time.monotonic() - t0, 3),
                    outcome=handle.outcome,
                ))

        try:
            yield handle
        except BaseException:
            finish(ok=False)
            raise
        finish(ok=True)

    def job(self, job: str, *, reason: str | None = None):
        return self._scope(scope="job", job=job, reason=reason)

    def item(self, job: str, *, stage: str, symbol: str | None = None,
             seq: str | None = None):
        return self._scope(scope="item", job=job, stage=stage, symbol=symbol,
                           seq=seq)


class NullReporter:
    """Default reporter: same interface, journals nothing."""

    @contextmanager
    def _noop(self):
        yield ActivityHandle()

    def job(self, job: str, *, reason: str | None = None):
        return self._noop()

    def item(self, job: str, *, stage: str, symbol: str | None = None,
             seq: str | None = None):
        return self._noop()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/ops/test_activity.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add ops/activity.py tests/ops/test_activity.py
git commit -m "feat(activity): ActivityReporter/NullReporter context managers"
```

---

### Task 3: Shared schedule constants (`ops/scheduler/times.py`)

**Files:**
- Create: `ops/scheduler/times.py`
- Modify: `ops/main.py:1136-1198` (`_start_full_scheduler` cron registrations use the constants)
- Test: `tests/ops/scheduler/test_times.py` (create)

**Interfaces:**
- Produces (Task 8's forecast imports these):
  - `TICK_MINUTES = (0, 30)` — orchestrator + overnight fire minutes
  - `TICK_HOUR_START = 9`, `TICK_HOUR_END = 15` — orchestrator cron hours (inclusive), ET
  - `TICK_CRON_MINUTE = "0,30"`, `TICK_CRON_HOUR = "9-15"`, `TICK_CRON_DOW = "mon-fri"` — the exact strings `ops/main.py` registers

- [ ] **Step 1: Write the failing test**

```python
"""One source of truth for scheduler cron facts (main.py + forecast)."""
from ops.scheduler import times


def test_constants():
    assert times.TICK_MINUTES == (0, 30)
    assert times.TICK_HOUR_START == 9
    assert times.TICK_HOUR_END == 15
    assert times.TICK_CRON_MINUTE == "0,30"
    assert times.TICK_CRON_HOUR == "9-15"
    assert times.TICK_CRON_DOW == "mon-fri"


def test_cron_strings_match_tuples():
    assert times.TICK_CRON_MINUTE == ",".join(str(m) for m in times.TICK_MINUTES)
    assert times.TICK_CRON_HOUR == f"{times.TICK_HOUR_START}-{times.TICK_HOUR_END}"


def test_main_uses_the_constants():
    import inspect
    from ops import main
    src = inspect.getsource(main._start_full_scheduler)
    assert "times.TICK_CRON_MINUTE" in src
    assert "times.TICK_CRON_HOUR" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ops/scheduler/test_times.py -v`
Expected: FAIL with `ImportError: cannot import name 'times'`

- [ ] **Step 3: Implement**

Create `ops/scheduler/times.py`:

```python
"""Scheduler cron facts shared by ops/main.py (job registration) and the
dashboard forecast (ops/dashboard/forecast.py). One source of truth so the
prediction can never drift from what actually fires. All times are
America/New_York (the BackgroundScheduler timezone)."""

# Orchestrator daily-cycle ticks: :00/:30, 09:00-15:30 ET, weekdays.
TICK_MINUTES = (0, 30)
TICK_HOUR_START = 9
TICK_HOUR_END = 15  # inclusive: last tick fires 15:30

TICK_CRON_MINUTE = ",".join(str(m) for m in TICK_MINUTES)
TICK_CRON_HOUR = f"{TICK_HOUR_START}-{TICK_HOUR_END}"
TICK_CRON_DOW = "mon-fri"

# The overnight research job fires every half hour all day; the deadline
# hour (config.research_drain_deadline_hour) bounds the actual window.
OVERNIGHT_CRON_MINUTE = TICK_CRON_MINUTE
```

In `ops/main.py`, add to the imports block (after `from ops.scheduler.orchestrator import Orchestrator`):

```python
from ops.scheduler import times
```

Then in `_start_full_scheduler`, replace the orchestrator trigger:

```python
    sched.add_job(
        orchestrator.tick,
        CronTrigger(minute=times.TICK_CRON_MINUTE, hour=times.TICK_CRON_HOUR,
                    day_of_week=times.TICK_CRON_DOW),
        id="orchestrator_tick", max_instances=1, misfire_grace_time=60,
    )
```

and the research_overnight trigger:

```python
        sched.add_job(
            lambda: _research_overnight_tick(journal, config),
            CronTrigger(minute=times.OVERNIGHT_CRON_MINUTE),
            id="research_overnight", max_instances=1, misfire_grace_time=600,
        )
```

(Keep the existing comments above both registrations.)

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/ops/scheduler/test_times.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add ops/scheduler/times.py ops/main.py tests/ops/scheduler/test_times.py
git commit -m "refactor(scheduler): shared cron constants module for main + forecast"
```

---

### Task 4: Pipeline adapter item instrumentation

**Files:**
- Modify: `ops/pipeline_adapter.py` (`TradingAgentsPipelineAdapter.__init__` and `propagate`)
- Test: `tests/ops/test_pipeline_adapter_activity.py` (create — do NOT edit the existing `tests/ops/test_pipeline_adapter.py`)

**Interfaces:**
- Consumes: Task 2's `ActivityReporter`/`NullReporter`.
- Produces: `TradingAgentsPipelineAdapter(backend=..., reporter=None, activity_job="daily_cycle", activity_stage="analyzing", **graph_kwargs)`. Each `propagate(symbol, ...)` is bracketed by an item event with a session-local ordinal `seq` ("1", "2", ...). `session()` resets the ordinal.

- [ ] **Step 1: Write the failing test**

```python
"""Adapter emits item breadcrumbs around each propagate()."""
from datetime import date

import pytest

from ops import events
from ops.activity import ActivityReporter
from ops.journal import Journal
from ops.pipeline_adapter import TradingAgentsPipelineAdapter


class _FakeGraph:
    def __init__(self, fail_for=frozenset()):
        self.fail_for = fail_for

    def propagate(self, symbol, iso, research_memo_context=""):
        if symbol in self.fail_for:
            raise RuntimeError("graph blew up")
        return {}, "Buy"


class _Adapter(TradingAgentsPipelineAdapter):
    def __init__(self, fail_for=frozenset(), **kwargs):
        super().__init__(**kwargs)
        self._fake = _FakeGraph(fail_for)

    def _build_graph(self):
        return self._fake


@pytest.fixture()
def journal(tmp_path):
    j = Journal(str(tmp_path / "j.db"))
    yield j
    j.close()


def _activity(journal):
    return [(e["kind"], e["payload"]) for e in journal.read_events()
            if e["kind"].startswith("activity_")]


def test_propagate_emits_item_pair_with_ordinal_seq(journal):
    adapter = _Adapter(reporter=ActivityReporter(journal),
                       activity_job="daily_cycle")
    with adapter.session():
        adapter.propagate("BAH", date(2026, 7, 14))
        adapter.propagate("CRC", date(2026, 7, 14))
    evs = _activity(journal)
    starts = [p for k, p in evs if k == events.KIND_ACTIVITY_STARTED]
    assert starts[0] == {"scope": "item", "job": "daily_cycle",
                         "stage": "analyzing", "symbol": "BAH", "seq": "1"}
    assert starts[1]["symbol"] == "CRC" and starts[1]["seq"] == "2"
    finishes = [p for k, p in evs if k == events.KIND_ACTIVITY_FINISHED]
    assert all(p["ok"] is True for p in finishes)


def test_session_resets_seq(journal):
    adapter = _Adapter(reporter=ActivityReporter(journal),
                       activity_job="overnight", activity_stage="vetting")
    with adapter.session():
        adapter.propagate("AAA", date(2026, 7, 14))
    with adapter.session():
        adapter.propagate("BBB", date(2026, 7, 14))
    starts = [p for k, p in _activity(journal)
              if k == events.KIND_ACTIVITY_STARTED]
    assert [s["seq"] for s in starts] == ["1", "1"]
    assert starts[1]["stage"] == "vetting"


def test_failed_propagate_finishes_not_ok_and_reraises(journal):
    adapter = _Adapter(fail_for={"BAD"}, reporter=ActivityReporter(journal))
    with pytest.raises(RuntimeError):
        adapter.propagate("BAD", date(2026, 7, 14))
    finishes = [p for k, p in _activity(journal)
                if k == events.KIND_ACTIVITY_FINISHED]
    assert finishes[0]["ok"] is False


def test_default_reporter_is_null(journal):
    adapter = _Adapter()
    adapter.propagate("BAH", date(2026, 7, 14))  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ops/test_pipeline_adapter_activity.py -v`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'reporter'`

- [ ] **Step 3: Implement**

In `ops/pipeline_adapter.py`, add to imports:

```python
from ops.activity import NullReporter
```

Change `TradingAgentsPipelineAdapter.__init__`:

```python
    def __init__(self, *, backend: ManagedBackend | None = None,
                 reporter=None, activity_job: str = "daily_cycle",
                 activity_stage: str = "analyzing", **graph_kwargs):
        self._kwargs = graph_kwargs
        self._graph: TradingAgentsGraph | None = None
        self._lock = threading.Lock()
        self._backend: ManagedBackend = backend or NullManagedBackend()
        self._reporter = reporter or NullReporter()
        self._activity_job = activity_job
        self._activity_stage = activity_stage
        self._seq = 0
```

Change `propagate` to bracket the work (the whole existing body moves inside the `with`):

```python
    def propagate(
        self, symbol: str, asof_date: date, research_context: str = "",
    ) -> PipelineResult:
        self._seq += 1
        with self._reporter.item(
            self._activity_job, stage=self._activity_stage,
            symbol=symbol, seq=str(self._seq),
        ):
            # Bring the managed backend up lazily — only when an analysis
            # actually runs, so ticks with no candidates never load a model.
            self._backend.ensure_up()
            graph = self._ensure_graph()
            raw, decision_text = graph.propagate(
                symbol, asof_date.isoformat(), research_memo_context=research_context,
            )
            decision = parse_decision(decision_text or "")
            raw_dict = raw if isinstance(raw, dict) else {"output": str(raw)}
            return PipelineResult(
                symbol=symbol, date=asof_date, decision=decision, raw=raw_dict,
                rating=(decision_text or "").strip(),
            )
```

Change `session` to reset the ordinal:

```python
    @contextmanager
    def session(self) -> Iterator[TradingAgentsPipelineAdapter]:
        """Bracket a batch of analyses; tear the managed backend down on exit."""
        self._seq = 0
        try:
            yield self
        finally:
            self._backend.shutdown()
```

- [ ] **Step 4: Run tests (new + existing adapter tests)**

Run: `python -m pytest tests/ops/test_pipeline_adapter_activity.py tests/ops/test_pipeline_adapter.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/pipeline_adapter.py tests/ops/test_pipeline_adapter_activity.py
git commit -m "feat(activity): pipeline adapter brackets each propagate with item events"
```

---

### Task 5: Orchestrator daily-cycle job instrumentation

**Files:**
- Modify: `ops/scheduler/orchestrator.py` (`__init__`, `_tick_impl`)
- Modify: `ops/main.py:250-276` (`_wire` builds one `ActivityReporter` and threads it into both the orchestrator and the adapter)
- Test: `tests/ops/scheduler/test_orchestrator_activity.py` (create)

**Interfaces:**
- Consumes: Task 2 (`ActivityReporter`, `NullReporter`), Task 4 (adapter kwargs).
- Produces: `Orchestrator(..., reporter=None)` — `NullReporter` default. Job event pair around the daily cycle: reason `"attempt N of 3"` (+ `", retrying failed cycle"` when N > 1), outcome `"analyzed A, placed P"`.

- [ ] **Step 1: Write the failing test**

Mirror the fixture style of `tests/ops/scheduler/test_orchestrator.py` (read it first; reuse its fakes if importable, otherwise define minimal ones as below):

```python
"""Daily-cycle job breadcrumbs from the orchestrator."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ops import events
from ops.activity import ActivityReporter
from ops.journal import Journal
from ops.scheduler.orchestrator import Orchestrator


class _Calendar:
    def is_open_now(self, at=None):
        return True


class _Broker:
    def get_positions(self):
        return []

    def get_equity(self):
        return Decimal("10000")

    def get_cash(self):
        return Decimal("10000")

    def place_order(self, order):
        pass


class _Adapter:
    def session(self):
        from contextlib import nullcontext
        return nullcontext(self)


class _Strategy:
    def propose_orders(self, *, candidates, pipeline, current_equity,
                       asof_date, live_max_position_cap, decision_sink):
        return []


class _Config:
    deny_list = frozenset()
    max_open_positions = 7
    stopout_reentry_cooldown_days = 5
    broker_mode = "paper"


@pytest.fixture()
def journal(tmp_path):
    j = Journal(str(tmp_path / "j.db"))
    yield j
    j.close()


def _orchestrator(journal):
    return Orchestrator(
        broker=_Broker(),
        universe_builder=lambda **kw: [],
        strategy=_Strategy(),
        pipeline_adapter=_Adapter(),
        calendar=_Calendar(),
        journal=journal,
        config=_Config(),
        members_loader=lambda: [],
        momentum_finder=lambda eligible, asof_date: [],
        closes_fetch=lambda *a, **kw: {},
        now_fn=lambda: datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc),
        reporter=ActivityReporter(journal),
    )


def test_cycle_emits_job_pair_with_reason_and_outcome(journal):
    _orchestrator(journal).tick()
    evs = [e for e in journal.read_events() if e["kind"].startswith("activity_")]
    job_starts = [e for e in evs if e["kind"] == events.KIND_ACTIVITY_STARTED
                  and e["payload"]["scope"] == "job"]
    assert job_starts[0]["payload"]["job"] == "daily_cycle"
    assert job_starts[0]["payload"]["reason"] == "attempt 1 of 3"
    job_fins = [e for e in evs if e["kind"] == events.KIND_ACTIVITY_FINISHED
                and e["payload"]["scope"] == "job"]
    assert job_fins[0]["payload"]["ok"] is True
    assert job_fins[0]["payload"]["outcome"] == "analyzed 0, placed 0"


def test_gated_tick_emits_nothing(journal):
    orch = _orchestrator(journal)
    orch.tick()  # completes the day's cycle
    before = len([e for e in journal.read_events()
                  if e["kind"].startswith("activity_")])
    orch.tick()  # gated: already completed today
    after = len([e for e in journal.read_events()
                 if e["kind"].startswith("activity_")])
    assert after == before


def test_second_attempt_reason_says_retrying(journal):
    # First attempt fails inside the cycle -> no completed event; the next
    # tick is attempt 2 and must say so.
    class _FailingStrategy(_Strategy):
        calls = 0

        def propose_orders(self, **kw):
            _FailingStrategy.calls += 1
            if _FailingStrategy.calls == 1:
                raise RuntimeError("LLM backend unreachable")
            return []

    orch = _orchestrator(journal)
    orch._strategy = _FailingStrategy()
    orch.tick()  # attempt 1: fails (recorded via orchestrator_tick_error)
    orch.tick()  # attempt 2
    starts = [e["payload"] for e in journal.read_events()
              if e["kind"] == events.KIND_ACTIVITY_STARTED
              and e["payload"]["scope"] == "job"]
    assert starts[0]["reason"] == "attempt 1 of 3"
    assert starts[1]["reason"] == "attempt 2 of 3, retrying failed cycle"
    fins = [e["payload"] for e in journal.read_events()
            if e["kind"] == events.KIND_ACTIVITY_FINISHED
            and e["payload"]["scope"] == "job"]
    assert fins[0]["ok"] is False
    assert fins[1]["ok"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ops/scheduler/test_orchestrator_activity.py -v`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'reporter'`

- [ ] **Step 3: Implement**

In `ops/scheduler/orchestrator.py`:

Add import:

```python
from ops.activity import NullReporter
```

`__init__` gains a `reporter=None` keyword (after `now_fn`), storing:

```python
        self._reporter = reporter if reporter is not None else NullReporter()
```

In `_tick_impl`, wrap everything AFTER the attempt-marker event in the job context. The code from `yf_pacing.snapshot_and_reset()` down to the `KIND_DAILY_CYCLE_COMPLETED` record moves one indent level into the `with` block (keep all existing comments):

```python
        # Attempt marker (recorded BEFORE the run, as before).
        self._journal.record_event(
            events.KIND_DAILY_CYCLE_RUN,
            events.daily_cycle_run_payload(asof_date=asof_date),
            at=now,
        )

        reason = f"attempt {attempts + 1} of {MAX_DAILY_CYCLE_ATTEMPTS}"
        if attempts:
            reason += ", retrying failed cycle"
        with self._reporter.job("daily_cycle", reason=reason) as activity:
            # ... existing body, indented ...
```

Inside the moved body, count placed orders — in the proposals loop, add `placed += 1` after the successful `place_order` (initialize `placed = 0` next to `decisions: list = []`), and set the outcome right after the decisions loop, before the `KIND_DAILY_CYCLE_COMPLETED` record:

```python
            activity.outcome = f"analyzed {len(decisions)}, placed {placed}"
```

(The reporter emits `ok=False` and re-raises on exception; `tick()`'s existing handler then records `orchestrator_tick_error` exactly as before.)

In `ops/main.py` `_wire`, build the reporter and thread it (replace the adapter/orchestrator construction):

```python
    from ops.activity import ActivityReporter

    reporter = ActivityReporter(journal)
    calendar = MarketCalendar()
    orchestrator = Orchestrator(
        broker=broker,
        universe_builder=build_composite_universe,
        strategy=PostEarningsMomentumStrategy(config=config),
        pipeline_adapter=TradingAgentsPipelineAdapter(
            backend=backend, reporter=reporter),
        calendar=calendar, journal=journal, config=config,
        reporter=reporter,
    )
```

- [ ] **Step 4: Run tests (new + existing orchestrator tests must stay green)**

Run: `python -m pytest tests/ops/scheduler/ -v`
Expected: all PASS (pre-existing tests unaffected — NullReporter default)

- [ ] **Step 5: Commit**

```bash
git add ops/scheduler/orchestrator.py ops/main.py tests/ops/scheduler/test_orchestrator_activity.py
git commit -m "feat(activity): daily-cycle job breadcrumbs with attempt reason + outcome"
```

---

### Task 6: Overnight window instrumentation (job bracket + drain/memo-lite items + vetting adapter)

**Files:**
- Modify: `ops/main.py` (`_research_overnight_tick`, `_research_vetting_stage`, `_short_vetting_stage`, `_short_overnight_pass` drain call, `_insider_memo_pass` call)
- Modify: `ops/research/drain.py` (`drain_pending` gains `reporter`/`activity_job` params)
- Modify: `ops/insider/memo_lite.py` (`author_pending_memos` gains `reporter` param)
- Test: `tests/ops/research/test_drain_activity.py` (create), `tests/ops/insider/test_memo_lite_activity.py` (create), `tests/ops/test_overnight_activity.py` (create)

**Interfaces:**
- Consumes: Tasks 2, 4.
- Produces:
  - `drain_pending(..., reporter=None, activity_job="overnight")` — item per attempted hit: stage `"researching"`, seq `"i/total"` where total = number of hits in this chunk after the `max_names` cap.
  - `author_pending_memos(..., reporter=None)` — item per attempted entry: job `"overnight"`, stage `"authoring_memo"`.
  - `_research_overnight_tick` wraps real work in `reporter.job("overnight", reason=...)`; reason format exactly: parts joined by `"; "` from — `"screened"` (when `screened_this_run`), `"{n} hit(s) to research"`, `"{n} memo(s) to vet"`, `"{n} short hit(s)"`, `"{n} short memo(s) to vet"`, `"{n} insider memo(s) to author"` — only non-zero parts. Outcome: `"researched {r}, vetted {v}, failed {f}"` from the tick's aggregated counters.
  - Vetting adapters constructed with `reporter=..., activity_job="overnight", activity_stage="vetting"`.

- [ ] **Step 1: Write the failing drain test** (`tests/ops/research/test_drain_activity.py`)

```python
"""drain_pending emits item breadcrumbs per attempted hit."""
import pytest

from ops import events
from ops.activity import ActivityReporter
from ops.journal import Journal
from ops.research.drain import drain_pending


class _Store:
    def __init__(self, hits):
        self._hits = list(hits)
        self.failed = []

    def pending_hits(self):
        return list(self._hits)

    def mark_researched(self, hit_id):
        self._hits = [h for h in self._hits if h["id"] != hit_id]

    def mark_failed(self, hit_id):
        self.failed.append(hit_id)
        self._hits = [h for h in self._hits if h["id"] != hit_id]


class _Outcome:
    status = "researched"
    symbol = "AAA"
    memo_id = "m1"
    recommendation = "Buy"
    evidence_kept = 1
    evidence_dropped = 0


@pytest.fixture()
def journal(tmp_path):
    j = Journal(str(tmp_path / "j.db"))
    yield j
    j.close()


def test_items_emitted_with_seq_over_chunk_total(journal):
    store = _Store([{"id": 1, "symbol": "AAA"}, {"id": 2, "symbol": "BBB"}])
    drain_pending(
        store=store, memo_store=None, evidence_llm=None, thesis_llm=None,
        thesis_model_spec="", reporter=ActivityReporter(journal),
        research_fn=lambda hit, **kw: _Outcome(),
    )
    starts = [e["payload"] for e in journal.read_events()
              if e["kind"] == events.KIND_ACTIVITY_STARTED]
    assert starts[0] == {"scope": "item", "job": "overnight",
                         "stage": "researching", "symbol": "AAA", "seq": "1/2"}
    assert starts[1]["symbol"] == "BBB" and starts[1]["seq"] == "2/2"


def test_failed_name_finishes_not_ok_and_queue_continues(journal):
    store = _Store([{"id": 1, "symbol": "BAD"}, {"id": 2, "symbol": "AAA"}])

    def research(hit, **kw):
        if hit["symbol"] == "BAD":
            raise RuntimeError("boom")
        return _Outcome()

    summary = drain_pending(
        store=store, memo_store=None, evidence_llm=None, thesis_llm=None,
        thesis_model_spec="", reporter=ActivityReporter(journal),
        research_fn=research,
    )
    assert summary.failed == 1 and summary.researched == 1
    fins = [e["payload"] for e in journal.read_events()
            if e["kind"] == events.KIND_ACTIVITY_FINISHED]
    assert [f["ok"] for f in fins] == [False, True]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/ops/research/test_drain_activity.py -v`
Expected: FAIL with `TypeError: drain_pending() got an unexpected keyword argument 'reporter'`

- [ ] **Step 3: Implement `drain_pending` instrumentation**

In `ops/research/drain.py`, add import `from ops.activity import NullReporter`, add params `reporter=None, activity_job: str = "overnight"` to `drain_pending`, and after `research_fn` resolution add `reporter = reporter or NullReporter()`. Wrap the per-hit body: the `try: outcome = research_fn(...)` call and outcome handling move inside the item context. The tricky part is that per-name failures are HANDLED (not re-raised past the loop), while the item must still record `ok=False`. Structure it so the reporter sees the exception but the loop's handling is preserved:

```python
    total = len(hits)
    for i, hit in enumerate(hits):
        if should_stop is not None and should_stop():
            break
        if deadline is not None and now() >= deadline:
            hit_deadline = True
            break
        try:
            with reporter.item(activity_job, stage="researching",
                               symbol=hit["symbol"], seq=f"{i + 1}/{total}"):
                outcome = research_fn(
                    hit, evidence_llm=evidence_llm, thesis_llm=thesis_llm,
                    memo_store=memo_store, thesis_model_spec=thesis_model_spec,
                )
                if outcome.status != "researched":
                    raise _NameFailed(outcome)
        except ResearchError:
            raise  # configuration problem: abort the whole batch
        except _NameFailed as nf:
            store.mark_failed(hit["id"])
            failed += 1
            echo(f"{nf.outcome.symbol}: FAILED — " + "; ".join(nf.outcome.errors))
            continue
        except Exception as exc:  # noqa: BLE001 - one bad name must not strand the queue
            store.mark_failed(hit["id"])
            failed += 1
            echo(f"{hit['symbol']}: FAILED ({type(exc).__name__}: {exc})")
            continue
        store.mark_researched(hit["id"])
        researched += 1
        echo(
            f"{outcome.symbol}: memo {outcome.memo_id} "
            f"({outcome.recommendation}; evidence {outcome.evidence_kept} kept"
            f"/{outcome.evidence_dropped} dropped)"
        )
```

with a module-level helper exception above `drain_pending`:

```python
class _NameFailed(Exception):
    """Internal: routes a failed ResearchOutcome through the item context
    so the breadcrumb records ok=False without changing drain semantics."""

    def __init__(self, outcome):
        self.outcome = outcome
```

- [ ] **Step 4: Run drain tests (new + existing)**

Run: `python -m pytest tests/ops/research/test_drain_activity.py tests/ops/research/test_drain.py -v`
Expected: all PASS

- [ ] **Step 5: Write the failing memo-lite test** (`tests/ops/insider/test_memo_lite_activity.py`)

Read `tests/ops/insider/test_memo_lite.py` first and reuse its fixture style for a fake `signal_store`/`thesis_llm`. The assertion core:

```python
def test_author_pending_emits_item_per_entry(journal, fake_signal_store, fake_llm, memo_store):
    from ops.activity import ActivityReporter
    from ops.insider.memo_lite import author_pending_memos

    author_pending_memos(
        signal_store=fake_signal_store, memo_store=memo_store,
        thesis_llm=fake_llm, reporter=ActivityReporter(journal),
        price_fetcher=fake_price_fetcher,
    )
    starts = [e["payload"] for e in journal.read_events()
              if e["kind"] == "activity_started"]
    assert all(s["stage"] == "authoring_memo" and s["job"] == "overnight"
               for s in starts)
    assert [s["symbol"] for s in starts] == [e["symbol"] for e in entries]
```

(Adapt names to the existing fixtures — the implementer writes the real fixture plumbing after reading that file. A failed entry must produce `ok: false` via the same `try` wrap pattern as drain: move the `_author_one`/save/`set_entry_memo` block inside `reporter.item(...)`, letting the existing `except Exception` sit OUTSIDE the `with` so the breadcrumb records the failure.)

- [ ] **Step 6: Implement `author_pending_memos` instrumentation**

In `ops/insider/memo_lite.py`: import `from ops.activity import NullReporter`, add `reporter=None` param, `reporter = reporter or NullReporter()` after `now_fn`, and wrap the loop body:

```python
    for entry in signal_store.entries_without_memo():
        if should_stop is not None and should_stop():
            break
        if deadline is not None and now_fn() >= deadline:
            break
        symbol, asof = entry["symbol"], entry["asof"]
        try:
            with reporter.item("overnight", stage="authoring_memo",
                               symbol=symbol):
                memo = _author_one(
                    symbol=symbol, asof=asof, signal_store=signal_store,
                    structured=structured, price_fetcher=price_fetcher,
                    thesis_model_spec=thesis_model_spec,
                )
                memo_store.save(memo)
                signal_store.set_entry_memo(symbol, asof, memo.memo_id)
            written += 1
            echo(f"{symbol}: memo {memo.memo_id}")
        except Exception as exc:  # noqa: BLE001 - one bad entry must not strand the queue
            echo(f"{symbol}: memo-lite FAILED ({type(exc).__name__}: {exc})")
```

Run: `python -m pytest tests/ops/insider/ -v` — all PASS.

- [ ] **Step 7: Write the failing overnight-bracket test** (`tests/ops/test_overnight_activity.py`)

Read `tests/ops/test_main_short.py` first — it exercises `_research_overnight_tick` with fake stores/backends; reuse its fixture approach. Core assertions:

```python
def test_overnight_job_bracket_reason_and_outcome(...):
    # queue state: 2 research hits pending, 1 memo pending vetting,
    # short + insider queues empty, screen not due
    _research_overnight_tick(journal, config, now=..., should_stop=..., ...)
    starts = [e["payload"] for e in journal.read_events()
              if e["kind"] == "activity_started" and e["payload"]["scope"] == "job"]
    assert starts[0]["job"] == "overnight"
    assert starts[0]["reason"] == "2 hit(s) to research; 1 memo(s) to vet"
    fins = [e["payload"] for e in journal.read_events()
            if e["kind"] == "activity_finished" and e["payload"]["scope"] == "job"]
    assert fins[0]["ok"] is True
    assert fins[0]["outcome"].startswith("researched ")


def test_idle_night_emits_no_job_events(...):
    # all queues empty, screen not due -> tick returns before the bracket
    _research_overnight_tick(journal, config, ...)
    assert not [e for e in journal.read_events()
                if e["kind"].startswith("activity_")]
```

- [ ] **Step 8: Implement the overnight bracket in `ops/main.py`**

Add a reason-builder helper above `_research_overnight_tick`:

```python
def _overnight_reason(config, *, screened_this_run: bool, store, memo_store) -> str:
    """Human reason string for the overnight job breadcrumb. Counts are
    best-effort — a missing store contributes nothing rather than raising."""
    parts: list[str] = []
    if screened_this_run:
        parts.append("screened")
    n = len(store.pending_hits())
    if n:
        parts.append(f"{n} hit(s) to research")
    n = len(memo_store.pending_vetting_memos())
    if n:
        parts.append(f"{n} memo(s) to vet")
    try:
        from ops.research.store import ScreenStore
        from tradingagents.memos.store import MemoStore

        n = len(ScreenStore(config.short_screen_store_path).pending_hits())
        if n:
            parts.append(f"{n} short hit(s)")
        n = len(MemoStore(config.short_memo_store_path).pending_vetting_memos())
        if n:
            parts.append(f"{n} short memo(s) to vet")
    except Exception:  # noqa: BLE001 - reason text must never kill the tick
        pass
    try:
        from ops.insider.store import SignalStore

        n = len(SignalStore(config.insider_signal_store_path).entries_without_memo())
        if n:
            parts.append(f"{n} insider memo(s) to author")
    except Exception:  # noqa: BLE001
        pass
    return "; ".join(parts) or "work pending"
```

In `_research_overnight_tick`, immediately after the early-return block that skips the idle night (`return` under `if research_idle:` when short+insider also idle), build the reporter and wrap everything from `base_stop = ...` (or from `backend = build_managed_backend(...)`) through the `finally: backend.shutdown()` in the job context:

```python
        from ops.activity import ActivityReporter

        reporter = ActivityReporter(journal)
        reason = _overnight_reason(
            config, screened_this_run=screened_this_run,
            store=store, memo_store=memo_store,
        )
        with reporter.job("overnight", reason=reason) as activity:
            base_stop = should_stop or _shutdown_event.is_set
            ...
            backend = build_managed_backend(load_managed_backend_config())
            ...
            try:
                while ...:
                    ...
                _short_overnight_pass(..., reporter=reporter, ...)
                _insider_memo_pass(..., reporter=reporter, ...)
                activity.outcome = (
                    f"researched {researched}, vetted {vetted}, "
                    f"failed {drain_failed + vet_failed}"
                )
            finally:
                backend.shutdown()
```

Concretely:
- The `with reporter.job(...)` line goes right before `base_stop = should_stop or _shutdown_event.is_set`; everything down to (and including) the `finally: backend.shutdown()` indents one level.
- `activity.outcome = ...` is set as the last statement inside the inner `try` (after `_insider_memo_pass`).
- Pass `reporter=reporter` into: the research `drain_pending(...)` call, `_short_overnight_pass(...)` (which forwards it to ITS `drain_pending` call — add a `reporter=None` parameter to `_short_overnight_pass` and forward), `_insider_memo_pass(...)` (add `reporter=None` param, forward to `author_pending_memos`), `_research_vetting_stage(...)` and `_short_vetting_stage(...)` (add `reporter=None` params; construct their default adapters as `TradingAgentsPipelineAdapter(backend=backend, reporter=reporter, activity_job="overnight", activity_stage="vetting")` — leave the `adapter_factory` test path untouched).
- The outer `except Exception` (records `research_drain_error`) stays outside the `with` — the reporter's `ok=False` finish fires first, then the error event records, same as before.

- [ ] **Step 9: Run the overnight tests + neighbors**

Run: `python -m pytest tests/ops/test_overnight_activity.py tests/ops/test_main_short.py tests/ops/test_main_insider.py tests/ops/research/ tests/ops/insider/ -v`
Expected: all PASS

- [ ] **Step 10: Commit**

```bash
git add ops/main.py ops/research/drain.py ops/insider/memo_lite.py tests/ops/test_overnight_activity.py tests/ops/research/test_drain_activity.py tests/ops/insider/test_memo_lite_activity.py
git commit -m "feat(activity): overnight job bracket + drain/vetting/memo-lite item breadcrumbs"
```

---

### Task 7: Activity feed renderers for the new kinds

**Files:**
- Modify: `ops/dashboard/events_view.py` (`_RENDERERS` dict)
- Test: `tests/ops/dashboard/test_events_view.py` (append test functions)

**Interfaces:**
- Consumes: payload shapes from Task 1.
- Produces: human strings — start: `"▶ overnight: vetting CRC (2/5)"` / `"▶ daily_cycle — attempt 1 of 3"`; finish: `"✓ overnight: vetting CRC (4.2s)"` / `"✗ daily_cycle — failed after 12.0s"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/ops/dashboard/test_events_view.py`, matching its existing style)

```python
def test_render_activity_started_item():
    text = render_event("activity_started", {
        "scope": "item", "job": "overnight", "stage": "vetting",
        "symbol": "CRC", "seq": "2/5"})
    assert text == "▶ overnight: vetting CRC (2/5)"


def test_render_activity_started_job_with_reason():
    text = render_event("activity_started", {
        "scope": "job", "job": "daily_cycle", "reason": "attempt 1 of 3"})
    assert text == "▶ daily_cycle — attempt 1 of 3"


def test_render_activity_finished_ok():
    text = render_event("activity_finished", {
        "scope": "item", "job": "overnight", "stage": "vetting",
        "symbol": "CRC", "ok": True, "duration_s": 4.2})
    assert text == "✓ overnight: vetting CRC (4.2s)"


def test_render_activity_finished_failed_job():
    text = render_event("activity_finished", {
        "scope": "job", "job": "daily_cycle", "ok": False, "duration_s": 12.0})
    assert text == "✗ daily_cycle — failed after 12.0s"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/ops/dashboard/test_events_view.py -v`
Expected: new tests FAIL (fallback rendering, not the pretty strings)

- [ ] **Step 3: Implement** — add above `_RENDERERS`:

```python
def _activity_desc(p: dict[str, Any]) -> str:
    """'overnight: vetting CRC (2/5)' for items; 'daily_cycle' for jobs."""
    if p.get("scope") == "item":
        bits = f"{p.get('job', '?')}: {p.get('stage', '?')}"
        if p.get("symbol"):
            bits += f" {p['symbol']}"
        if p.get("seq"):
            bits += f" ({p['seq']})"
        return bits
    return str(p.get("job", "?"))


def _render_activity_started(p: dict[str, Any]) -> str:
    desc = _activity_desc(p)
    if p.get("scope") == "job" and p.get("reason"):
        return f"▶ {desc} — {p['reason']}"
    return f"▶ {desc}"


def _render_activity_finished(p: dict[str, Any]) -> str:
    desc = _activity_desc(p)
    dur = p.get("duration_s")
    if p.get("ok"):
        return f"✓ {desc} ({dur}s)" if dur is not None else f"✓ {desc}"
    return f"✗ {desc} — failed after {dur}s" if dur is not None else f"✗ {desc} — failed"
```

and register in `_RENDERERS`:

```python
    "activity_started": _render_activity_started,
    "activity_finished": _render_activity_finished,
```

- [ ] **Step 4: Run** `python -m pytest tests/ops/dashboard/test_events_view.py -v` — all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/dashboard/events_view.py tests/ops/dashboard/test_events_view.py
git commit -m "feat(activity): feed renderers for activity breadcrumbs"
```

---

### Task 8: Snapshot `activity` section — current + recent_runs

**Files:**
- Create: `ops/dashboard/activity_view.py`
- Modify: `ops/dashboard/snapshot.py` (`build_snapshot` adds the section)
- Test: `tests/ops/dashboard/test_snapshot_activity.py` (create)

**Interfaces:**
- Consumes: Task 1 kinds; `ro_conn` from `snapshot.py`; the health section's verdict.
- Produces (Task 9 extends this dict with `next_work`; Task 10's TS types mirror it):

```python
def activity_section(config, now, *, health_verdict: str) -> dict:
    # {"current": {...}|None, "stale": bool, "recent_runs": [...]}
```

  - `current`: `{job, stage|None, symbol|None, seq|None, reason|None, started_at, age_seconds}` — newest activity event wins: a start = busy (an item finish while its job is still open falls back to the open job start); a job finish = idle (`current: None`).
  - `stale`: True when a dangling start exists but `health_verdict != "RUNNING"` or the start is older than `MAX_CURRENT_AGE_S = 4 * 3600` — `current` is then forced to None.
  - `recent_runs`: newest-first, ≤ 20, each `{job, reason|None, started_at, finished_at|None, ok|None, duration_s|None, outcome|None}`; a job start followed by a `service_started` (and no finish) gets `ok: False, outcome: "interrupted"`.

- [ ] **Step 1: Write the failing test**

```python
"""Snapshot activity section: current-work derivation + recent runs."""
from datetime import datetime, timedelta, timezone

import pytest

from ops import events
from ops.dashboard.activity_view import activity_section
from ops.journal import Journal


NOW = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)


@pytest.fixture()
def config(tmp_path):
    class _C:
        journal_path = str(tmp_path / "j.db")
    # touch the journal file so ro_conn can open it
    Journal(_C.journal_path).close()
    return _C()


def _journal(config):
    return Journal(config.journal_path)


def _start(j, *, scope, job, at, **kw):
    j.record_event(events.KIND_ACTIVITY_STARTED,
                   events.activity_started_payload(scope=scope, job=job, **kw),
                   at=at)


def _finish(j, *, scope, job, at, ok=True, duration_s=1.0, **kw):
    j.record_event(events.KIND_ACTIVITY_FINISHED,
                   events.activity_finished_payload(
                       scope=scope, job=job, ok=ok, duration_s=duration_s, **kw),
                   at=at)


def test_open_item_is_current(config):
    with _journal(config) as j:
        _start(j, scope="job", job="daily_cycle", at=NOW - timedelta(minutes=10),
               reason="attempt 1 of 3")
        _start(j, scope="item", job="daily_cycle", stage="analyzing",
               symbol="BAH", seq="3", at=NOW - timedelta(minutes=6))
    out = activity_section(config, NOW, health_verdict="RUNNING")
    assert out["current"]["symbol"] == "BAH"
    assert out["current"]["stage"] == "analyzing"
    assert out["current"]["age_seconds"] == 360.0
    assert out["stale"] is False


def test_item_finish_falls_back_to_open_job(config):
    with _journal(config) as j:
        _start(j, scope="job", job="overnight", at=NOW - timedelta(minutes=30),
               reason="2 hit(s) to research")
        _start(j, scope="item", job="overnight", stage="researching",
               symbol="AAA", at=NOW - timedelta(minutes=20))
        _finish(j, scope="item", job="overnight", stage="researching",
                symbol="AAA", at=NOW - timedelta(minutes=10))
    out = activity_section(config, NOW, health_verdict="RUNNING")
    assert out["current"]["job"] == "overnight"
    assert out["current"]["stage"] is None
    assert out["current"]["reason"] == "2 hit(s) to research"


def test_job_finish_means_idle(config):
    with _journal(config) as j:
        _start(j, scope="job", job="overnight", at=NOW - timedelta(hours=2))
        _finish(j, scope="job", job="overnight", at=NOW - timedelta(hours=1),
                outcome="researched 2, vetted 1, failed 0")
    out = activity_section(config, NOW, health_verdict="RUNNING")
    assert out["current"] is None
    assert out["stale"] is False


def test_dangling_start_with_dead_service_is_stale(config):
    with _journal(config) as j:
        _start(j, scope="item", job="daily_cycle", stage="analyzing",
               symbol="BAH", at=NOW - timedelta(minutes=5))
    out = activity_section(config, NOW, health_verdict="STOPPED")
    assert out["current"] is None
    assert out["stale"] is True


def test_dangling_start_older_than_cap_is_stale(config):
    with _journal(config) as j:
        _start(j, scope="item", job="daily_cycle", stage="analyzing",
               symbol="BAH", at=NOW - timedelta(hours=5))
    out = activity_section(config, NOW, health_verdict="RUNNING")
    assert out["current"] is None
    assert out["stale"] is True


def test_recent_runs_joined_and_interrupted(config):
    with _journal(config) as j:
        # run 1: clean
        _start(j, scope="job", job="overnight", at=NOW - timedelta(hours=12),
               reason="screened")
        _finish(j, scope="job", job="overnight", at=NOW - timedelta(hours=10),
                duration_s=7200.0, outcome="researched 3, vetted 1, failed 0")
        # run 2: interrupted by a restart
        _start(j, scope="job", job="daily_cycle", at=NOW - timedelta(hours=4),
               reason="attempt 1 of 3")
        j.record_event(events.KIND_SERVICE_STARTED, {"pid": 1},
                       at=NOW - timedelta(hours=3))
        # run 3: still open (current)
        _start(j, scope="job", job="daily_cycle", at=NOW - timedelta(minutes=10),
               reason="attempt 2 of 3, retrying failed cycle")
    out = activity_section(config, NOW, health_verdict="RUNNING")
    runs = out["recent_runs"]
    assert [r["job"] for r in runs] == ["daily_cycle", "daily_cycle", "overnight"]
    assert runs[0]["finished_at"] is None and runs[0]["ok"] is None
    assert runs[1]["ok"] is False and runs[1]["outcome"] == "interrupted"
    assert runs[2]["ok"] is True
    assert runs[2]["outcome"] == "researched 3, vetted 1, failed 0"
    assert runs[2]["duration_s"] == 7200.0


def test_missing_journal_returns_empty(config, tmp_path):
    config.journal_path = str(tmp_path / "missing.db")
    out = activity_section(config, NOW, health_verdict="UNKNOWN")
    assert out == {"current": None, "stale": False, "recent_runs": []}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/ops/dashboard/test_snapshot_activity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.dashboard.activity_view'`

- [ ] **Step 3: Implement `ops/dashboard/activity_view.py`**

```python
"""Derive the dashboard's activity section from breadcrumb events.

Read-only (ro_conn), same isolation contract as the other snapshot
sections. Everything is computed from the last ~300 activity/service
events: `current` from the newest dangling start, `recent_runs` from
job-scope start/finish pairs, interruption from service_started markers."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from ops import events
from ops.dashboard.snapshot import ro_conn

# A single item never legitimately runs this long; a dangling start older
# than this is a crash artifact, not live work.
MAX_CURRENT_AGE_S = 4 * 3600.0
_FETCH_LIMIT = 300
_MAX_RUNS = 20


def _rows(config) -> list[dict[str, Any]]:
    """Newest-first activity + service_started rows."""
    try:
        with closing(ro_conn(config.journal_path)) as conn:
            rows = conn.execute(
                "SELECT id, at, kind, payload FROM events WHERE kind IN (?,?,?)"
                " ORDER BY id DESC LIMIT ?",
                (events.KIND_ACTIVITY_STARTED, events.KIND_ACTIVITY_FINISHED,
                 events.KIND_SERVICE_STARTED, _FETCH_LIMIT),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except (TypeError, ValueError):
            payload = {}
        out.append({"id": r["id"], "at": datetime.fromisoformat(r["at"]),
                    "kind": r["kind"], "payload": payload})
    return out


def _as_current(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    p = row["payload"]
    return {
        "job": p.get("job"), "stage": p.get("stage"),
        "symbol": p.get("symbol"), "seq": p.get("seq"),
        "reason": p.get("reason"), "started_at": row["at"],
        "age_seconds": (now - row["at"]).total_seconds(),
    }


def _find_current(rows: list[dict[str, Any]], now: datetime) -> tuple[dict | None, bool]:
    """(current, is_dangling): newest activity event decides; item finishes
    fall back to the still-open enclosing job start."""
    for row in rows:
        if row["kind"] == events.KIND_SERVICE_STARTED:
            continue
        p = row["payload"]
        if row["kind"] == events.KIND_ACTIVITY_STARTED:
            return _as_current(row, now), True
        if p.get("scope") == "job":
            return None, False  # job finished cleanly -> idle
        # item finished; is its job still open? (job start w/o later job finish)
        for r2 in rows:
            if r2["id"] >= row["id"] or r2["kind"] == events.KIND_SERVICE_STARTED:
                continue
            p2 = r2["payload"]
            if p2.get("scope") != "job":
                continue
            if r2["kind"] == events.KIND_ACTIVITY_STARTED:
                return _as_current(r2, now), True
            return None, False  # newest job-scope event is a finish -> idle
        return None, False
    return None, False


def _recent_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Job starts newest-first, joined to the next finish for the same job.
    rows is newest-first; walk chronologically (reversed) tracking one open
    run per job name."""
    runs: list[dict[str, Any]] = []
    open_runs: dict[str, dict[str, Any]] = {}
    for row in reversed(rows):
        p = row["payload"]
        if row["kind"] == events.KIND_SERVICE_STARTED:
            for run in open_runs.values():
                run["ok"] = False
                run["outcome"] = "interrupted"
                run["_closed"] = True
            open_runs.clear()
            continue
        if p.get("scope") != "job":
            continue
        if row["kind"] == events.KIND_ACTIVITY_STARTED:
            run = {"job": p.get("job"), "reason": p.get("reason"),
                   "started_at": row["at"], "finished_at": None, "ok": None,
                   "duration_s": None, "outcome": None}
            # a same-job start while one is open supersedes it (crash w/o
            # a service_started in the fetch window)
            prev = open_runs.get(run["job"])
            if prev is not None and not prev.get("_closed"):
                prev["ok"] = False
                prev["outcome"] = "interrupted"
            open_runs[run["job"]] = run
            runs.append(run)
        else:
            run = open_runs.pop(p.get("job"), None)
            if run is not None:
                run["finished_at"] = row["at"]
                run["ok"] = bool(p.get("ok"))
                run["duration_s"] = p.get("duration_s")
                run["outcome"] = p.get("outcome")
    for run in runs:
        run.pop("_closed", None)
    runs.reverse()
    return runs[:_MAX_RUNS]


def activity_section(config, now: datetime, *, health_verdict: str) -> dict[str, Any]:
    rows = _rows(config)
    current, dangling = _find_current(rows, now)
    stale = False
    if dangling and current is not None:
        if health_verdict != "RUNNING" or current["age_seconds"] > MAX_CURRENT_AGE_S:
            current, stale = None, True
    return {"current": current, "stale": stale, "recent_runs": _recent_runs(rows)}
```

- [ ] **Step 4: Wire into `build_snapshot`** (`ops/dashboard/snapshot.py`)

The activity section needs the health verdict; build health first, then pass it:

```python
def build_snapshot(
    config: OpsConfig, *, now: datetime | None = None,
) -> dict[str, Any]:
    from ops.dashboard.activity_view import activity_section

    when = now if now is not None else datetime.now(timezone.utc)
    health = section(lambda: _health_section(config, when))
    verdict = health.get("verdict", "UNKNOWN") if "error" not in health else "UNKNOWN"
    return {
        "generated_at": when.isoformat(),
        "health": health,
        "sleeves": section(lambda: _sleeves_section(config, when)),
        "funnel": section(lambda: _funnel_section(config, when)),
        "anomalies_7d": section(lambda: _anomalies_section(config, when)),
        "market": section(lambda: _market_section(config, when)),
        "activity": section(
            lambda: activity_section(config, when, health_verdict=verdict)),
    }
```

- [ ] **Step 5: Run**

Run: `python -m pytest tests/ops/dashboard/ -v`
Expected: all PASS (new + existing snapshot tests)

- [ ] **Step 6: Commit**

```bash
git add ops/dashboard/activity_view.py ops/dashboard/snapshot.py tests/ops/dashboard/test_snapshot_activity.py
git commit -m "feat(dashboard): snapshot activity section — current work + recent runs"
```

---

### Task 9: Gate-aware next-work forecast

**Files:**
- Create: `ops/dashboard/forecast.py`
- Modify: `ops/dashboard/activity_view.py` (`activity_section` adds `next_work`)
- Test: `tests/ops/dashboard/test_forecast.py` (create)

**Interfaces:**
- Consumes: Task 3 constants; `MarketCalendar.is_trading_day`; `Journal(readonly=True)` gates; `ro_conn` store queries; `MAX_DAILY_CYCLE_ATTEMPTS` from `ops.scheduler.orchestrator`.
- Produces: `next_work(config, *, now: datetime, calendar=None) -> list[dict]`, each entry `{"at": datetime (UTC), "job": str, "purpose": str}` sorted by `at`. `calendar` injectable for tests (needs only `is_trading_day(date) -> bool`).
  - Daily cycle purposes: `"daily cycle: leaderboard, exits, up to {config.daily_analysis_budget} analyses"`; retry variant: `"retry daily cycle, attempt {n} of {MAX_DAILY_CYCLE_ATTEMPTS}"`.
  - Overnight purposes: joined by `" · "` from `"screen due"`, `"{n} hit(s) to research"`, `"{n} memo(s) to vet"`, `"{n} insider memo(s) to author"`; empty → `"likely idle: queues empty, screen not due until {YYYY-MM-DD}"`.

- [ ] **Step 1: Write the failing test**

```python
"""Gate-aware next-work forecast. All scenarios frozen-clock."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ops import events
from ops.dashboard.forecast import next_work
from ops.journal import Journal

ET = ZoneInfo("America/New_York")


class _Calendar:
    """Weekdays are trading days; no holidays."""

    def is_trading_day(self, d):
        return d.weekday() < 5


@pytest.fixture()
def config(tmp_path):
    class _C:
        journal_path = str(tmp_path / "j.db")
        screen_store_path = str(tmp_path / "screen.db")
        short_screen_store_path = str(tmp_path / "short_screen.db")
        memo_store_path = str(tmp_path / "memos.db")
        short_memo_store_path = str(tmp_path / "short_memos.db")
        insider_signal_store_path = str(tmp_path / "insider.db")
        research_screen_interval_days = 3
        research_drain_deadline_hour = 8
        daily_analysis_budget = 8
    Journal(_C.journal_path).close()
    return _C()


def _et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def _seed_screen_run(config, created_at_iso):
    from ops.research.store import ScreenStore
    store = ScreenStore(config.screen_store_path)
    store.record_run(asof=date(2026, 7, 12), universe_size=500,
                     passed=[], created_at=created_at_iso)


# --- daily cycle ---

def test_cycle_not_done_predicts_next_halfhour_tick(config):
    # Tuesday 2026-07-14 13:12 ET, cycle not completed, 1 failed attempt
    with Journal(config.journal_path) as j:
        j.record_event(events.KIND_DAILY_CYCLE_RUN, {"asof_date": "2026-07-14"},
                       at=_et(2026, 7, 14, 10, 0))
    out = next_work(config, now=_et(2026, 7, 14, 13, 12), calendar=_Calendar())
    cycle = [w for w in out if w["job"] == "daily_cycle"][0]
    assert cycle["at"] == _et(2026, 7, 14, 13, 30).astimezone(timezone.utc)
    assert cycle["purpose"] == "retry daily cycle, attempt 2 of 3"


def test_cycle_done_today_predicts_tomorrow(config):
    with Journal(config.journal_path) as j:
        j.record_event(events.KIND_DAILY_CYCLE_COMPLETED,
                       {"asof_date": "2026-07-14"}, at=_et(2026, 7, 14, 10, 0))
    out = next_work(config, now=_et(2026, 7, 14, 13, 12), calendar=_Calendar())
    cycle = [w for w in out if w["job"] == "daily_cycle"][0]
    assert cycle["at"] == _et(2026, 7, 15, 9, 0).astimezone(timezone.utc)
    assert cycle["purpose"] == (
        "daily cycle: leaderboard, exits, up to 8 analyses")


def test_friday_evening_predicts_monday(config):
    # Friday 2026-07-17 16:00 ET, cycle done
    with Journal(config.journal_path) as j:
        j.record_event(events.KIND_DAILY_CYCLE_COMPLETED,
                       {"asof_date": "2026-07-17"}, at=_et(2026, 7, 17, 10, 0))
    out = next_work(config, now=_et(2026, 7, 17, 16, 0), calendar=_Calendar())
    cycle = [w for w in out if w["job"] == "daily_cycle"][0]
    assert cycle["at"] == _et(2026, 7, 20, 9, 0).astimezone(timezone.utc)


def test_attempts_exhausted_predicts_tomorrow(config):
    with Journal(config.journal_path) as j:
        for h in (9, 10, 11):
            j.record_event(events.KIND_DAILY_CYCLE_RUN,
                           {"asof_date": "2026-07-14"}, at=_et(2026, 7, 14, h, 0))
    out = next_work(config, now=_et(2026, 7, 14, 11, 40), calendar=_Calendar())
    cycle = [w for w in out if w["job"] == "daily_cycle"][0]
    assert cycle["at"].astimezone(ET).date() == date(2026, 7, 15)


# --- overnight ---

def test_overnight_purpose_lists_queues(config):
    from ops.research.store import ScreenStore
    store = ScreenStore(config.screen_store_path)
    store.enqueue_hit(symbol="AAA", asof=date(2026, 7, 14), payload={},
                      screen_ttl_days=0)
    store.enqueue_hit(symbol="BBB", asof=date(2026, 7, 14), payload={},
                      screen_ttl_days=0)
    out = next_work(config, now=_et(2026, 7, 14, 13, 0), calendar=_Calendar())
    night = [w for w in out if w["job"] == "overnight"][0]
    assert night["at"] == _et(2026, 7, 15, 0, 0).astimezone(timezone.utc)
    assert "2 hit(s) to research" in night["purpose"]
    assert "screen due" in night["purpose"]  # no screen run recorded yet


def test_overnight_idle_when_queues_empty_and_screen_fresh(config):
    _seed_screen_run(config, "2026-07-14T04:00:00+00:00")
    out = next_work(config, now=_et(2026, 7, 14, 13, 0), calendar=_Calendar())
    night = [w for w in out if w["job"] == "overnight"][0]
    assert night["purpose"].startswith("likely idle: queues empty")
    assert "2026-07-17" in night["purpose"]


def test_inside_window_predicts_next_halfhour(config):
    # 01:10 ET: the overnight window is live; next fire is 01:30
    out = next_work(config, now=_et(2026, 7, 15, 1, 10), calendar=_Calendar())
    night = [w for w in out if w["job"] == "overnight"][0]
    assert night["at"] == _et(2026, 7, 15, 1, 30).astimezone(timezone.utc)


def test_sorted_by_time(config):
    out = next_work(config, now=_et(2026, 7, 14, 13, 0), calendar=_Calendar())
    ats = [w["at"] for w in out]
    assert ats == sorted(ats)
```

Note for the implementer: check `ScreenStore.record_run`/`enqueue_hit` signatures in `ops/research/store.py` before writing the seeds — if `record_run` doesn't accept `created_at`, insert the row with raw SQL in the test instead:

```python
import sqlite3
conn = sqlite3.connect(config.screen_store_path)
conn.execute("INSERT INTO screen_runs (run_id, asof, created_at, universe_size, passed_count)"
             " VALUES ('r1', '2026-07-12', ?, 500, 0)", (created_at_iso,))
conn.commit(); conn.close()
```

(Instantiating the real `ScreenStore` in tests is fine — tests may write; only the forecast itself must stay read-only.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/ops/dashboard/test_forecast.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.dashboard.forecast'`

- [ ] **Step 3: Implement `ops/dashboard/forecast.py`**

```python
"""Gate-aware forecast of the next real ds4 work.

Pure read-only computation: cron facts from ops.scheduler.times, day gates
from the momentum journal, queue depths via mode=ro SQL (never the store
classes — instantiating them runs CREATE TABLE writes). Purposes describe
what the run WILL do, not just when the scheduler fires — a tick that
would no-op through its gates is not "work" and is skipped."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ops import events
from ops.dashboard.snapshot import ro_conn
from ops.journal import Journal
from ops.scheduler import times
from ops.scheduler.orchestrator import MAX_DAILY_CYCLE_ATTEMPTS

ET = ZoneInfo("America/New_York")


def _count(path: str, sql: str, params: tuple = ()) -> int:
    """ro count; a missing store is an empty queue, not an error."""
    try:
        with closing(ro_conn(path)) as conn:
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row is not None else 0
    except sqlite3.OperationalError:
        return 0


def _last_screen_run_at(path: str) -> datetime | None:
    try:
        with closing(ro_conn(path)) as conn:
            row = conn.execute(
                "SELECT created_at FROM screen_runs"
                " ORDER BY created_at DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    dt = datetime.fromisoformat(row[0])
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _next_half_hour(now_et: datetime) -> datetime:
    base = now_et.replace(second=0, microsecond=0)
    if base.minute < 30:
        return base.replace(minute=30)
    return (base + timedelta(hours=1)).replace(minute=0)


def _next_cycle_tick(now_et: datetime, calendar, *, skip_today: bool) -> datetime:
    """Next :00/:30 tick within TICK_HOUR_START..TICK_HOUR_END on a trading
    day, strictly after now (or from tomorrow when skip_today)."""
    day = now_et.date()
    for _ in range(14):  # two weeks covers any holiday cluster
        if calendar.is_trading_day(day) and not (skip_today and day == now_et.date()):
            for hour in range(times.TICK_HOUR_START, times.TICK_HOUR_END + 1):
                for minute in times.TICK_MINUTES:
                    tick = datetime(day.year, day.month, day.day, hour, minute,
                                    tzinfo=ET)
                    if tick > now_et:
                        return tick
        day += timedelta(days=1)
    raise RuntimeError("no trading day within 14 days")


def _cycle_entry(config, now_et: datetime, calendar) -> dict[str, Any]:
    with Journal(config.journal_path, readonly=True) as j:
        done_today = j.has_event_today(
            events.KIND_DAILY_CYCLE_COMPLETED, now=now_et)
        from ops.trading_time import trading_day_start
        attempts = j.count_events(
            events.KIND_DAILY_CYCLE_RUN, since=trading_day_start(now_et))
    skip_today = done_today or attempts >= MAX_DAILY_CYCLE_ATTEMPTS
    at = _next_cycle_tick(now_et, calendar, skip_today=skip_today)
    if not skip_today and attempts > 0:
        purpose = (f"retry daily cycle, attempt {attempts + 1}"
                   f" of {MAX_DAILY_CYCLE_ATTEMPTS}")
    else:
        purpose = (f"daily cycle: leaderboard, exits, up to "
                   f"{config.daily_analysis_budget} analyses")
    return {"at": at.astimezone(timezone.utc), "job": "daily_cycle",
            "purpose": purpose}


def _overnight_entry(config, now_et: datetime) -> dict[str, Any]:
    deadline_h = config.research_drain_deadline_hour
    in_window = now_et.hour < deadline_h or now_et.weekday() >= 5
    if in_window:
        at = _next_half_hour(now_et)
    else:
        at = (now_et + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)

    pending_sql = "SELECT COUNT(*) FROM screen_hits WHERE status = 'pending'"
    vet_sql = "SELECT COUNT(*) FROM memos WHERE status = 'pending_vetting'"
    hits = (_count(config.screen_store_path, pending_sql)
            + _count(config.short_screen_store_path, pending_sql))
    memos = (_count(config.memo_store_path, vet_sql)
             + _count(config.short_memo_store_path, vet_sql))
    insider = _count(config.insider_signal_store_path,
                     "SELECT COUNT(*) FROM sleeve_entries WHERE memo_id = ''")

    last_screen = _last_screen_run_at(config.screen_store_path)
    interval = timedelta(days=config.research_screen_interval_days)
    screen_due = last_screen is None or (at.astimezone(timezone.utc)
                                         - last_screen) >= interval

    parts: list[str] = []
    if screen_due:
        parts.append("screen due")
    if hits:
        parts.append(f"{hits} hit(s) to research")
    if memos:
        parts.append(f"{memos} memo(s) to vet")
    if insider:
        parts.append(f"{insider} insider memo(s) to author")
    if parts:
        purpose = " · ".join(parts)
    else:
        due_date = (last_screen + interval).astimezone(ET).date()
        purpose = (f"likely idle: queues empty, screen not due until "
                   f"{due_date.isoformat()}")
    return {"at": at.astimezone(timezone.utc), "job": "overnight",
            "purpose": purpose}


def next_work(config, *, now: datetime, calendar=None) -> list[dict[str, Any]]:
    if calendar is None:
        from ops.scheduler.market_calendar import MarketCalendar
        calendar = MarketCalendar()
    now_et = now.astimezone(ET)
    entries = [
        _cycle_entry(config, now_et, calendar),
        _overnight_entry(config, now_et),
    ]
    entries.sort(key=lambda e: e["at"])
    return entries
```

- [ ] **Step 4: Wire into the activity section** — in `ops/dashboard/activity_view.py`:

```python
def activity_section(config, now: datetime, *, health_verdict: str) -> dict[str, Any]:
    from ops.dashboard.forecast import next_work

    rows = _rows(config)
    current, dangling = _find_current(rows, now)
    stale = False
    if dangling and current is not None:
        if health_verdict != "RUNNING" or current["age_seconds"] > MAX_CURRENT_AGE_S:
            current, stale = None, True
    return {
        "current": current, "stale": stale, "recent_runs": _recent_runs(rows),
        "next_work": next_work(config, now=now),
    }
```

Update Task 8's `test_missing_journal_returns_empty` expectation to tolerate `next_work` (assert the other keys individually instead of full-dict equality), and note the Task 8 test config must then carry the store-path/interval fields from THIS task's config fixture (extend the fixture accordingly). `next_work` failures are covered by the section's outer `section(...)` isolation, but a missing store must not raise (the `_count` guards).

- [ ] **Step 5: Run**

Run: `python -m pytest tests/ops/dashboard/ -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add ops/dashboard/forecast.py ops/dashboard/activity_view.py tests/ops/dashboard/test_forecast.py tests/ops/dashboard/test_snapshot_activity.py
git commit -m "feat(dashboard): gate-aware next-work forecast in the activity section"
```

---

### Task 10: UI types + pure formatting helpers

**Files:**
- Modify: `dashboard-ui/src/data/types.ts`
- Create: `dashboard-ui/src/lib/activity.ts`
- Test: `dashboard-ui/src/lib/activity.test.ts` (create)

**Interfaces:**
- Consumes: the JSON shapes from Tasks 8–9 (datetimes arrive as ISO strings, durations as numbers).
- Produces (Task 11's components consume):
  - Types: `CurrentActivity`, `RunRow`, `NextWork`, `Activity` (and `Snapshot.activity: Section<Activity>`).
  - `nowLine(activity, healthVerdict) -> { state: "busy" | "idle" | "stale" | "unknown"; text: string }`
  - `fmtDur(s: number | null | undefined) -> string` — `"—"`, `"42s"`, `"12m"`, `"2h 05m"`
  - `runOutcome(r: RunRow) -> string` — outcome text or `"running…"` / `"interrupted"`

- [ ] **Step 1: Write the failing test**

```typescript
import { describe, expect, it } from "vitest";
import type { Activity } from "../data/types";
import { fmtDur, nowLine, runOutcome } from "./activity";

const base: Activity = { current: null, stale: false, recent_runs: [], next_work: [] };

describe("fmtDur", () => {
  it("formats", () => {
    expect(fmtDur(null)).toBe("—");
    expect(fmtDur(42)).toBe("42s");
    expect(fmtDur(720)).toBe("12m");
    expect(fmtDur(7500)).toBe("2h 05m");
  });
});

describe("nowLine", () => {
  it("busy: item with seq", () => {
    const a: Activity = {
      ...base,
      current: {
        job: "daily_cycle", stage: "analyzing", symbol: "BAH", seq: "3",
        reason: null, started_at: "2026-07-14T16:40:00+00:00", age_seconds: 360,
      },
    };
    const line = nowLine(a, "RUNNING");
    expect(line.state).toBe("busy");
    expect(line.text).toBe("daily cycle — analyzing BAH (3)");
  });

  it("busy: job-level fallback shows reason", () => {
    const a: Activity = {
      ...base,
      current: {
        job: "overnight", stage: null, symbol: null, seq: null,
        reason: "2 hit(s) to research", started_at: "2026-07-14T04:00:00+00:00",
        age_seconds: 60,
      },
    };
    expect(nowLine(a, "RUNNING").text).toBe("overnight — 2 hit(s) to research");
  });

  it("idle: shows next work headline", () => {
    const a: Activity = {
      ...base,
      next_work: [{ at: "2026-07-15T04:00:00+00:00", job: "overnight",
                    purpose: "screen due · 2 hit(s) to research" }],
    };
    const line = nowLine(a, "RUNNING");
    expect(line.state).toBe("idle");
    expect(line.text).toContain("overnight");
    expect(line.text).toContain("screen due · 2 hit(s) to research");
  });

  it("stale wins", () => {
    expect(nowLine({ ...base, stale: true }, "STOPPED").state).toBe("stale");
  });

  it("null activity is unknown", () => {
    expect(nowLine(null, "UNKNOWN").state).toBe("unknown");
  });
});

describe("runOutcome", () => {
  const run = {
    job: "overnight", reason: null, started_at: "x", finished_at: null,
    ok: null, duration_s: null, outcome: null,
  };
  it("open run", () => expect(runOutcome(run)).toBe("running…"));
  it("finished", () =>
    expect(runOutcome({ ...run, finished_at: "y", ok: true,
                        outcome: "researched 4" })).toBe("researched 4"));
  it("failed without outcome", () =>
    expect(runOutcome({ ...run, finished_at: "y", ok: false })).toBe("failed"));
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd dashboard-ui && npm test`
Expected: FAIL — `Cannot find module './activity'`

- [ ] **Step 3: Implement**

Append to `dashboard-ui/src/data/types.ts`:

```typescript
export interface CurrentActivity {
  job: string; stage: string | null; symbol: string | null;
  seq: string | null; reason: string | null;
  started_at: string; age_seconds: number;
}

export interface RunRow {
  job: string; reason: string | null; started_at: string;
  finished_at: string | null; ok: boolean | null;
  duration_s: number | null; outcome: string | null;
}

export interface NextWork { at: string; job: string; purpose: string }

export interface Activity {
  current: CurrentActivity | null;
  stale: boolean;
  recent_runs: RunRow[];
  next_work: NextWork[];
}
```

and add to the `Snapshot` interface:

```typescript
  activity: Section<Activity>;
```

Create `dashboard-ui/src/lib/activity.ts`:

```typescript
import type { Activity } from "../data/types";
import { hhmmET } from "./format";

// "daily_cycle" -> "daily cycle" for display.
const jobLabel = (job: string) => job.replace(/_/g, " ");

export function fmtDur(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

export interface NowLineResult {
  state: "busy" | "idle" | "stale" | "unknown";
  text: string;
}

export function nowLine(
  a: Activity | null, healthVerdict: string,
): NowLineResult {
  if (a == null) return { state: "unknown", text: "activity unavailable" };
  if (a.stale) {
    return { state: "stale",
             text: `activity trail went cold (service ${healthVerdict})` };
  }
  const c = a.current;
  if (c != null) {
    let what: string;
    if (c.stage != null) {
      what = c.stage + (c.symbol ? ` ${c.symbol}` : "") + (c.seq ? ` (${c.seq})` : "");
    } else {
      what = c.reason ?? "working";
    }
    return { state: "busy", text: `${jobLabel(c.job)} — ${what}` };
  }
  const next = a.next_work[0];
  if (next != null) {
    return { state: "idle",
             text: `idle — next: ${jobLabel(next.job)} ${hhmmET(next.at)} — ${next.purpose}` };
  }
  return { state: "idle", text: "idle" };
}

export function runOutcome(r: {
  finished_at: string | null; ok: boolean | null; outcome: string | null;
}): string {
  if (r.finished_at == null && r.ok == null) return r.outcome ?? "running…";
  if (r.outcome != null) return r.outcome;
  return r.ok ? "done" : "failed";
}
```

Check `dashboard-ui/src/lib/format.ts` for an existing ET time formatter; if `hhmmET` does not exist, add it there:

```typescript
export function hhmmET(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", {
    hour: "2-digit", minute: "2-digit", hour12: false,
    timeZone: "America/New_York",
  }) + " ET";
}
```

and adjust the `nowLine` idle text expectation in the test to match its output (the test above uses `toContain`, so it passes regardless of exact time formatting).

- [ ] **Step 4: Run** `cd dashboard-ui && npm test` — all PASS (new + existing)

- [ ] **Step 5: Commit**

```bash
git add dashboard-ui/src/data/types.ts dashboard-ui/src/lib/activity.ts dashboard-ui/src/lib/activity.test.ts dashboard-ui/src/lib/format.ts
git commit -m "feat(dashboard-ui): activity types + now-line/duration formatting helpers"
```

---

### Task 11: NowStrip + RunsPanel components, App wiring, CSS

**Files:**
- Create: `dashboard-ui/src/components/NowStrip.tsx`
- Create: `dashboard-ui/src/components/RunsPanel.tsx`
- Modify: `dashboard-ui/src/App.tsx` (wire both)
- Modify: `dashboard-ui/src/app.css` (strip + runs styling)

**Interfaces:**
- Consumes: Task 10 types/helpers; `Section`/`isErr` from `data/types`; `hhmmss`, `relAge` from `lib/format`; `Unavail` component.
- Produces: `<NowStrip activity={...} health={...} />`, `<RunsPanel activity={...} />`.

- [ ] **Step 1: Implement `NowStrip.tsx`**

```tsx
import type { Activity, Health, Section } from "../data/types";
import { isErr } from "../data/types";
import { nowLine } from "../lib/activity";
import { relAge } from "../lib/format";

export default function NowStrip({ activity, health }: {
  activity: Section<Activity> | null;
  health: Section<Health> | null;
}) {
  const a = activity && !isErr(activity) ? activity : null;
  const verdict = health && !isErr(health) ? health.verdict : "UNKNOWN";
  const line = nowLine(a, verdict);
  const started = a?.current?.started_at;
  return (
    <div className={`now-strip now-${line.state}`}>
      <span className="now-dot" aria-hidden="true" />
      <span className="now-text">{line.text}</span>
      {line.state === "busy" && started && (
        <span className="now-age">started {relAge(started)} ago</span>
      )}
      {activity && isErr(activity) && (
        <span className="now-age">{activity.error}</span>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Implement `RunsPanel.tsx`**

```tsx
import type { Activity, Section } from "../data/types";
import { isErr } from "../data/types";
import { fmtDur, runOutcome } from "../lib/activity";
import { hhmmss, relAge } from "../lib/format";
import Unavail from "./Unavail";

export default function RunsPanel({ activity }: {
  activity: Section<Activity> | null;
}) {
  const a = activity && !isErr(activity) ? activity : null;
  return (
    <div className="panel">
      <div className="panel-head"><span>Runs</span></div>
      {activity && isErr(activity) ? <Unavail msg={activity.error} /> : (
        <>
          <div className="runs">
            {(a?.recent_runs ?? []).length === 0 && (
              <div className="panel-empty">no runs recorded yet</div>
            )}
            {(a?.recent_runs ?? []).map((r, i) => (
              <div key={`${r.job}-${r.started_at}-${i}`}
                className={`run-row${r.ok === false ? " run-bad" : ""}`}>
                <span className="t">{hhmmss(r.started_at)}</span>
                <span className="run-job">{r.job.replace(/_/g, " ")}</span>
                <span className="run-detail">
                  {r.reason && <span className="sub">{r.reason} · </span>}
                  {runOutcome(r)}
                </span>
                <span className="run-dur">{fmtDur(r.duration_s)}</span>
              </div>
            ))}
          </div>
          {(a?.next_work ?? []).length > 0 && (
            <div className="runs-next">
              {(a?.next_work ?? []).map((w) => (
                <div key={`${w.job}-${w.at}`} className="kv">
                  <span className="k">next {w.job.replace(/_/g, " ")}</span>
                  <span className="v">{hhmmss(w.at)}{" "}
                    <span className="sub">· {relAge(w.at)} · {w.purpose}</span>
                  </span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
```

(Note: `relAge` on a FUTURE timestamp — check its implementation in `lib/format.ts`; if it clamps or renders nonsense for future times, render just `w.purpose` without the age instead. The implementer verifies and adapts.)

- [ ] **Step 3: Wire into `App.tsx`**

Add imports:

```tsx
import NowStrip from "./components/NowStrip";
import RunsPanel from "./components/RunsPanel";
```

Insert `<NowStrip …>` directly after `<AlertBanner …/>` (before `<div className="wrap">`):

```tsx
      <AlertBanner health={health} />
      <NowStrip activity={snap?.activity ?? null} health={snap?.health ?? null} />
```

Insert `<RunsPanel …>` in the right column between `<OvernightPanel …/>` and `<AnomaliesPanel …/>`:

```tsx
            <OvernightPanel funnel={snap?.funnel ?? null} />
            <RunsPanel activity={snap?.activity ?? null} />
            <AnomaliesPanel anomalies={snap?.anomalies_7d ?? null} />
```

- [ ] **Step 4: CSS** — append to `dashboard-ui/src/app.css`, reusing the existing custom properties (inspect the file's variable names first — `--panel-bg`, `--warn`, etc. — and match them):

```css
/* ---- now strip ---- */
.now-strip {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 18px; font-size: 13px;
  border-bottom: 1px solid var(--border, #2a2f3a);
}
.now-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.now-busy .now-dot { background: var(--ok, #4caf7d); animation: now-pulse 1.6s ease-in-out infinite; }
.now-idle .now-dot { background: var(--muted, #6b7280); }
.now-stale .now-dot, .now-unknown .now-dot { background: var(--warn, #e0a458); }
.now-text { font-weight: 600; }
.now-age { color: var(--muted, #6b7280); font-size: 12px; }
@keyframes now-pulse { 50% { opacity: 0.3; } }

/* ---- runs panel ---- */
.runs { max-height: 260px; overflow-y: auto; }
.run-row {
  display: flex; gap: 8px; align-items: baseline;
  padding: 5px 14px; font-size: 12px;
  border-bottom: 1px solid var(--border, #2a2f3a);
}
.run-row .t { flex: none; }
.run-job { flex: none; font-weight: 600; min-width: 82px; }
.run-detail { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.run-dur { flex: none; color: var(--muted, #6b7280); }
.run-bad { background: color-mix(in srgb, var(--bad, #c0504d) 12%, transparent); }
.runs-next { padding: 6px 14px; border-top: 1px solid var(--border, #2a2f3a); }
```

- [ ] **Step 5: Type-check + tests + dev-render sanity**

Run: `cd dashboard-ui && npx tsc --noEmit && npm test`
Expected: clean type-check, all tests PASS

- [ ] **Step 6: Commit**

```bash
git add dashboard-ui/src/components/NowStrip.tsx dashboard-ui/src/components/RunsPanel.tsx dashboard-ui/src/App.tsx dashboard-ui/src/app.css
git commit -m "feat(dashboard-ui): NowStrip live-activity banner + RunsPanel history/forecast"
```

---

### Task 12: Build the bundle, extend the shipped-bundle test, full verification

**Files:**
- Modify: `tests/ops/dashboard/test_snapshot_sleeves.py` (extend `test_shipped_bundle_contains_every_backend_sleeve`'s neighborhood with a new test)
- Regenerate: `ops/dashboard/static/**` (vite build output)

- [ ] **Step 1: Write the failing bundle test** — append to `tests/ops/dashboard/test_snapshot_sleeves.py`, following the exact style of `test_shipped_bundle_contains_every_backend_sleeve` (line ~229, reads the bundle as bytes):

```python
def test_shipped_bundle_contains_activity_ui():
    """The built app must ship the activity strip and runs panel — a stale
    bundle silently hides the feature."""
    bundle_path = (Path(__file__).resolve().parents[3]
                   / "ops" / "dashboard" / "static" / "assets" / "app.js")
    bundle_bytes = bundle_path.read_bytes()
    for needle in (b"now-strip", b"no runs recorded yet", b"activity"):
        assert needle in bundle_bytes, f"missing {needle!r} in shipped bundle"
```

(Match the existing test's path-resolution and fixture conventions exactly — read it first.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/ops/dashboard/test_snapshot_sleeves.py -v -k activity_ui`
Expected: FAIL (stale bundle lacks the new markers)

- [ ] **Step 3: Build the bundle**

Run: `cd dashboard-ui && npm run build`
Expected: vite build succeeds, writes `ops/dashboard/static/`

- [ ] **Step 4: Full verification**

```bash
python -m pytest tests/ops/ -v
cd dashboard-ui && npx tsc --noEmit && npm test
```

Expected: everything passes EXCEPT the 11 pre-existing `tests/ops/test_main.py` failures (compare against `git stash && python -m pytest tests/ops/test_main.py | tail -1 && git stash pop` if unsure whether a failure is pre-existing — the count must not grow).

Then end-to-end: use the project's `verify` skill (drives the ops dashboard server + built frontend) to confirm the NowStrip and RunsPanel render against a real snapshot.

- [ ] **Step 5: Commit**

```bash
git add ops/dashboard/static tests/ops/dashboard/test_snapshot_sleeves.py
git commit -m "feat(dashboard): ship activity UI bundle + bundle-content guard"
```
