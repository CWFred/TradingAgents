# Exit Engine Part 2 (Sells, Provenance, Cooldown) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every position gets a defined lifecycle: rank-decay/trend-break exits for momentum positions, max-hold for earnings positions, a stop-out re-entry cooldown, all evaluated in the daily tick before buys — so freed slots refill the same morning.

**Architecture:** A pure module `ops/exits/engine.py` evaluates held positions against today's leaderboard and bar history; the orchestrator runs it after the halt gates and before universe building, sells via `GuardedBroker.close_position`, and journals typed events. Provenance comes from a `position_opened` journal event written at buy time. Spec: `docs/superpowers/specs/2026-07-05-momentum-sleeve-and-risk-envelope-design.md` Component 6. **Prerequisite: Part 1 (`2026-07-05-momentum-sleeve-part-1-entries.md`) is fully landed.**

**Tech Stack:** Python 3.12+, dataclasses, Decimal, sqlite journal, pytest.

## Global Constraints

- `PositionGuardian` is NOT touched — it keeps stop enforcement; the exit engine is a separate daily concern.
- Never sell on missing data: unfetchable bars → skip + journal, never a close.
- Decay/max-hold exits respect the drawdown halts (the existing halt check precedes the exit step; do not reorder).
- Exit defaults (spec Component 6): `momentum_exit_rank=25`, `earnings_max_hold_days=40`, `stopout_reentry_cooldown_days=10`; validation requires `momentum_exit_rank > daily_analysis_budget`.
- Trading-day counting is weekday-based (Mon–Fri, holidays ignored) — the same approximation `ops/universe/earnings.py:69-82` already uses; do not introduce a holiday calendar.
- Run tests with `python -m pytest <path> -v` from the repo root (venv active). Commit after every task. Branch: `feat/momentum-sleeve`.

---

### Task 1: Trading-day helpers in `ops/trading_time.py`

**Files:**
- Modify: `ops/trading_time.py`
- Test: `tests/ops/test_trading_time.py`

**Interfaces:**
- Produces: `trading_days_back(asof: date, n: int) -> date` (n trading days strictly before `asof`) and `trading_days_between(start: date, end: date) -> int` (weekdays strictly after `start` up to and including `end`; 0 when `end <= start`). Tasks 6–7 rely on these exact names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ops/test_trading_time.py`:

```python
from datetime import date

from ops.trading_time import trading_days_back, trading_days_between


def test_trading_days_between_same_week():
    # Mon 2026-07-06 -> Fri 2026-07-10: Tue, Wed, Thu, Fri = 4
    assert trading_days_between(date(2026, 7, 6), date(2026, 7, 10)) == 4


def test_trading_days_between_spans_weekend():
    # Fri 2026-07-10 -> Mon 2026-07-13: just Monday = 1
    assert trading_days_between(date(2026, 7, 10), date(2026, 7, 13)) == 1


def test_trading_days_between_zero_for_same_or_reversed():
    assert trading_days_between(date(2026, 7, 6), date(2026, 7, 6)) == 0
    assert trading_days_between(date(2026, 7, 10), date(2026, 7, 6)) == 0


def test_trading_days_back_skips_weekend():
    # 2 trading days before Mon 2026-07-13 = Thu 2026-07-09
    assert trading_days_back(date(2026, 7, 13), 2) == date(2026, 7, 9)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/test_trading_time.py -v`
Expected: FAIL — `ImportError: cannot import name 'trading_days_back'`

- [ ] **Step 3: Implement**

Append to `ops/trading_time.py`:

```python
def _is_trading_day(d: date) -> bool:
    # Mon=0..Fri=4. Holidays are not handled — same approximation as
    # ops/universe/earnings.py; a holiday merely shortens a window by a day.
    return d.weekday() < 5


def trading_days_back(asof: date, n: int) -> date:
    """The date n trading days strictly before `asof`."""
    d = asof
    counted = 0
    while counted < n:
        d -= timedelta(days=1)
        if _is_trading_day(d):
            counted += 1
    return d


def trading_days_between(start: date, end: date) -> int:
    """Trading days strictly after `start`, up to and including `end`."""
    if end <= start:
        return 0
    d, count = start, 0
    while d < end:
        d += timedelta(days=1)
        if _is_trading_day(d):
            count += 1
    return count
```

Ensure `from datetime import date, timedelta` covers the names used (extend the existing import line if needed).

- [ ] **Step 4: Run tests** — `python -m pytest tests/ops/test_trading_time.py -v` → ALL PASS

- [ ] **Step 5: Commit**

```bash
git add ops/trading_time.py tests/ops/test_trading_time.py
git commit -m "feat(trading-time): trading_days_back / trading_days_between helpers"
```

---

### Task 2: Typed exit events + notify classification

**Files:**
- Modify: `ops/events.py`, `ops/notify/policy.py`
- Test: `tests/ops/test_exit_events.py` (new), `tests/ops/notify/test_policy.py` (existing enforcement)

**Interfaces:**
- Produces kind constants (string values frozen): `KIND_POSITION_OPENED = "position_opened"`, `KIND_EXIT_DECISION = "exit_decision"`, `KIND_EXIT_ORDER_PLACED = "exit_order_placed"`, `KIND_EXIT_SKIPPED_MISSING_DATA = "exit_skipped_missing_data"`, `KIND_EXIT_CHECK_ERROR = "exit_check_error"`; and builders:
  - `position_opened_payload(*, symbol: str, source: str, entry_date: date, client_order_id: str, entry_rank: int | None = None) -> dict` (`entry_rank` key omitted when None; `entry_date` stored ISO)
  - `exit_decision_payload(*, symbol: str, rule: str, evidence: str) -> dict`
  - `exit_order_placed_payload(*, symbol: str, client_order_id: str, rule: str) -> dict`
  - `exit_skipped_missing_data_payload(*, symbol: str, reason: str) -> dict`
  - `exit_check_error_payload(*, error: str) -> dict`

- [ ] **Step 1: Write the failing tests**

Create `tests/ops/test_exit_events.py`:

```python
from datetime import date

from ops import events


def test_position_opened_payload_shape_and_optional_rank():
    p = events.position_opened_payload(
        symbol="NVDA", source="MOMENTUM", entry_date=date(2026, 7, 2),
        client_order_id="pem-x", entry_rank=3,
    )
    assert p == {"symbol": "NVDA", "source": "MOMENTUM",
                 "entry_date": "2026-07-02", "client_order_id": "pem-x",
                 "entry_rank": 3}
    p2 = events.position_opened_payload(
        symbol="MSFT", source="EARNINGS", entry_date=date(2026, 7, 2),
        client_order_id="pem-y",
    )
    assert "entry_rank" not in p2


def test_exit_event_builders_registered():
    for kind in (events.KIND_POSITION_OPENED, events.KIND_EXIT_DECISION,
                 events.KIND_EXIT_ORDER_PLACED,
                 events.KIND_EXIT_SKIPPED_MISSING_DATA,
                 events.KIND_EXIT_CHECK_ERROR):
        assert kind in events.BUILDERS


def test_exit_decision_payload_shape():
    p = events.exit_decision_payload(symbol="NVDA", rule="rank_decay",
                                     evidence="rank 31 > 25")
    assert p == {"symbol": "NVDA", "rule": "rank_decay",
                 "evidence": "rank 31 > 25"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/test_exit_events.py -v`
Expected: FAIL — `AttributeError: module 'ops.events' has no attribute 'position_opened_payload'`

- [ ] **Step 3: Implement**

In `ops/events.py`:

Add kind constants (new section after the live-gate kinds):

```python
# Position lifecycle / exit engine
KIND_POSITION_OPENED = "position_opened"
KIND_EXIT_DECISION = "exit_decision"
KIND_EXIT_ORDER_PLACED = "exit_order_placed"
KIND_EXIT_SKIPPED_MISSING_DATA = "exit_skipped_missing_data"
KIND_EXIT_CHECK_ERROR = "exit_check_error"
```

Add builders (Decimal-free, `date` stored ISO):

```python
def position_opened_payload(
    *, symbol: str, source: str, entry_date: date,
    client_order_id: str, entry_rank: int | None = None,
) -> dict[str, Any]:
    """symbol/source/entry_date are read back by the exit engine's
    provenance loader (json_extract on symbol) — keys frozen."""
    payload: dict[str, Any] = {
        "symbol": symbol,
        "source": source,
        "entry_date": entry_date.isoformat(),
        "client_order_id": client_order_id,
    }
    if entry_rank is not None:
        payload["entry_rank"] = entry_rank
    return payload


def exit_decision_payload(*, symbol: str, rule: str, evidence: str) -> dict[str, Any]:
    return {"symbol": symbol, "rule": rule, "evidence": evidence}


def exit_order_placed_payload(
    *, symbol: str, client_order_id: str, rule: str,
) -> dict[str, Any]:
    return {"symbol": symbol, "client_order_id": client_order_id, "rule": rule}


def exit_skipped_missing_data_payload(*, symbol: str, reason: str) -> dict[str, Any]:
    return {"symbol": symbol, "reason": reason}


def exit_check_error_payload(*, error: str) -> dict[str, Any]:
    """`error` is inherently dynamic — mirrors guardian_check_error."""
    return {"error": error}
```

Add `from datetime import date, datetime` to the imports (the module currently imports only `datetime`). Register all five in `BUILDERS`. Add the four non-error kinds to `AUDIT_ONLY` with this comment: `# Exit lifecycle: the sell itself already notifies via KIND_FILL (push); these are audit breadcrumbs.` Do NOT add `KIND_EXIT_CHECK_ERROR` to `AUDIT_ONLY`.

In `ops/notify/policy.py`, add to `POLICY` next to `KIND_GUARDIAN_CHECK_ERROR`:

```python
    events.KIND_EXIT_CHECK_ERROR: _EMAIL_THROTTLED,
```

- [ ] **Step 4: Run enforcement + new tests**

Run: `python -m pytest tests/ops/test_exit_events.py tests/ops/notify -v`
Expected: ALL PASS. If a renderer test fails for `exit_check_error`, grep `guardian_check_error` in `ops/notify/` and replicate each appearance for `exit_check_error` (it has the identical `{"error": str}` payload).

- [ ] **Step 5: Commit**

```bash
git add ops/events.py ops/notify/policy.py tests/ops/test_exit_events.py
git commit -m "feat(events): typed position_opened + exit-engine event contracts"
```

---

### Task 3: Config — exit rules

**Files:**
- Modify: `ops/config.py`
- Test: `tests/ops/test_config.py`

**Interfaces:**
- Produces fields: `momentum_exit_rank: int = 25`, `earnings_max_hold_days: int = 40`, `stopout_reentry_cooldown_days: int = 10`; env vars `OPS_MOMENTUM_EXIT_RANK`, `OPS_EARNINGS_MAX_HOLD_DAYS`, `OPS_STOPOUT_REENTRY_COOLDOWN_DAYS`; validation: day counts `> 0`, `momentum_exit_rank > daily_analysis_budget`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ops/test_config.py`:

```python
def test_exit_defaults_and_env(monkeypatch):
    cfg = OpsConfig()
    assert cfg.momentum_exit_rank == 25
    assert cfg.earnings_max_hold_days == 40
    assert cfg.stopout_reentry_cooldown_days == 10
    monkeypatch.setenv("OPS_MOMENTUM_EXIT_RANK", "30")
    monkeypatch.setenv("OPS_EARNINGS_MAX_HOLD_DAYS", "50")
    monkeypatch.setenv("OPS_STOPOUT_REENTRY_COOLDOWN_DAYS", "5")
    loaded = load_config()
    assert (loaded.momentum_exit_rank, loaded.earnings_max_hold_days,
            loaded.stopout_reentry_cooldown_days) == (30, 50, 5)


def test_exit_rank_must_exceed_analysis_budget():
    # Exit rank at or below the entry budget removes the hysteresis band
    # and guarantees churn at the boundary.
    with pytest.raises(ValueError):
        OpsConfig(momentum_exit_rank=8)
    with pytest.raises(ValueError):
        OpsConfig(daily_analysis_budget=8, momentum_exit_rank=8)
    OpsConfig(momentum_exit_rank=9)  # boundary: budget+1 is valid


def test_exit_day_counts_must_be_positive():
    with pytest.raises(ValueError):
        OpsConfig(earnings_max_hold_days=0)
    with pytest.raises(ValueError):
        OpsConfig(stopout_reentry_cooldown_days=0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/test_config.py -v`
Expected: new tests FAIL (unknown fields)

- [ ] **Step 3: Implement**

In `ops/config.py`, add fields after `daily_analysis_budget`:

```python
    # Exit engine (spec Component 6). Entry is top-daily_analysis_budget;
    # the gap up to momentum_exit_rank is deliberate hysteresis.
    momentum_exit_rank: int = 25
    earnings_max_hold_days: int = 40
    stopout_reentry_cooldown_days: int = 10
```

In `__post_init__`, after the `daily_analysis_budget` check:

```python
        for fname in ("earnings_max_hold_days", "stopout_reentry_cooldown_days"):
            val = getattr(self, fname)
            if val <= 0:
                raise ValueError(f"{fname} must be > 0, got {val}")
        if self.momentum_exit_rank <= self.daily_analysis_budget:
            raise ValueError(
                "momentum_exit_rank must exceed daily_analysis_budget "
                f"(hysteresis band), got {self.momentum_exit_rank} <= "
                f"{self.daily_analysis_budget}"
            )
```

In `load_config()`, add the three `_env_int` blocks following the existing pattern (`OPS_MOMENTUM_EXIT_RANK`, `OPS_EARNINGS_MAX_HOLD_DAYS`, `OPS_STOPOUT_REENTRY_COOLDOWN_DAYS`).

- [ ] **Step 4: Run tests** — `python -m pytest tests/ops/test_config.py -v` → ALL PASS

- [ ] **Step 5: Commit**

```bash
git add ops/config.py tests/ops/test_config.py
git commit -m "feat(config): exit-rule dials with hysteresis validation"
```

---

### Task 4: Journal queries for provenance + cooldown

**Files:**
- Modify: `ops/journal.py`
- Test: `tests/ops/test_journal.py`

**Interfaces:**
- Produces: `Journal.latest_event_payload_by_symbol(kind: str) -> dict[str, dict]` (latest payload per `payload["symbol"]`) and `Journal.event_symbols_since(kind: str, since: datetime) -> frozenset[str]`. Tasks 6–7 rely on these exact names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ops/test_journal.py` (use the file's existing tmp-path journal fixture pattern):

```python
def test_latest_event_payload_by_symbol(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("position_opened", {"symbol": "NVDA", "source": "MOMENTUM"})
    j.record_event("position_opened", {"symbol": "MSFT", "source": "EARNINGS"})
    j.record_event("position_opened", {"symbol": "NVDA", "source": "EARNINGS"})
    by_sym = j.latest_event_payload_by_symbol("position_opened")
    assert by_sym["NVDA"]["source"] == "EARNINGS"   # latest wins
    assert by_sym["MSFT"]["source"] == "EARNINGS"
    assert j.latest_event_payload_by_symbol("no_such_kind") == {}


def test_event_symbols_since(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("stop_hit", {"symbol": "OLD"})
    cutoff = datetime.now(timezone.utc)
    j.record_event("stop_hit", {"symbol": "NEW"})
    assert j.event_symbols_since("stop_hit", cutoff) == frozenset({"NEW"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/test_journal.py -v`
Expected: FAIL — `AttributeError: 'Journal' object has no attribute 'latest_event_payload_by_symbol'`

- [ ] **Step 3: Implement**

Append to the `Journal` class in `ops/journal.py` (same lock/conn discipline as `read_events`, `ops/journal.py:165`):

```python
    def latest_event_payload_by_symbol(self, kind: str) -> dict[str, dict[str, Any]]:
        """Latest payload per payload['symbol'] for events of `kind`.

        Ascending id scan; later rows overwrite earlier ones, so the value
        is the most recent event for that symbol. Payloads without a symbol
        key are ignored."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM events WHERE kind = ? ORDER BY id",
                (kind,),
            ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for (raw,) in rows:
            payload = json.loads(raw)
            sym = payload.get("symbol")
            if sym:
                out[sym] = payload
        return out

    def event_symbols_since(self, kind: str, since: datetime) -> frozenset[str]:
        """Distinct payload['symbol'] values of `kind` events at >= since."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT json_extract(payload, '$.symbol') FROM events"
                " WHERE kind = ? AND at >= ?",
                (kind, _to_iso(since)),
            ).fetchall()
        return frozenset(r[0] for r in rows if r[0])
```

- [ ] **Step 4: Run tests** — `python -m pytest tests/ops/test_journal.py -v` → ALL PASS

- [ ] **Step 5: Commit**

```bash
git add ops/journal.py tests/ops/test_journal.py
git commit -m "feat(journal): per-symbol latest-payload and symbols-since queries"
```

---

### Task 5: Journal `position_opened` at buy time

**Files:**
- Modify: `ops/scheduler/orchestrator.py` (the proposals loop in `_tick_impl`)
- Test: `tests/ops/scheduler/test_orchestrator.py`

**Interfaces:**
- Consumes: `events.position_opened_payload` (Task 2); `proposal.candidate.source` / `.momentum` (Part 1).
- Produces: one `position_opened` event per successful `place_order`, with `source=candidate.source.value`, `entry_date=asof_date`, `entry_rank=candidate.momentum.rank if candidate.momentum else None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/ops/scheduler/test_orchestrator.py` (using the file's existing fake broker/strategy/journal helpers; the candidate fake must carry `source=CandidateSource.MOMENTUM` and a `MomentumHit` with `rank=3`):

```python
def test_successful_buy_journals_position_opened():
    journal = _make_journal()  # file's existing tmp journal helper
    orch = _make_orchestrator_that_buys(["NVDA"], journal=journal)
    orch.tick()
    evts = [e for e in journal.read_events() if e["kind"] == "position_opened"]
    assert len(evts) == 1
    p = evts[0]["payload"]
    assert p["symbol"] == "NVDA"
    assert p["source"] == "MOMENTUM"
    assert p["entry_rank"] == 3
    assert p["entry_date"] == datetime.now(timezone.utc).date().isoformat()


def test_rejected_order_does_not_journal_position_opened():
    journal = _make_journal()
    orch = _make_orchestrator_whose_broker_rejects(["NVDA"], journal=journal)
    orch.tick()
    assert all(e["kind"] != "position_opened" for e in journal.read_events())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/scheduler/test_orchestrator.py -v`
Expected: first new test FAILS (no `position_opened` event)

- [ ] **Step 3: Implement**

In `_tick_impl`'s proposals loop, journal AFTER a successful `place_order` (order placement must stay first — the journal event is bookkeeping, not a gate):

```python
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

- [ ] **Step 4: Run tests** — `python -m pytest tests/ops/scheduler -v` → ALL PASS

- [ ] **Step 5: Commit**

```bash
git add ops/scheduler/orchestrator.py tests/ops/scheduler/test_orchestrator.py
git commit -m "feat(scheduler): journal position_opened provenance on every buy"
```

---

### Task 6: Exit engine (`ops/exits/engine.py`)

**Files:**
- Create: `ops/exits/__init__.py`, `ops/exits/engine.py`
- Test: `tests/ops/exits/__init__.py` (empty), `tests/ops/exits/test_engine.py`

**Interfaces:**
- Consumes: `MomentumHit`, `SMA_WINDOW` from `ops/universe/momentum.py`; `Position` from `ops/broker/types.py`; `trading_days_between` (Task 1); config fields (Task 3).
- Produces (`ops/exits/__init__.py` re-exports all four; Task 7 relies on these):

```python
@dataclass(frozen=True)
class ExitDecision:
    symbol: str
    rule: str        # "rank_decay" | "trend_break" | "max_hold"
    evidence: str

@dataclass(frozen=True)
class ExitSkip:
    symbol: str
    reason: str

@dataclass(frozen=True)
class ExitReport:
    decisions: list[ExitDecision]
    skips: list[ExitSkip]
    unknown_provenance: list[str]

def evaluate_exits(*, positions, provenance, leaderboard, closes_fetch,
                   config, asof_date) -> ExitReport
```

`provenance` is `dict[str, dict]` — symbol → `position_opened` payload. `closes_fetch` has the same signature as `fetch_closes_and_volumes_from_yfinance`.

**Rule semantics (spec Component 6, encode exactly):**
- `EARNINGS` source: exit iff `trading_days_between(entry_date, asof_date) >= earnings_max_hold_days`. No decay rules — overlap names were bought on the event thesis.
- `MOMENTUM` or unknown source (unknown also lands in `unknown_provenance`): fetch closes; if unavailable or fewer than `SMA_WINDOW + 1` bars → `ExitSkip` (never sell on missing data). Otherwise:
  - **trend_break:** last close < today's SMA200 AND previous close < yesterday's SMA200 (two consecutive closes, each against its own-day MA).
  - **rank_decay:** symbol present on the leaderboard with `rank > momentum_exit_rank`. Absence from the leaderboard alone NEVER fires rank_decay — an absent-but-fetchable name failed the MA gate (single close below MA), and one close must not trigger an exit; that distinguishes *evaluated-and-ranked-low* from *could-not-evaluate*.
  - First matching rule wins (evaluate trend_break, then rank_decay).

- [ ] **Step 1: Write the failing tests**

Create `tests/ops/exits/test_engine.py`:

```python
from datetime import date
from decimal import Decimal

from ops.config import OpsConfig
from ops.broker.types import Position
from ops.exits import evaluate_exits
from ops.universe.momentum import SMA_WINDOW, MomentumHit

ASOF = date(2026, 7, 6)  # a Monday
CFG = OpsConfig()


def _pos(sym):
    return Position(symbol=sym, quantity=Decimal("1"),
                    avg_entry_price=Decimal("100"))


def _prov(sym, source, entry="2026-07-02", rank=None):
    p = {"symbol": sym, "source": source, "entry_date": entry,
         "client_order_id": "x"}
    if rank is not None:
        p["entry_rank"] = rank
    return p


def _mhit(sym, rank):
    return MomentumHit(symbol=sym, asof_date=ASOF,
                       trailing_return_6m=Decimal("0.2"),
                       close=Decimal("110"), sma_200=Decimal("100"),
                       avg_dollar_volume_20d=Decimal("100000000"), rank=rank)


def _uptrend_closes():
    # 201 rising closes: both last closes comfortably above their MAs.
    return [Decimal(100) + Decimal(i) for i in range(SMA_WINDOW + 1)]


def _broken_closes():
    # Flat at 100 for 199 bars, then two closes far below both days' MAs.
    return [Decimal(100)] * (SMA_WINDOW - 1) + [Decimal(50), Decimal(50)]


def _fetch(mapping):
    return lambda sym: mapping.get(sym)


def _run(positions, provenance, leaderboard, closes, config=CFG):
    return evaluate_exits(
        positions=positions, provenance=provenance, leaderboard=leaderboard,
        closes_fetch=_fetch(closes), config=config, asof_date=ASOF,
    )


def test_rank_decay_fires_at_26_not_25():
    closes = {"A": (_uptrend_closes(), []), "B": (_uptrend_closes(), [])}
    prov = {"A": _prov("A", "MOMENTUM"), "B": _prov("B", "MOMENTUM")}
    board = [_mhit("B", 25), _mhit("A", 26)]
    report = _run([_pos("A"), _pos("B")], prov, board, closes)
    assert [d.symbol for d in report.decisions] == ["A"]
    assert report.decisions[0].rule == "rank_decay"


def test_trend_break_needs_two_consecutive_closes():
    one_below = _uptrend_closes()
    one_below[-1] = Decimal("1")  # only the LAST close dips below the MA
    closes = {"TWO": (_broken_closes(), []), "ONE": (one_below, [])}
    prov = {s: _prov(s, "MOMENTUM") for s in ("TWO", "ONE")}
    report = _run([_pos("TWO"), _pos("ONE")], prov, [], closes)
    assert [d.symbol for d in report.decisions] == ["TWO"]
    assert report.decisions[0].rule == "trend_break"
    # ONE is off the leaderboard with a single below-MA close: no exit.
    assert report.skips == []


def test_missing_data_skips_never_sells():
    prov = {"GONE": _prov("GONE", "MOMENTUM"),
            "SHORT": _prov("SHORT", "MOMENTUM")}
    closes = {"SHORT": ([Decimal(100)] * 50, [])}
    report = _run([_pos("GONE"), _pos("SHORT")], prov, [], closes)
    assert report.decisions == []
    assert sorted(s.symbol for s in report.skips) == ["GONE", "SHORT"]


def test_earnings_max_hold_fires_on_day_40_not_39():
    # 40 trading days before Mon 2026-07-06 is Mon 2026-05-11 (weekday count).
    prov39 = {"E": _prov("E", "EARNINGS", entry="2026-05-12")}
    prov40 = {"E": _prov("E", "EARNINGS", entry="2026-05-11")}
    assert _run([_pos("E")], prov39, [], {}).decisions == []
    report = _run([_pos("E")], prov40, [], {})
    assert [d.rule for d in report.decisions] == ["max_hold"]


def test_earnings_source_ignores_rank_and_ma():
    # Earnings-sourced overlap name ranked 200 with broken trend: still held.
    prov = {"E": _prov("E", "EARNINGS", entry="2026-07-02")}
    closes = {"E": (_broken_closes(), [])}
    report = _run([_pos("E")], prov, [_mhit("E", 200)], closes)
    assert report.decisions == []


def test_unknown_provenance_gets_momentum_rules_and_is_reported():
    closes = {"MYSTERY": (_broken_closes(), [])}
    report = _run([_pos("MYSTERY")], {}, [], closes)
    assert report.unknown_provenance == ["MYSTERY"]
    assert [d.rule for d in report.decisions] == ["trend_break"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/exits/test_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.exits'`

- [ ] **Step 3: Implement**

Create `ops/exits/engine.py`:

```python
"""Position exit engine (spec Component 6): daily, rule-based sell decisions.

Pure function over injected data — no I/O, no broker, no journal. The
orchestrator supplies positions, provenance (position_opened payloads),
today's leaderboard, and a closes fetcher, then acts on the report.

Never sells on missing data: an unfetchable symbol is a skip, not a close.
A data outage must not liquidate the book."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from ops.broker.types import Position
from ops.config import OpsConfig
from ops.trading_time import trading_days_between
from ops.universe.momentum import SMA_WINDOW, MomentumHit


@dataclass(frozen=True)
class ExitDecision:
    symbol: str
    rule: str  # "rank_decay" | "trend_break" | "max_hold"
    evidence: str


@dataclass(frozen=True)
class ExitSkip:
    symbol: str
    reason: str


@dataclass(frozen=True)
class ExitReport:
    decisions: list[ExitDecision]
    skips: list[ExitSkip]
    unknown_provenance: list[str]


def _sma(closes: list[Decimal], end: int) -> Decimal:
    window = closes[end - SMA_WINDOW:end]
    return sum(window) / Decimal(SMA_WINDOW)


def evaluate_exits(
    *,
    positions: list[Position],
    provenance: dict[str, dict[str, Any]],
    leaderboard: list[MomentumHit],
    closes_fetch: Callable[[str], tuple[list[Decimal], list[Decimal]] | None],
    config: OpsConfig,
    asof_date: date,
) -> ExitReport:
    rank_by_symbol = {h.symbol: h.rank for h in leaderboard}
    decisions: list[ExitDecision] = []
    skips: list[ExitSkip] = []
    unknown: list[str] = []

    for pos in positions:
        payload = provenance.get(pos.symbol)
        source = (payload or {}).get("source")

        if source == "EARNINGS":
            entry = date.fromisoformat(payload["entry_date"])
            held = trading_days_between(entry, asof_date)
            if held >= config.earnings_max_hold_days:
                decisions.append(ExitDecision(
                    symbol=pos.symbol, rule="max_hold",
                    evidence=(f"held {held} trading days >= "
                              f"{config.earnings_max_hold_days} (PEAD window)"),
                ))
            continue

        # MOMENTUM — or unknown provenance, which gets the general
        # "does this name still deserve a slot" test, flagged for audit.
        if source != "MOMENTUM":
            unknown.append(pos.symbol)

        data = closes_fetch(pos.symbol)
        if data is None or len(data[0]) < SMA_WINDOW + 1:
            skips.append(ExitSkip(
                symbol=pos.symbol,
                reason="insufficient close history for exit evaluation",
            ))
            continue
        closes = data[0]
        n = len(closes)
        sma_today = _sma(closes, n)
        sma_prev = _sma(closes, n - 1)
        if closes[-1] < sma_today and closes[-2] < sma_prev:
            decisions.append(ExitDecision(
                symbol=pos.symbol, rule="trend_break",
                evidence=(f"two closes below 200d MA "
                          f"({closes[-2]}, {closes[-1]} < {sma_today})"),
            ))
            continue
        rank = rank_by_symbol.get(pos.symbol)
        # Absence from the leaderboard alone never fires rank_decay: an
        # absent-but-fetchable name failed the MA gate on ONE close, and a
        # single close must not trigger an exit (hysteresis / whipsaw).
        if rank is not None and rank > config.momentum_exit_rank:
            decisions.append(ExitDecision(
                symbol=pos.symbol, rule="rank_decay",
                evidence=f"rank {rank} > {config.momentum_exit_rank}",
            ))

    return ExitReport(decisions=decisions, skips=skips,
                      unknown_provenance=unknown)
```

Create `ops/exits/__init__.py`:

```python
from ops.exits.engine import ExitDecision, ExitReport, ExitSkip, evaluate_exits

__all__ = ["ExitDecision", "ExitReport", "ExitSkip", "evaluate_exits"]
```

Create empty `tests/ops/exits/__init__.py`.

- [ ] **Step 4: Run tests** — `python -m pytest tests/ops/exits -v` → 7 PASS

- [ ] **Step 5: Commit**

```bash
git add ops/exits tests/ops/exits
git commit -m "feat(exits): pure exit engine — rank decay, trend break, max hold"
```

---

### Task 7: Orchestrator exit step + cooldown + shared leaderboard

**Files:**
- Modify: `ops/scheduler/orchestrator.py`
- Test: `tests/ops/scheduler/test_orchestrator.py`

**Interfaces:**
- Consumes: everything above. Constructor gains optional keyword args with real defaults: `members_loader=load_sp500_members`, `momentum_finder=find_momentum_leaders`, `closes_fetch=fetch_closes_and_volumes_from_yfinance` (import all three at module top; existing construction sites keep working).
- Produces the final tick order (spec Data Flow): halts → leaderboard once → exits → cooldown → builder(`momentum_leaders=`, `excluded_symbols=`) → buys.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ops/scheduler/test_orchestrator.py`. Reuse the file's existing fake helpers where they exist; define the two new ones like this (`_mhit` and `_uptrend_closes` exactly as in `tests/ops/exits/test_engine.py`):

```python
def _broker_holding_momentum(sym):
    """Fake broker holding one position; close_position removes it."""
    from datetime import datetime, timezone
    from ops.broker.types import Fill, Position, Side
    broker = MagicMock()
    state = {"positions": [Position(symbol=sym, quantity=Decimal("1"),
                                    avg_entry_price=Decimal("100"))]}
    broker.get_positions.side_effect = lambda: list(state["positions"])
    broker.get_equity.return_value = Decimal("1000")

    def close(symbol, **kwargs):
        state["positions"] = [p for p in state["positions"] if p.symbol != symbol]
        return Fill(order_id="o", client_order_id=f"exit-{symbol}",
                    symbol=symbol, side=Side.SELL, quantity=Decimal("1"),
                    price=Decimal("100"),
                    filled_at=datetime.now(timezone.utc))
    broker.close_position.side_effect = close
    return broker


def _journal_position_opened(journal, sym, source):
    from datetime import datetime, timezone
    from ops import events
    journal.record_event(events.KIND_POSITION_OPENED, events.position_opened_payload(
        symbol=sym, source=source,
        entry_date=datetime.now(timezone.utc).date(), client_order_id="x",
    ))


def _raises(exc):
    def f(*args, **kwargs):
        raise exc
    return f
```

```python
def test_exit_decision_sells_and_journals_and_frees_slot():
    # Broker holds NVDA (momentum, rank 30 today -> rank_decay).
    # After the exit, the builder must see NVDA gone and one more free slot.
    journal = _make_journal()
    seen = {}
    orch = _make_orchestrator(
        broker=_broker_holding_momentum("NVDA"),
        universe_builder=_fake_universe([], seen),
        journal=journal,
        momentum_finder=lambda members, asof_date: [_mhit("NVDA", 30)],
        closes_fetch=lambda s: (_uptrend_closes(), []),
    )
    _journal_position_opened(journal, "NVDA", "MOMENTUM")
    orch.tick()
    kinds = [e["kind"] for e in journal.read_events()]
    assert "exit_decision" in kinds and "exit_order_placed" in kinds
    assert seen["held_symbols"] == frozenset()
    assert seen["free_slots"] == OpsConfig().max_open_positions
    assert seen["momentum_leaders"][0].symbol == "NVDA"  # computed once, passed through


def test_recent_stop_out_is_excluded_from_builder():
    journal = _make_journal()
    journal.record_event("stop_hit", {"symbol": "BURNED"})
    seen = {}
    orch = _make_orchestrator(
        broker=_fake_broker_with_positions([]),
        universe_builder=_fake_universe([], seen), journal=journal,
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    orch.tick()
    assert "BURNED" in seen["excluded_symbols"]


def test_exit_engine_crash_is_journaled_and_buys_proceed():
    journal = _make_journal()
    seen = {}
    orch = _make_orchestrator(
        broker=_fake_broker_with_positions([]),
        universe_builder=_fake_universe([], seen), journal=journal,
        momentum_finder=_raises(RuntimeError("boom")),
        closes_fetch=lambda s: None,
    )
    orch.tick()
    assert any(e["kind"] == "exit_check_error" for e in journal.read_events())
    assert "held_symbols" in seen  # tick reached the builder anyway
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/scheduler/test_orchestrator.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'momentum_finder'`

- [ ] **Step 3: Implement**

In `ops/scheduler/orchestrator.py`:

Module imports:

```python
from datetime import datetime, time, timezone

from ops.exits import evaluate_exits
from ops.trading_time import trading_day_start, trading_days_back, trading_week_start
from ops.universe.filters import apply_deny_list
from ops.universe.momentum import (
    fetch_closes_and_volumes_from_yfinance,
    find_momentum_leaders,
)
from ops.universe.sp500 import load_sp500_members
```

Constructor additions (optional, real defaults):

```python
    def __init__(
        self, *, broker, universe_builder, strategy, pipeline_adapter,
        calendar, journal, config,
        members_loader=load_sp500_members,
        momentum_finder=find_momentum_leaders,
        closes_fetch=fetch_closes_and_volumes_from_yfinance,
    ) -> None:
        ...
        self._members_loader = members_loader
        self._momentum_finder = momentum_finder
        self._closes_fetch = closes_fetch
```

Rework `_tick_impl` between the halt checks and the strategy call:

```python
        asof_date = datetime.now(timezone.utc).date()

        # Leaderboard is computed ONCE per tick: the exit engine reads held
        # names' ranks off it and the builder takes its head for entries.
        # A failure here (or anywhere in the exit step) must not kill the
        # tick — buys degrade gracefully, and the guardian still owns stops.
        leaderboard = []
        try:
            eligible = apply_deny_list(self._members_loader(), self._config.deny_list)
            leaderboard = self._momentum_finder(eligible, asof_date=asof_date)
            self._run_exits(leaderboard, asof_date)
        except Exception as exc:
            self._journal.record_event(
                events.KIND_EXIT_CHECK_ERROR,
                events.exit_check_error_payload(
                    error=f"{type(exc).__name__}: {exc}",
                ),
            )

        held = {p.symbol for p in self._broker.get_positions()}
        free_slots = max(0, self._config.max_open_positions - len(held))
        candidates = self._universe_builder(
            asof_date=asof_date, config=self._config,
            held_symbols=frozenset(held), free_slots=free_slots,
            excluded_symbols=self._cooldown_symbols(asof_date),
            momentum_leaders=leaderboard,
        )
        fresh_candidates = [c for c in candidates if c.symbol not in held]
```

Add the two helpers:

```python
    def _run_exits(self, leaderboard, asof_date) -> None:
        report = evaluate_exits(
            positions=list(self._broker.get_positions()),
            provenance=self._journal.latest_event_payload_by_symbol(
                events.KIND_POSITION_OPENED,
            ),
            leaderboard=leaderboard,
            closes_fetch=self._closes_fetch,
            config=self._config,
            asof_date=asof_date,
        )
        for skip in report.skips:
            self._journal.record_event(
                events.KIND_EXIT_SKIPPED_MISSING_DATA,
                events.exit_skipped_missing_data_payload(
                    symbol=skip.symbol, reason=skip.reason,
                ),
            )
        for decision in report.decisions:
            self._journal.record_event(
                events.KIND_EXIT_DECISION,
                events.exit_decision_payload(
                    symbol=decision.symbol, rule=decision.rule,
                    evidence=decision.evidence,
                ),
            )
            try:
                fill = self._broker.close_position(decision.symbol)
            except BrokerError as exc:
                # Position still held, condition still true next tick — the
                # engine is idempotent, so journal and move on.
                self._journal.record_event(
                    events.KIND_EXIT_CHECK_ERROR,
                    events.exit_check_error_payload(
                        error=(f"close_position({decision.symbol}) failed: "
                               f"{type(exc).__name__}: {exc}"),
                    ),
                )
                continue
            self._journal.record_event(
                events.KIND_EXIT_ORDER_PLACED,
                events.exit_order_placed_payload(
                    symbol=decision.symbol,
                    client_order_id=fill.client_order_id,
                    rule=decision.rule,
                ),
            )

    def _cooldown_symbols(self, asof_date) -> frozenset[str]:
        since_date = trading_days_back(
            asof_date, self._config.stopout_reentry_cooldown_days,
        )
        since = datetime.combine(since_date, time.min, tzinfo=timezone.utc)
        return self._journal.event_symbols_since(events.KIND_STOP_HIT, since)
```

Note the `asof_date` assignment moves above the exit step; delete the old duplicate line.

- [ ] **Step 4: Inject fakes into every existing Orchestrator construction in tests**

CRITICAL: the new constructor defaults are the REAL yfinance-backed functions, so any existing test that constructs an `Orchestrator` without overriding them will fetch ~500 symbols from the network on `tick()`. Find every site:

Run: `grep -rn "Orchestrator(" tests/`

At each construction (expect ~10 in `tests/ops/scheduler/test_orchestrator.py`, 1 in `tests/ops/test_integration_orchestrator.py`, plus any decide-once tests), add:

```python
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
```

If the file has a shared `_make_orchestrator` helper, put these defaults there once instead of at every call site.

- [ ] **Step 5: Run scheduler tests, then the full suite**

Run: `python -m pytest tests/ops/scheduler -v` → ALL PASS
Run: `python -m pytest tests/ops -v` → ALL PASS, and fast — a hang or multi-second slowdown here means a construction site was missed in Step 4 and is hitting the network.

- [ ] **Step 6: Commit**

```bash
git add ops/scheduler/orchestrator.py tests/ops
git commit -m "feat(scheduler): exit step before buys — sells, cooldown, shared leaderboard"
```

---

### Task 8: End-to-end lifecycle test + verification

**Files:**
- Test: `tests/ops/test_integration_exits.py` (new)

- [ ] **Step 1: Write the lifecycle test**

Create `tests/ops/test_integration_exits.py`. Everything downstream of yfinance/pipeline is REAL: composite builder, exit engine, strategy, guarded paper broker, journal. `max_open_positions=1` makes the story sharp: the only way AMD gets bought on day 2 is that the exit engine freed NVDA's slot in the same tick.

```python
"""End-to-end momentum lifecycle: buy on day 1; on day 2 the rank-decay exit
sells and the freed slot is refilled the SAME tick. Real composite builder,
exit engine, strategy, guarded paper broker, and journal; yfinance, pipeline,
and calendar are faked (no network, no LLM calls)."""
import functools
from decimal import Decimal
from unittest.mock import MagicMock

from ops import build_guarded_paper_broker
from ops.config import OpsConfig
from ops.journal import Journal
from ops.pipeline_adapter import PipelineDecision, PipelineResult
from ops.scheduler.orchestrator import Orchestrator
from ops.strategy.post_earnings_momentum import PostEarningsMomentumStrategy
from ops.universe.composite import build_composite_universe
from ops.universe.momentum import SMA_WINDOW, MomentumHit

_QUOTES = {"NVDA": Decimal("100"), "AMD": Decimal("100")}
_UPTREND = [Decimal(50) + Decimal(i) for i in range(SMA_WINDOW + 1)]


class _AlwaysBuyPipeline:
    def propagate(self, symbol, asof_date):
        return PipelineResult(symbol=symbol, date=asof_date,
                              decision=PipelineDecision.BUY, raw={})


def test_momentum_lifecycle_buy_then_rank_decay_sell_then_refill(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    cfg = OpsConfig(max_open_positions=1)
    broker = build_guarded_paper_broker(
        config=cfg, journal=j, quote_source=_QUOTES.__getitem__,
        starting_cash=Decimal("1000"),
        start_of_day_equity=lambda: Decimal("1000"),
        start_of_week_equity=lambda: Decimal("1000"),
    )
    calendar = MagicMock()
    calendar.is_open_now.return_value = True

    board = {"ranks": [("NVDA", 1), ("AMD", 2)]}

    def momentum_finder(members, asof_date):
        return [
            MomentumHit(symbol=sym, asof_date=asof_date,
                        trailing_return_6m=Decimal("1") / Decimal(rank),
                        close=Decimal("100"), sma_200=Decimal("80"),
                        avg_dollar_volume_20d=Decimal("100000000"), rank=rank)
            for sym, rank in board["ranks"]
        ]

    universe_builder = functools.partial(
        build_composite_universe,
        members_loader=lambda: ["NVDA", "AMD"],
        earnings_finder=lambda syms, asof_date, lookback_days, fetch=None: [],
        metrics_fetcher=lambda sym: (Decimal("100"), Decimal("100000000")),
    )
    orch = Orchestrator(
        broker=broker, universe_builder=universe_builder,
        strategy=PostEarningsMomentumStrategy(config=cfg),
        pipeline_adapter=_AlwaysBuyPipeline(), calendar=calendar,
        journal=j, config=cfg,
        members_loader=lambda: ["NVDA", "AMD"],
        momentum_finder=momentum_finder,
        closes_fetch=lambda s: (_UPTREND, [Decimal("1000000")] * len(_UPTREND)),
    )

    orch.tick()  # day 1: NVDA is rank 1, one free slot -> bought
    assert {p.symbol for p in broker.get_positions()} == {"NVDA"}

    # day 2: NVDA decays to rank 30 (> exit rank 25, uptrend intact so no
    # trend_break); AMD is the new leader.
    board["ranks"] = [("AMD", 1), ("NVDA", 30)]
    orch.tick()
    assert {p.symbol for p in broker.get_positions()} == {"AMD"}

    kinds = [e["kind"] for e in j.read_events()]
    assert kinds.count("position_opened") == 2   # NVDA day 1, AMD day 2
    decisions = [e for e in j.read_events() if e["kind"] == "exit_decision"]
    assert [d["payload"]["symbol"] for d in decisions] == ["NVDA"]
    assert decisions[0]["payload"]["rule"] == "rank_decay"
    assert "exit_order_placed" in kinds
```

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/ops/test_integration_exits.py -v`
Expected: PASS. If it fails, fix the production code (not the assertions) — this test IS the spec's Data Flow diagram.

- [ ] **Step 3: Full suite + verification**

Run: `python -m pytest tests/ops -v` → ALL PASS
Run: `python -m pytest tests -x -q` → no regressions elsewhere.

- [ ] **Step 4: Commit**

```bash
git add tests/ops/test_integration_exits.py
git commit -m "test(ops): end-to-end momentum lifecycle — buy, decay exit, same-tick refill"
```

**Part 2 complete:** the book turns over daily — exits free slots, cooldown blocks stop-out re-buys, and a full book costs zero pipeline runs.
