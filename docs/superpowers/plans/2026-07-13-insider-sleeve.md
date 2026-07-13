# Insider-Cluster Sleeve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fifth paper sleeve that mechanically buys small/mid-caps where ≥2 insiders just made open-market, non-10b5-1 buys — high trade throughput, fixed exits, an LLM memo as passenger (never gatekeeper).

**Architecture:** A nightly EDGAR daily-index scan persists Form 4 buys for universe names into a signal store; cluster detection scores them BASIC/STRONG; a post-close trade step enters via the long `PaperBroker` on a dedicated journal and exits on stop/target/time; a one-shot memo per entry is authored the following night inside the existing overnight ds4 bracket and resolved mechanically at exit. Spec: `docs/superpowers/specs/2026-07-13-short-and-insider-sleeves-design.md`.

**Tech Stack:** Python 3.11+, stdlib sqlite3, Pydantic v2, APScheduler, pytest. No new dependencies.

## Global Constraints

- Money is `Decimal`, always.
- Scheduler wrappers journal `*_error` events, never raise. Per-name failures are logged and skipped.
- ds4 may only be spun up inside `_research_overnight_tick`'s managed-backend bracket. The 16:29 trade tick makes NO LLM calls.
- The sleeve touches only `insider_journal_path`, `insider_memo_store_path`, `insider_signal_store_path`; the main journal gets gate/summary/error events only.
- The memo never gates a trade. Memo authoring failure = journaled error + the trade stands.
- SEC requests go through `edgar._throttled_get` (rate-limit throttle + required User-Agent). Never bypass it.
- Deny-list (`config.deny_list`) is enforced at entry.
- Run tests with `.venv/bin/pytest`; `tests/test_main.py` has 11 pre-existing failures on main — scope runs to the suites named per task.

## File Map

| file | role |
|---|---|
| `ops/insider/__init__.py` (create) | package |
| `ops/insider/store.py` (create) | signal store: transactions, entries, cooldowns |
| `ops/insider/scan.py` (create) | EDGAR daily-index Form 4 scan |
| `ops/insider/clusters.py` (create) | cluster detection + strength |
| `ops/insider/trading.py` (create) | entries/exits, fences |
| `ops/insider/memo_lite.py` (create) | one-shot memo author + mechanical resolution |
| `ops/events.py` (modify) | new event kinds |
| `ops/config.py` (modify) | paths + starting cash |
| `ops/main.py` (modify) | scan tick, trade tick, overnight memo stage |
| `ops/notify/overview.py` (modify) | sleeve section |

---

### Task 1: signal store

**Files:**
- Create: `ops/insider/__init__.py` (empty), `ops/insider/store.py`
- Test: `tests/ops/insider/test_store.py` (create; add `tests/ops/insider/__init__.py` if the suite convention needs it — check `tests/ops/research/`)

**Interfaces:**
- Produces: `class SignalStore(db_path)` (sqlite3 + process lock + ISO-8601 TEXT timestamps, following `tradingagents/memos/store.py` conventions) with:
  - `record_transactions(symbol: str, txns: list[InsiderTransaction]) -> int` — upsert by `(accession, insider_name, transaction_date, shares)` unique key; returns rows actually inserted (idempotent re-scan)
  - `buys_in_window(symbol: str, *, since: date, until: date) -> list[dict]` — rows with keys `insider_name, insider_title, is_director, is_officer, transaction_date, shares, price, accession` for open-market, non-10b5-1 buys (`code = 'P' AND NOT ten_b5_1`)
  - `symbols_with_new_buys(*, since: date) -> list[str]`
  - `record_entry(symbol: str, *, asof: date, memo_id: str = "") -> None` and `last_entry_date(symbol) -> date | None` — the 90-day cooldown source
  - `entries_without_memo() -> list[dict]` (keys `symbol, asof`) and `set_entry_memo(symbol, asof, memo_id)` — the overnight memo queue
  - `scan_watermark() -> date | None` / `set_scan_watermark(d: date)` — last daily index ingested

Schema (one executescript, `CREATE TABLE IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS insider_transactions (
    symbol TEXT NOT NULL, insider_name TEXT NOT NULL, insider_title TEXT,
    is_director INTEGER NOT NULL, is_officer INTEGER NOT NULL,
    transaction_date TEXT, code TEXT NOT NULL, ten_b5_1 INTEGER NOT NULL,
    shares TEXT, price TEXT, accession TEXT NOT NULL, filed_date TEXT NOT NULL,
    UNIQUE(accession, insider_name, transaction_date, shares)
);
CREATE INDEX IF NOT EXISTS idx_txn_symbol_date ON insider_transactions(symbol, transaction_date);
CREATE TABLE IF NOT EXISTS sleeve_entries (
    symbol TEXT NOT NULL, asof TEXT NOT NULL, memo_id TEXT NOT NULL DEFAULT '',
    UNIQUE(symbol, asof)
);
CREATE TABLE IF NOT EXISTS scan_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
```

(`shares`/`price` stored as TEXT of the Decimal — same money-as-text discipline as the journal.)

- [ ] **Step 1: Write the failing tests** — round-trip a canned `InsiderTransaction` list (build real `form4.InsiderTransaction` objects); re-recording the same list inserts 0 new rows; `buys_in_window` excludes code-S, 10b5-1, and out-of-window rows; cooldown and watermark round-trips; `entries_without_memo` → `set_entry_memo` transition.
- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/ops/insider/test_store.py -v` → `ModuleNotFoundError`
- [ ] **Step 3: Implement** — follow `MemoStore`'s structure (`_connect`, `threading.Lock`, `Path.mkdir(parents=True)`). `record_transactions` uses `INSERT OR IGNORE` and sums `cursor.rowcount`.
- [ ] **Step 4: Run** — all PASS
- [ ] **Step 5: Commit**

```bash
git add ops/insider/ tests/ops/insider/
git commit -m "feat(insider): signal store — transactions, entries, cooldowns, watermark"
```

---

### Task 2: daily-index scan

**Files:**
- Create: `ops/insider/scan.py`
- Test: `tests/ops/insider/test_scan.py` (create)

**Interfaces:**
- Consumes: `SignalStore` (Task 1), `edgar._throttled_get`, `form4.parse_form4_xml(xml_text, *, accession, filed_date)`, `load_smallcap_members()` (symbols), `edgar.get_cik`.
- Produces: `scan_daily_index(*, store: SignalStore, day: date, universe_symbols: list[str], fetch_raw=None, cik_resolver=None) -> ScanSummary` with `ScanSummary(day, form4_seen: int, universe_matches: int, transactions_recorded: int, errors: list[str])`, and `run_insider_scan(*, store, universe_loader=None, today=None, fetch_raw=None, cik_resolver=None) -> list[ScanSummary]` which scans every business day after `store.scan_watermark()` up to yesterday (bounded to 7 days back on first run) and advances the watermark per day completed.

Daily index format (one fetch per day):
`https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{q}/master.{YYYYMMDD}.idx` — pipe-delimited lines `CIK|Company Name|Form Type|Date Filed|Filename` after a dashed header; filter `Form Type == "4"`. A 404 (holiday/weekend) is an empty day, not an error. For each matching CIK (intersected against `{get_cik(sym): sym}` built once per run over `universe_symbols`, skipping symbols that raise `KeyError`), fetch `https://www.sec.gov/Archives/{Filename}` (the full submission `.txt`), extract the first `<XML>…</XML>` block containing `<ownershipDocument`, and feed it to `parse_form4_xml` with the accession parsed from the filename (`0001209191-26-037449` from `edgar/data/…/0001209191-26-037449.txt`).

- [ ] **Step 1: Write the failing tests** — canned idx text (3 lines: one Form 4 in-universe, one Form 4 out-of-universe, one 10-K) + canned submission text with an embedded ownership XML (crib a minimal one from the existing form4 tests — `grep -rl parse_form4_xml tests/`). Assert: only the in-universe Form 4 is fetched and recorded; 404 idx → `form4_seen == 0`, no error; a submission fetch that raises records an error string and the scan continues; `run_insider_scan` advances the watermark and re-running is a no-op.
- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`
- [ ] **Step 3: Implement**

```python
"""EDGAR daily-index Form 4 scan for the insider sleeve.

One index fetch per day + one document fetch per in-universe Form 4 —
NEVER per-ticker polling (a 1500-name universe would hammer SEC rate
limits; the throttle in edgar._throttled_get is process-wide and required).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

from ops.insider.store import SignalStore
from tradingagents.dataflows.form4 import parse_form4_xml

DAILY_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{q}/master.{ymd}.idx"
)
ARCHIVE_URL = "https://www.sec.gov/Archives/{path}"
FIRST_RUN_LOOKBACK_DAYS = 7
_XML_BLOCK = re.compile(r"<XML>(.*?)</XML>", re.DOTALL | re.IGNORECASE)


@dataclass
class ScanSummary:
    day: date
    form4_seen: int = 0
    universe_matches: int = 0
    transactions_recorded: int = 0
    errors: list[str] = field(default_factory=list)


def _default_fetch_raw(url: str) -> str:
    from tradingagents.dataflows.edgar import _throttled_get

    return _throttled_get(url).text


def _quarter(d: date) -> int:
    return (d.month - 1) // 3 + 1


def _accession_from_path(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".txt")


def _ownership_xml(submission_text: str) -> str | None:
    for m in _XML_BLOCK.finditer(submission_text):
        if "<ownershipDocument" in m.group(1):
            return m.group(1).strip()
    return None


def scan_daily_index(
    *, store: SignalStore, day: date, universe_symbols: list[str],
    fetch_raw=None, cik_resolver=None,
) -> ScanSummary:
    fetch_raw = fetch_raw or _default_fetch_raw
    if cik_resolver is None:
        from tradingagents.dataflows.edgar import get_cik
        cik_resolver = get_cik
    summary = ScanSummary(day=day)

    symbol_by_cik: dict[int, str] = {}
    for sym in universe_symbols:
        try:
            symbol_by_cik[cik_resolver(sym)] = sym
        except Exception:  # not every listed symbol is in company_tickers.json
            continue

    url = DAILY_INDEX_URL.format(year=day.year, q=_quarter(day), ymd=day.strftime("%Y%m%d"))
    try:
        idx_text = fetch_raw(url)
    except Exception as exc:
        if "404" in str(exc):
            return summary  # holiday/weekend: an empty day, not an error
        summary.errors.append(f"daily index fetch failed: {exc}")
        return summary

    for line in idx_text.splitlines():
        parts = line.split("|")
        if len(parts) != 5 or parts[2].strip() != "4":
            continue
        summary.form4_seen += 1
        try:
            cik = int(parts[0])
        except ValueError:
            continue
        symbol = symbol_by_cik.get(cik)
        if symbol is None:
            continue
        summary.universe_matches += 1
        path = parts[4].strip()
        try:
            xml = _ownership_xml(fetch_raw(ARCHIVE_URL.format(path=path)))
            if xml is None:
                raise ValueError("no ownershipDocument XML block")
            txns = parse_form4_xml(
                xml, accession=_accession_from_path(path), filed_date=day,
            )
            summary.transactions_recorded += store.record_transactions(symbol, txns)
        except Exception as exc:  # one bad document must not kill the scan
            summary.errors.append(f"{symbol} {path}: {exc}")
            print(f"[insider-scan] {symbol}: {exc}", file=sys.stderr)
    return summary


def run_insider_scan(
    *, store: SignalStore, universe_loader=None, today: date | None = None,
    fetch_raw=None, cik_resolver=None,
) -> list[ScanSummary]:
    if universe_loader is None:
        from ops.universe.smallcap import load_smallcap_members

        universe_loader = lambda: [m.symbol for m in load_smallcap_members()]
    today = today or date.today()
    start = store.scan_watermark()
    if start is None:
        start = today - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
    symbols = universe_loader()
    out = []
    day = start + timedelta(days=1)
    while day < today:
        if day.weekday() < 5:
            out.append(scan_daily_index(
                store=store, day=day, universe_symbols=symbols,
                fetch_raw=fetch_raw, cik_resolver=cik_resolver,
            ))
        store.set_scan_watermark(day)
        day += timedelta(days=1)
    return out
```

(Adjust the 404 detection to whatever `_throttled_get` actually raises —
`requests.HTTPError` carries `response.status_code`; test both branches.)

- [ ] **Step 4: Run** — all PASS
- [ ] **Step 5: Commit**

```bash
git add ops/insider/scan.py tests/ops/insider/test_scan.py
git commit -m "feat(insider): EDGAR daily-index Form 4 scan into the signal store"
```

---

### Task 3: cluster detection + strength

**Files:**
- Create: `ops/insider/clusters.py`
- Test: `tests/ops/insider/test_clusters.py` (create)

**Interfaces:**
- Consumes: `SignalStore.buys_in_window` / `symbols_with_new_buys` / `last_entry_date` (Task 1).
- Produces: `find_clusters(store: SignalStore, *, asof: date) -> list[Cluster]` with `Cluster(symbol: str, strength: str, buyers: tuple[str, ...], agg_dollars: Decimal, accessions: tuple[str, ...], latest_buy: date)`; `strength ∈ {"BASIC", "STRONG"}`. Constants: `CLUSTER_WINDOW_DAYS = 30`, `MIN_BUYERS = 2`, `MIN_BUY_DOLLARS = Decimal("10000")` (per-insider aggregate in window), `STRONG_MIN_BUYERS = 3`, `STRONG_MIN_AGG_DOLLARS = Decimal("250000")`, `COOLDOWN_DAYS = 90`. STRONG also when any buyer's `insider_title` matches `CEO|Chief Executive|CFO|Chief Financial` (case-insensitive). Names inside their cooldown window (`last_entry_date` within 90 days of `asof`) are excluded.

- [ ] **Step 1: Write the failing tests** — seed the store via `record_transactions` with crafted `InsiderTransaction` lists: 2 buyers ≥$10k each → BASIC; 3 buyers → STRONG; 2 buyers incl. a "Chief Financial Officer" title → STRONG; $260k aggregate from 2 buyers → STRONG; one buyer below $10k doesn't count toward `MIN_BUYERS`; a name with `record_entry` 30 days ago is excluded; buys older than 30 days don't count.
- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`
- [ ] **Step 3: Implement** — per symbol from `symbols_with_new_buys(since=asof-30d)`: group `buys_in_window` rows by `insider_name`; per-insider dollars = Σ(`shares × price`, skipping rows where either is None); qualified buyers = those ≥ `MIN_BUY_DOLLARS`; emit a `Cluster` when `len(qualified) >= MIN_BUYERS` and the cooldown check passes; strength per the constants above.
- [ ] **Step 4: Run** — all PASS
- [ ] **Step 5: Commit**

```bash
git add ops/insider/clusters.py tests/ops/insider/test_clusters.py
git commit -m "feat(insider): cluster detection with BASIC/STRONG scoring and cooldown"
```

---

### Task 4: trade step — entries, exits, events

**Files:**
- Create: `ops/insider/trading.py`
- Modify: `ops/events.py`
- Test: `tests/ops/insider/test_trading.py` (create)

**Interfaces:**
- Consumes: `PaperBroker` (the long one — this sleeve buys), `find_clusters` (Task 3), `SignalStore.record_entry`, `config.deny_list`.
- Produces: `trade_insider_sleeve(*, signal_store, insider_journal, main_journal, quote_source, starting_cash, deny_list: frozenset[str], asof, now=None, adv_fetcher=None) -> TradeOutcome` (reuse `TradeOutcome` from `ops/research/trading.py`). Constants: `BASIC_SLICE_PCT = Decimal("0.03")`, `STRONG_SLICE_PCT = Decimal("0.05")`, `NAME_CAP_PCT = Decimal("0.05")`, `MAX_POSITIONS = 25`, `ADV_CAP_PCT = Decimal("0.05")`, `MIN_ORDER_DOLLARS = Decimal("100")`, `STOP_PCT = Decimal("-0.20")`, `TARGET_PCT = Decimal("0.40")`, `MAX_HOLD_CALENDAR_DAYS = 126` (~90 trading days). New event kinds (payload builders mirroring the research ones): `KIND_INSIDER_POSITION_OPENED/CLOSED = "insider_position_opened"/"insider_position_closed"` (insider journal; opened-payload carries `symbol, strength, entry_date, client_order_id, notional, buyers, accessions`), `KIND_INSIDER_TRADE_RUN/ERROR = "insider_trade_run"/"insider_trade_error"`, `KIND_INSIDER_SCAN_RUN/ERROR = "insider_scan_run"/"insider_scan_error"` (main journal; add all to the audit frozenset).

- [ ] **Step 1: Write the failing tests** — fixtures as in `tests/ops/research/test_trading.py` (tmp journals, dict quote source, seeded signal store). Exits first, first-match-wins, one test each: stop (quote ≤ entry × 0.80), target (quote ≥ entry × 1.40), time (entry_date older than 126 days — from the opened-event payload). Entries: BASIC → 3% of equity, STRONG → 5%; deny-listed symbol skipped with reason; 26th position rejected by `MAX_POSITIONS`; name cap; ADV cap; already-held symbol skipped; entry records `signal_store.record_entry` + opened event; summary event on main journal + `kind="insider_run"` equity snapshot on the insider journal; exits run before entries and an exited symbol is not re-entered same run (cooldown covers later runs).
- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`
- [ ] **Step 3: Implement** — structure transcribed from `ops/research/trading.py`: `PaperBroker.from_journal(journal=insider_journal, ...)`; provenance via `insider_journal.latest_event_payload_by_symbol(events.KIND_INSIDER_POSITION_OPENED)`; `_exit_reason(pos, quote, entry_date)` pure function; entry sizing inline (slice by strength, then clamp to name-cap room from `cost_basis(broker.get_positions())`, ADV room, cash; reject < `MIN_ORDER_DOLLARS`); equity fallback mirrors `trading._equity_with_fallback`.
- [ ] **Step 4: Run** — `.venv/bin/pytest tests/ops/insider/ -v` → all PASS
- [ ] **Step 5: Commit**

```bash
git add ops/insider/trading.py ops/events.py tests/ops/insider/test_trading.py
git commit -m "feat(insider): mechanical trade step — strength-sized entries, stop/target/time exits"
```

---

### Task 5: memo-lite — author (overnight) + resolve (at exit)

**Files:**
- Create: `ops/insider/memo_lite.py`
- Modify: `ops/insider/trading.py` (resolution hook on exit)
- Test: `tests/ops/insider/test_memo_lite.py` (create)

**Interfaces:**
- Consumes: `Memo`/`EventThesis`/`Resolution` schema, `MemoStore` (pointed at the INSIDER store path), `SignalStore.entries_without_memo`/`set_entry_memo`, `bind_structured`, `fetch_price_context` (for the IWM benchmark).
- Produces:
  - `author_pending_memos(*, signal_store, memo_store, thesis_llm, deadline=None, should_stop=None, now=None) -> int` (memos written) — for each `entries_without_memo()` row: re-derive the cluster's buys via `signal_store.buys_in_window(symbol, since=asof - CLUSTER_WINDOW_DAYS, until=asof)` (accessions, buyer names, dollars — the evidence inputs), then one structured call for a `MemoLiteDraft(thesis: str, must_be_true: list[str] (min_length=1), scenarios: list[ReturnScenario])`; code owns everything else: `thesis_type="event"`, `event_block=EventThesis(event_type="insider_cluster", seller_identity="open-market counterparties (buy-side signal; no forced seller)", why_non_economic="insiders bought with personal cash outside 10b5-1 plans — informed accumulation, not forced flow")`, evidence = one `EvidenceItem` per cluster accession (`source_type="filing"`, `source_ref=accession`, claim naming the buyer), falsifiers = the mechanical exits restated (`drawdown_from_cost_pct <= -20`, machine-checkable), `conviction_tier` = "starter" (BASIC) / "medium" (STRONG) — provenance only, sizing never reads it; `status="open"`, `vetting=None`. Failure per entry → journaled note, next entry.
  - `resolve_on_exit(*, memo_store, memo_id, entry_price: Decimal, exit_price: Decimal, entry_date: date, exit_date: date, reason: str, benchmark_fetcher=None) -> None` — builds `Resolution` with `realized_return_pct = float(exit/entry - 1)`, `benchmark_return_pct` from IWM closes over the same window (`fetch_price_context("IWM")`; unavailability ⇒ `0.0` + a note in `narrative`), `holding_days`, mechanical `outcome_label` (target → `thesis_right_made_money`, stop → `thesis_wrong_lost_money`, time-stop → by sign of realized return), `falsifiers_tripped=[0]` on stop. Called from the trade step's exit pass when the closed position's provenance carries a non-empty `memo_id`; resolution failure is journaled, never blocks the exit.

- [ ] **Step 1: Write the failing tests** — stub LLM (canned `MemoLiteDraft`): authoring writes an `open` memo with `insider_cluster` event block + accession-cited evidence into the store and sets the entry's memo_id; LLM raising → 0 memos, entry still pending, no exception. Resolution: target exit → `thesis_right_made_money` with correct `realized_return_pct`; stop exit → `thesis_wrong_lost_money` + `falsifiers_tripped=[0]`; benchmark fetcher raising → resolution still lands with `benchmark_return_pct == 0.0`.
- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`
- [ ] **Step 3: Implement** — author loop mirrors `vet_pending`'s deadline/stop-boxed shape (check stop/deadline BEFORE each entry). Resolution maps reason strings exactly as the trade step emits them (`"stop"`, `"target"`, `"time"`).
- [ ] **Step 4: Run** — `.venv/bin/pytest tests/ops/insider/ -v` → all PASS
- [ ] **Step 5: Commit**

```bash
git add ops/insider/memo_lite.py ops/insider/trading.py tests/ops/insider/test_memo_lite.py
git commit -m "feat(insider): memo-lite — non-gating overnight authoring, mechanical resolution"
```

---

### Task 6: config + daemon wiring

**Files:**
- Modify: `ops/config.py`, `ops/main.py`
- Test: extend the config test module; `tests/ops/test_main_insider.py` (create)

**Interfaces:**
- Produces:
  - `OpsConfig` fields (defaults XDG-aware like `_default_research_journal_path`): `insider_journal_path` (`.../insider_journal.sqlite`), `insider_memo_store_path` (`.../insider_memos.sqlite`), `insider_signal_store_path` (`.../insider_signals.sqlite`), `insider_starting_cash: Decimal = Decimal("10000")` (validated > 0). Env overrides `OPS_INSIDER_JOURNAL_PATH`, `OPS_INSIDER_MEMO_STORE_PATH`, `OPS_INSIDER_SIGNAL_STORE_PATH`, `OPS_INSIDER_STARTING_CASH`.
  - `_insider_scan_tick(journal, config)` — `CronTrigger(hour=0, minute=15)` daily; gate `journal.has_event_today(events.KIND_INSIDER_SCAN_RUN)`; runs `run_insider_scan`, journals one aggregated `insider_scan_run` (days, form4_seen, matches, recorded, errors); failure → `KIND_INSIDER_SCAN_ERROR`. No LLM, no ds4.
  - `_insider_trade_tick(journal, config)` — `CronTrigger(hour=16, minute=29, day_of_week="mon-fri")`; gate on `KIND_INSIDER_TRADE_RUN`; calls `trade_insider_sleeve(..., deny_list=config.deny_list)`; failure → `KIND_INSIDER_TRADE_ERROR`. No LLM.
  - Overnight memo stage: inside `_research_overnight_tick`'s while-loop, after the short stage (if the short-sleeve plan landed; otherwise after the research drain block), call `author_pending_memos(..., thesis_llm=build_stage_llm(config.research_thesis_model), deadline=deadline, should_stop=stop)` guarded like the other stages — it contributes to `progress`, failures journal `KIND_INSIDER_SCAN_ERROR`-style (`"insider_memo_error"` kind, add constant `KIND_INSIDER_MEMO_ERROR`), and it only builds the LLM if `signal_store.entries_without_memo()` is non-empty (never spins ds4 for an empty queue).

- [ ] **Step 1: Write the failing tests** — config defaults/env/validation; scan tick gates and journals the error kind on a raising scanner; trade tick likewise; overnight stage with empty memo queue builds no LLM (assert via a `build_llm` stub that fails the test if called).
- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement** — copy the established `_research_trade_tick` wrapper pattern; register both crons in `_start_full_scheduler` next to the research jobs (`ops/main.py:792`).
- [ ] **Step 4: Run** — `.venv/bin/pytest tests/ops/test_main_insider.py tests/ -k config -v` plus `-k overnight` → all PASS
- [ ] **Step 5: Commit**

```bash
git add ops/config.py ops/main.py ops/events.py tests/
git commit -m "feat(insider): config + daemon wiring — 00:15 scan, 16:29 trade, overnight memos"
```

---

### Task 7: overview section + docs

**Files:**
- Modify: `ops/notify/overview.py`, `docs/research_trading.md`
- Create: `docs/insider_sleeve.md`
- Test: extend `tests/ops/notify/test_overview.py`

**Interfaces:**
- Produces: `build_daily_overview(...)` accepts `insider_journal` (optional; `None` ⇒ not-configured line) and gains an insider-sleeve section: today's `insider_trade_run` (entered with strengths, exited with reasons), latest `insider_run` equity snapshot, today's `insider_scan_run` counts, any `insider_*_error` events. Journal-only, mirroring the research section.

- [ ] **Step 1: Write the failing tests** — overview with a seeded insider journal mentions equity, an entry, and scan counts; `None` journal → not-configured; errors surface.
- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement** — `_insider_section(...)` following the existing section pattern; thread the journal at the `ops/main.py` overview-tick call site.
- [ ] **Step 4: Write `docs/insider_sleeve.md`** — one page: signal definition (cluster rules + strength table), what runs when (00:15 scan, 16:29 trade, overnight memo stage), fences and exits, the memo-is-passenger experiment rationale, manual commands. Add rows to `docs/research_trading.md`'s "what runs when" table.
- [ ] **Step 5: Full-suite check + commit**

Run: `.venv/bin/pytest tests/ --ignore=tests/test_main.py -q`
Expected: all PASS

```bash
git add ops/notify/overview.py ops/main.py docs/insider_sleeve.md docs/research_trading.md tests/
git commit -m "feat(insider): daily-overview section + insider-sleeve runbook"
```
