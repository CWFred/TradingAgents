# News-Driven Discovery Funnel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-07-05-news-discovery-design.md`

**Goal:** A once-daily, pre-market, research-only discovery sweep that reads global financial news, funnels ticker mentions through hard filters and a cheap LLM triage, runs the top survivors through the existing TradingAgents pipeline, persists structured theses to a new `theses` table, and emits a digest through the existing notify dispatcher.

**Architecture:** A new `ops/discovery/` package (feeds → mentions → screen → triage → job → reports) plus a `ThesesStore` in `ops/theses.py` sharing the journal SQLite file, a structured-output method on the pipeline adapter, one new notify policy entry, a `discovery_sweep` APScheduler job in `ops/main.py`, and an `ops discover --once` CLI command. Two prep changes land first: generalizing `Candidate.earnings` into a tagged `origin` field (the promotion seam), and exposing the Portfolio Manager's structured output through the pipeline adapter.

**Tech Stack:** Python 3.11+ dataclasses, sqlite3 (WAL), click, APScheduler, Alpha Vantage `NEWS_SENTIMENT` (via `tradingagents/dataflows/alpha_vantage_news.py`), yfinance metrics (via `ops/universe/filters.py`), pytest.

## Global Constraints

- Run tests with `.venv/bin/pytest` from the repo root. The default suite must never touch network or LLMs — every external dependency is a stub or injected callable.
- `ops/discovery/` and `ops/theses.py` MUST NEVER import `ops.broker` (any module under it). Held positions arrive via an injected `held_symbols_provider: Callable[[], set[str]]` wired at composition time in `ops/main.py` / `ops/cli.py`. A structural test enforces this (Task 10).
- The append-only SQLite journal is the source of truth. Discovery stage outcomes land as journal events with exactly these kinds: `discovery_sweep_started`, `discovery_triage`, `discovery_thesis`, `discovery_sweep_error`, `discovery_digest`.
- Config is a frozen dataclass with `OPS_DISCOVERY_*` env overrides, following the `OpsConfig` / `NotifyConfig` patterns in `ops/config.py` and `ops/notify/config.py`.
- Defaults copied verbatim from the spec: `enabled=False`, `max_full_runs=5`, `triage_min_score=6`, `relevance_threshold=0.5`, `cooldown_days=14`, `max_screen_symbols=30`, `reports_dir=${XDG_STATE_HOME:-~/.local/state}/tradingagents/discovery_reports`, `triage_model` defaults to the upstream `quick_think_llm` setting.
- Decimals are stored as text in SQLite (journal convention); all datetimes stored are timezone-aware and normalized to UTC (reuse `ops.journal._to_iso` / `_from_iso`).
- Error policy (spec § Error handling): news fetch garbage → abort sweep with event; one yfinance failure → drop symbol, continue; malformed triage JSON → one retry then abort; one pipeline failure → record event, continue; zero theses → no digest.
- Opt-in live test gated by `OPS_DISCOVERY_LIVE_TESTS=1` (mirrors `OPS_RH_LIVE_TESTS` in `tests/ops/broker/test_mcp_live.py`).
- Commit after every task. Do not `git add -A` — the repo may carry unrelated dirty files; always `git add` explicit paths.

## Spec ambiguities — resolutions adopted by this plan

These were not fully specified; the plan resolves them as follows (flagged to the user, do not re-litigate during execution):

1. **ETF exclusion in `mentions.py`**: Alpha Vantage tags crypto (`CRYPTO:BTC`) and forex (`FOREX:USD`) with prefixes, but does not mark ETFs. Resolution: drop prefixed symbols plus a small static frozenset of broad/index ETFs (`_BROAD_ETFS`). Leveraged ETFs are already caught downstream by the ops deny list in `screen.py`.
2. **`trigger_conditions` source**: the PM's structured output has no trigger-conditions field. Resolution: populate from the Trader's structured proposal (`**Entry Price**` / `**Stop Loss**` lines in `final_state["trader_investment_plan"]`, rendered deterministically by `render_trader_proposal`), stored as JSON `{"entry_price": ..., "stop_loss": ...}` with nulls when absent.
3. **`review_after` derivation**: "DATE derived from time_horizon" — resolution: parse "3-6 months"-style strings, take the *upper* bound, add to `created_at` (month=30 days, year=365); `NULL` when unparseable.
4. **Per-pipeline-run failure event kind**: the spec's event list has no per-run error kind. Resolution: reuse `discovery_sweep_error` with a `stage` payload field (`"pipeline"` for per-run failures, `"news_fetch"` / `"triage"` / `"sweep"` for aborts), keeping the event vocabulary to the spec's list.
5. **`discovery_sweep_error` notification**: spec only mandates rendering `discovery_digest`. Resolution: also add `discovery_sweep_error` to the notify policy as throttled email (same posture as `orchestrator_tick_error`).
6. **Manual CLI vs `enabled`**: `enabled` gates *scheduling* only; `ops discover --once` runs regardless, since it is an explicit human request.
7. **Min price / min ADV values for the discovery screen**: not in the config table. Resolution: module constants in `screen.py` equal to the universe's v1 constants (`$5`, `$50M`), with a comment cross-referencing `ops/universe/__init__.py`.
8. **`_start_full_scheduler` latent bug**: the existing `daily_summary` lambda in `ops/main.py` references a free variable `calendar` that is not in scope (NameError when the 16:05 cron fires). Task 12 must pass `calendar` as a parameter anyway to wire discovery's holiday gate; this incidentally fixes the bug. Noted so the fix is deliberate, not accidental.

## File structure

| Path | Responsibility |
|---|---|
| `ops/universe/__init__.py` (modify) | `Candidate.origin: EarningsHit \| DiscoveryHit` (renamed from `earnings`); `DiscoveryHit` dataclass |
| `ops/strategy/post_earnings_momentum.py` (modify) | Read `cand.origin`, skip non-earnings origins |
| `ops/cli.py` (modify) | `decide-once` display reads `origin`; new `discover --once` command |
| `ops/theses.py` (create) | `ThesesStore` + `theses` table + `derive_review_after` |
| `ops/pipeline_adapter.py` (modify) | `StructuredPipelineResult`, `propagate_structured` on both adapters, markdown section parser |
| `ops/discovery/__init__.py` (create) | empty package marker |
| `ops/discovery/config.py` (create) | `DiscoveryConfig` + `load_discovery_config` |
| `ops/discovery/feeds.py` (create) | AV payload → typed `Article` records; only module knowing AV's JSON shape |
| `ops/discovery/mentions.py` (create) | articles → `TickerMention` per symbol; drops crypto/forex/ETF |
| `ops/discovery/screen.py` (create) | hard filters (deny, held, cooldown, liquidity), yfinance cap |
| `ops/discovery/triage.py` (create) | one batched cheap-LLM call, strict JSON, retry/abort |
| `ops/discovery/reports.py` (create) | per-symbol markdown report rendering + archival |
| `ops/discovery/job.py` (create) | `DiscoveryJob` orchestration, journal events, budget enforcement, digest |
| `ops/notify/policy.py` (modify) | `discovery_digest` + `discovery_sweep_error` policy/rendering |
| `ops/main.py` (modify) | build + schedule discovery job (8:00 ET, trading days, when enabled) |
| `tests/ops/test_theses.py`, `tests/ops/discovery/*` (create), plus edits to existing tests | per-task test files listed in each task |

---

### Task 1: Generalize `Candidate.earnings` into tagged `origin` (promotion seam prep)

**Files:**
- Modify: `ops/universe/__init__.py`
- Modify: `ops/strategy/post_earnings_momentum.py`
- Modify: `ops/cli.py` (the `decide-once` universe display, lines ~96-99)
- Modify: `tests/ops/universe/test_universe.py`, `tests/ops/strategy/test_post_earnings_momentum.py`, `tests/ops/test_cli_decide_once.py`, `tests/ops/test_integration_decide_once.py`
- Test: `tests/ops/strategy/test_post_earnings_momentum.py` (new test)

**Interfaces:**
- Produces: `Candidate(symbol: str, origin: EarningsHit | DiscoveryHit, last_price: Decimal, avg_dollar_volume_20d: Decimal)` (frozen dataclass); `DiscoveryHit(symbol: str, thesis_id: int, triage_score: int, catalyst_type: str)` (frozen dataclass), both exported from `ops.universe`. Later tasks and future promotion code rely on these exact names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ops/strategy/test_post_earnings_momentum.py`:

```python
def test_skips_candidate_with_discovery_origin():
    """The earnings strategy must ignore candidates whose origin tag is not
    an EarningsHit — discovery-origin candidates get their own sizing later
    (promotion seam, deferred)."""
    from ops.universe import DiscoveryHit
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"NEWX": PipelineDecision.BUY})
    cand = Candidate(
        symbol="NEWX",
        origin=DiscoveryHit(symbol="NEWX", thesis_id=1, triage_score=8,
                            catalyst_type="regulatory"),
        last_price=Decimal("50"), avg_dollar_volume_20d=Decimal("60000000"),
    )
    orders = strat.propose_orders(
        candidates=[cand], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=date(2026, 7, 6),
    )
    assert orders == []
```

Then rename the constructor keyword in the three test helpers that build `Candidate`s — change `earnings=` to `origin=` in:
- `tests/ops/strategy/test_post_earnings_momentum.py` `_candidate()` (line ~19)
- `tests/ops/test_cli_decide_once.py` `_candidate()` (line ~21)
- `tests/ops/test_integration_decide_once.py` `_candidate()` (line ~19)

(`tests/ops/universe/test_universe.py` builds candidates only through `build_universe`, no keyword change needed there.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/strategy/test_post_earnings_momentum.py tests/ops/test_cli_decide_once.py -x -q`
Expected: FAIL — `ImportError: cannot import name 'DiscoveryHit'` and/or `TypeError: Candidate.__init__() got an unexpected keyword argument 'origin'`.

- [ ] **Step 3: Implement**

In `ops/universe/__init__.py`, replace the `Candidate` dataclass and add `DiscoveryHit` (keep the module docstring and imports as-is):

```python
@dataclass(frozen=True)
class DiscoveryHit:
    """Origin tag for a candidate sourced from a persisted discovery thesis.

    Placeholder for the deferred promotion seam (theses_candidate_source):
    nothing constructs it in this iteration, but Candidate.origin is typed
    against it from day one so promotion later is additive."""
    symbol: str
    thesis_id: int
    triage_score: int
    catalyst_type: str


@dataclass(frozen=True)
class Candidate:
    symbol: str
    origin: EarningsHit | DiscoveryHit
    last_price: Decimal
    avg_dollar_volume_20d: Decimal
```

In `build_universe`, change the constructor call from `earnings=hits_by_sym[sym]` to `origin=hits_by_sym[sym]`. Update the export list:

```python
__all__ = ["Candidate", "DiscoveryHit", "build_universe"]
```

In `ops/strategy/post_earnings_momentum.py`, inside the `for cand in candidates:` loop, add the tag check *before* the pipeline call (never spend an LLM run on a candidate we cannot act on) and switch the reason string to `origin`:

```python
from ops.universe import Candidate
from ops.universe.earnings import EarningsHit   # add this import at top

        for cand in candidates:
            if not isinstance(cand.origin, EarningsHit):
                # Discovery-origin candidates are research-only until the
                # promotion seam lands its own sizing rules.
                continue
            result = pipeline.propagate(cand.symbol, asof_date)
            ...
                reason=f"post-earnings beat (EPS {cand.origin.eps_actual} vs "
                       f"est {cand.origin.eps_estimate}); pipeline BUY",
```

In `ops/cli.py` `decide_once`, replace the universe display loop body (lines ~96-99):

```python
    for c in candidates:
        if isinstance(c.origin, EarningsHit):
            click.echo(f"  - {c.symbol}: price=${c.last_price} "
                       f"earnings beat (EPS {c.origin.eps_actual}/"
                       f"{c.origin.eps_estimate})")
        else:
            click.echo(f"  - {c.symbol}: price=${c.last_price} "
                       f"discovery (triage {c.origin.triage_score})")
```

Add `from ops.universe.earnings import EarningsHit` to `ops/cli.py`'s imports.

- [ ] **Step 4: Run the full ops suite**

Run: `.venv/bin/pytest tests/ops -q`
Expected: PASS (no remaining `earnings=` constructor usages; `grep -rn "\.earnings\b" ops/ tests/ops/ --include='*.py' | grep -v universe/earnings` should show no `Candidate.earnings` reads).

- [ ] **Step 5: Commit**

```bash
git add ops/universe/__init__.py ops/strategy/post_earnings_momentum.py ops/cli.py \
  tests/ops/strategy/test_post_earnings_momentum.py tests/ops/test_cli_decide_once.py \
  tests/ops/test_integration_decide_once.py
git commit -m "refactor(ops/universe): generalize Candidate.earnings into tagged origin field"
```

---

### Task 2: `ThesesStore` — persistent thesis table

**Files:**
- Create: `ops/theses.py`
- Test: `tests/ops/test_theses.py`

**Interfaces:**
- Consumes: `ops.journal._to_iso`, `_from_iso`, `_now_iso` (UTC-normalization helpers; same-package private import, precedent: `ops/universe/filters.py` imports `_safe_decimal`).
- Produces (Task 10 and CLI depend on these exact signatures):
  - `ThesesStore(path: str)` — opens its own sqlite3 connection (WAL) on the journal file; creates the `theses` table.
  - `ThesesStore.insert_thesis(*, symbol: str, source: str, rating: str | None, price_target: Decimal | None, time_horizon: str | None, thesis_summary: str | None, triage_score: int | None, triage_rationale: str | None, source_articles: list[dict], last_price: Decimal | None, avg_dollar_volume: Decimal | None, report_path: str | None, trigger_conditions: dict | None, created_at: datetime | None = None) -> int` — supersedes prior `active` rows for the symbol, inserts the new row as `active`, returns its id.
  - `ThesesStore.recent_symbols(*, since: datetime) -> set[str]`
  - `ThesesStore.read_theses(symbol: str | None = None) -> list[ThesisRow]`
  - `ThesesStore.close()`, context-manager support.
  - `derive_review_after(created_at: datetime, time_horizon: str | None) -> date | None`
  - `ThesisRow` frozen dataclass with fields: `id, created_at, symbol, source, rating, price_target, time_horizon, thesis_summary, triage_score, triage_rationale, source_articles, last_price, avg_dollar_volume, report_path, trigger_conditions, review_after, status`.

- [ ] **Step 1: Write the failing tests**

Create `tests/ops/test_theses.py`:

```python
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ops.theses import ThesesStore, derive_review_after


def _store(tmp_path):
    return ThesesStore(str(tmp_path / "j.sqlite"))


def _insert(store, sym="ACME", created_at=None, **overrides):
    kwargs = dict(
        symbol=sym, source="discovery", rating="Buy",
        price_target=Decimal("120.50"), time_horizon="3-6 months",
        thesis_summary="Underappreciated regulatory tailwind.",
        triage_score=8, triage_rationale="FDA approval ahead of consensus",
        source_articles=[{"title": "t", "url": "http://x", "published_at": "2026-07-05T08:00:00+00:00"}],
        last_price=Decimal("95.10"), avg_dollar_volume=Decimal("60000000"),
        report_path="/tmp/reports/2026-07-05/ACME.md",
        trigger_conditions={"entry_price": "93", "stop_loss": "85"},
        created_at=created_at,
    )
    kwargs.update(overrides)
    return store.insert_thesis(**kwargs)


def test_insert_and_read_roundtrip(tmp_path):
    with _store(tmp_path) as store:
        tid = _insert(store)
        rows = store.read_theses(symbol="ACME")
        assert len(rows) == 1
        row = rows[0]
        assert row.id == tid
        assert row.symbol == "ACME"
        assert row.source == "discovery"
        assert row.rating == "Buy"
        assert row.price_target == Decimal("120.50")
        assert row.time_horizon == "3-6 months"
        assert row.triage_score == 8
        assert row.source_articles[0]["url"] == "http://x"
        assert row.trigger_conditions == {"entry_price": "93", "stop_loss": "85"}
        assert row.status == "active"
        # review_after = created_at + 6 months (upper bound of "3-6 months")
        assert row.review_after == (row.created_at + timedelta(days=180)).date()


def test_insert_supersedes_prior_active_for_symbol(tmp_path):
    with _store(tmp_path) as store:
        first = _insert(store)
        second = _insert(store)
        rows = {r.id: r for r in store.read_theses(symbol="ACME")}
        assert rows[first].status == "superseded"
        assert rows[second].status == "active"


def test_supersede_is_per_symbol(tmp_path):
    with _store(tmp_path) as store:
        a = _insert(store, sym="AAA")
        b = _insert(store, sym="BBB")
        assert all(r.status == "active" for r in store.read_theses())
        assert {r.symbol for r in store.read_theses()} == {"AAA", "BBB"}


def test_recent_symbols_window(tmp_path):
    now = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    with _store(tmp_path) as store:
        _insert(store, sym="OLD", created_at=now - timedelta(days=20))
        _insert(store, sym="NEW", created_at=now - timedelta(days=3))
        recent = store.recent_symbols(since=now - timedelta(days=14))
        assert recent == {"NEW"}


def test_recent_symbols_counts_superseded_rows(tmp_path):
    """Cooldown means 'analyzed recently', regardless of status."""
    now = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    with _store(tmp_path) as store:
        _insert(store, sym="ACME", created_at=now - timedelta(days=2))
        _insert(store, sym="ACME", created_at=now - timedelta(days=1))
        assert store.recent_symbols(since=now - timedelta(days=14)) == {"ACME"}


def test_naive_created_at_rejected(tmp_path):
    with _store(tmp_path) as store:
        with pytest.raises(ValueError):
            _insert(store, created_at=datetime(2026, 7, 5, 12, 0))


def test_nullable_fields_roundtrip_as_none(tmp_path):
    with _store(tmp_path) as store:
        _insert(store, rating=None, price_target=None, time_horizon=None,
                thesis_summary=None, triage_score=None, triage_rationale=None,
                last_price=None, avg_dollar_volume=None, report_path=None,
                trigger_conditions=None)
        row = store.read_theses()[0]
        assert row.rating is None and row.price_target is None
        assert row.trigger_conditions is None and row.review_after is None


def test_shares_journal_sqlite_file(tmp_path):
    """theses lives in the SAME file as the ops journal."""
    from ops.journal import Journal
    path = str(tmp_path / "j.sqlite")
    j = Journal(path)
    j.record_event("discovery_sweep_started", {"date": "2026-07-05"})
    with ThesesStore(path) as store:
        _insert(store)
        assert len(store.read_theses()) == 1
    assert len(j.read_events()) == 1
    j.close()


@pytest.mark.parametrize("horizon,days", [
    ("3-6 months", 180),
    ("6 months", 180),
    ("2 weeks", 14),
    ("1-2 years", 730),
    ("10 days", 10),
])
def test_derive_review_after(horizon, days):
    created = datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc)
    assert derive_review_after(created, horizon) == (created + timedelta(days=days)).date()


def test_derive_review_after_unparseable_is_none():
    created = datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc)
    assert derive_review_after(created, None) is None
    assert derive_review_after(created, "until the cows come home") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/test_theses.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.theses'`.

- [ ] **Step 3: Implement `ops/theses.py`**

```python
"""Persistent thesis store — the durable home for structured PM output.

Lives in the SAME SQLite file as the ops journal (spec: "New `theses` table
in the existing journal SQLite") but through its own connection so
ops/journal.py stays append-only-events-focused and unmodified. WAL mode
(set by whichever connection opens first) makes the two connections safe.

Research-only rule: this module must never import ops.broker.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ops.journal import _from_iso, _now_iso, _to_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS theses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    rating TEXT,
    price_target TEXT,
    time_horizon TEXT,
    thesis_summary TEXT,
    triage_score INTEGER,
    triage_rationale TEXT,
    source_articles TEXT,
    last_price TEXT,
    avg_dollar_volume TEXT,
    report_path TEXT,
    trigger_conditions TEXT,
    review_after TEXT,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_theses_symbol ON theses(symbol);
CREATE INDEX IF NOT EXISTS idx_theses_created_at ON theses(created_at);
"""

# "3-6 months", "6 months", "2 weeks", "1-2 years", "10 days" — captures an
# optional lower bound and a required upper bound + unit. Upper bound wins:
# review_after marks when the thesis is definitely stale, not merely due.
_HORIZON_RE = re.compile(
    r"(?:(\d+)\s*(?:-|–|to)\s*)?(\d+)\s*(day|week|month|year)s?",
    re.IGNORECASE,
)

_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def derive_review_after(created_at: datetime, time_horizon: str | None) -> date | None:
    """Upper bound of a '3-6 months'-style horizon added to created_at.
    None when the horizon is missing or unparseable — never guessed."""
    if not time_horizon:
        return None
    m = _HORIZON_RE.search(time_horizon)
    if m is None:
        return None
    n = int(m.group(2))
    days = _UNIT_DAYS[m.group(3).lower()] * n
    return (created_at + timedelta(days=days)).date()


@dataclass(frozen=True)
class ThesisRow:
    id: int
    created_at: datetime
    symbol: str
    source: str
    rating: str | None
    price_target: Decimal | None
    time_horizon: str | None
    thesis_summary: str | None
    triage_score: int | None
    triage_rationale: str | None
    source_articles: list[dict]
    last_price: Decimal | None
    avg_dollar_volume: Decimal | None
    report_path: str | None
    trigger_conditions: dict | None
    review_after: date | None
    status: str


def _dec_or_none(s: str | None) -> Decimal | None:
    return Decimal(s) if s is not None else None


class ThesesStore:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        parent = Path(path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)

    def insert_thesis(
        self, *, symbol: str, source: str,
        rating: str | None, price_target: Decimal | None,
        time_horizon: str | None, thesis_summary: str | None,
        triage_score: int | None, triage_rationale: str | None,
        source_articles: list[dict], last_price: Decimal | None,
        avg_dollar_volume: Decimal | None, report_path: str | None,
        trigger_conditions: dict | None, created_at: datetime | None = None,
    ) -> int:
        ts = _to_iso(created_at) if created_at is not None else _now_iso()
        review_after = derive_review_after(_from_iso(ts), time_horizon)
        with self._lock:
            self._conn.execute(
                "UPDATE theses SET status = 'superseded'"
                " WHERE symbol = ? AND status = 'active'",
                (symbol,),
            )
            cur = self._conn.execute(
                "INSERT INTO theses (created_at, symbol, source, rating,"
                " price_target, time_horizon, thesis_summary, triage_score,"
                " triage_rationale, source_articles, last_price,"
                " avg_dollar_volume, report_path, trigger_conditions,"
                " review_after, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                (
                    ts, symbol, source, rating,
                    str(price_target) if price_target is not None else None,
                    time_horizon, thesis_summary, triage_score,
                    triage_rationale, json.dumps(source_articles, default=str),
                    str(last_price) if last_price is not None else None,
                    str(avg_dollar_volume) if avg_dollar_volume is not None else None,
                    report_path,
                    json.dumps(trigger_conditions) if trigger_conditions is not None else None,
                    review_after.isoformat() if review_after is not None else None,
                ),
            )
            return int(cur.lastrowid)

    def recent_symbols(self, *, since: datetime) -> set[str]:
        """Symbols with ANY thesis row (regardless of status) at or after
        `since` — the cooldown question."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT DISTINCT symbol FROM theses WHERE created_at >= ?",
                (_to_iso(since),),
            )
            return {row[0] for row in cur.fetchall()}

    def read_theses(self, symbol: str | None = None) -> list[ThesisRow]:
        sql = ("SELECT id, created_at, symbol, source, rating, price_target,"
               " time_horizon, thesis_summary, triage_score, triage_rationale,"
               " source_articles, last_price, avg_dollar_volume, report_path,"
               " trigger_conditions, review_after, status FROM theses")
        params: tuple[Any, ...] = ()
        if symbol is not None:
            sql += " WHERE symbol = ?"
            params = (symbol,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            ThesisRow(
                id=r[0], created_at=_from_iso(r[1]), symbol=r[2], source=r[3],
                rating=r[4], price_target=_dec_or_none(r[5]),
                time_horizon=r[6], thesis_summary=r[7], triage_score=r[8],
                triage_rationale=r[9],
                source_articles=json.loads(r[10]) if r[10] else [],
                last_price=_dec_or_none(r[11]),
                avg_dollar_volume=_dec_or_none(r[12]), report_path=r[13],
                trigger_conditions=json.loads(r[14]) if r[14] else None,
                review_after=date.fromisoformat(r[15]) if r[15] else None,
                status=r[16],
            )
            for r in rows
        ]

    def __enter__(self) -> "ThesesStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/test_theses.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add ops/theses.py tests/ops/test_theses.py
git commit -m "feat(ops): ThesesStore — persistent theses table in the journal SQLite"
```

---

### Task 3: Structured PM output through the pipeline adapter

**Files:**
- Modify: `ops/pipeline_adapter.py`
- Test: `tests/ops/test_pipeline_adapter.py` (append)

**Interfaces:**
- Consumes: `TradingAgentsGraph.propagate(symbol, iso_date) -> (final_state: dict, decision_text: str)`. `final_state["final_trade_decision"]` is markdown rendered by `tradingagents/agents/schemas.py::render_pm_decision` with deterministic `**Rating**:` / `**Executive Summary**:` / `**Investment Thesis**:` / `**Price Target**:` / `**Time Horizon**:` headers; `final_state["trader_investment_plan"]` rendered by `render_trader_proposal` with `**Entry Price**:` / `**Stop Loss**:` headers. Structured-output fallback means headers may be absent (freetext) — parser must return `None`s, never raise.
- Produces (Tasks 9, 10, 12 rely on these):
  - `StructuredPipelineResult` frozen dataclass: `symbol: str, date: date, decision: PipelineDecision, rating: str | None, executive_summary: str | None, investment_thesis: str | None, price_target: Decimal | None, time_horizon: str | None, entry_price: Decimal | None, stop_loss: Decimal | None, pm_markdown: str, raw: dict`.
  - `TradingAgentsPipelineAdapter.propagate_structured(symbol: str, asof_date: date) -> StructuredPipelineResult` (one graph run, no double-propagate).
  - `StubPipelineAdapter(decisions=None, structured: dict[str, StructuredPipelineResult] | None = None)` with matching `propagate_structured`.
  - Helpers: `parse_labeled_markdown(text: str) -> dict[str, str]`, `structured_result_from_state(symbol, asof_date, final_state, decision_text) -> StructuredPipelineResult`.
  - The existing `propagate` / `PipelineResult` / `parse_decision` are untouched (trading path unchanged).

- [ ] **Step 1: Write the failing tests**

Append to `tests/ops/test_pipeline_adapter.py`:

```python
from datetime import date
from decimal import Decimal

from ops.pipeline_adapter import (
    PipelineDecision,
    StructuredPipelineResult,
    StubPipelineAdapter,
    parse_labeled_markdown,
    structured_result_from_state,
)

_PM_MD = """**Rating**: Overweight

**Executive Summary**: Enter on weakness below $95; size 5%.
Watch the FDA decision date.

**Investment Thesis**: The market underprices the approval odds.

**Price Target**: 120.5

**Time Horizon**: 3-6 months"""

_TRADER_MD = """**Action**: Buy

**Reasoning**: Approval odds mispriced.

**Entry Price**: 93.0

**Stop Loss**: 85

FINAL TRANSACTION PROPOSAL: **BUY**"""


def test_parse_labeled_markdown_sections_and_multiline():
    sections = parse_labeled_markdown(_PM_MD)
    assert sections["Rating"] == "Overweight"
    assert sections["Executive Summary"].startswith("Enter on weakness")
    assert "FDA decision date" in sections["Executive Summary"]
    assert sections["Price Target"] == "120.5"
    assert sections["Time Horizon"] == "3-6 months"


def test_structured_result_from_state_full():
    state = {"final_trade_decision": _PM_MD, "trader_investment_plan": _TRADER_MD}
    r = structured_result_from_state("ACME", date(2026, 7, 6), state, "Overweight")
    assert r.symbol == "ACME"
    assert r.decision == PipelineDecision.HOLD  # Overweight collapses to HOLD
    assert r.rating == "Overweight"
    assert r.price_target == Decimal("120.5")
    assert r.time_horizon == "3-6 months"
    assert r.entry_price == Decimal("93.0")
    assert r.stop_loss == Decimal("85")
    assert r.pm_markdown == _PM_MD
    assert "underprices" in r.investment_thesis


def test_structured_result_from_freetext_fallback_is_all_none():
    """Providers without structured output produce prose — parser must
    degrade to Nones, never raise."""
    state = {"final_trade_decision": "I think we should buy this one.",
             "trader_investment_plan": "Just buy some."}
    r = structured_result_from_state("ACME", date(2026, 7, 6), state, "Buy")
    assert r.decision == PipelineDecision.BUY
    assert r.rating is None and r.price_target is None
    assert r.entry_price is None and r.stop_loss is None
    assert r.pm_markdown == "I think we should buy this one."


def test_structured_result_missing_state_keys():
    r = structured_result_from_state("ACME", date(2026, 7, 6), {}, "")
    assert r.decision == PipelineDecision.HOLD
    assert r.rating is None and r.pm_markdown == ""


def test_stub_adapter_propagate_structured_canned_and_default():
    canned = StructuredPipelineResult(
        symbol="ACME", date=date(2026, 7, 6), decision=PipelineDecision.BUY,
        rating="Buy", executive_summary="s", investment_thesis="t",
        price_target=Decimal("120"), time_horizon="3-6 months",
        entry_price=None, stop_loss=None, pm_markdown="**Rating**: Buy", raw={},
    )
    stub = StubPipelineAdapter(structured={"ACME": canned})
    assert stub.propagate_structured("ACME", date(2026, 7, 6)) is canned
    default = stub.propagate_structured("OTHR", date(2026, 7, 6))
    assert default.decision == PipelineDecision.HOLD
    assert default.rating is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/test_pipeline_adapter.py -q`
Expected: FAIL with `ImportError: cannot import name 'StructuredPipelineResult'`.

- [ ] **Step 3: Implement in `ops/pipeline_adapter.py`**

Add below the existing `PipelineResult` (imports to extend: `from decimal import Decimal`):

```python
_LABEL_RE = re.compile(r"^\*\*(?P<label>[^*]+)\*\*:\s*(?P<rest>.*)$")


def parse_labeled_markdown(text: str) -> dict[str, str]:
    """Parse the deterministic '**Label**: value' sections produced by
    tradingagents.agents.schemas render helpers. A section's value runs
    until the next labeled line. Freetext without labels → empty dict."""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = _LABEL_RE.match(line.strip())
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group("label").strip()
            buf = [m.group("rest")]
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _decimal_or_none(s: str | None) -> Decimal | None:
    if not s:
        return None
    m = _NUM_RE.search(s.replace(",", ""))
    return Decimal(m.group(0)) if m else None


@dataclass(frozen=True)
class StructuredPipelineResult:
    """Full structured PM output + the collapsed trading decision.

    The trading path keeps consuming PipelineResult/BUY-HOLD-SELL; this type
    is the research path's view (spec § "Pipeline adapter exposes structured
    output")."""
    symbol: str
    date: date
    decision: PipelineDecision
    rating: str | None
    executive_summary: str | None
    investment_thesis: str | None
    price_target: Decimal | None
    time_horizon: str | None
    entry_price: Decimal | None
    stop_loss: Decimal | None
    pm_markdown: str
    raw: dict = field(default_factory=dict, repr=False)


def structured_result_from_state(
    symbol: str, asof_date: date, final_state: dict, decision_text: str,
) -> StructuredPipelineResult:
    pm_md = str(final_state.get("final_trade_decision") or "")
    pm = parse_labeled_markdown(pm_md)
    trader = parse_labeled_markdown(str(final_state.get("trader_investment_plan") or ""))
    return StructuredPipelineResult(
        symbol=symbol,
        date=asof_date,
        decision=parse_decision(decision_text or ""),
        rating=pm.get("Rating") or None,
        executive_summary=pm.get("Executive Summary") or None,
        investment_thesis=pm.get("Investment Thesis") or None,
        price_target=_decimal_or_none(pm.get("Price Target")),
        time_horizon=pm.get("Time Horizon") or None,
        entry_price=_decimal_or_none(trader.get("Entry Price")),
        stop_loss=_decimal_or_none(trader.get("Stop Loss")),
        pm_markdown=pm_md,
        raw=final_state,
    )
```

Extend the `PipelineAdapter` protocol:

```python
class PipelineAdapter(Protocol):
    def propagate(self, symbol: str, asof_date: date) -> PipelineResult: ...
    def propagate_structured(self, symbol: str, asof_date: date) -> StructuredPipelineResult: ...
```

Add to `TradingAgentsPipelineAdapter`:

```python
    def propagate_structured(self, symbol: str, asof_date: date) -> StructuredPipelineResult:
        graph = self._ensure_graph()
        raw, decision_text = graph.propagate(symbol, asof_date.isoformat())
        final_state = raw if isinstance(raw, dict) else {"output": str(raw)}
        return structured_result_from_state(symbol, asof_date, final_state, decision_text or "")
```

Replace `StubPipelineAdapter.__init__` and add `propagate_structured`:

```python
class StubPipelineAdapter:
    """In-memory adapter for tests and dry-runs. Returns fixed decisions."""

    def __init__(
        self,
        decisions: dict[str, PipelineDecision] | None = None,
        structured: dict[str, StructuredPipelineResult] | None = None,
    ):
        self._decisions = decisions or {}
        self._structured = structured or {}

    def propagate(self, symbol: str, asof_date: date) -> PipelineResult:
        decision = self._decisions.get(symbol, PipelineDecision.HOLD)
        return PipelineResult(symbol=symbol, date=asof_date, decision=decision, raw={})

    def propagate_structured(self, symbol: str, asof_date: date) -> StructuredPipelineResult:
        if symbol in self._structured:
            return self._structured[symbol]
        decision = self._decisions.get(symbol, PipelineDecision.HOLD)
        return StructuredPipelineResult(
            symbol=symbol, date=asof_date, decision=decision,
            rating=None, executive_summary=None, investment_thesis=None,
            price_target=None, time_horizon=None, entry_price=None,
            stop_loss=None, pm_markdown="", raw={},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/test_pipeline_adapter.py tests/ops -q`
Expected: PASS (structured tests pass; existing suite unaffected).

- [ ] **Step 5: Commit**

```bash
git add ops/pipeline_adapter.py tests/ops/test_pipeline_adapter.py
git commit -m "feat(ops): expose structured PM output via propagate_structured"
```

---

### Task 4: `DiscoveryConfig`

**Files:**
- Create: `ops/discovery/__init__.py` (empty, like `ops/notify/__init__.py`)
- Create: `ops/discovery/config.py`
- Test: `tests/ops/discovery/__init__.py` (empty), `tests/ops/discovery/test_config.py`

**Interfaces:**
- Consumes: `ops.config._env_decimal`, `_env_int`; `ops.notify.config._env_bool`.
- Produces: `DiscoveryConfig` frozen dataclass with fields exactly `enabled: bool = False`, `max_full_runs: int = 5`, `triage_model: str | None = None`, `triage_min_score: int = 6`, `relevance_threshold: Decimal = Decimal("0.5")`, `cooldown_days: int = 14`, `max_screen_symbols: int = 30`, `reports_dir: str` (XDG default); `load_discovery_config() -> DiscoveryConfig` reading `OPS_DISCOVERY_ENABLED`, `OPS_DISCOVERY_MAX_FULL_RUNS`, `OPS_DISCOVERY_TRIAGE_MODEL`, `OPS_DISCOVERY_TRIAGE_MIN_SCORE`, `OPS_DISCOVERY_RELEVANCE_THRESHOLD`, `OPS_DISCOVERY_COOLDOWN_DAYS`, `OPS_DISCOVERY_MAX_SCREEN_SYMBOLS`, `OPS_DISCOVERY_REPORTS_DIR`.

- [ ] **Step 1: Write the failing tests**

Create empty `tests/ops/discovery/__init__.py`, then `tests/ops/discovery/test_config.py`:

```python
from decimal import Decimal

import pytest

from ops.discovery.config import DiscoveryConfig, load_discovery_config


def test_defaults_match_spec():
    cfg = DiscoveryConfig()
    assert cfg.enabled is False
    assert cfg.max_full_runs == 5
    assert cfg.triage_model is None          # None → upstream quick_think_llm
    assert cfg.triage_min_score == 6
    assert cfg.relevance_threshold == Decimal("0.5")
    assert cfg.cooldown_days == 14
    assert cfg.max_screen_symbols == 30
    assert cfg.reports_dir.endswith("tradingagents/discovery_reports")


def test_reports_dir_respects_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cfg = DiscoveryConfig()
    assert cfg.reports_dir == str(tmp_path / "tradingagents" / "discovery_reports")


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("OPS_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("OPS_DISCOVERY_MAX_FULL_RUNS", "2")
    monkeypatch.setenv("OPS_DISCOVERY_TRIAGE_MODEL", "gpt-cheapo")
    monkeypatch.setenv("OPS_DISCOVERY_TRIAGE_MIN_SCORE", "8")
    monkeypatch.setenv("OPS_DISCOVERY_RELEVANCE_THRESHOLD", "0.7")
    monkeypatch.setenv("OPS_DISCOVERY_COOLDOWN_DAYS", "7")
    monkeypatch.setenv("OPS_DISCOVERY_MAX_SCREEN_SYMBOLS", "10")
    monkeypatch.setenv("OPS_DISCOVERY_REPORTS_DIR", "/tmp/reports")
    cfg = load_discovery_config()
    assert cfg.enabled is True
    assert cfg.max_full_runs == 2
    assert cfg.triage_model == "gpt-cheapo"
    assert cfg.triage_min_score == 8
    assert cfg.relevance_threshold == Decimal("0.7")
    assert cfg.cooldown_days == 7
    assert cfg.max_screen_symbols == 10
    assert cfg.reports_dir == "/tmp/reports"


@pytest.mark.parametrize("field,value", [
    ("max_full_runs", -1),
    ("triage_min_score", 11),
    ("triage_min_score", -1),
    ("relevance_threshold", Decimal("1.5")),
    ("relevance_threshold", Decimal("-0.1")),
    ("cooldown_days", -1),
    ("max_screen_symbols", 0),
])
def test_validation_rejects_out_of_range(field, value):
    with pytest.raises(ValueError):
        DiscoveryConfig(**{field: value})


def test_invalid_env_int_raises_named_error(monkeypatch):
    monkeypatch.setenv("OPS_DISCOVERY_MAX_FULL_RUNS", "lots")
    with pytest.raises(ValueError, match="OPS_DISCOVERY_MAX_FULL_RUNS"):
        load_discovery_config()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/discovery/test_config.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.discovery'`.

- [ ] **Step 3: Implement**

Create empty `ops/discovery/__init__.py`. Create `ops/discovery/config.py`:

```python
"""Discovery sweep configuration, env-overridable via OPS_DISCOVERY_*.

Follows the OpsConfig frozen-dataclass pattern. Research-only rule: this
package never imports ops.broker.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

from ops.config import _env_decimal, _env_int
from ops.notify.config import _env_bool


def _default_reports_dir() -> str:
    """Same XDG base-dir convention as ops.config._default_journal_path."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(os.path.expanduser(base), "tradingagents", "discovery_reports")


@dataclass(frozen=True)
class DiscoveryConfig:
    enabled: bool = False
    max_full_runs: int = 5
    triage_model: str | None = None  # None → upstream quick_think_llm setting
    triage_min_score: int = 6
    relevance_threshold: Decimal = Decimal("0.5")
    cooldown_days: int = 14
    max_screen_symbols: int = 30
    reports_dir: str = field(default_factory=_default_reports_dir)

    def __post_init__(self) -> None:
        if self.max_full_runs < 0:
            raise ValueError(f"max_full_runs must be >= 0, got {self.max_full_runs}")
        if not (0 <= self.triage_min_score <= 10):
            raise ValueError(
                f"triage_min_score must be in [0, 10], got {self.triage_min_score}"
            )
        if not (Decimal("0") <= self.relevance_threshold <= Decimal("1")):
            raise ValueError(
                f"relevance_threshold must be in [0, 1], got {self.relevance_threshold}"
            )
        if self.cooldown_days < 0:
            raise ValueError(f"cooldown_days must be >= 0, got {self.cooldown_days}")
        if self.max_screen_symbols <= 0:
            raise ValueError(
                f"max_screen_symbols must be > 0, got {self.max_screen_symbols}"
            )


def load_discovery_config() -> DiscoveryConfig:
    kwargs: dict = {}
    if os.environ.get("OPS_DISCOVERY_ENABLED") is not None:
        kwargs["enabled"] = _env_bool("OPS_DISCOVERY_ENABLED")
    max_full_runs = _env_int("OPS_DISCOVERY_MAX_FULL_RUNS")
    if max_full_runs is not None:
        kwargs["max_full_runs"] = max_full_runs
    triage_model = os.environ.get("OPS_DISCOVERY_TRIAGE_MODEL")
    if triage_model is not None:
        kwargs["triage_model"] = triage_model
    triage_min_score = _env_int("OPS_DISCOVERY_TRIAGE_MIN_SCORE")
    if triage_min_score is not None:
        kwargs["triage_min_score"] = triage_min_score
    relevance_threshold = _env_decimal("OPS_DISCOVERY_RELEVANCE_THRESHOLD")
    if relevance_threshold is not None:
        kwargs["relevance_threshold"] = relevance_threshold
    cooldown_days = _env_int("OPS_DISCOVERY_COOLDOWN_DAYS")
    if cooldown_days is not None:
        kwargs["cooldown_days"] = cooldown_days
    max_screen_symbols = _env_int("OPS_DISCOVERY_MAX_SCREEN_SYMBOLS")
    if max_screen_symbols is not None:
        kwargs["max_screen_symbols"] = max_screen_symbols
    reports_dir = os.environ.get("OPS_DISCOVERY_REPORTS_DIR")
    if reports_dir is not None:
        kwargs["reports_dir"] = reports_dir
    return DiscoveryConfig(**kwargs)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/discovery/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/discovery/__init__.py ops/discovery/config.py tests/ops/discovery/__init__.py tests/ops/discovery/test_config.py
git commit -m "feat(ops/discovery): DiscoveryConfig with OPS_DISCOVERY_* env overrides"
```

---

### Task 5: `feeds.py` — Alpha Vantage news → typed `Article` records

**Files:**
- Create: `ops/discovery/feeds.py`
- Create: `tests/ops/discovery/fixtures/av_news_sentiment.json`
- Test: `tests/ops/discovery/test_feeds.py`

**Interfaces:**
- Consumes: `tradingagents.dataflows.alpha_vantage_news.get_global_news(curr_date: str, look_back_days: int, limit: int) -> dict | str` (returns the raw JSON **string** on success; raises `AlphaVantageRateLimitError` / `AlphaVantageNotConfiguredError` from `alpha_vantage_common`).
- Produces (Tasks 6, 10, 13 rely on these):
  - `TickerTag(symbol: str, relevance: Decimal, sentiment: Decimal)` (frozen)
  - `Article(title: str, summary: str, url: str, published_at: datetime, tickers: tuple[TickerTag, ...])` (frozen)
  - `FeedError(Exception)`
  - `parse_global_news(raw: str | dict) -> list[Article]` — raises `FeedError` on non-JSON or missing `feed` list; skips malformed items/tags silently.
  - `fetch_articles(asof_date: date, *, limit: int = 200, fetcher: Callable = get_global_news) -> list[Article]` — last-24h window (`look_back_days=1`).

- [ ] **Step 1: Create the recorded fixture**

Create `tests/ops/discovery/fixtures/av_news_sentiment.json` (real NEWS_SENTIMENT schema, synthetic content):

```json
{
  "items": "4",
  "sentiment_score_definition": "x <= -0.35: Bearish; ...",
  "relevance_score_definition": "0 < x <= 1, with a higher score indicating higher relevance.",
  "feed": [
    {
      "title": "Acme Corp wins surprise FDA approval",
      "url": "https://example.com/acme-fda",
      "time_published": "20260705T081500",
      "summary": "Acme Corp received FDA approval for its lead drug two quarters early.",
      "ticker_sentiment": [
        {"ticker": "ACME", "relevance_score": "0.91", "ticker_sentiment_score": "0.62", "ticker_sentiment_label": "Bullish"},
        {"ticker": "RIVL", "relevance_score": "0.22", "ticker_sentiment_score": "-0.31", "ticker_sentiment_label": "Somewhat-Bearish"}
      ]
    },
    {
      "title": "Acme approval reshapes the sector",
      "url": "https://example.com/acme-sector",
      "time_published": "20260705T090000",
      "summary": "Analysts scramble to reprice the space after Acme's approval.",
      "ticker_sentiment": [
        {"ticker": "ACME", "relevance_score": "0.66", "ticker_sentiment_score": "0.41", "ticker_sentiment_label": "Bullish"},
        {"ticker": "SPY", "relevance_score": "0.80", "ticker_sentiment_score": "0.05", "ticker_sentiment_label": "Neutral"}
      ]
    },
    {
      "title": "Bitcoin rallies as dollar weakens",
      "url": "https://example.com/btc",
      "time_published": "20260705T070000",
      "summary": "Crypto markets extend gains.",
      "ticker_sentiment": [
        {"ticker": "CRYPTO:BTC", "relevance_score": "0.95", "ticker_sentiment_score": "0.55", "ticker_sentiment_label": "Bullish"},
        {"ticker": "FOREX:USD", "relevance_score": "0.70", "ticker_sentiment_score": "-0.20", "ticker_sentiment_label": "Somewhat-Bearish"}
      ]
    },
    {
      "title": "Article with no tags and a malformed one",
      "url": "https://example.com/none",
      "time_published": "20260705T060000",
      "summary": "Broad macro commentary.",
      "ticker_sentiment": [
        {"ticker": "BROK", "relevance_score": "not-a-number", "ticker_sentiment_score": "0.1", "ticker_sentiment_label": "Neutral"}
      ]
    }
  ]
}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/ops/discovery/test_feeds.py`:

```python
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ops.discovery.feeds import Article, FeedError, fetch_articles, parse_global_news

_FIXTURE = Path(__file__).parent / "fixtures" / "av_news_sentiment.json"


def _raw() -> str:
    return _FIXTURE.read_text()


def test_parse_fixture_yields_typed_articles():
    articles = parse_global_news(_raw())
    assert len(articles) == 4
    a = articles[0]
    assert isinstance(a, Article)
    assert a.title == "Acme Corp wins surprise FDA approval"
    assert a.url == "https://example.com/acme-fda"
    assert a.published_at == datetime(2026, 7, 5, 8, 15, tzinfo=timezone.utc)
    assert a.tickers[0].symbol == "ACME"
    assert a.tickers[0].relevance == Decimal("0.91")
    assert a.tickers[0].sentiment == Decimal("0.62")


def test_parse_skips_malformed_tags_not_whole_article():
    articles = parse_global_news(_raw())
    no_tags = [a for a in articles if a.title.startswith("Article with no tags")]
    assert len(no_tags) == 1
    assert no_tags[0].tickers == ()   # malformed relevance_score dropped


def test_parse_accepts_already_decoded_dict():
    articles = parse_global_news(json.loads(_raw()))
    assert len(articles) == 4


def test_parse_raises_feed_error_on_non_json():
    with pytest.raises(FeedError):
        parse_global_news("<html>rate limited</html>")


def test_parse_raises_feed_error_when_feed_key_missing():
    with pytest.raises(FeedError):
        parse_global_news(json.dumps({"Information": "call frequency"}))


def test_parse_skips_items_missing_required_fields():
    payload = {"feed": [
        {"url": "https://x", "time_published": "20260705T060000"},  # no title
        {"title": "ok", "url": "https://y", "time_published": "garbage"},
        {"title": "good", "url": "https://z", "time_published": "20260705T060000",
         "summary": "s", "ticker_sentiment": []},
    ]}
    articles = parse_global_news(payload)
    assert [a.title for a in articles] == ["good"]


def test_fetch_articles_requests_last_24h_and_parses():
    seen = {}

    def fake_fetcher(curr_date, look_back_days, limit):
        seen.update(curr_date=curr_date, look_back_days=look_back_days, limit=limit)
        return _raw()

    articles = fetch_articles(date(2026, 7, 5), fetcher=fake_fetcher)
    assert seen == {"curr_date": "2026-07-05", "look_back_days": 1, "limit": 200}
    assert len(articles) == 4
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/discovery/test_feeds.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.discovery.feeds'`.

- [ ] **Step 4: Implement `ops/discovery/feeds.py`**

```python
"""Alpha Vantage NEWS_SENTIMENT → typed Article records.

The ONLY module that knows AV's JSON shape (spec § Components). Everything
downstream consumes Article/TickerTag. Research-only rule: never imports
ops.broker.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from tradingagents.dataflows.alpha_vantage_news import get_global_news


class FeedError(Exception):
    """News payload missing or garbled — the sweep must abort, not guess."""


@dataclass(frozen=True)
class TickerTag:
    symbol: str
    relevance: Decimal
    sentiment: Decimal


@dataclass(frozen=True)
class Article:
    title: str
    summary: str
    url: str
    published_at: datetime
    tickers: tuple[TickerTag, ...]


def _parse_time(raw: str) -> datetime:
    # AV time_published format: "20260705T081500" (UTC).
    return datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)


def _parse_tags(raw_tags: object) -> tuple[TickerTag, ...]:
    tags: list[TickerTag] = []
    if not isinstance(raw_tags, list):
        return ()
    for t in raw_tags:
        if not isinstance(t, dict):
            continue
        try:
            tags.append(TickerTag(
                symbol=str(t["ticker"]).upper(),
                relevance=Decimal(str(t["relevance_score"])),
                sentiment=Decimal(str(t["ticker_sentiment_score"])),
            ))
        except (KeyError, TypeError, InvalidOperation):
            continue  # one malformed tag never kills the article
    return tuple(tags)


def parse_global_news(raw: str | dict) -> list[Article]:
    """Normalize an AV NEWS_SENTIMENT payload. Raises FeedError when the
    payload as a whole is unusable; silently skips individual bad items."""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FeedError(f"Alpha Vantage payload is not JSON: {exc}") from exc
    else:
        data = raw
    if not isinstance(data, dict) or not isinstance(data.get("feed"), list):
        raise FeedError("Alpha Vantage payload has no 'feed' list")
    articles: list[Article] = []
    for item in data["feed"]:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        url = item.get("url")
        ts = item.get("time_published")
        if not title or not url or not ts:
            continue
        try:
            published = _parse_time(str(ts))
        except ValueError:
            continue
        articles.append(Article(
            title=str(title),
            summary=str(item.get("summary") or ""),
            url=str(url),
            published_at=published,
            tickers=_parse_tags(item.get("ticker_sentiment")),
        ))
    return articles


def fetch_articles(
    asof_date: date,
    *,
    limit: int = 200,
    fetcher: Callable[..., dict | str] = get_global_news,
) -> list[Article]:
    """Fetch + parse the last 24h of global financial news."""
    raw = fetcher(asof_date.isoformat(), look_back_days=1, limit=limit)
    return parse_global_news(raw)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/discovery/test_feeds.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ops/discovery/feeds.py tests/ops/discovery/test_feeds.py tests/ops/discovery/fixtures/av_news_sentiment.json
git commit -m "feat(ops/discovery): feeds — normalize AV news payloads into typed Articles"
```

---

### Task 6: `mentions.py` — aggregate ticker mentions

**Files:**
- Create: `ops/discovery/mentions.py`
- Test: `tests/ops/discovery/test_mentions.py`

**Interfaces:**
- Consumes: `Article`, `TickerTag` from `ops.discovery.feeds` (Task 5).
- Produces (Tasks 7, 8, 10 rely on these):
  - `TickerMention(symbol: str, mention_count: int, max_relevance: Decimal, relevance_weight: Decimal, avg_sentiment: Decimal, articles: tuple[Article, ...])` (frozen). `relevance_weight` = sum of qualifying per-article relevance scores — the ranking key the screen's yfinance cap uses ("top ~30 mentions by relevance weight").
  - `aggregate_mentions(articles: list[Article], *, relevance_threshold: Decimal) -> list[TickerMention]` — pure function, sorted by `relevance_weight` desc then symbol asc; drops `CRYPTO:`/`FOREX:` prefixed and broad-ETF symbols.

- [ ] **Step 1: Write the failing tests**

Create `tests/ops/discovery/test_mentions.py`:

```python
from datetime import datetime, timezone
from decimal import Decimal

from ops.discovery.feeds import Article, TickerTag
from ops.discovery.mentions import aggregate_mentions


def _article(title, tags):
    return Article(
        title=title, summary=f"{title} summary", url=f"https://x/{title}",
        published_at=datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc),
        tickers=tuple(TickerTag(symbol=s, relevance=Decimal(r), sentiment=Decimal(v))
                      for s, r, v in tags),
    )


def test_aggregates_counts_relevance_and_sentiment():
    articles = [
        _article("a1", [("ACME", "0.9", "0.6")]),
        _article("a2", [("ACME", "0.7", "0.2"), ("RIVL", "0.8", "-0.3")]),
    ]
    mentions = aggregate_mentions(articles, relevance_threshold=Decimal("0.5"))
    by_sym = {m.symbol: m for m in mentions}
    acme = by_sym["ACME"]
    assert acme.mention_count == 2
    assert acme.max_relevance == Decimal("0.9")
    assert acme.relevance_weight == Decimal("1.6")
    assert acme.avg_sentiment == Decimal("0.4")   # (0.6 + 0.2) / 2
    assert [a.title for a in acme.articles] == ["a1", "a2"]
    assert by_sym["RIVL"].mention_count == 1


def test_relevance_threshold_drops_weak_tags():
    articles = [_article("a1", [("ACME", "0.9", "0.6"), ("WEAK", "0.2", "0.9")])]
    mentions = aggregate_mentions(articles, relevance_threshold=Decimal("0.5"))
    assert [m.symbol for m in mentions] == ["ACME"]


def test_drops_crypto_forex_and_broad_etf_tags():
    articles = [_article("a1", [
        ("CRYPTO:BTC", "0.95", "0.5"), ("FOREX:USD", "0.9", "0.1"),
        ("SPY", "0.9", "0.0"), ("QQQ", "0.9", "0.0"), ("ACME", "0.8", "0.3"),
    ])]
    mentions = aggregate_mentions(articles, relevance_threshold=Decimal("0.5"))
    assert [m.symbol for m in mentions] == ["ACME"]


def test_sorted_by_relevance_weight_desc_then_symbol():
    articles = [
        _article("a1", [("BBB", "0.9", "0.0"), ("AAA", "0.6", "0.0")]),
        _article("a2", [("BBB", "0.9", "0.0"), ("CCC", "0.6", "0.0"), ("ZZZ", "0.6", "0.0")]),
    ]
    mentions = aggregate_mentions(articles, relevance_threshold=Decimal("0.5"))
    assert [m.symbol for m in mentions] == ["BBB", "AAA", "CCC", "ZZZ"]


def test_empty_input_returns_empty_list():
    assert aggregate_mentions([], relevance_threshold=Decimal("0.5")) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/discovery/test_mentions.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.discovery.mentions'`.

- [ ] **Step 3: Implement `ops/discovery/mentions.py`**

```python
"""Pure aggregation: articles → one TickerMention per symbol.

Drops crypto/forex tags (AV prefixes them) and broad/index ETFs (AV does not
mark ETFs, so a small static set covers the high-volume index products;
leveraged ETFs are caught downstream by the ops deny list in screen.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ops.discovery.feeds import Article

_EXCLUDED_PREFIXES = ("CRYPTO:", "FOREX:")

_BROAD_ETFS = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "IVV", "GLD", "SLV",
    "TLT", "HYG", "LQD", "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP",
})


@dataclass(frozen=True)
class TickerMention:
    symbol: str
    mention_count: int
    max_relevance: Decimal
    relevance_weight: Decimal  # sum of qualifying relevances — screen's ranking key
    avg_sentiment: Decimal
    articles: tuple[Article, ...]


def _tradeable(symbol: str) -> bool:
    if any(symbol.startswith(p) for p in _EXCLUDED_PREFIXES):
        return False
    return symbol not in _BROAD_ETFS


def aggregate_mentions(
    articles: list[Article], *, relevance_threshold: Decimal,
) -> list[TickerMention]:
    counts: dict[str, int] = {}
    max_rel: dict[str, Decimal] = {}
    weight: dict[str, Decimal] = {}
    sent_sum: dict[str, Decimal] = {}
    contributing: dict[str, list[Article]] = {}
    for article in articles:
        for tag in article.tickers:
            if tag.relevance < relevance_threshold or not _tradeable(tag.symbol):
                continue
            sym = tag.symbol
            counts[sym] = counts.get(sym, 0) + 1
            max_rel[sym] = max(max_rel.get(sym, Decimal("0")), tag.relevance)
            weight[sym] = weight.get(sym, Decimal("0")) + tag.relevance
            sent_sum[sym] = sent_sum.get(sym, Decimal("0")) + tag.sentiment
            contributing.setdefault(sym, []).append(article)
    mentions = [
        TickerMention(
            symbol=sym,
            mention_count=counts[sym],
            max_relevance=max_rel[sym],
            relevance_weight=weight[sym],
            avg_sentiment=sent_sum[sym] / Decimal(counts[sym]),
            articles=tuple(contributing[sym]),
        )
        for sym in counts
    ]
    mentions.sort(key=lambda m: (-m.relevance_weight, m.symbol))
    return mentions
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/discovery/test_mentions.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/discovery/mentions.py tests/ops/discovery/test_mentions.py
git commit -m "feat(ops/discovery): mentions — aggregate ticker mentions from articles"
```

---

### Task 7: `screen.py` — hard filters

**Files:**
- Create: `ops/discovery/screen.py`
- Test: `tests/ops/discovery/test_screen.py`

**Interfaces:**
- Consumes: `TickerMention` (Task 6); `apply_deny_list`, `fetch_price_and_adv_from_yfinance` from `ops.universe.filters`; `DiscoveryConfig` (Task 4).
- Produces (Tasks 8, 10 rely on these):
  - `ScreenedCandidate(mention: TickerMention, last_price: Decimal, avg_dollar_volume_20d: Decimal)` (frozen) with a `symbol` property returning `mention.symbol`.
  - `screen_mentions(mentions: list[TickerMention], *, config: DiscoveryConfig, deny_list: frozenset[str], held_symbols_provider: Callable[[], set[str]], cooldown_symbols: set[str], metrics_fetcher: Callable[[str], tuple[Decimal, Decimal] | None] = fetch_price_and_adv_from_yfinance) -> list[ScreenedCandidate]`.
  - Module constants `MIN_PRICE = Decimal("5")`, `MIN_ADV = Decimal("50000000")` (mirror `ops/universe/__init__.py` v1 constants).

- [ ] **Step 1: Write the failing tests**

Create `tests/ops/discovery/test_screen.py`:

```python
from datetime import datetime, timezone
from decimal import Decimal

from ops.discovery.config import DiscoveryConfig
from ops.discovery.feeds import Article
from ops.discovery.mentions import TickerMention
from ops.discovery.screen import screen_mentions


def _mention(sym, weight="1.0"):
    art = Article(title=f"{sym} news", summary="s", url="https://x",
                  published_at=datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc),
                  tickers=())
    return TickerMention(symbol=sym, mention_count=1,
                         max_relevance=Decimal(weight),
                         relevance_weight=Decimal(weight),
                         avg_sentiment=Decimal("0"), articles=(art,))


_GOOD = (Decimal("50"), Decimal("60000000"))


def _metrics(table):
    def fetch(sym):
        return table.get(sym, _GOOD)
    return fetch


def test_deny_held_and_cooldown_are_filtered_before_metrics():
    calls = []

    def fetch(sym):
        calls.append(sym)
        return _GOOD

    out = screen_mentions(
        [_mention("SPOT"), _mention("HELD"), _mention("COOL"), _mention("OKAY")],
        config=DiscoveryConfig(),
        deny_list=frozenset({"SPOT"}),
        held_symbols_provider=lambda: {"HELD"},
        cooldown_symbols={"COOL"},
        metrics_fetcher=fetch,
    )
    assert [c.symbol for c in out] == ["OKAY"]
    assert calls == ["OKAY"]   # cheap filters run BEFORE yfinance lookups


def test_liquidity_floors_and_fetch_failure_drop_symbols():
    table = {
        "PENNY": (Decimal("2"), Decimal("60000000")),
        "ILLIQ": (Decimal("50"), Decimal("10000000")),
        "NODATA": None,
    }
    out = screen_mentions(
        [_mention("PENNY"), _mention("ILLIQ"), _mention("NODATA"), _mention("OKAY")],
        config=DiscoveryConfig(),
        deny_list=frozenset(), held_symbols_provider=set, cooldown_symbols=set(),
        metrics_fetcher=_metrics(table),
    )
    assert [c.symbol for c in out] == ["OKAY"]
    okay = out[0]
    assert okay.last_price == Decimal("50")
    assert okay.avg_dollar_volume_20d == Decimal("60000000")


def test_yfinance_lookups_capped_at_max_screen_symbols_by_weight():
    calls = []

    def fetch(sym):
        calls.append(sym)
        return _GOOD

    mentions = [_mention("LOW", "0.5"), _mention("HI", "2.0"), _mention("MID", "1.0")]
    out = screen_mentions(
        mentions,
        config=DiscoveryConfig(max_screen_symbols=2),
        deny_list=frozenset(), held_symbols_provider=set, cooldown_symbols=set(),
        metrics_fetcher=fetch,
    )
    assert calls == ["HI", "MID"]          # top-2 by relevance_weight, re-sorted
    assert [c.symbol for c in out] == ["HI", "MID"]


def test_empty_mentions_short_circuits():
    out = screen_mentions(
        [], config=DiscoveryConfig(), deny_list=frozenset(),
        held_symbols_provider=set, cooldown_symbols=set(),
        metrics_fetcher=lambda s: _GOOD,
    )
    assert out == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/discovery/test_screen.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.discovery.screen'`.

- [ ] **Step 3: Implement `ops/discovery/screen.py`**

```python
"""Hard filters between raw mentions and the LLM triage.

Ordering is cost-ordered: free set-membership filters first (deny list, held
positions, cooldown), then the yfinance liquidity lookup capped at
config.max_screen_symbols by relevance weight.

Held positions arrive via an injected held_symbols_provider callable wired
at composition time (ops/main.py, ops/cli.py) — this package NEVER imports
ops.broker (spec § Structural rules).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from ops.discovery.config import DiscoveryConfig
from ops.discovery.mentions import TickerMention
from ops.universe.filters import apply_deny_list, fetch_price_and_adv_from_yfinance

# Mirrors the v1 hard-coded floors in ops/universe/__init__.py (_MIN_PRICE /
# _MIN_ADV). Promote both to shared config only when tuning demands it.
MIN_PRICE = Decimal("5")
MIN_ADV = Decimal("50000000")


@dataclass(frozen=True)
class ScreenedCandidate:
    mention: TickerMention
    last_price: Decimal
    avg_dollar_volume_20d: Decimal

    @property
    def symbol(self) -> str:
        return self.mention.symbol


def screen_mentions(
    mentions: list[TickerMention],
    *,
    config: DiscoveryConfig,
    deny_list: frozenset[str],
    held_symbols_provider: Callable[[], set[str]],
    cooldown_symbols: set[str],
    metrics_fetcher: Callable[[str], tuple[Decimal, Decimal] | None] = fetch_price_and_adv_from_yfinance,
) -> list[ScreenedCandidate]:
    if not mentions:
        return []
    allowed = set(apply_deny_list([m.symbol for m in mentions], deny_list))
    held = held_symbols_provider()
    kept = [
        m for m in mentions
        if m.symbol in allowed and m.symbol not in held
        and m.symbol not in cooldown_symbols
    ]
    # Defensive re-sort: callers pass mentions pre-sorted, but the yfinance
    # budget cap must be deterministic regardless.
    kept.sort(key=lambda m: (-m.relevance_weight, m.symbol))
    kept = kept[: config.max_screen_symbols]
    out: list[ScreenedCandidate] = []
    for m in kept:
        metrics = metrics_fetcher(m.symbol)
        if metrics is None:
            continue  # one failed lookup drops that symbol, never the sweep
        price, adv = metrics
        if price < MIN_PRICE or adv < MIN_ADV:
            continue
        out.append(ScreenedCandidate(
            mention=m, last_price=price, avg_dollar_volume_20d=adv,
        ))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/discovery/test_screen.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/discovery/screen.py tests/ops/discovery/test_screen.py
git commit -m "feat(ops/discovery): screen — deny/held/cooldown/liquidity hard filters"
```

---

### Task 8: `triage.py` — batched cheap-model materiality scoring

**Files:**
- Create: `ops/discovery/triage.py`
- Test: `tests/ops/discovery/test_triage.py`

**Interfaces:**
- Consumes: `ScreenedCandidate` (Task 7). Production LLM via `tradingagents.llm_clients.create_llm_client(provider, model)` + `DEFAULT_CONFIG["llm_provider"]` / `["quick_think_llm"]` from `tradingagents.default_config` — imported lazily inside the factory only.
- Produces (Task 10, 12 rely on these):
  - `TriageResult(symbol: str, score: int, catalyst_type: str, rationale: str)` (frozen)
  - `TriageError(Exception)`
  - `CATALYST_TYPES = ("earnings", "m&a", "regulatory", "product", "macro", "other")`
  - `run_triage(candidates: list[ScreenedCandidate], *, invoke: Callable[[str], str]) -> list[TriageResult]` — ONE batched call; on malformed response retries the call exactly once, then raises `TriageError`. Results sorted by score desc then symbol asc; unknown catalyst types coerce to `"other"`; results for symbols not in the batch are dropped.
  - `make_default_triage_invoke(model: str | None = None) -> Callable[[str], str]` — lazy client construction (no SDK import / no credentials needed until first call).
  - `build_triage_prompt(candidates) -> str` (exposed for tests).

- [ ] **Step 1: Write the failing tests**

Create `tests/ops/discovery/test_triage.py`:

```python
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ops.discovery.feeds import Article
from ops.discovery.mentions import TickerMention
from ops.discovery.screen import ScreenedCandidate
from ops.discovery.triage import TriageError, build_triage_prompt, run_triage


def _candidate(sym, title="headline"):
    art = Article(title=f"{sym} {title}", summary=f"{sym} summary", url="https://x",
                  published_at=datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc),
                  tickers=())
    mention = TickerMention(symbol=sym, mention_count=1,
                            max_relevance=Decimal("0.9"),
                            relevance_weight=Decimal("0.9"),
                            avg_sentiment=Decimal("0.2"), articles=(art,))
    return ScreenedCandidate(mention=mention, last_price=Decimal("50"),
                             avg_dollar_volume_20d=Decimal("60000000"))


def _canned(items):
    return json.dumps(items)


def test_one_batched_call_parses_and_sorts_by_score():
    calls = []

    def invoke(prompt):
        calls.append(prompt)
        return _canned([
            {"symbol": "AAA", "score": 4, "catalyst_type": "product", "rationale": "meh"},
            {"symbol": "BBB", "score": 9, "catalyst_type": "regulatory", "rationale": "big"},
        ])

    results = run_triage([_candidate("AAA"), _candidate("BBB")], invoke=invoke)
    assert len(calls) == 1
    assert [(r.symbol, r.score) for r in results] == [("BBB", 9), ("AAA", 4)]
    assert results[0].catalyst_type == "regulatory"


def test_prompt_contains_each_symbol_and_headlines():
    prompt = build_triage_prompt([_candidate("ACME", "wins FDA approval")])
    assert "ACME" in prompt
    assert "wins FDA approval" in prompt
    assert "JSON" in prompt


def test_code_fenced_json_is_accepted():
    def invoke(prompt):
        return "```json\n" + _canned(
            [{"symbol": "AAA", "score": 7, "catalyst_type": "macro", "rationale": "r"}]
        ) + "\n```"

    results = run_triage([_candidate("AAA")], invoke=invoke)
    assert results[0].score == 7


def test_unknown_catalyst_coerces_to_other_and_foreign_symbols_dropped():
    def invoke(prompt):
        return _canned([
            {"symbol": "AAA", "score": 7, "catalyst_type": "alien-invasion", "rationale": "r"},
            {"symbol": "NOTB", "score": 9, "catalyst_type": "macro", "rationale": "r"},
        ])

    results = run_triage([_candidate("AAA")], invoke=invoke)
    assert [(r.symbol, r.catalyst_type) for r in results] == [("AAA", "other")]


def test_malformed_json_retries_once_then_raises():
    calls = []

    def invoke(prompt):
        calls.append(prompt)
        return "I cannot answer in JSON, sorry."

    with pytest.raises(TriageError):
        run_triage([_candidate("AAA")], invoke=invoke)
    assert len(calls) == 2   # exactly one retry


def test_retry_succeeds_on_second_attempt():
    responses = iter(["garbage", _canned(
        [{"symbol": "AAA", "score": 6, "catalyst_type": "earnings", "rationale": "r"}]
    )])

    results = run_triage([_candidate("AAA")], invoke=lambda p: next(responses))
    assert results[0].score == 6


@pytest.mark.parametrize("bad", [
    [{"symbol": "AAA", "score": 15, "catalyst_type": "macro", "rationale": "r"}],
    [{"symbol": "AAA", "score": -1, "catalyst_type": "macro", "rationale": "r"}],
    [{"symbol": "AAA", "catalyst_type": "macro", "rationale": "r"}],
    {"symbol": "AAA", "score": 5},
])
def test_out_of_range_or_missing_fields_are_malformed(bad):
    with pytest.raises(TriageError):
        run_triage([_candidate("AAA")], invoke=lambda p: json.dumps(bad))


def test_empty_candidates_returns_empty_without_llm_call():
    def invoke(prompt):
        raise AssertionError("must not be called")

    assert run_triage([], invoke=invoke) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/discovery/test_triage.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.discovery.triage'`.

- [ ] **Step 3: Implement `ops/discovery/triage.py`**

```python
"""One batched cheap-model triage call: materiality 0-10 + catalyst type +
one-line rationale per candidate.

The LLM is an injected `invoke: Callable[[str], str]` so unit tests use
canned strings. `make_default_triage_invoke` builds the production callable
lazily (no SDK import, no credential requirement until first use).

Error policy (spec § Error handling): malformed JSON → one retry, then
TriageError — the sweep aborts rather than guessing on un-triaged names.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

from ops.discovery.screen import ScreenedCandidate

CATALYST_TYPES = ("earnings", "m&a", "regulatory", "product", "macro", "other")

_MAX_ARTICLES_PER_SYMBOL = 5
_MAX_SUMMARY_CHARS = 300


class TriageError(Exception):
    """Triage response unusable after one retry — abort the sweep."""


@dataclass(frozen=True)
class TriageResult:
    symbol: str
    score: int
    catalyst_type: str
    rationale: str


def build_triage_prompt(candidates: list[ScreenedCandidate]) -> str:
    lines = [
        "You are a pre-market equity triage analyst. For each candidate below,",
        "score how MATERIAL the last 24 hours of news is to the company's",
        "investment case, on a 0-10 scale (0 = noise, 10 = thesis-changing).",
        "By the time news is public the fast money has traded it — score the",
        "second-order, under-appreciated stories highly, not the loudest ones.",
        "",
        "Respond with ONLY a JSON array (no prose, no markdown fences), one",
        "object per candidate, each with exactly these keys:",
        '  {"symbol": <string>, "score": <integer 0-10>,',
        f'   "catalyst_type": <one of {list(CATALYST_TYPES)}>,',
        '   "rationale": <one line, max 30 words>}',
        "",
        "Candidates:",
    ]
    for c in candidates:
        m = c.mention
        lines.append(
            f"## {c.symbol} — {m.mention_count} mention(s), "
            f"max relevance {m.max_relevance}, avg sentiment {m.avg_sentiment}"
        )
        for a in m.articles[:_MAX_ARTICLES_PER_SYMBOL]:
            lines.append(f"- {a.title}: {a.summary[:_MAX_SUMMARY_CHARS]}")
    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    return t


def _parse_response(text: str, expected: set[str]) -> list[TriageResult]:
    """Raises ValueError/TypeError/KeyError/json.JSONDecodeError on any
    malformed shape — callers treat all of those as 'retry once'."""
    data = json.loads(_strip_fences(text))
    if not isinstance(data, list):
        raise ValueError("triage response is not a JSON array")
    results: list[TriageResult] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("triage item is not an object")
        symbol = str(item["symbol"]).upper()
        score = item["score"]
        if not isinstance(score, int) or isinstance(score, bool) or not (0 <= score <= 10):
            raise ValueError(f"triage score out of range for {symbol}: {score!r}")
        if symbol not in expected:
            continue  # hallucinated symbol — drop it, keep the batch
        catalyst = str(item.get("catalyst_type", "other")).lower()
        if catalyst not in CATALYST_TYPES:
            catalyst = "other"
        results.append(TriageResult(
            symbol=symbol, score=score, catalyst_type=catalyst,
            rationale=str(item.get("rationale", "")),
        ))
    results.sort(key=lambda r: (-r.score, r.symbol))
    return results


def run_triage(
    candidates: list[ScreenedCandidate], *, invoke: Callable[[str], str],
) -> list[TriageResult]:
    if not candidates:
        return []
    prompt = build_triage_prompt(candidates)
    expected = {c.symbol for c in candidates}
    last_exc: Exception | None = None
    for _attempt in range(2):  # initial call + exactly one retry
        try:
            return _parse_response(invoke(prompt), expected)
        except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
            last_exc = exc
    raise TriageError(f"triage response unusable after retry: {last_exc}") from last_exc


def make_default_triage_invoke(model: str | None = None) -> Callable[[str], str]:
    """Production invoke: provider from the upstream pipeline config, model
    from DiscoveryConfig.triage_model or the upstream quick_think_llm
    (spec: provider choice stays in one place). Construction is deferred to
    first call so composing the job never needs credentials."""
    state: dict = {}

    def invoke(prompt: str) -> str:
        if "llm" not in state:
            from tradingagents.default_config import DEFAULT_CONFIG
            from tradingagents.llm_clients import create_llm_client
            client = create_llm_client(
                provider=DEFAULT_CONFIG["llm_provider"],
                model=model or DEFAULT_CONFIG["quick_think_llm"],
            )
            state["llm"] = client.get_llm()
        resp = state["llm"].invoke(prompt)
        content = getattr(resp, "content", resp)
        if isinstance(content, list):  # anthropic-style content blocks
            content = "".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in content
            )
        return str(content)

    return invoke
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/discovery/test_triage.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/discovery/triage.py tests/ops/discovery/test_triage.py
git commit -m "feat(ops/discovery): triage — batched cheap-model materiality scoring"
```

---

### Task 9: `reports.py` — markdown report rendering + archive

**Files:**
- Create: `ops/discovery/reports.py`
- Test: `tests/ops/discovery/test_reports.py`

**Interfaces:**
- Consumes: `TriageResult` (Task 8), `TickerMention` (Task 6), `StructuredPipelineResult` (Task 3).
- Produces (Task 10 relies on these):
  - `render_report(*, asof_date: date, triage: TriageResult, mention: TickerMention, result: StructuredPipelineResult) -> str`
  - `write_report(reports_dir: str, asof_date: date, symbol: str, content: str) -> str` — writes `<reports_dir>/YYYY-MM-DD/SYMBOL.md` (mkdir -p), returns the absolute path.

- [ ] **Step 1: Write the failing tests**

Create `tests/ops/discovery/test_reports.py`:

```python
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from ops.discovery.feeds import Article
from ops.discovery.mentions import TickerMention
from ops.discovery.reports import render_report, write_report
from ops.discovery.triage import TriageResult
from ops.pipeline_adapter import PipelineDecision, StructuredPipelineResult


def _fixtures():
    art = Article(title="ACME wins FDA approval", summary="early approval",
                  url="https://example.com/acme",
                  published_at=datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc),
                  tickers=())
    mention = TickerMention(symbol="ACME", mention_count=2,
                            max_relevance=Decimal("0.91"),
                            relevance_weight=Decimal("1.5"),
                            avg_sentiment=Decimal("0.4"), articles=(art,))
    triage = TriageResult(symbol="ACME", score=8, catalyst_type="regulatory",
                          rationale="Approval two quarters early")
    result = StructuredPipelineResult(
        symbol="ACME", date=date(2026, 7, 5), decision=PipelineDecision.BUY,
        rating="Buy", executive_summary="Enter below 95.",
        investment_thesis="Approval odds mispriced.",
        price_target=Decimal("120.5"), time_horizon="3-6 months",
        entry_price=Decimal("93"), stop_loss=Decimal("85"),
        pm_markdown="**Rating**: Buy\n\n**Executive Summary**: Enter below 95.",
        raw={},
    )
    return mention, triage, result


def test_render_includes_triage_context_and_pm_report():
    mention, triage, result = _fixtures()
    md = render_report(asof_date=date(2026, 7, 5), triage=triage,
                       mention=mention, result=result)
    assert "# ACME — discovery report (2026-07-05)" in md
    assert "8/10" in md and "regulatory" in md
    assert "Approval two quarters early" in md
    assert "ACME wins FDA approval" in md and "https://example.com/acme" in md
    assert "**Rating**: Buy" in md          # full PM markdown embedded


def test_render_survives_all_none_structured_fields():
    mention, triage, result = _fixtures()
    from dataclasses import replace
    bare = replace(result, rating=None, executive_summary=None,
                   investment_thesis=None, price_target=None,
                   time_horizon=None, entry_price=None, stop_loss=None,
                   pm_markdown="freeform prose only")
    md = render_report(asof_date=date(2026, 7, 5), triage=triage,
                       mention=mention, result=bare)
    assert "freeform prose only" in md


def test_write_report_creates_dated_tree_and_returns_abs_path(tmp_path):
    path = write_report(str(tmp_path / "reports"), date(2026, 7, 5), "ACME", "# hi")
    p = Path(path)
    assert p.is_absolute()
    assert p == tmp_path / "reports" / "2026-07-05" / "ACME.md"
    assert p.read_text() == "# hi"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/discovery/test_reports.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.discovery.reports'`.

- [ ] **Step 3: Implement `ops/discovery/reports.py`**

```python
"""Render + archive per-symbol discovery reports.

<reports_dir>/YYYY-MM-DD/SYMBOL.md — full PM markdown plus the triage
context that got the name through the funnel."""
from __future__ import annotations

import os
from datetime import date

from ops.discovery.mentions import TickerMention
from ops.discovery.triage import TriageResult
from ops.pipeline_adapter import StructuredPipelineResult


def render_report(
    *, asof_date: date, triage: TriageResult, mention: TickerMention,
    result: StructuredPipelineResult,
) -> str:
    lines = [
        f"# {triage.symbol} — discovery report ({asof_date.isoformat()})",
        "",
        "## Triage",
        f"- Materiality: {triage.score}/10 ({triage.catalyst_type})",
        f"- Rationale: {triage.rationale}",
        f"- Mentions: {mention.mention_count} article(s), "
        f"max relevance {mention.max_relevance}, avg sentiment {mention.avg_sentiment}",
        "",
        "### Source articles",
    ]
    for a in mention.articles:
        lines.append(f"- [{a.title}]({a.url}) — {a.published_at.isoformat()}")
    lines += [
        "",
        "## Portfolio Manager report",
        "",
        result.pm_markdown or "(no PM output captured)",
    ]
    return "\n".join(lines)


def write_report(reports_dir: str, asof_date: date, symbol: str, content: str) -> str:
    day_dir = os.path.join(
        os.path.abspath(os.path.expanduser(reports_dir)), asof_date.isoformat(),
    )
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, f"{symbol}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/discovery/test_reports.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/discovery/reports.py tests/ops/discovery/test_reports.py
git commit -m "feat(ops/discovery): reports — render and archive per-symbol markdown"
```

---

### Task 10: `job.py` — `DiscoveryJob` orchestration

**Files:**
- Create: `ops/discovery/job.py`
- Test: `tests/ops/discovery/test_job.py`

**Interfaces:**
- Consumes: everything from Tasks 2–9 exactly as specified in their Produces blocks; `ops.journal.Journal.record_event`; `ops.trading_time.TRADING_TZ`; `ops.config.OpsConfig` (for `deny_list`).
- Produces (Task 12 relies on this):
  - `DiscoveryJob(*, discovery_config: DiscoveryConfig, ops_config: OpsConfig, journal: Journal, theses: ThesesStore, pipeline, held_symbols_provider: Callable[[], set[str]], triage_invoke: Callable[[str], str], article_fetcher: Callable[[date], list[Article]] | None = None, metrics_fetcher=None, is_trading_day: Callable[[date], bool] | None = None, now: Callable[[], datetime] | None = None)`
  - `DiscoveryJob.run_once() -> int` — returns the number of theses written; NEVER raises (scheduler-safe); all failures land as `discovery_sweep_error` events.
  - Journal event payloads:
    - `discovery_sweep_started`: `{"date": iso}`
    - `discovery_triage` (one per triaged symbol): `{"date", "symbol", "score", "catalyst_type", "rationale"}`
    - `discovery_thesis` (one per persisted thesis): `{"date", "symbol", "rating", "price_target", "time_horizon", "thesis_id", "report_path"}`
    - `discovery_sweep_error`: `{"date", "stage": "news_fetch"|"triage"|"pipeline"|"sweep", "error": "<Type>: <msg>", ("symbol" when stage=="pipeline")}`
    - `discovery_digest`: `{"date", "count", "items": [{"symbol", "rating", "price_target", "thesis_line", "report_path"}, ...]}`

- [ ] **Step 1: Write the failing tests**

Create `tests/ops/discovery/test_job.py`:

```python
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ops.config import OpsConfig
from ops.discovery.config import DiscoveryConfig
from ops.discovery.feeds import Article, TickerTag
from ops.discovery.job import DiscoveryJob
from ops.journal import Journal
from ops.pipeline_adapter import (
    PipelineDecision, StructuredPipelineResult, StubPipelineAdapter,
)
from ops.theses import ThesesStore

_NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)  # 08:00 ET Monday
_ASOF = date(2026, 7, 6)


def _article(sym, relevance="0.9", title=None):
    return Article(
        title=title or f"{sym} big news", summary=f"{sym} summary",
        url=f"https://x/{sym}", published_at=_NOW - timedelta(hours=2),
        tickers=(TickerTag(symbol=sym, relevance=Decimal(relevance),
                           sentiment=Decimal("0.3")),),
    )


def _structured(sym, rating="Buy"):
    return StructuredPipelineResult(
        symbol=sym, date=_ASOF, decision=PipelineDecision.BUY, rating=rating,
        executive_summary=f"{sym} exec summary.", investment_thesis="thesis",
        price_target=Decimal("120"), time_horizon="3-6 months",
        entry_price=Decimal("93"), stop_loss=Decimal("85"),
        pm_markdown=f"**Rating**: {rating}", raw={},
    )


def _triage_response(scored):
    return json.dumps([
        {"symbol": s, "score": sc, "catalyst_type": "regulatory", "rationale": "r"}
        for s, sc in scored
    ])


def _job(tmp_path, *, articles, triage_json, structured=None,
         dcfg=None, held=frozenset(), now=_NOW, metrics=None):
    journal = Journal(str(tmp_path / "j.sqlite"))
    theses = ThesesStore(str(tmp_path / "j.sqlite"))
    dcfg = dcfg or DiscoveryConfig(reports_dir=str(tmp_path / "reports"))
    job = DiscoveryJob(
        discovery_config=dcfg,
        ops_config=OpsConfig(journal_path=str(tmp_path / "j.sqlite")),
        journal=journal,
        theses=theses,
        pipeline=StubPipelineAdapter(structured=structured or {}),
        held_symbols_provider=lambda: set(held),
        triage_invoke=(triage_json if callable(triage_json)
                       else lambda prompt: triage_json),
        article_fetcher=(articles if callable(articles)
                         else lambda asof: list(articles)),
        metrics_fetcher=metrics or (lambda s: (Decimal("50"), Decimal("60000000"))),
        is_trading_day=lambda d: d.weekday() < 5,
        now=lambda: now,
    )
    return job, journal, theses


def _kinds(journal):
    return [e["kind"] for e in journal.read_events()]


def test_happy_path_event_sequence_thesis_report_and_digest(tmp_path):
    job, journal, theses = _job(
        tmp_path,
        articles=[_article("ACME")],
        triage_json=_triage_response([("ACME", 8)]),
        structured={"ACME": _structured("ACME")},
    )
    n = job.run_once()
    assert n == 1
    assert _kinds(journal) == [
        "discovery_sweep_started", "discovery_triage",
        "discovery_thesis", "discovery_digest",
    ]
    rows = theses.read_theses(symbol="ACME")
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "discovery"
    assert row.rating == "Buy"
    assert row.price_target == Decimal("120")
    assert row.last_price == Decimal("50")
    assert row.avg_dollar_volume == Decimal("60000000")
    assert row.triage_score == 8
    assert row.trigger_conditions == {"entry_price": "93", "stop_loss": "85"}
    assert row.source_articles[0]["url"] == "https://x/ACME"
    assert Path(row.report_path).read_text().startswith("# ACME — discovery report")
    digest = journal.read_events()[-1]["payload"]
    assert digest["count"] == 1
    assert digest["items"][0]["symbol"] == "ACME"
    assert digest["items"][0]["report_path"] == row.report_path


def test_budget_max_full_runs_enforced_in_score_order(tmp_path):
    job, journal, theses = _job(
        tmp_path,
        articles=[_article("AAA"), _article("BBB"), _article("CCC")],
        triage_json=_triage_response([("AAA", 7), ("BBB", 9), ("CCC", 8)]),
        structured={s: _structured(s) for s in ("AAA", "BBB", "CCC")},
        dcfg=DiscoveryConfig(max_full_runs=2, reports_dir=str(tmp_path / "r")),
    )
    assert job.run_once() == 2
    analyzed = {r.symbol for r in theses.read_theses()}
    assert analyzed == {"BBB", "CCC"}   # top-2 by triage score


def test_triage_min_score_floor_holds_even_with_budget_left(tmp_path):
    job, journal, theses = _job(
        tmp_path,
        articles=[_article("AAA"), _article("BBB")],
        triage_json=_triage_response([("AAA", 5), ("BBB", 9)]),
        structured={s: _structured(s) for s in ("AAA", "BBB")},
    )
    assert job.run_once() == 1          # AAA scored 5 < default floor 6
    assert {r.symbol for r in theses.read_theses()} == {"BBB"}


def test_cooldown_symbol_never_reaches_triage(tmp_path):
    journal_path = str(tmp_path / "j.sqlite")
    pre = ThesesStore(journal_path)
    pre.insert_thesis(
        symbol="ACME", source="discovery", rating="Buy", price_target=None,
        time_horizon=None, thesis_summary=None, triage_score=None,
        triage_rationale=None, source_articles=[], last_price=None,
        avg_dollar_volume=None, report_path=None, trigger_conditions=None,
        created_at=_NOW - timedelta(days=3),
    )
    pre.close()

    def failing_triage(prompt):
        raise AssertionError("triage must not run — nothing survived the screen")

    job, journal, theses = _job(
        tmp_path, articles=[_article("ACME")], triage_json=failing_triage,
    )
    assert job.run_once() == 0
    assert _kinds(journal) == ["discovery_sweep_started"]


def test_held_symbol_is_screened_out(tmp_path):
    job, journal, _ = _job(
        tmp_path, articles=[_article("ACME")],
        triage_json=lambda p: (_ for _ in ()).throw(AssertionError("no survivors")),
        held={"ACME"},
    )
    assert job.run_once() == 0


def test_news_fetch_failure_aborts_with_stage_tagged_event(tmp_path):
    from ops.discovery.feeds import FeedError

    def fetcher(asof):
        raise FeedError("no feed list")

    job, journal, _ = _job(tmp_path, articles=fetcher, triage_json="[]")
    assert job.run_once() == 0
    events = journal.read_events()
    assert _kinds(journal) == ["discovery_sweep_started", "discovery_sweep_error"]
    assert events[-1]["payload"]["stage"] == "news_fetch"


def test_malformed_triage_aborts_with_stage_tagged_event(tmp_path):
    job, journal, _ = _job(
        tmp_path, articles=[_article("ACME")], triage_json="not json at all",
    )
    assert job.run_once() == 0
    events = journal.read_events()
    assert events[-1]["kind"] == "discovery_sweep_error"
    assert events[-1]["payload"]["stage"] == "triage"


def test_one_pipeline_failure_continues_with_remaining_survivors(tmp_path):
    class HalfBrokenPipeline:
        def propagate_structured(self, symbol, asof_date):
            if symbol == "BBB":
                raise RuntimeError("provider 500")
            return _structured(symbol)

    journal = Journal(str(tmp_path / "j.sqlite"))
    theses = ThesesStore(str(tmp_path / "j.sqlite"))
    job = DiscoveryJob(
        discovery_config=DiscoveryConfig(reports_dir=str(tmp_path / "r")),
        ops_config=OpsConfig(journal_path=str(tmp_path / "j.sqlite")),
        journal=journal, theses=theses, pipeline=HalfBrokenPipeline(),
        held_symbols_provider=set,
        triage_invoke=lambda p: _triage_response([("AAA", 8), ("BBB", 9)]),
        article_fetcher=lambda asof: [_article("AAA"), _article("BBB")],
        metrics_fetcher=lambda s: (Decimal("50"), Decimal("60000000")),
        is_trading_day=lambda d: True, now=lambda: _NOW,
    )
    assert job.run_once() == 1
    kinds = _kinds(journal)
    assert kinds.count("discovery_sweep_error") == 1
    err = [e for e in journal.read_events() if e["kind"] == "discovery_sweep_error"][0]
    assert err["payload"]["stage"] == "pipeline"
    assert err["payload"]["symbol"] == "BBB"
    assert {r.symbol for r in theses.read_theses()} == {"AAA"}
    assert kinds[-1] == "discovery_digest"   # digest still goes out


def test_zero_theses_means_no_digest(tmp_path):
    job, journal, _ = _job(
        tmp_path, articles=[_article("ACME")],
        triage_json=_triage_response([("ACME", 3)]),   # below floor
    )
    assert job.run_once() == 0
    assert "discovery_digest" not in _kinds(journal)


def test_non_trading_day_is_a_silent_noop(tmp_path):
    saturday = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    job, journal, _ = _job(
        tmp_path, articles=[_article("ACME")], triage_json="[]", now=saturday,
    )
    assert job.run_once() == 0
    assert journal.read_events() == []


def test_run_once_never_raises_on_unexpected_error(tmp_path):
    def exploding_fetcher(asof):
        raise RuntimeError("boom")

    job, journal, _ = _job(tmp_path, articles=exploding_fetcher, triage_json="[]")
    job.run_once()   # must NOT raise
    events = journal.read_events()
    assert events[-1]["kind"] == "discovery_sweep_error"
    assert "boom" in events[-1]["payload"]["error"]


def test_discovery_package_never_imports_broker():
    """Structural rule (spec): research-only is enforced by structure.
    Source-scan every module in ops/discovery/ plus ops/theses.py."""
    import ops.discovery
    pkg_dir = Path(ops.discovery.__file__).parent
    offenders = []
    for py in list(pkg_dir.glob("*.py")) + [pkg_dir.parent / "theses.py"]:
        text = py.read_text()
        if "ops.broker" in text or "from ops import" in text:
            offenders.append(py.name)
    assert offenders == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/discovery/test_job.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.discovery.job'`.

- [ ] **Step 3: Implement `ops/discovery/job.py`**

```python
"""DiscoveryJob — wires the funnel stages, enforces budgets, journals every
stage outcome, and emits the digest event.

Research-only by structure: this module composes feeds → mentions → screen →
triage → pipeline → theses/reports and NEVER imports ops.broker. Held
positions arrive through the injected held_symbols_provider.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable

from ops.config import OpsConfig
from ops.discovery.config import DiscoveryConfig
from ops.discovery.feeds import Article, fetch_articles
from ops.discovery.mentions import aggregate_mentions
from ops.discovery.reports import render_report, write_report
from ops.discovery.screen import screen_mentions
from ops.discovery.triage import TriageError, run_triage
from ops.journal import Journal
from ops.theses import ThesesStore
from ops.trading_time import TRADING_TZ

_THESIS_LINE_MAX = 160


def _one_line(text: str | None, limit: int = _THESIS_LINE_MAX) -> str:
    if not text:
        return ""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _err(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


class DiscoveryJob:
    def __init__(
        self,
        *,
        discovery_config: DiscoveryConfig,
        ops_config: OpsConfig,
        journal: Journal,
        theses: ThesesStore,
        pipeline,  # needs .propagate_structured(symbol, asof_date)
        held_symbols_provider: Callable[[], set[str]],
        triage_invoke: Callable[[str], str],
        article_fetcher: Callable[[date], list[Article]] | None = None,
        metrics_fetcher: Callable | None = None,
        is_trading_day: Callable[[date], bool] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._cfg = discovery_config
        self._ops_cfg = ops_config
        self._journal = journal
        self._theses = theses
        self._pipeline = pipeline
        self._held_symbols_provider = held_symbols_provider
        self._triage_invoke = triage_invoke
        self._article_fetcher = article_fetcher or fetch_articles
        self._metrics_fetcher = metrics_fetcher
        self._is_trading_day = is_trading_day
        self._now = now or (lambda: datetime.now(timezone.utc))

    def run_once(self) -> int:
        """One sweep. Scheduler-safe: never raises; a discovery failure must
        never affect the trading service sharing the scheduler."""
        now = self._now()
        asof = now.astimezone(TRADING_TZ).date()
        if self._is_trading_day is not None and not self._is_trading_day(asof):
            return 0
        try:
            return self._run(now, asof)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
            self._journal.record_event(
                "discovery_sweep_error",
                {"date": asof.isoformat(), "stage": "sweep", "error": _err(exc)},
            )
            return 0

    def _run(self, now: datetime, asof: date) -> int:
        self._journal.record_event(
            "discovery_sweep_started", {"date": asof.isoformat()},
        )

        try:
            articles = self._article_fetcher(asof)
        except Exception as exc:  # fetch fails or returns garbage → abort
            self._journal.record_event(
                "discovery_sweep_error",
                {"date": asof.isoformat(), "stage": "news_fetch", "error": _err(exc)},
            )
            return 0

        mentions = aggregate_mentions(
            articles, relevance_threshold=self._cfg.relevance_threshold,
        )
        cooldown = self._theses.recent_symbols(
            since=now - timedelta(days=self._cfg.cooldown_days),
        )
        screen_kwargs = dict(
            config=self._cfg,
            deny_list=self._ops_cfg.deny_list,
            held_symbols_provider=self._held_symbols_provider,
            cooldown_symbols=cooldown,
        )
        if self._metrics_fetcher is not None:
            screen_kwargs["metrics_fetcher"] = self._metrics_fetcher
        screened = screen_mentions(mentions, **screen_kwargs)
        if not screened:
            return 0

        try:
            triage = run_triage(screened, invoke=self._triage_invoke)
        except TriageError as exc:
            self._journal.record_event(
                "discovery_sweep_error",
                {"date": asof.isoformat(), "stage": "triage", "error": _err(exc)},
            )
            return 0
        for t in triage:
            self._journal.record_event("discovery_triage", {
                "date": asof.isoformat(), "symbol": t.symbol, "score": t.score,
                "catalyst_type": t.catalyst_type, "rationale": t.rationale,
            })

        by_symbol = {c.symbol: c for c in screened}
        survivors = [
            t for t in triage if t.score >= self._cfg.triage_min_score
        ][: self._cfg.max_full_runs]

        digest_items: list[dict] = []
        for t in survivors:
            cand = by_symbol[t.symbol]
            try:
                result = self._pipeline.propagate_structured(t.symbol, asof)
            except Exception as exc:  # one failed run never kills the sweep
                self._journal.record_event(
                    "discovery_sweep_error",
                    {"date": asof.isoformat(), "stage": "pipeline",
                     "symbol": t.symbol, "error": _err(exc)},
                )
                continue
            report_md = render_report(
                asof_date=asof, triage=t, mention=cand.mention, result=result,
            )
            report_path = write_report(
                self._cfg.reports_dir, asof, t.symbol, report_md,
            )
            trigger = None
            if result.entry_price is not None or result.stop_loss is not None:
                trigger = {
                    "entry_price": str(result.entry_price)
                    if result.entry_price is not None else None,
                    "stop_loss": str(result.stop_loss)
                    if result.stop_loss is not None else None,
                }
            thesis_id = self._theses.insert_thesis(
                symbol=t.symbol,
                source="discovery",
                rating=result.rating,
                price_target=result.price_target,
                time_horizon=result.time_horizon,
                thesis_summary=result.executive_summary or result.investment_thesis,
                triage_score=t.score,
                triage_rationale=t.rationale,
                source_articles=[
                    {"title": a.title, "url": a.url,
                     "published_at": a.published_at.isoformat()}
                    for a in cand.mention.articles
                ],
                last_price=cand.last_price,
                avg_dollar_volume=cand.avg_dollar_volume_20d,
                report_path=report_path,
                trigger_conditions=trigger,
                created_at=now,
            )
            self._journal.record_event("discovery_thesis", {
                "date": asof.isoformat(), "symbol": t.symbol,
                "rating": result.rating,
                "price_target": str(result.price_target)
                if result.price_target is not None else None,
                "time_horizon": result.time_horizon,
                "thesis_id": thesis_id, "report_path": report_path,
            })
            digest_items.append({
                "symbol": t.symbol,
                "rating": result.rating,
                "price_target": str(result.price_target)
                if result.price_target is not None else None,
                "thesis_line": _one_line(result.executive_summary),
                "report_path": report_path,
            })

        if digest_items:  # zero theses → quiet day, no digest
            self._journal.record_event("discovery_digest", {
                "date": asof.isoformat(), "count": len(digest_items),
                "items": digest_items,
            })
        return len(digest_items)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/discovery/test_job.py tests/ops/discovery -q`
Expected: PASS (all discovery tests including the no-broker-import structural test).

- [ ] **Step 5: Commit**

```bash
git add ops/discovery/job.py tests/ops/discovery/test_job.py
git commit -m "feat(ops/discovery): DiscoveryJob — funnel orchestration, budgets, journal events"
```

---

### Task 11: Notify policy — render `discovery_digest`

**Files:**
- Modify: `ops/notify/policy.py`
- Test: `tests/ops/notify/test_policy.py` (append)

**Interfaces:**
- Consumes: `discovery_digest` / `discovery_sweep_error` payload shapes from Task 10.
- Produces: `POLICY["discovery_digest"] = PolicyEntry(("push", "email"), "normal", None)`; `POLICY["discovery_sweep_error"] = _EMAIL_THROTTLED`; a `render` branch for `discovery_digest`. The existing SPOT scrub in `render` applies automatically (it wraps title+body).

- [ ] **Step 1: Write the failing tests**

Append to `tests/ops/notify/test_policy.py`:

```python
def test_discovery_digest_policy_and_rendering():
    from ops.notify.policy import POLICY, render
    entry = POLICY["discovery_digest"]
    assert entry.channels == ("push", "email")
    assert entry.cooldown_seconds is None
    payload = {"date": "2026-07-06", "count": 2, "items": [
        {"symbol": "ACME", "rating": "Buy", "price_target": "120",
         "thesis_line": "Approval mispriced.", "report_path": "/r/ACME.md"},
        {"symbol": "BETA", "rating": "Hold", "price_target": None,
         "thesis_line": "Wait for guidance.", "report_path": "/r/BETA.md"},
    ]}
    msg = render("discovery_digest", payload)
    assert "2" in msg.title and "2026-07-06" in msg.title
    assert "ACME: Buy" in msg.body and "PT 120" in msg.body
    assert "Approval mispriced." in msg.body
    assert "BETA: Hold" in msg.body
    assert "/r/ACME.md" in msg.body


def test_discovery_digest_scrubs_spot():
    from ops.notify.policy import render
    payload = {"date": "2026-07-06", "count": 1, "items": [
        {"symbol": "ACME", "rating": "Buy", "price_target": "1",
         "thesis_line": "Better than Spot on every axis.", "report_path": "/r"},
    ]}
    msg = render("discovery_digest", payload)
    assert "Spot" not in msg.body and "[redacted]" in msg.body


def test_discovery_sweep_error_is_throttled_email():
    from ops.notify.policy import POLICY
    entry = POLICY["discovery_sweep_error"]
    assert entry.channels == ("email",)
    assert entry.cooldown_seconds == 600
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/notify/test_policy.py -q`
Expected: FAIL with `KeyError: 'discovery_digest'`.

- [ ] **Step 3: Implement in `ops/notify/policy.py`**

Add to the `POLICY` dict (after the `daily_summary` entry):

```python
    "discovery_digest": PolicyEntry(("push", "email"), "normal", None),
    "discovery_sweep_error": _EMAIL_THROTTLED,
```

Add a branch to `render` (before the final `else`):

```python
    elif kind == "discovery_digest":
        items = payload.get("items", [])
        title = f"Discovery digest ({payload.get('date', '')}): {len(items)} thesis(es)"
        lines = []
        for it in items:
            pt = f" PT {it.get('price_target')}" if it.get("price_target") else ""
            lines.append(
                f"{it.get('symbol')}: {it.get('rating')}{pt} — "
                f"{it.get('thesis_line')} [{it.get('report_path')}]"
            )
        body = "\n".join(lines) or str(payload)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/notify -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/notify/policy.py tests/ops/notify/test_policy.py
git commit -m "feat(ops/notify): render discovery_digest; throttle discovery_sweep_error"
```

---

### Task 12: Service + CLI wiring

**Files:**
- Modify: `ops/main.py`
- Modify: `ops/cli.py`
- Test: `tests/ops/test_main.py` (append), `tests/ops/test_cli_discover.py` (create)

**Interfaces:**
- Consumes: `DiscoveryJob` (Task 10), `load_discovery_config` (Task 4), `make_default_triage_invoke` (Task 8), `ThesesStore` (Task 2), `TradingAgentsPipelineAdapter.propagate_structured` (Task 3), existing `_build_broker`, `MarketCalendar`.
- Produces:
  - `ops.main._build_discovery_job(config: OpsConfig, journal: Journal, broker, calendar, dcfg: DiscoveryConfig) -> DiscoveryJob` — unconditional composition (used by CLI too).
  - `ops.main._build_discovery(config, journal, broker, calendar) -> DiscoveryJob | None` — returns None unless `load_discovery_config().enabled`.
  - `_start_full_scheduler(orchestrator, guardian, dispatcher, journal, broker, calendar, discovery)` — new `calendar` and `discovery` parameters; `discovery_sweep` cron job at 08:00 ET mon-fri when `discovery is not None`. Passing `calendar` also fixes the pre-existing free-variable NameError in the `daily_summary` lambda (see "Spec ambiguities" #8).
  - `ops discover --once` CLI command.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ops/test_main.py`:

```python
def test_build_discovery_none_when_disabled(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from ops.main import _build_discovery
    monkeypatch.delenv("OPS_DISCOVERY_ENABLED", raising=False)
    cfg = OpsConfig(journal_path=str(tmp_path / "j.sqlite"))
    j = Journal(cfg.journal_path)
    assert _build_discovery(cfg, j, MagicMock(), MagicMock()) is None


def test_build_discovery_job_when_enabled(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from ops.discovery.job import DiscoveryJob
    from ops.main import _build_discovery
    monkeypatch.setenv("OPS_DISCOVERY_ENABLED", "1")
    cfg = OpsConfig(journal_path=str(tmp_path / "j.sqlite"))
    j = Journal(cfg.journal_path)
    job = _build_discovery(cfg, j, MagicMock(), MagicMock())
    assert isinstance(job, DiscoveryJob)
    assert callable(job.run_once)


def test_full_scheduler_registers_discovery_job_only_when_present(tmp_path):
    from unittest.mock import MagicMock
    from ops.main import _build_dispatcher, _start_full_scheduler
    cfg = OpsConfig(journal_path=str(tmp_path / "j.sqlite"))
    j = Journal(cfg.journal_path)
    dispatcher = _build_dispatcher(j)
    broker = MagicMock()
    calendar = MagicMock()

    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), dispatcher, j, broker, calendar, None,
    )
    try:
        assert sched.get_job("discovery_sweep") is None
    finally:
        sched.shutdown(wait=False)

    discovery = MagicMock()
    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), dispatcher, j, broker, calendar, discovery,
    )
    try:
        job = sched.get_job("discovery_sweep")
        assert job is not None
        # 08:00 ET pre-market, weekdays
        assert "hour='8'" in str(job.trigger)
    finally:
        sched.shutdown(wait=False)
```

Create `tests/ops/test_cli_discover.py`:

```python
from unittest.mock import MagicMock

from click.testing import CliRunner

from ops.cli import cli


def test_discover_requires_once_flag():
    result = CliRunner().invoke(cli, ["discover"])
    assert result.exit_code != 0
    assert "--once" in result.output


def test_discover_once_runs_job_and_reports_count(monkeypatch, tmp_path):
    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    job = MagicMock()
    job.run_once.return_value = 2
    monkeypatch.setattr("ops.main._build_broker", lambda cfg, j: MagicMock())
    monkeypatch.setattr(
        "ops.main._build_discovery_job",
        lambda cfg, j, broker, calendar, dcfg: job,
    )
    result = CliRunner().invoke(cli, ["discover", "--once"])
    assert result.exit_code == 0, result.output
    job.run_once.assert_called_once()
    assert "2 thesis(es)" in result.output


def test_discover_once_runs_even_when_discovery_disabled(monkeypatch, tmp_path):
    """`enabled` gates SCHEDULING; an explicit CLI run always executes."""
    monkeypatch.delenv("OPS_DISCOVERY_ENABLED", raising=False)
    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    job = MagicMock()
    job.run_once.return_value = 0
    monkeypatch.setattr("ops.main._build_broker", lambda cfg, j: MagicMock())
    monkeypatch.setattr(
        "ops.main._build_discovery_job",
        lambda cfg, j, broker, calendar, dcfg: job,
    )
    result = CliRunner().invoke(cli, ["discover", "--once"])
    assert result.exit_code == 0, result.output
    job.run_once.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/test_main.py tests/ops/test_cli_discover.py -q`
Expected: FAIL — `ImportError: cannot import name '_build_discovery'` and `Error: No such command 'discover'`.

- [ ] **Step 3: Implement wiring in `ops/main.py`**

Add after `_wire`:

```python
def _build_discovery_job(
    config: OpsConfig, journal: Journal, broker, calendar, dcfg,
):
    """Compose a DiscoveryJob. This is the composition seam where broker
    state crosses into discovery — as a plain callable, never an import:
    ops/discovery must stay broker-free (spec § Structural rules)."""
    from ops.discovery.job import DiscoveryJob
    from ops.discovery.triage import make_default_triage_invoke
    from ops.pipeline_adapter import TradingAgentsPipelineAdapter
    from ops.theses import ThesesStore

    return DiscoveryJob(
        discovery_config=dcfg,
        ops_config=config,
        journal=journal,
        theses=ThesesStore(config.journal_path),
        pipeline=TradingAgentsPipelineAdapter(),
        held_symbols_provider=lambda: {p.symbol for p in broker.get_positions()},
        triage_invoke=make_default_triage_invoke(dcfg.triage_model),
        is_trading_day=calendar.is_trading_day,
    )


def _build_discovery(config: OpsConfig, journal: Journal, broker, calendar):
    """Discovery job for the always-on service, or None when disabled
    (enabled=False means the job is simply never scheduled)."""
    from ops.discovery.config import load_discovery_config

    dcfg = load_discovery_config()
    if not dcfg.enabled:
        return None
    return _build_discovery_job(config, journal, broker, calendar, dcfg)
```

Change `_startup` to also build discovery:

```python
def _startup(config: OpsConfig, journal: Journal):
    broker = _build_broker(config, journal)
    orchestrator, guardian, calendar = _wire(broker, journal, config)
    discovery = _build_discovery(config, journal, broker, calendar)
    result = reconcile(journal=journal, broker=broker, broker_mode=config.broker_mode)
    return broker, orchestrator, guardian, calendar, discovery, result
```

Change `_start_full_scheduler`'s signature and add the job (this also fixes the pre-existing `calendar` free-variable NameError in the `daily_summary` lambda):

```python
def _start_full_scheduler(
    orchestrator: Orchestrator, guardian: PositionGuardian,
    dispatcher: NotifyDispatcher, journal: Journal, broker,
    calendar, discovery,
) -> BackgroundScheduler:
```

…keep the four existing `add_job` calls unchanged (the `daily_summary` lambda now closes over the `calendar` parameter), and before `sched.start()` add:

```python
    if discovery is not None:
        sched.add_job(
            discovery.run_once,
            CronTrigger(hour=8, minute=0, day_of_week="mon-fri"),
            id="discovery_sweep", max_instances=1, misfire_grace_time=3600,
        )
```

Update `run()`'s two call sites:

```python
            broker, orchestrator, guardian, calendar, discovery, result = _startup(config, journal)
```

and

```python
        sched = _start_full_scheduler(
            orchestrator, guardian, dispatcher, journal, broker, calendar, discovery,
        )
```

(The reconciliation-halted path keeps `_start_guardian_only` unchanged — a halted service must not spend money on discovery either.)

Add the `discover` command to `ops/cli.py` (after `notify_once`):

```python
@cli.command()
@click.option("--once", is_flag=True,
              help="Run one discovery sweep now and exit.")
def discover(once: bool) -> None:
    """Run the news-driven discovery sweep (research-only, no orders).

    Runs regardless of OPS_DISCOVERY_ENABLED — the flag only gates the
    scheduled job inside `ops run`."""
    if not once:
        raise click.UsageError("only --once is supported (use `ops run` for the schedule)")
    from ops.discovery.config import load_discovery_config
    from ops.main import _build_broker, _build_discovery_job
    from ops.scheduler.market_calendar import MarketCalendar

    cfg = load_config()
    dcfg = load_discovery_config()
    journal = Journal(cfg.journal_path)
    try:
        broker = _build_broker(cfg, journal)
        job = _build_discovery_job(cfg, journal, broker, MarketCalendar(), dcfg)
        n = job.run_once()
        click.echo(f"discovery sweep complete: {n} thesis(es) written")
    finally:
        journal.close()
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest tests/ops -q`
Expected: PASS (new wiring tests plus the whole existing suite).

- [ ] **Step 5: Commit**

```bash
git add ops/main.py ops/cli.py tests/ops/test_main.py tests/ops/test_cli_discover.py
git commit -m "feat(ops): schedule discovery sweep at 08:00 ET; add ops discover --once"
```

---

### Task 13: Opt-in live Alpha Vantage smoke test

**Files:**
- Create: `tests/ops/discovery/test_feeds_live.py`

**Interfaces:**
- Consumes: `fetch_articles` (Task 5). Gated on `OPS_DISCOVERY_LIVE_TESTS=1` (mirrors `OPS_RH_LIVE_TESTS` in `tests/ops/broker/test_mcp_live.py`); also requires `ALPHA_VANTAGE_API_KEY` in the environment.

- [ ] **Step 1: Write the test**

Create `tests/ops/discovery/test_feeds_live.py`:

```python
"""Opt-in live READ-ONLY smoke test against the real Alpha Vantage feed.

Gated on OPS_DISCOVERY_LIVE_TESTS=1 (skipped by default — the normal suite
must never touch the network). Requires ALPHA_VANTAGE_API_KEY. Mirrors the
opt-in gate pattern in tests/ops/broker/test_mcp_live.py.
"""
import os
from datetime import date
from decimal import Decimal

import pytest

from ops.discovery.feeds import Article, fetch_articles

pytestmark = pytest.mark.skipif(
    os.environ.get("OPS_DISCOVERY_LIVE_TESTS") != "1",
    reason="live Alpha Vantage tests are opt-in; set OPS_DISCOVERY_LIVE_TESTS=1 to run",
)


def test_fetch_articles_returns_typed_articles_from_real_feed():
    if not os.environ.get("ALPHA_VANTAGE_API_KEY"):
        pytest.skip("ALPHA_VANTAGE_API_KEY not set")
    articles = fetch_articles(date.today(), limit=50)
    assert isinstance(articles, list)
    assert len(articles) > 0, "expected at least one global-news article in 24h"
    for a in articles[:10]:
        assert isinstance(a, Article)
        assert a.title and a.url
        assert a.published_at.tzinfo is not None
        for tag in a.tickers:
            assert isinstance(tag.relevance, Decimal)
            assert isinstance(tag.sentiment, Decimal)
```

- [ ] **Step 2: Verify it is skipped by default**

Run: `.venv/bin/pytest tests/ops/discovery/test_feeds_live.py -q`
Expected: `1 skipped` (no network touched).

- [ ] **Step 3: Run the entire ops suite one final time**

Run: `.venv/bin/pytest tests/ops -q`
Expected: PASS, with the live test skipped.

- [ ] **Step 4: Commit**

```bash
git add tests/ops/discovery/test_feeds_live.py
git commit -m "test(ops/discovery): opt-in live Alpha Vantage feed smoke test"
```

---

## Spec coverage checklist (self-review)

| Spec requirement | Task |
|---|---|
| `Candidate.origin` generalization (promotion-seam prep) | 1 |
| `theses` table + `ThesesStore` in `ops/theses.py`, exact schema | 2 |
| `trigger_conditions` + `review_after` populated, consumed by nothing | 2, 10 |
| Pipeline adapter exposes structured PM output; trading collapse untouched | 3 |
| `DiscoveryConfig` fields/defaults, env-overridable | 4 |
| `feeds.py` — only module knowing AV JSON; malformed payload cases | 5 |
| `mentions.py` — pure aggregation; drops crypto/forex/ETF | 6 |
| `screen.py` — deny/held/cooldown/liquidity; yfinance cap; injected `held_symbols_provider` | 7 |
| `triage.py` — one batched call, strict JSON, retry-once-then-abort | 8 |
| `reports.py` — `<reports_dir>/YYYY-MM-DD/SYMBOL.md` | 9 |
| `job.py` — budgets, serial pipeline runs, journal event vocabulary, digest, error policy, quiet days silent | 10 |
| Digest rendered by notify dispatcher; SPOT scrub applies | 11 |
| Scheduled pre-market ~8:00 ET on existing APScheduler; `ops discover --once`; failure never affects trading service | 10, 12 |
| No `ops.broker` imports in discovery — structural test | 10 |
| Cost controls: 1 news call, ≤`max_screen_symbols` lookups, 1 triage call, ≤`max_full_runs` runs, cooldown | 7, 8, 10 |
| Opt-in live AV smoke test | 13 |
| Deferred items prepared (trigger_conditions, review_after, promotion seam, outcome-scoring fields) | 1, 2, 10 |
