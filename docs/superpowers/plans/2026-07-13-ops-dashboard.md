# Ops Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only, loopback-only web dashboard for the ops layer: health verdict, live activity feed, research funnel, per-sleeve P&L — served by a stdlib HTTP server that boots as a sibling launchd agent.

**Architecture:** New `ops/dashboard/` package. `snapshot.py` builds one JSON-safe dict from the three sleeve journals + memo store + screen store, all opened SQLite read-only (`mode=ro` URI, WAL concurrent reads). `events_view.py` merges and human-renders journal event streams. `server.py` is a `ThreadingHTTPServer` hard-bound to `127.0.0.1` serving `static/` (no-build vanilla JS polling every 5 s) plus three JSON routes. One tiny ops-service change: the guardian touches a liveness file each pass so the dashboard can see it's alive.

**Tech Stack:** Python 3.12+ stdlib only (sqlite3, http.server). No new pip dependencies. No node/build step. Existing test stack: pytest via `.venv/bin/pytest`, lint via `.venv/bin/ruff check`.

**Spec:** `docs/superpowers/specs/2026-07-13-ops-dashboard-design.md` — read it first.

## Global Constraints

- **Read-only by construction:** the dashboard never writes any store, never imports broker/OAuth/quote modules. All SQLite opens use `mode=ro` URIs.
- **Loopback only:** server host is the literal `"127.0.0.1"`, hard-coded, not configurable.
- **Journal-only data:** no network calls anywhere in `ops/dashboard/` (MarketCalendar is offline — pandas_market_calendars schedule math).
- **JSON safety:** `Decimal` serializes as `str` (never float), `datetime` as UTC ISO-8601. `json.dumps(build_snapshot(...))` must never raise.
- **Section isolation:** every top-level snapshot section is exception-wrapped; a failing section returns `{"error": "<TypeName>: <msg>"}` and the rest still build.
- **No secrets in responses:** never read or echo `OPS_HEARTBEAT_URL`, tokens, or notify credentials.
- **House style:** comments state constraints the code can't show (see `ops/status.py` for tone); tests assert on dicts, not rendered output. Run `.venv/bin/ruff check ops tests` before every commit.
- **Commits:** conventional prefixes (`feat(dashboard): ...`), end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Shared contract: the snapshot JSON

Tasks 3–5 produce it, Task 7 serves it, Task 8 consumes it. Top-level keys (each either its shape below or `{"error": str}`):

```
{
  "generated_at": "<iso>",
  "health": {
    "verdict": "RUNNING" | "STOPPED" | "STALE" | "UNKNOWN",
    "broker_mode": "paper" | "robinhood",
    "last_started": {"at": iso, "age_seconds": float} | null,
    "last_stopping": {"at": iso, "age_seconds": float, "exit_code": int|null} | null,
    "guardian": {"alive_at": iso|null, "age_seconds": float|null},
    "daily_cycle": {"last_run_at": iso|null, "last_completed_at": iso|null},
    "halts": {"daily_halt_today": bool, "kill_switch_this_week": bool},
    "research_paused": bool,
    "live_gate": {"flip_marker_present": bool, "flip_at": iso|null,
                  "live_buy_fills": int, "cap": str, "gate_count": int, "remaining": int},
    "notify": {"cursor": int, "max_event_id": int, "lag": int},
    "heartbeat_errors_24h": int
  },
  "sleeves": {
    "momentum" | "research" | "baseline": {
      "equity": str|null, "cash": str|null, "equity_at": iso|null, "equity_kind": str|null,
      "day_pnl_pct": str|null,
      "series": [{"at": iso, "equity": str}],          // oldest-first, max 60
      "positions": [{"symbol": str, "quantity": str, "entry": str, "stop": str|null}],
      "fills_today": [{"symbol": str, "side": str, "quantity": str, "price": str, "filled_at": iso}]
    }
  },
  "funnel": {
    "screener": {"last_run": {"run_id": str, "asof": str, "created_at": str,
                              "universe_size": int, "passed_count": int} | null,
                 "hits_by_status": {"<status>": int}},
    "memos": {"by_status": {"<status>": int},
              "open": [{"memo_id": str, "ticker": str, "thesis_type": str,
                        "conviction_tier": str, "created_at": str, "status": str}]},
    "overnight": {"last_vetting_run": {"at": iso, "payload": {}} | null,
                  "last_drain_run": {"at": iso, "payload": {}} | null,
                  "paused": bool},
    "signals_7d": {"falsifier_tripped": int, "research_escalation": int,
                   "resolution_due": int, "catalyst_due": int}
  },
  "anomalies_7d": {"<kind>": {"count": int, "last_at": iso|null}},
  "market": {"is_open": bool, "next_open": iso, "previous_close": iso,
             "is_trading_day": bool, "research_deadline_hour_et": int}
}
```

Verdict rules (health): no `service_started` ever → `UNKNOWN`. Latest `service_stopping` newer than latest `service_started` → `STOPPED`. Otherwise: guardian liveness file mtime ≤ 180 s old → `RUNNING`; file present but older → `STALE`; file absent → `UNKNOWN`.

`/api/events` items: `{"source": "momentum"|"research"|"baseline", "id": int, "at": iso, "kind": str, "text": str, "payload": {}}`, newest-first.

---

### Task 1: Read-only mode for Journal

`Journal.__init__` today always connects read-write and runs schema/migrations (`ops/journal.py:126-148`). The dashboard needs the same query API with a `mode=ro` guarantee.

**Files:**
- Modify: `ops/journal.py:108-148`
- Test: `tests/ops/test_journal_readonly.py` (create)

**Interfaces:**
- Consumes: existing `Journal` internals.
- Produces: `Journal(path, readonly=True)` — opens `mode=ro` via SQLite URI, skips directory creation, schema, and migrations; raises `sqlite3.OperationalError` if the file does not exist; all read methods work; all write methods raise `sqlite3.OperationalError`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/test_journal_readonly.py
"""Journal(readonly=True): the dashboard's hard mode=ro guarantee."""
import sqlite3

import pytest

from ops.journal import Journal


def _seed(path: str) -> None:
    with Journal(path) as j:
        j.record_event("service_started", {"pid": 1})


def test_readonly_reads_existing_journal(tmp_path):
    p = str(tmp_path / "j.sqlite")
    _seed(p)
    ro = Journal(p, readonly=True)
    try:
        events = ro.read_events()
        assert len(events) == 1
        assert events[0]["kind"] == "service_started"
    finally:
        ro.close()


def test_readonly_rejects_writes(tmp_path):
    p = str(tmp_path / "j.sqlite")
    _seed(p)
    ro = Journal(p, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.record_event("fill", {"symbol": "XYZ"})
    finally:
        ro.close()


def test_readonly_missing_file_raises_not_creates(tmp_path):
    p = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        Journal(str(p), readonly=True)
    assert not p.exists()  # ro open must not have created the file


def test_readonly_concurrent_with_writer(tmp_path):
    """WAL: a ro reader sees committed writes from a live rw connection."""
    p = str(tmp_path / "j.sqlite")
    rw = Journal(p)
    ro = Journal(p, readonly=True)
    try:
        rw.record_event("fill", {"symbol": "ABC"})
        assert any(e["kind"] == "fill" for e in ro.read_events())
    finally:
        ro.close()
        rw.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/test_journal_readonly.py -v`
Expected: FAIL — `TypeError: Journal.__init__() got an unexpected keyword argument 'readonly'`

- [ ] **Step 3: Implement**

In `ops/journal.py`, change the signature and add an early-return branch before the directory-creation block (keep the existing comment block on the rw path intact):

```python
    def __init__(self, path: str, *, readonly: bool = False):
        self._path = path
        # (existing lock comment stays here)
        self._lock = threading.Lock()
        if readonly:
            # mode=ro is the dashboard's hard guarantee: this connection
            # can never hold a write lock, run migrations, or create the
            # file. Missing file → sqlite3.OperationalError, never a
            # silently-created empty journal. as_uri() handles path
            # escaping (spaces, '#') that a hand-built f-string would not.
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            self._conn = sqlite3.connect(
                uri, uri=True, isolation_level=None, check_same_thread=False,
            )
            self._conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            return
        # ... existing rw body unchanged (mkdir, connect, WAL, schema, migrations)
```

The `SELECT 1` forces the lazy open so a missing file raises in the constructor (test 3), not on first query.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/test_journal_readonly.py -v`
Expected: 4 passed

- [ ] **Step 5: Full journal suite + lint, then commit**

Run: `.venv/bin/pytest tests/ops -k journal -v && .venv/bin/ruff check ops tests`
Expected: all pass.

```bash
git add ops/journal.py tests/ops/test_journal_readonly.py
git commit -m "feat(journal): readonly mode for dashboard reads

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Guardian liveness touch-file

Guardian pass recency lives only in memory (`PositionGuardian.last_pass_started_at`, `ops/position_guardian.py:63,74`); a journal reader cannot see it. Touch a file at the start of every pass — best-effort, never disturbing the pass. This is the ONLY change to the running ops service in this plan.

**Files:**
- Modify: `ops/config.py` (new `OpsConfig` field)
- Modify: `ops/position_guardian.py` (touch in `check_stops_once`)
- Test: `tests/ops/test_guardian_liveness.py` (create)

**Interfaces:**
- Consumes: `OpsConfig` (frozen dataclass), `PositionGuardian.check_stops_once`.
- Produces: `OpsConfig.guardian_liveness_path: str` (default `${XDG_STATE_HOME:-~/.local/state}/tradingagents/guardian.alive`); the file's mtime = last guardian pass start. Task 3 reads it via `os.stat`.

- [ ] **Step 1: Write the failing tests**

Look at existing guardian construction in `tests/ops/` (grep `PositionGuardian(`) and reuse its fake-broker pattern; the essential test:

```python
# tests/ops/test_guardian_liveness.py
"""Guardian liveness file: mtime = last pass start (dashboard reads it)."""
import os
from decimal import Decimal

from ops.config import OpsConfig
from ops.journal import Journal
from ops.position_guardian import PositionGuardian


class _FakeBroker:
    def __init__(self, journal):
        self.journal = journal

    def get_positions(self):
        return []

    def get_cash(self):
        return Decimal("100")


def _make_guardian(tmp_path, liveness_path):
    journal = Journal(str(tmp_path / "j.sqlite"))
    cfg = OpsConfig(
        journal_path=str(tmp_path / "j.sqlite"),
        guardian_liveness_path=str(liveness_path),
    )
    return PositionGuardian(
        _FakeBroker(journal),
        lambda symbol: Decimal("10"),
        cfg,
        journal=journal,
        # Market closed: the touch must happen BEFORE the market-hours
        # gate — liveness answers "is the loop scheduled?", which
        # overnight passes still answer yes to.
        market_open_fn=lambda: False,
    )


def test_pass_touches_liveness_file(tmp_path):
    liveness = tmp_path / "state" / "guardian.alive"
    g = _make_guardian(tmp_path, liveness)
    assert not liveness.exists()
    g.check_stops_once()
    assert liveness.exists()


def test_second_pass_updates_mtime(tmp_path):
    liveness = tmp_path / "guardian.alive"
    g = _make_guardian(tmp_path, liveness)
    g.check_stops_once()
    os.utime(liveness, (1, 1))  # backdate instead of sleeping
    g.check_stops_once()
    assert os.stat(liveness).st_mtime > 1


def test_touch_failure_never_breaks_pass(tmp_path):
    # A directory at the file's path makes touch() raise OSError.
    liveness = tmp_path / "guardian.alive"
    liveness.mkdir()
    g = _make_guardian(tmp_path, liveness)
    g.check_stops_once()  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/test_guardian_liveness.py -v`
Expected: FAIL — `TypeError: OpsConfig.__init__() got an unexpected keyword argument 'guardian_liveness_path'`

- [ ] **Step 3: Implement**

`ops/config.py` — add the field next to `research_pause_flag_path` (same default-factory idiom):

```python
    # Guardian liveness file: the guardian best-effort-touches this at the
    # start of every pass; the (separate-process) dashboard reads its mtime.
    # A file, not a journal event: one event/minute forever is noise.
    guardian_liveness_path: str = field(
        default_factory=lambda: os.path.join(
            os.path.expanduser(os.environ.get("XDG_STATE_HOME") or "~/.local/state"),
            "tradingagents", "guardian.alive",
        )
    )
```

`ops/position_guardian.py` — in `check_stops_once`, immediately after `self.last_pass_started_at = time.monotonic()` (line 74), add `self._touch_liveness()`, and add the method (needs `import os` and `from pathlib import Path` at top if absent):

```python
    def _touch_liveness(self) -> None:
        # Best-effort by hard rule: the guardian is the last line of
        # defence on real money — no filesystem problem may ever stop a
        # stop-loss pass. getattr: configs constructed before this field
        # existed (old pickles/tests) must not crash the guardian either.
        path = getattr(self._cfg, "guardian_liveness_path", None)
        if not path:
            return
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
        except OSError:
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/test_guardian_liveness.py -v`
Expected: 3 passed

- [ ] **Step 5: Guardian + config suites, lint, commit**

Run: `.venv/bin/pytest tests/ops -k "guardian or config" -v && .venv/bin/ruff check ops tests`
Expected: all pass (existing guardian tests unaffected — the touch is additive and swallowed).

```bash
git add ops/config.py ops/position_guardian.py tests/ops/test_guardian_liveness.py
git commit -m "feat(ops): guardian liveness touch-file for dashboard health

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Snapshot skeleton — JSON safety, section isolation, health, market

**Files:**
- Create: `ops/dashboard/__init__.py` (empty)
- Create: `ops/dashboard/snapshot.py`
- Test: `tests/ops/dashboard/__init__.py` (empty), `tests/ops/dashboard/test_snapshot_health.py`

**Interfaces:**
- Consumes: `Journal(path, readonly=True)` (Task 1); `OpsConfig.guardian_liveness_path` (Task 2); `ops.events` KIND_* constants; `ops.live_gate.flip_epoch` / `count_live_buy_fills`; `ops.scheduler.market_calendar.MarketCalendar`.
- Produces (Tasks 4, 5, 7 build on these):
  - `build_snapshot(config: OpsConfig, *, now: datetime | None = None) -> dict[str, Any]` — the contract dict (sleeves/funnel raise `NotImplementedError` in this task, arriving as `{"error": ...}` via the wrapper; Tasks 4–5 replace them).
  - `jsonable(value: Any) -> Any` — deep-converts Decimal→str, datetime→UTC ISO, tuples→lists.
  - `section(builder: Callable[[], dict]) -> dict` — exception wrapper.
  - `ro_conn(path: str) -> sqlite3.Connection` — `mode=ro` URI connection, `sqlite3.Row` row factory.
  - Module constant `GUARDIAN_STALE_S = 180.0`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/dashboard/test_snapshot_health.py
"""build_snapshot: health verdict, section isolation, JSON safety."""
import json
import os
from datetime import datetime, timedelta, timezone

from ops import events
from ops.config import OpsConfig
from ops.journal import Journal
from ops.dashboard.snapshot import build_snapshot


def _config(tmp_path) -> OpsConfig:
    return OpsConfig(
        journal_path=str(tmp_path / "ops.sqlite"),
        baseline_journal_path=str(tmp_path / "baseline.sqlite"),
        research_journal_path=str(tmp_path / "research.sqlite"),
        screen_store_path=str(tmp_path / "screen.sqlite"),
        memo_store_path=str(tmp_path / "memos.sqlite"),
        guardian_liveness_path=str(tmp_path / "guardian.alive"),
        research_pause_flag_path=str(tmp_path / "research.paused"),
    )


def _started_journal(cfg: OpsConfig) -> None:
    with Journal(cfg.journal_path) as j:
        j.record_event(events.KIND_SERVICE_STARTED, {"pid": 42, "broker_mode": "paper"})


def test_verdict_running_when_guardian_fresh(tmp_path):
    cfg = _config(tmp_path)
    _started_journal(cfg)
    open(cfg.guardian_liveness_path, "w").close()  # mtime = now
    snap = build_snapshot(cfg)
    assert snap["health"]["verdict"] == "RUNNING"
    assert snap["health"]["guardian"]["age_seconds"] < 60


def test_verdict_stale_when_guardian_old(tmp_path):
    cfg = _config(tmp_path)
    _started_journal(cfg)
    open(cfg.guardian_liveness_path, "w").close()
    os.utime(cfg.guardian_liveness_path, (1, 1))  # 1970
    assert build_snapshot(cfg)["health"]["verdict"] == "STALE"


def test_verdict_unknown_without_liveness_file(tmp_path):
    cfg = _config(tmp_path)
    _started_journal(cfg)
    snap = build_snapshot(cfg)
    assert snap["health"]["verdict"] == "UNKNOWN"
    assert snap["health"]["guardian"]["alive_at"] is None


def test_verdict_stopped_when_stopping_is_latest(tmp_path):
    cfg = _config(tmp_path)
    now = datetime.now(timezone.utc)
    with Journal(cfg.journal_path) as j:
        j.record_event(events.KIND_SERVICE_STARTED, {"pid": 1},
                       at=now - timedelta(hours=2))
        j.record_event(events.KIND_SERVICE_STOPPING, {"exit_code": 0},
                       at=now - timedelta(hours=1))
    open(cfg.guardian_liveness_path, "w").close()  # fresh file must not win
    snap = build_snapshot(cfg)
    assert snap["health"]["verdict"] == "STOPPED"
    assert snap["health"]["last_stopping"]["exit_code"] == 0


def test_missing_journal_isolated_to_health_section(tmp_path):
    cfg = _config(tmp_path)  # no journal file created at all
    snap = build_snapshot(cfg)
    assert "error" in snap["health"]
    assert "is_open" in snap["market"]  # market still built


def test_snapshot_is_json_serializable(tmp_path):
    cfg = _config(tmp_path)
    _started_journal(cfg)
    json.dumps(build_snapshot(cfg))  # must not raise


def test_research_paused_flag(tmp_path):
    cfg = _config(tmp_path)
    _started_journal(cfg)
    open(cfg.research_pause_flag_path, "w").close()
    assert build_snapshot(cfg)["health"]["research_paused"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/dashboard/test_snapshot_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.dashboard'`

- [ ] **Step 3: Implement `ops/dashboard/snapshot.py`**

```python
"""Read-only snapshot builder for the local ops dashboard.

Same contract as ops/status.py::build_status, wider scope: reads ONLY the
sqlite stores (mode=ro URIs — a hard guarantee, not a convention) plus two
flag files. No broker, no MCP, no OAuth, no quotes, no LLM, no network.

Every top-level section is exception-isolated: a missing or mid-migration
store turns into {"error": ...} for that section while the rest of the
snapshot still builds — a partial dashboard beats a blank page.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from ops import events
from ops.config import OpsConfig
from ops.journal import Journal
from ops.live_gate import count_live_buy_fills, flip_epoch

# A guardian pass starts every 60s; 3 missed passes = stale. Matches the
# heartbeat's staleness window in ops/main.py.
GUARDIAN_STALE_S = 180.0


def jsonable(value: Any) -> Any:
    """Deep-convert to JSON-safe types. Decimal -> str (never float: this
    is money), aware datetime -> UTC ISO-8601."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def section(builder: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return jsonable(builder())
    except Exception as exc:  # noqa: BLE001 — isolation is the point
        return {"error": f"{type(exc).__name__}: {exc}"}


def ro_conn(path: str) -> sqlite3.Connection:
    """mode=ro sqlite connection (raises OperationalError if missing)."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _event_view(ev: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    if ev is None:
        return None
    return {
        "at": ev["at"],
        "age_seconds": (now - ev["at"]).total_seconds(),
        "payload": ev["payload"],
    }


def _health_section(config: OpsConfig, now: datetime) -> dict[str, Any]:
    with Journal(config.journal_path, readonly=True) as j:
        started = j.last_event(events.KIND_SERVICE_STARTED)
        stopping = j.last_event(events.KIND_SERVICE_STOPPING)
        halts = {
            "daily_halt_today": j.has_event_today(events.KIND_DAILY_HALT, now=now),
            "kill_switch_this_week": j.has_event_since_last_monday(
                events.KIND_KILL_SWITCH, now=now),
        }
        cycle_run = j.last_event(events.KIND_DAILY_CYCLE_RUN)
        cycle_done = j.last_event(events.KIND_DAILY_CYCLE_COMPLETED)
        cursor = j.get_cursor("notify")
        max_event_id = j.last_event_id_before(now) or 0
        epoch = flip_epoch(j)
        live_fills = count_live_buy_fills(j)
        heartbeat_errors = j.count_events(
            events.KIND_HEARTBEAT_ERROR, since=now - timedelta(hours=24))

    guardian_alive_at: datetime | None = None
    try:
        mtime = os.stat(config.guardian_liveness_path).st_mtime
        guardian_alive_at = datetime.fromtimestamp(mtime, tz=timezone.utc)
    except OSError:
        pass
    guardian_age = (
        (now - guardian_alive_at).total_seconds()
        if guardian_alive_at is not None else None
    )

    # Verdict: journal start/stop ordering first (a fresh liveness file
    # from a guardian that outlived a clean shutdown must not say
    # RUNNING), then liveness recency.
    if started is None:
        verdict = "UNKNOWN"
    elif stopping is not None and stopping["at"] > started["at"]:
        verdict = "STOPPED"
    elif guardian_age is None:
        verdict = "UNKNOWN"
    elif guardian_age <= GUARDIAN_STALE_S:
        verdict = "RUNNING"
    else:
        verdict = "STALE"

    last_stopping = _event_view(stopping, now)
    if last_stopping is not None:
        last_stopping["exit_code"] = last_stopping.pop("payload").get("exit_code")
    last_started = _event_view(started, now)
    if last_started is not None:
        last_started.pop("payload")

    return {
        "verdict": verdict,
        "broker_mode": config.broker_mode,
        "last_started": last_started,
        "last_stopping": last_stopping,
        "guardian": {"alive_at": guardian_alive_at, "age_seconds": guardian_age},
        "daily_cycle": {
            "last_run_at": cycle_run["at"] if cycle_run else None,
            "last_completed_at": cycle_done["at"] if cycle_done else None,
        },
        "halts": halts,
        "research_paused": os.path.exists(config.research_pause_flag_path),
        "live_gate": {
            "flip_marker_present": epoch is not None,
            "flip_at": epoch,
            "live_buy_fills": live_fills,
            "cap": config.live_max_position,
            "gate_count": config.live_fill_gate_count,
            "remaining": max(0, config.live_fill_gate_count - live_fills),
        },
        "notify": {
            "cursor": cursor,
            "max_event_id": max_event_id,
            "lag": max(0, max_event_id - cursor),
        },
        "heartbeat_errors_24h": heartbeat_errors,
    }


def _market_section(config: OpsConfig, now: datetime) -> dict[str, Any]:
    from ops.scheduler.market_calendar import MarketCalendar

    cal = MarketCalendar()
    return {
        "is_open": cal.is_open_now(now),
        "next_open": cal.next_open(now),
        "previous_close": cal.previous_close(now),
        "is_trading_day": cal.is_trading_day(now.date()),
        "research_deadline_hour_et": config.research_drain_deadline_hour,
    }


def _sleeves_section(config: OpsConfig, now: datetime) -> dict[str, Any]:
    raise NotImplementedError("Task 4")


def _funnel_section(config: OpsConfig, now: datetime) -> dict[str, Any]:
    raise NotImplementedError("Task 5")


def _anomalies_section(config: OpsConfig, now: datetime) -> dict[str, Any]:
    raise NotImplementedError("Task 4")


def build_snapshot(
    config: OpsConfig, *, now: datetime | None = None,
) -> dict[str, Any]:
    when = now if now is not None else datetime.now(timezone.utc)
    return {
        "generated_at": when.isoformat(),
        "health": section(lambda: _health_section(config, when)),
        "sleeves": section(lambda: _sleeves_section(config, when)),
        "funnel": section(lambda: _funnel_section(config, when)),
        "anomalies_7d": section(lambda: _anomalies_section(config, when)),
        "market": section(lambda: _market_section(config, when)),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/dashboard/test_snapshot_health.py -v`
Expected: 7 passed

- [ ] **Step 5: Lint and commit**

Run: `.venv/bin/ruff check ops tests`

```bash
git add ops/dashboard tests/ops/dashboard
git commit -m "feat(dashboard): snapshot skeleton — health verdict, market, section isolation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Sleeves & anomalies sections

**Files:**
- Modify: `ops/dashboard/snapshot.py` (replace `_sleeves_section` / `_anomalies_section` stubs)
- Test: `tests/ops/dashboard/test_snapshot_sleeves.py` (create)

**Interfaces:**
- Consumes: `Journal(path, readonly=True)`; `PaperBroker.from_journal(journal=..., quote_source=..., starting_cash=...)` (see `ops/status.py:94-97` for the exact replay convention — copy it, including the refuse-quotes guard); `ops.trading_time.trading_day_start`.
- Produces: the `sleeves` and `anomalies_7d` contract shapes. Sleeve keys are exactly `"momentum"`, `"research"`, `"baseline"` mapping to `config.journal_path`, `config.research_journal_path`, `config.baseline_journal_path`.

Implementation notes (constraints, not code): each sleeve builds inside its own try — one missing journal yields `{"error": ...}` for that sleeve only, so wrap per-sleeve, not the whole section. `equity`/`equity_at`/`equity_kind` come from the latest equity snapshot of ANY kind (`get_latest_equity_snapshot()`); `day_pnl_pct` = change from the latest snapshot strictly before `trading_day_start(now)` to the latest snapshot, as `str(Decimal)`, `None` when either side is missing or zero-denominator (mirror `_momentum_equity_and_pnl` in `ops/notify/overview.py:92-111`); `series` = last 60 of `read_equity_snapshots()` oldest-first, `positions` via the `PaperBroker.from_journal` replay with a refuse-quotes callable (copy `_refuse_quotes` from `ops/status.py:40-47` — do not import the private name), `fills_today` filtered by `filled_at >= trading_day_start(now)`.

`_anomalies_section`: over the momentum journal, the five kinds in `ops/status.py:31-37`; over the research journal (own try), `KIND_RESEARCH_MONITOR_ERROR`, `KIND_RESEARCH_TRADE_ERROR`, `KIND_RESEARCH_VETTING_ERROR`, `KIND_RESEARCH_DRAIN_ERROR`. Shape per kind: `{"count": count_events(kind, since=now-7d), "last_at": last_event(kind)["at"] or None}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/dashboard/test_snapshot_sleeves.py
"""Sleeves: journal-replay positions, snapshot-based P&L, per-sleeve isolation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ops import events
from ops.config import OpsConfig
from ops.journal import Journal
from ops.dashboard.snapshot import build_snapshot


def _config(tmp_path) -> OpsConfig:
    return OpsConfig(
        journal_path=str(tmp_path / "ops.sqlite"),
        baseline_journal_path=str(tmp_path / "baseline.sqlite"),
        research_journal_path=str(tmp_path / "research.sqlite"),
        screen_store_path=str(tmp_path / "screen.sqlite"),
        memo_store_path=str(tmp_path / "memos.sqlite"),
        guardian_liveness_path=str(tmp_path / "guardian.alive"),
        research_pause_flag_path=str(tmp_path / "research.paused"),
    )


def _seed_momentum(cfg: OpsConfig, now: datetime) -> None:
    with Journal(cfg.journal_path) as j:
        j.record_cash_adjustment(kind="seed", amount=Decimal("250"), note="test")
        j.record_equity_snapshot(
            equity=Decimal("250"), cash=Decimal("250"), kind="open_day",
            at=now - timedelta(days=1))
        j.record_equity_snapshot(
            equity=Decimal("260"), cash=Decimal("160"), kind="open_day", at=now)
        j.record_fill(
            order_id="o1", client_order_id="c1", symbol="XYZ", side="buy",
            quantity=Decimal("10"), price=Decimal("10"), filled_at=now,
            stop_loss_price=Decimal("9.20"))


def test_sleeve_positions_and_fills_from_replay(tmp_path):
    cfg = _config(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_momentum(cfg, now)
    mom = build_snapshot(cfg, now=now)["sleeves"]["momentum"]
    assert mom["positions"] == [
        {"symbol": "XYZ", "quantity": "10", "entry": "10", "stop": "9.20"}]
    assert len(mom["fills_today"]) == 1
    assert mom["equity"] == "260"


def test_day_pnl_pct_from_consecutive_snapshots(tmp_path):
    cfg = _config(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_momentum(cfg, now)
    mom = build_snapshot(cfg, now=now)["sleeves"]["momentum"]
    # (260-250)/250 = 0.04
    assert Decimal(mom["day_pnl_pct"]) == Decimal("0.04")


def test_missing_sleeve_journal_isolated(tmp_path):
    cfg = _config(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_momentum(cfg, now)  # research + baseline journals never created
    sleeves = build_snapshot(cfg, now=now)["sleeves"]
    assert "error" in sleeves["research"]
    assert "error" in sleeves["baseline"]
    assert "error" not in sleeves["momentum"]


def test_anomalies_counts_and_last_at(tmp_path):
    cfg = _config(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_momentum(cfg, now)
    with Journal(cfg.journal_path) as j:
        j.record_event(events.KIND_STOP_FAILED, {"symbol": "XYZ"})
        j.record_event(events.KIND_STOP_FAILED, {"symbol": "XYZ"})
    anom = build_snapshot(cfg, now=now)["anomalies_7d"]
    assert anom[events.KIND_STOP_FAILED]["count"] == 2
    assert anom[events.KIND_STOP_FAILED]["last_at"] is not None
    assert anom[events.KIND_GUARDIAN_BLIND]["count"] == 0
```

Check `Journal.record_fill` / `record_cash_adjustment` / `record_equity_snapshot` signatures in `ops/journal.py:282-457` before writing — match parameter names exactly; adjust the seed helper if a name differs (e.g. `at=` support on snapshots).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/dashboard/test_snapshot_sleeves.py -v`
Expected: FAIL — sections return `{"error": "NotImplementedError: Task 4"}` → KeyError/assert failures.

- [ ] **Step 3: Implement** `_sleeves_section` and `_anomalies_section` per the notes above (replace the stubs). Positions replay:

```python
def _refuse_quotes(symbol: str) -> Decimal:
    raise RuntimeError(
        f"dashboard snapshot is journal-only, but a quote was requested "
        f"for {symbol!r}")


def _one_sleeve(path: str, now: datetime) -> dict[str, Any]:
    from ops.broker.paper import PaperBroker
    from ops.trading_time import trading_day_start

    day_start = trading_day_start(now)
    with Journal(path, readonly=True) as j:
        snaps = j.read_equity_snapshots()
        fills = j.read_fills()
        replay = PaperBroker.from_journal(
            journal=j, quote_source=_refuse_quotes, starting_cash=Decimal("0"))
        positions = [
            {"symbol": p.symbol, "quantity": p.quantity,
             "entry": p.avg_entry_price, "stop": p.stop_loss_price}
            for p in replay.get_positions()
        ]
        cash = replay.get_cash()

    latest = snaps[-1] if snaps else None
    before_today = [s for s in snaps if s["at"] < day_start]
    day_pnl = None
    if latest and before_today and before_today[-1]["equity"] != 0:
        prev = before_today[-1]["equity"]
        day_pnl = (latest["equity"] - prev) / prev
    return {
        "equity": latest["equity"] if latest else None,
        "cash": cash,
        "equity_at": latest["at"] if latest else None,
        "equity_kind": latest["kind"] if latest else None,
        "day_pnl_pct": day_pnl,
        "series": [{"at": s["at"], "equity": s["equity"]} for s in snaps[-60:]],
        "positions": positions,
        "fills_today": [
            {"symbol": f["symbol"], "side": f["side"], "quantity": f["quantity"],
             "price": f["price"], "filled_at": f["filled_at"]}
            for f in fills if f["filled_at"] >= day_start
        ],
    }


def _sleeves_section(config: OpsConfig, now: datetime) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, path in (
        ("momentum", config.journal_path),
        ("research", config.research_journal_path),
        ("baseline", config.baseline_journal_path),
    ):
        out[name] = section(lambda p=path: _one_sleeve(p, now))
    return out
```

(If `read_equity_snapshots()` rows carry different key names — check `ops/journal.py:445-457` — adapt.) `_anomalies_section` analogously with the kind lists from the Interfaces block, research-journal kinds inside their own try so a missing research journal zeroes out as `{"count": 0, "last_at": None}`… no — absence of the store is information: put research-journal kinds under the same per-journal `section()` isolation pattern, but since the contract is flat `{kind: {...}}`, on a missing research journal simply omit its kinds.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/dashboard/ -v`
Expected: all pass (including Task 3's).

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check ops tests
git add ops/dashboard/snapshot.py tests/ops/dashboard/test_snapshot_sleeves.py
git commit -m "feat(dashboard): sleeves P&L + anomalies sections

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Research funnel section

**Files:**
- Modify: `ops/dashboard/snapshot.py` (replace `_funnel_section` stub)
- Test: `tests/ops/dashboard/test_snapshot_funnel.py` (create)

**Interfaces:**
- Consumes: `ro_conn` (Task 3); the memos schema (`tradingagents/memos/store.py:25-40`: columns `memo_id, ticker, thesis_type, status, conviction_tier, created_at, as_of_date, resolved_at, outcome_label, payload`); the screen-store schema (`ops/research/store.py:29-48`: `screen_runs(run_id, asof, created_at, universe_size, passed_count)`, `screen_hits(status, ...)`); research-journal event kinds `research_vetting_run`, `research_drain_run`, `falsifier_tripped`, `research_escalation`, `resolution_due`, `catalyst_due`.
- Produces: the `funnel` contract shape.

Deliberate choice, keep it: query the memo/screen SQLite **columns directly via `ro_conn`** — do NOT instantiate `MemoStore`/`ScreenStore` (their constructors run `CREATE TABLE` schema writes, `store.py` both files) and do NOT deserialize memo payloads through Pydantic (a schema bump must not break the dashboard).

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/dashboard/test_snapshot_funnel.py
"""Funnel: memo/screen column queries (ro), overnight run views, 7d signals."""
import sqlite3
from datetime import datetime, timezone

from ops import events
from ops.config import OpsConfig
from ops.journal import Journal
from ops.dashboard.snapshot import build_snapshot

from tradingagents.memos.store import MemoStore  # seeding only
from ops.research.store import ScreenStore        # seeding only


def _config(tmp_path) -> OpsConfig:
    return OpsConfig(
        journal_path=str(tmp_path / "ops.sqlite"),
        baseline_journal_path=str(tmp_path / "baseline.sqlite"),
        research_journal_path=str(tmp_path / "research.sqlite"),
        screen_store_path=str(tmp_path / "screen.sqlite"),
        memo_store_path=str(tmp_path / "memos.sqlite"),
        guardian_liveness_path=str(tmp_path / "guardian.alive"),
        research_pause_flag_path=str(tmp_path / "research.paused"),
    )


def _seed_memos_raw(path: str) -> None:
    # Raw INSERT: the dashboard reads columns only, so tests need not
    # build full pydantic Memos.
    MemoStore(path)  # creates schema
    conn = sqlite3.connect(path)
    for i, status in enumerate(["open", "open", "passed", "rejected"]):
        conn.execute(
            "INSERT INTO memos (memo_id, ticker, thesis_type, status,"
            " conviction_tier, created_at, as_of_date, payload)"
            " VALUES (?, ?, 'value', ?, 'core', ?, '2026-07-01', '{}')",
            (f"m{i}", f"TK{i}", status, "2026-07-0%dT00:00:00+00:00" % (i + 1)),
        )
    conn.commit()
    conn.close()


def test_memo_counts_and_open_list(tmp_path):
    cfg = _config(tmp_path)
    _seed_memos_raw(cfg.memo_store_path)
    ScreenStore(cfg.screen_store_path)  # empty but present
    Journal(cfg.research_journal_path).close()
    funnel = build_snapshot(cfg)["funnel"]
    assert funnel["memos"]["by_status"] == {"open": 2, "passed": 1, "rejected": 1}
    assert [m["ticker"] for m in funnel["memos"]["open"]] == ["TK1", "TK0"]  # newest first


def test_screener_last_run_and_hit_counts(tmp_path):
    cfg = _config(tmp_path)
    _seed_memos_raw(cfg.memo_store_path)
    store = ScreenStore(cfg.screen_store_path)
    store.record_run(run_id="r1", asof="2026-07-11", universe_size=500,
                     passed_count=2, hits=[])
    Journal(cfg.research_journal_path).close()
    funnel = build_snapshot(cfg)["funnel"]
    assert funnel["screener"]["last_run"]["run_id"] == "r1"
    assert funnel["screener"]["last_run"]["universe_size"] == 500


def test_overnight_runs_and_signals(tmp_path):
    cfg = _config(tmp_path)
    _seed_memos_raw(cfg.memo_store_path)
    ScreenStore(cfg.screen_store_path)
    with Journal(cfg.research_journal_path) as j:
        j.record_event(events.KIND_RESEARCH_VETTING_RUN, {"vetted": 3, "passed": 1})
        j.record_event(events.KIND_FALSIFIER_TRIPPED, {"memo_id": "m0"})
    funnel = build_snapshot(cfg)["funnel"]
    assert funnel["overnight"]["last_vetting_run"]["payload"]["vetted"] == 3
    assert funnel["overnight"]["last_drain_run"] is None
    assert funnel["signals_7d"]["falsifier_tripped"] == 1
    assert funnel["overnight"]["paused"] is False


def test_missing_memo_store_isolated(tmp_path):
    cfg = _config(tmp_path)  # nothing seeded
    snap = build_snapshot(cfg)
    assert "error" in snap["funnel"]
```

Before running: check `ScreenStore.record_run`'s exact signature at `ops/research/store.py:84` and fix the seeding call if parameter names differ.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/dashboard/test_snapshot_funnel.py -v`
Expected: FAIL — funnel is `{"error": "NotImplementedError: Task 5"}`.

- [ ] **Step 3: Implement `_funnel_section`**

```python
def _funnel_section(config: OpsConfig, now: datetime) -> dict[str, Any]:
    with ro_conn(config.memo_store_path) as mconn:
        by_status = {
            r["status"]: r["n"]
            for r in mconn.execute(
                "SELECT status, COUNT(*) AS n FROM memos GROUP BY status")
        }
        open_memos = [
            dict(r) for r in mconn.execute(
                "SELECT memo_id, ticker, thesis_type, conviction_tier,"
                " created_at, status FROM memos WHERE status = 'open'"
                " ORDER BY created_at DESC LIMIT 50")
        ]

    with ro_conn(config.screen_store_path) as sconn:
        run_row = sconn.execute(
            "SELECT run_id, asof, created_at, universe_size, passed_count"
            " FROM screen_runs ORDER BY created_at DESC LIMIT 1").fetchone()
        hits_by_status = {
            r["status"]: r["n"]
            for r in sconn.execute(
                "SELECT status, COUNT(*) AS n FROM screen_hits GROUP BY status")
        }

    week_ago = now - timedelta(days=7)
    with Journal(config.research_journal_path, readonly=True) as rj:
        overnight = {
            "last_vetting_run": _event_view(
                rj.last_event(events.KIND_RESEARCH_VETTING_RUN), now),
            "last_drain_run": _event_view(
                rj.last_event(events.KIND_RESEARCH_DRAIN_RUN), now),
            "paused": os.path.exists(config.research_pause_flag_path),
        }
        signals = {
            kind: rj.count_events(getattr(events, const), since=week_ago)
            for kind, const in (
                ("falsifier_tripped", "KIND_FALSIFIER_TRIPPED"),
                ("research_escalation", "KIND_RESEARCH_ESCALATION"),
                ("resolution_due", "KIND_RESOLUTION_DUE"),
                ("catalyst_due", "KIND_CATALYST_DUE"),
            )
        }

    return {
        "screener": {
            "last_run": dict(run_row) if run_row is not None else None,
            "hits_by_status": hits_by_status,
        },
        "memos": {"by_status": by_status, "open": open_memos},
        "overnight": overnight,
        "signals_7d": signals,
    }
```

(`sqlite3.Connection` as context manager does not close — call pattern: use `contextlib.closing` or explicit try/finally `conn.close()`; write it with `contextlib.closing` and keep the import at the top.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/dashboard/ -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check ops tests
git add ops/dashboard/snapshot.py tests/ops/dashboard/test_snapshot_funnel.py
git commit -m "feat(dashboard): research funnel section

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Merged, human-rendered events feed

**Files:**
- Create: `ops/dashboard/events_view.py`
- Test: `tests/ops/dashboard/test_events_view.py` (create)

**Interfaces:**
- Consumes: `ro_conn` from `ops.dashboard.snapshot` (Task 3).
- Produces (Task 7 serves this):
  - `merged_events(journal_paths: dict[str, str], *, limit: int = 100, kinds: frozenset[str] | None = None) -> list[dict[str, Any]]` — newest-first across sources; each item `{"source", "id", "at", "kind", "text", "payload"}`; a missing journal file contributes nothing (never raises).
  - `render_event(kind: str, payload: dict[str, Any]) -> str` — one human sentence; unknown kinds fall back to `"<kind>: <compact json>"` truncated to 200 chars; a renderer raising falls back the same way.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/dashboard/test_events_view.py
"""Merged event feed: ordering, filtering, resilient rendering."""
from ops.journal import Journal
from ops.dashboard.events_view import merged_events, render_event


def test_merge_orders_newest_first_across_sources(tmp_path):
    p1, p2 = str(tmp_path / "a.sqlite"), str(tmp_path / "b.sqlite")
    with Journal(p1) as j:
        j.record_event("service_started", {"pid": 1})
    with Journal(p2) as j:
        j.record_event("research_vetting_run", {"vetted": 2, "passed": 1})
    items = merged_events({"momentum": p1, "research": p2})
    assert len(items) == 2
    assert items[0]["at"] >= items[1]["at"]
    assert {i["source"] for i in items} == {"momentum", "research"}


def test_limit_and_kind_filter(tmp_path):
    p = str(tmp_path / "a.sqlite")
    with Journal(p) as j:
        for i in range(10):
            j.record_event("fill", {"symbol": f"S{i}", "side": "buy",
                                    "quantity": "1", "price": "10"})
        j.record_event("daily_halt", {})
    only_halt = merged_events({"m": p}, kinds=frozenset({"daily_halt"}))
    assert [i["kind"] for i in only_halt] == ["daily_halt"]
    assert len(merged_events({"m": p}, limit=5)) == 5


def test_missing_journal_contributes_nothing(tmp_path):
    items = merged_events({"m": str(tmp_path / "nope.sqlite")})
    assert items == []


def test_render_known_kind_is_a_sentence():
    text = render_event("fill", {"symbol": "XYZ", "side": "buy",
                                 "quantity": "10", "price": "34.10"})
    assert "XYZ" in text and "34.10" in text
    assert not text.startswith("fill:")  # rendered, not fallback


def test_render_unknown_kind_falls_back_compact():
    text = render_event("brand_new_kind", {"a": 1})
    assert text.startswith("brand_new_kind")
    assert len(text) <= 220
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/dashboard/test_events_view.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `ops/dashboard/events_view.py`**

```python
"""Merge the sleeve journals' event streams and render them for humans.

Rendering is defensive by contract: payload shapes evolve, and a feed that
crashes on a new event kind is worse than one that prints the raw payload.
Every renderer uses .get with fallbacks; any renderer exception falls back
to the compact form.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any, Callable

from ops.dashboard.snapshot import ro_conn

_MAX_FALLBACK = 200


def _fmt_money(v: Any) -> str:
    return f"${v}" if v is not None else "$?"


_RENDERERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "fill": lambda p: (
        f"{str(p.get('side', '?')).upper()} {p.get('quantity', '?')} "
        f"{p.get('symbol', '?')} @ {_fmt_money(p.get('price'))}"),
    "order_rejected": lambda p: (
        f"Order rejected: {p.get('symbol', '?')} — "
        f"{p.get('reason', p.get('rule', 'no reason recorded'))}"),
    "stop_hit": lambda p: (
        f"STOP HIT: {p.get('symbol', '?')} at {_fmt_money(p.get('price'))}"),
    "stop_failed": lambda p: (
        f"STOP FAILED: {p.get('symbol', '?')} — {p.get('error', 'unknown')}"),
    "daily_halt": lambda p: "Daily drawdown halt — trading paused for the day",
    "kill_switch": lambda p: "KILL SWITCH — weekly drawdown breached",
    "service_started": lambda p: (
        f"Service started (pid {p.get('pid', '?')}, "
        f"{p.get('broker_mode', '?')} mode)"),
    "service_stopping": lambda p: (
        f"Service stopping (exit code {p.get('exit_code', '?')})"),
    "startup_halted": lambda p: "Startup halted: reconciliation found diffs",
    "inconsistency": lambda p: f"Reconciliation inconsistency: {p}",
    "guardian_check_error": lambda p: (
        f"Guardian check error: {p.get('error', 'unknown')}"),
    "heartbeat_error": lambda p: (
        f"Heartbeat ping failed: {p.get('error', 'unknown')}"),
    "daily_cycle_run": lambda p: "Daily cycle started",
    "daily_cycle_completed": lambda p: "Daily cycle completed",
    "analysis_decision": lambda p: (
        f"Analysis: {p.get('symbol', '?')} → {p.get('decision', '?')}"),
    "baseline_screen_run": lambda p: "Baseline screen run",
    "research_vetting_run": lambda p: (
        f"Vetting run: {p.get('vetted', '?')} vetted, "
        f"{p.get('passed', '?')} passed"),
    "research_drain_run": lambda p: (
        f"Overnight drain: {p.get('researched', p.get('count', '?'))} name(s)"),
    "research_position_opened": lambda p: (
        f"Research position opened: {p.get('symbol', '?')}"),
    "research_position_closed": lambda p: (
        f"Research position closed: {p.get('symbol', '?')}"),
    "falsifier_tripped": lambda p: (
        f"FALSIFIER TRIPPED: memo {p.get('memo_id', '?')} "
        f"({p.get('ticker', p.get('symbol', ''))})"),
    "research_escalation": lambda p: (
        f"Research escalation: {p.get('reason', p.get('memo_id', '?'))}"),
    "resolution_due": lambda p: f"Resolution due: memo {p.get('memo_id', '?')}",
    "catalyst_due": lambda p: f"Catalyst due: memo {p.get('memo_id', '?')}",
}


def render_event(kind: str, payload: dict[str, Any]) -> str:
    fn = _RENDERERS.get(kind)
    if fn is not None:
        try:
            return fn(payload)
        except Exception:  # noqa: BLE001 — fall through to compact form
            pass
    if not payload:
        return kind
    compact = json.dumps(payload, default=str)
    return f"{kind}: {compact}"[:_MAX_FALLBACK]


def merged_events(
    journal_paths: dict[str, str],
    *,
    limit: int = 100,
    kinds: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for source, path in journal_paths.items():
        try:
            with closing(ro_conn(path)) as conn:
                if kinds:
                    marks = ",".join("?" for _ in kinds)
                    rows = conn.execute(
                        f"SELECT id, at, kind, payload FROM events"
                        f" WHERE kind IN ({marks})"
                        f" ORDER BY id DESC LIMIT ?",
                        (*sorted(kinds), limit)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, at, kind, payload FROM events"
                        " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        except sqlite3.OperationalError:
            continue  # missing/locked journal: feed shows the others
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except (TypeError, ValueError):
                payload = {}
            items.append({
                "source": source, "id": r["id"], "at": r["at"],
                "kind": r["kind"], "text": render_event(r["kind"], payload),
                "payload": payload,
            })
    # ISO-8601 UTC strings (journal normalizes to +00:00) sort correctly
    # as strings — same property the journal itself relies on.
    items.sort(key=lambda i: i["at"], reverse=True)
    return items[:limit]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/dashboard/test_events_view.py -v`
Expected: 5 passed

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check ops tests
git add ops/dashboard/events_view.py tests/ops/dashboard/test_events_view.py
git commit -m "feat(dashboard): merged human-rendered events feed

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: HTTP server — loopback-only, three JSON routes, static files, log tail

**Files:**
- Create: `ops/dashboard/server.py`
- Create: `ops/dashboard/static/index.html` (placeholder for this task: `<h1>ops dashboard</h1>` — Task 8 replaces it)
- Test: `tests/ops/dashboard/test_server.py` (create)

**Interfaces:**
- Consumes: `build_snapshot` (Tasks 3–5), `merged_events` (Task 6), `load_config` from `ops.config`.
- Produces (Tasks 8, 9 build on these):
  - `make_server(config: OpsConfig, port: int) -> ThreadingHTTPServer` — bound to `("127.0.0.1", port)`; `port=0` picks an ephemeral port (tests read `server.server_address[1]`).
  - `serve(port: int | None = None) -> int` — loads config, resolves port (`arg > $OPS_DASHBOARD_PORT > 8321`), prints the URL, `serve_forever()` until KeyboardInterrupt, returns 0.
  - `DEFAULT_PORT = 8321`.
  - Routes: `GET /` + static; `GET /api/snapshot`; `GET /api/events?limit=&kinds=a,b`; `GET /api/logs?file=out|err&lines=N` → `{"file":..., "text": str}` (400 on bad `file`, `lines` clamped to 1..2000, missing log file → empty text).

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/dashboard/test_server.py
"""Dashboard HTTP server: loopback bind, routes, traversal safety."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from ops.config import OpsConfig
from ops.journal import Journal
from ops.dashboard.server import make_server


@pytest.fixture()
def base_url(tmp_path):
    cfg = OpsConfig(
        journal_path=str(tmp_path / "ops.sqlite"),
        baseline_journal_path=str(tmp_path / "baseline.sqlite"),
        research_journal_path=str(tmp_path / "research.sqlite"),
        screen_store_path=str(tmp_path / "screen.sqlite"),
        memo_store_path=str(tmp_path / "memos.sqlite"),
        guardian_liveness_path=str(tmp_path / "guardian.alive"),
        research_pause_flag_path=str(tmp_path / "research.paused"),
    )
    with Journal(cfg.journal_path) as j:
        j.record_event("service_started", {"pid": 1})
    server = make_server(cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    assert host == "127.0.0.1"  # the security property, asserted directly
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read()


def test_snapshot_route_returns_json(base_url):
    status, body = _get(base_url + "/api/snapshot")
    assert status == 200
    snap = json.loads(body)
    assert "health" in snap and "sleeves" in snap


def test_events_route_with_filter(base_url):
    status, body = _get(base_url + "/api/events?limit=10&kinds=service_started")
    assert status == 200
    items = json.loads(body)
    assert items and items[0]["kind"] == "service_started"


def test_logs_route_rejects_unknown_file(base_url):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base_url + "/api/logs?file=../../etc/passwd")
    assert e.value.code == 400


def test_logs_route_missing_file_empty_text(base_url):
    status, body = _get(base_url + "/api/logs?file=out")
    assert status == 200
    assert json.loads(body)["text"] == ""


def test_index_served(base_url):
    status, body = _get(base_url + "/")
    assert status == 200 and b"ops dashboard" in body.lower()


def test_static_traversal_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base_url + "/..%2f..%2fconfig.py")
    assert e.value.code == 404


def test_unknown_route_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base_url + "/api/nope")
    assert e.value.code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/dashboard/test_server.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `ops/dashboard/server.py`**

```python
"""Loopback-only HTTP server for the ops dashboard.

The bind host is the literal 127.0.0.1 below — deliberately not a config
knob, so no env-var typo can ever expose this beyond the machine. The
server is read-only end to end: no mutating routes exist, and everything
it serves comes from mode=ro snapshot/event readers.
"""
from __future__ import annotations

import json
import os
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ops.config import OpsConfig, load_config
from ops.dashboard.events_view import merged_events
from ops.dashboard.snapshot import build_snapshot

DEFAULT_PORT = 8321
_HOST = "127.0.0.1"
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
_MAX_LOG_LINES = 2000


def _log_files() -> dict[str, Path]:
    # Enum key -> known path. Never a client-supplied path: the querystring
    # picks a key, the server owns the mapping.
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    logs = Path(base).expanduser() / "tradingagents" / "logs"
    return {"out": logs / "ops.out.log", "err": logs / "ops.err.log"}


def _tail(path: Path, lines: int) -> str:
    try:
        with path.open("r", errors="replace") as f:
            return "".join(deque(f, maxlen=lines))
    except OSError:
        return ""


class _Handler(BaseHTTPRequestHandler):
    config: OpsConfig  # injected by make_server via subclassing

    def log_message(self, *args) -> None:  # noqa: D102 — quiet by design
        pass

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status: int = 200) -> None:
        self._send(status, "application/json",
                   json.dumps(obj, default=str).encode())

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/snapshot":
                self._send_json(build_snapshot(self.config))
            elif parsed.path == "/api/events":
                self._api_events(query)
            elif parsed.path == "/api/logs":
                self._api_logs(query)
            elif parsed.path.startswith("/api/"):
                self._send_json({"error": "not found"}, status=404)
            else:
                self._static(parsed.path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 — a handler crash kills the tab
            self._send_json(
                {"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def _api_events(self, query) -> None:
        limit = min(500, max(1, int(query.get("limit", ["100"])[0])))
        kinds_raw = query.get("kinds", [""])[0]
        kinds = frozenset(k for k in kinds_raw.split(",") if k) or None
        paths = {
            "momentum": self.config.journal_path,
            "research": self.config.research_journal_path,
            "baseline": self.config.baseline_journal_path,
        }
        self._send_json(merged_events(paths, limit=limit, kinds=kinds))

    def _api_logs(self, query) -> None:
        key = query.get("file", [""])[0]
        files = _log_files()
        if key not in files:
            self._send_json(
                {"error": f"file must be one of {sorted(files)}"}, status=400)
            return
        lines = min(_MAX_LOG_LINES, max(1, int(query.get("lines", ["200"])[0])))
        self._send_json({"file": key, "text": _tail(files[key], lines)})

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (_STATIC_DIR / name).resolve()
        # resolve() + is_relative_to: no client path may escape static/.
        if not target.is_relative_to(_STATIC_DIR) or not target.is_file():
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, ctype, target.read_bytes())


def make_server(config: OpsConfig, port: int) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"config": config})
    return ThreadingHTTPServer((_HOST, port), handler)


def serve(port: int | None = None) -> int:
    config = load_config()
    if port is None:
        port = int(os.environ.get("OPS_DASHBOARD_PORT", DEFAULT_PORT))
    server = make_server(config, port)
    print(f"ops dashboard (read-only): http://{_HOST}:{server.server_address[1]}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
```

And the placeholder `ops/dashboard/static/index.html`:

```html
<!doctype html><title>ops dashboard</title><h1>ops dashboard</h1>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/dashboard/test_server.py -v`
Expected: 7 passed

- [ ] **Step 5: Package data check**

Static files must ship with the package. Check `pyproject.toml` — if it uses setuptools auto-discovery without `package-data`, add:

```toml
[tool.setuptools.package-data]
"ops.dashboard" = ["static/*"]
```

(Repo installs editable, so this only matters for future non-editable installs — still do it.)

- [ ] **Step 6: Lint and commit**

```bash
.venv/bin/ruff check ops tests
git add ops/dashboard/server.py ops/dashboard/static tests/ops/dashboard/test_server.py pyproject.toml
git commit -m "feat(dashboard): loopback-only HTTP server with snapshot/events/logs routes

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Frontend — single-page dashboard UI

> **Implementer note:** invoke the `dataviz` skill before writing the sparkline/stat-tile code — it calibrates chart colors and form. Keep its guidance within the palette below.

**Files:**
- Modify: `ops/dashboard/static/index.html` (replace placeholder)
- Create: `ops/dashboard/static/app.js`
- Create: `ops/dashboard/static/style.css`

**Interfaces:**
- Consumes: the snapshot JSON contract (plan header) via `GET /api/snapshot`; `GET /api/events?limit=80`; `GET /api/logs?file=out|err&lines=200`. Nothing external — no CDN, no fonts, no fetches beyond these three routes.
- Produces: the page at `/`. No JS exports; Task 9's manual verification drives it.

Layout (CSS grid, dark theme, monospace numerals):

```
┌──────────────────────────────────────────────────────────────┐
│ HEALTH STRIP  ● RUNNING · paper · guardian 12s · market OPEN │
│ [full-width red banner here when down/halted/paused]         │
├─────────────────────────┬────────────────────────────────────┤
│ SLEEVES (3 cards:       │ ACTIVITY FEED                      │
│  equity, day P&L,       │  22:14 research  Vetting run: …    │
│  sparkline)             │  21:02 momentum  BUY 12 XYZ @ …    │
│ POSITIONS table         │  … (kind filter <select>)          │
│ FILLS TODAY             │ OVERNIGHT card (vet/drain, paused) │
├─────────────────────────┼────────────────────────────────────┤
│ RESEARCH FUNNEL         │ ANOMALIES 7d      LOGS (collapsed) │
│ pending→open→passed…    │ table kind/count  <details> tail   │
└─────────────────────────┴────────────────────────────────────┘
```

Behavior requirements (each must be visibly true when done):

1. Poll `/api/snapshot` and `/api/events?limit=80` every 5 s (`setInterval`; skip a tick if the previous fetch is still in flight).
2. Any fetch failure → fixed top banner “dashboard disconnected — last update HH:MM:SS”; clears on next success.
3. `health.verdict` colors the status dot (RUNNING green, STALE amber, STOPPED/UNKNOWN red) and any of: STOPPED/STALE verdict, `halts.*` true, `research_paused` true → red/amber alert banner listing each condition.
4. Section with `"error"` key → panel renders a single muted line `unavailable: <error>` (chip), never a blank panel or JS exception.
5. Sleeve cards: equity (large, monospace), `day_pnl_pct` as `+4.00%` green / `−1.20%` red / `—` when null, sparkline = inline SVG polyline of `series` (no axes; 120×28px; single accent stroke).
6. Events list: `at` rendered as local HH:MM:SS + relative age, source as a small tag, `text` as the line. Kind filter is a client-side `<select>` populated from the kinds present.
7. Logs: `<details>` per file; on open, fetch once and display in `<pre>`; refresh button.
8. All money values displayed as received (strings) — never `parseFloat` for display (only for sparkline y-scaling, where float loss is fine).
9. No horizontal page scroll at ≥1100px width; tables scroll inside their own containers.

Palette (CSS custom properties — dark, terminal-adjacent):

```css
:root {
  --bg: #0f1115; --panel: #161a21; --border: #262c37;
  --text: #d6dae2; --muted: #8b93a1;
  --green: #4ade80; --amber: #fbbf24; --red: #f87171; --accent: #60a5fa;
  font-family: -apple-system, "SF Pro Text", sans-serif;
}
.num { font-family: ui-monospace, "SF Mono", monospace; }
```

Steps (no backend tests possible; verification is the real browser):

- [ ] **Step 1:** Write `index.html` — semantic skeleton with ids: `#banner`, `#health`, `#market`, `#sleeves`, `#positions`, `#fills`, `#feed`, `#feed-filter`, `#overnight`, `#funnel`, `#anomalies`, `#logs`. Loads `style.css` and `app.js` (defer). No inline scripts.
- [ ] **Step 2:** Write `app.js` — top-level: `const REFRESH_MS = 5000;` `async function tick()` fetching both endpoints in parallel (`Promise.allSettled`), one `render(snapshot, events)` dispatching to per-panel pure functions `renderHealth`, `renderSleeves`, `renderFeed`, `renderFunnel`, `renderAnomalies`, `renderMarket`. Build DOM via `document.createElement`/`textContent` — never `innerHTML` with data (journal payloads may contain user-ish strings; keep XSS impossible rather than escaping).
- [ ] **Step 3:** Write `style.css` per the palette and layout above.
- [ ] **Step 4: Verify against a seeded snapshot.** Write a throwaway script `scratchpad/seed_dashboard_demo.py` (session scratchpad, not the repo) that creates tmp stores via the Task 3–5 test seeding patterns (journal with fills/snapshots/halt event, memos, screen runs), then `make_server(cfg, 8321).serve_forever()`. Run it, open `http://127.0.0.1:8321/`, and check every behavior requirement 1–9 (kill the server mid-view to see requirement 2). Screenshot-verify if running under an agent that can.
- [ ] **Step 5: Commit**

```bash
git add ops/dashboard/static
git commit -m "feat(dashboard): single-page frontend — panels, feed, sparklines

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: CLI command + launchd deployment

**Files:**
- Modify: `ops/cli.py` (new `dashboard` command, extend `install-service`)
- Create: `ops/deploy/com.tradingagents.dashboard.plist.template`
- Modify: `ops/deploy/__init__.py` (add `render_dashboard_plist`, `DASHBOARD_LABEL`)
- Test: `tests/ops/test_deploy_dashboard.py` (create); extend pattern from `tests/ops/test_deploy.py`

**Interfaces:**
- Consumes: `serve(port)` from `ops.dashboard.server` (Task 7); `_render`/`_install_plist` idioms in `ops/deploy/__init__.py:21-43` and `ops/cli.py:43-57`.
- Produces: `ops dashboard [--port N]` CLI; `render_dashboard_plist(*, repo_root, venv_python, log_dir) -> str`; `ops install-service` writes BOTH plists and prints both bootstrap commands.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/test_deploy_dashboard.py
"""Dashboard plist rendering + install-service writing both agents."""
from click.testing import CliRunner

from ops.deploy import render_dashboard_plist
from ops.cli import cli


def test_render_dashboard_plist_substitutes_paths():
    rendered = render_dashboard_plist(
        repo_root="/repo", venv_python="/repo/.venv/bin/python",
        log_dir="/logs")
    assert "com.tradingagents.dashboard" in rendered
    assert "<string>dashboard</string>" in rendered
    assert "/logs/dashboard.out.log" in rendered
    assert "{{" not in rendered


def test_install_service_writes_both_plists(tmp_path):
    runner = CliRunner()
    ops_plist = tmp_path / "com.tradingagents.ops.plist"
    result = runner.invoke(cli, [
        "install-service", "--output", str(ops_plist),
        "--log-dir", str(tmp_path / "logs")])
    assert result.exit_code == 0, result.output
    assert ops_plist.exists()
    dash_plist = tmp_path / "com.tradingagents.dashboard.plist"
    assert dash_plist.exists()
    assert "com.tradingagents.dashboard" in dash_plist.read_text()
    # Both load commands printed.
    assert result.output.count("launchctl bootstrap") == 2
```

Check how `tests/ops/test_deploy.py` and `tests/ops/test_cli_*.py` invoke the CLI (CliRunner vs subprocess) and match the house pattern if it differs from the above.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/test_deploy_dashboard.py -v`
Expected: FAIL — `ImportError: cannot import name 'render_dashboard_plist'`

- [ ] **Step 3: Implement**

`ops/deploy/com.tradingagents.dashboard.plist.template` — copy the ops template's structure with these differences: `Label` = `com.tradingagents.dashboard`; program args `{{VENV_PYTHON}} -m ops.cli dashboard`; no `EnvironmentVariables` dict (the dashboard reads the same state dir by default; set `OPS_DASHBOARD_PORT` here later if ever needed); logs `{{LOG_DIR}}/dashboard.out.log` / `dashboard.err.log`; keep `KeepAlive {Crashed: true, SuccessfulExit: false}`, `ThrottleInterval 60`, `RunAtLoad true`, `WorkingDirectory {{REPO_ROOT}}`.

`ops/deploy/__init__.py`:

```python
_DASHBOARD_TEMPLATE_PATH = Path(__file__).with_name(
    "com.tradingagents.dashboard.plist.template")

DASHBOARD_LABEL = "com.tradingagents.dashboard"


def render_dashboard_plist(
    *, repo_root: str, venv_python: str, log_dir: str,
) -> str:
    """Render the read-only dashboard sibling agent's plist."""
    return _render(_DASHBOARD_TEMPLATE_PATH, {
        "REPO_ROOT": repo_root,
        "VENV_PYTHON": venv_python,
        "LOG_DIR": log_dir,
    })
```

`ops/cli.py` — new command after `status`:

```python
@cli.command("dashboard")
@click.option("--port", default=None, type=int,
              help="Port on 127.0.0.1 (default: $OPS_DASHBOARD_PORT or 8321)")
def dashboard(port: int | None) -> None:
    """Serve the read-only local ops dashboard (127.0.0.1 only).

    Observes the journals, memo store, and screen store via mode=ro
    reads — no broker, no writes, no network. Runs in the foreground;
    launchd owns backgrounding (see install-service)."""
    import sys

    from ops.dashboard.server import serve

    sys.exit(serve(port=port))
```

and in `install_service` (after the existing `_install_plist` call, before the pmset note): render the dashboard plist to a sibling path of `--output` named `com.tradingagents.dashboard.plist` and `_install_plist` it too:

```python
    from ops.deploy import render_dashboard_plist

    dash_output = str(Path(os.path.abspath(os.path.expanduser(output_path)))
                      .with_name("com.tradingagents.dashboard.plist"))
    rendered_dash = render_dashboard_plist(
        repo_root=repo_root, venv_python=sys.executable, log_dir=log_dir)
    _install_plist(rendered_dash, dash_output, log_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/test_deploy_dashboard.py tests/ops/test_deploy.py -v`
Expected: all pass (existing deploy tests must not regress).

- [ ] **Step 5: End-to-end verification against the REAL state dir (read-only, safe beside the live service)**

```bash
.venv/bin/python -m ops.cli dashboard --port 8321 &
sleep 1
curl -s http://127.0.0.1:8321/api/snapshot | python3 -m json.tool | head -40
curl -s "http://127.0.0.1:8321/api/events?limit=5" | python3 -m json.tool | head -30
kill %1
```

Expected: real health/sleeve data from the live journals; no tracebacks in output. Then open `http://127.0.0.1:8321/` in a browser and confirm the page renders real data.

- [ ] **Step 6: Full suite, lint, commit**

```bash
.venv/bin/pytest tests/ops -q && .venv/bin/ruff check ops tests
git add ops/cli.py ops/deploy tests/ops/test_deploy_dashboard.py
git commit -m "feat(dashboard): ops dashboard CLI + sibling launchd agent

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task dependency graph (for parallel dispatch)

- Task 1 (journal ro) and Task 2 (guardian liveness) — independent, run in parallel.
- Tasks 3 → 4 → 5 — sequential (same file `snapshot.py`).
- Task 6 (events_view) — after Task 3 (imports `ro_conn`); parallel with 4–5.
- Task 7 (server) — after 3–6.
- Task 8 (frontend) — after 7 (needs routes live for verification; the JSON contract in this header is its data spec).
- Task 9 (CLI/deploy) — after 7; parallel with 8.

## Final verification (after all tasks)

1. `.venv/bin/pytest tests/ -q` — full repo suite green.
2. `.venv/bin/ruff check .` — clean.
3. `ops install-service` → confirm both plists written; `launchctl bootstrap` both; confirm `curl http://127.0.0.1:8321/api/snapshot` works and `launchctl list | grep tradingagents` shows both agents. (This step touches the live launchd setup — do it with the user, not unilaterally.)
4. Invoke `superpowers:verification-before-completion`, then `superpowers:finishing-a-development-branch`.
