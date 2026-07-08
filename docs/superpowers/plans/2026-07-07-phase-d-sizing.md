# Phase D: Sizing + Calibration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open memos become sized paper positions on their own third ledger under hard mechanical caps (name-at-cost, sector, ADV); exits fire from Phase C signals (falsifier trips, price targets, resolution); resolution becomes a one-command human act with the arithmetic automated; and `ops research report` renders the calibration corpus (outcome 2×2, scenario calibration, bought-vs-passed, sleeve-vs-baseline, per-model attribution) as markdown.

**Architecture:** A pure sizing module (`ops/research/sizing.py`) turns a memo's conviction tier into a notional and enforces the three spec fences mechanically — the spec's "guardrails profile" is implemented as this dedicated fence module rather than new `Rule` subclasses, because the ops guardrail chain's `RuleContext(order, broker, config)` cannot see sector/ADV/memo without invasive threading, and the sleeve's sell rules live in memos, not broker stops (the chain's `StopAttachedRule` would force stops the design forbids). A deterministic trading step (`ops/research/trading.py`) runs post-close after the Phase C monitor: exits first (memo resolved / falsifier tripped / price target hit), then entries (open memos without positions, tier-sized, fenced), all on a `PaperBroker` over the new `research_journal.sqlite` — mirroring the baseline's journal-scoped pattern, with provenance events linking every position to its memo. Memos gain a single additive `authored_by_model` field (populated from the config's model-spec string — never introspected from LangChain objects) so the report can attribute outcomes per model. The report follows `ops/status.py`'s dict-builder + string-renderer split. **No LLM runs anywhere in this phase.**

**Tech Stack:** Python 3.10+, stdlib, Pydantic (one additive Memo field), APScheduler (existing daemon), SQLite journals, yfinance via the existing paced helpers (ADV + benchmark closes), pytest (all network/quotes mocked).

**Spec:** `docs/superpowers/specs/2026-07-06-finish-research-system-design.md`, section "Phase D — Sizing + calibration". Companion: `docs/long_horizon_research.md` build-order steps 7–8 (embedding lookup in step 8 stays DEFERRED — corpus too small; only the calibration report is built). Read both once before starting.

## Global Constraints

- Work happens in `/Users/frednick/Code/TradingAgents` on a new branch `feat/phase-d-sizing` cut from **`feat/phase-c-loop`** (@ d288def — Phase D consumes Phase C's monitor/events; PR #16 is open but unmerged, so this branch STACKS on it; its PR will target `feat/phase-c-loop` until #16 merges). Never commit to `main`; never `git checkout main` (it lives in the deploy worktree).
- The working tree may contain unrelated user-modified files (`main.py` at repo root, `tradingagents/dataflows/reddit.py`) — NEVER stage them; explicit file lists only, never `git add -A` / `git add .`.
- Lint: `ruff check <files you touched>` passes (line-length 100, py310+). Pre-existing errors in untouched files are not yours to fix.
- Tests: pytest; new test modules set `pytestmark = pytest.mark.unit`; ALL network, quotes, and EDGAR mocked/injected. Full suite green before every commit: `.venv/bin/python -m pytest tests/ -q` (baseline before this plan: **1443 passed, 13 skipped**, 69 subtests).
- Money math in `Decimal` end-to-end on the trading path (`_quantize_money` for cents); report percentages/ratios may be floats (calibration data).
- **No LLM calls in this phase.** LLM-stated probabilities are NEVER sizing inputs (locked decision — sizing reads `conviction_tier` only). No code path added here may require an API key or `SEC_EDGAR_USER_AGENT` to import or test.
- The research sleeve trades ONLY `research_journal.sqlite` — never the momentum journal (`ops_journal`), never `baseline_journal`. Notify-policy events (trade-run summary) go to the MAIN journal (the dispatcher polls only it); per-position provenance events go to the RESEARCH journal.
- Every new event kind registered in `ops/events.py` `BUILDERS` + exactly one of `POLICY`/`AUDIT_ONLY` (enforcement test).
- The Memo schema change is EXACTLY one additive optional field (`authored_by_model: str = ""`) — nothing else in `tradingagents/memos/schema.py` may change, and old stored memos must deserialize unchanged (the default covers them).
- Never run `launchctl`. No new launchd jobs in this phase (the trading step rides the existing daemon; the report is on-demand).
- **Escalation rule for the implementer:** if an instruction contradicts what you find in the code, STOP and report BLOCKED with details. Do not improvise around it.

## File structure (what this plan touches)

| File | Task | Responsibility |
|---|---|---|
| `ops/config.py` | 1 | `research_journal_path` + `research_starting_cash` config |
| `tradingagents/memos/schema.py`, `ops/research/brain.py`, `ops/cli.py` | 2 | `authored_by_model` attribution field, threaded from config |
| `ops/events.py`, `ops/notify/policy.py` | 3 | 4 new event kinds (trade run, position opened/closed, trade error) |
| `ops/research/sizing.py` (new) | 4 | tier sizing + the three hard fences (pure) |
| `ops/research/trading.py` (new) | 5 | the post-close trade step: exits → entries → snapshot → summary |
| `ops/main.py`, `ops/cli.py` | 6 | daemon job 16:25 + `ops research trade` CLI |
| `ops/cli.py`, `ops/research/resolution.py` (new) | 7 | `ops research resolve` — computed Resolution, human label |
| `ops/research/report.py` (new), `ops/cli.py` | 8 | `ops research report` — markdown calibration report |
| `docs/research_trading.md` (new), `docs/long_horizon_research.md`, `docs/research_monitor.md` | 9 | runbook, build-order checkmarks, PR |

## Key repo facts (verified 2026-07-07 @ d288def — re-verify signatures you depend on before coding)

- `OpsConfig` (`ops/config.py`): `baseline_journal_path` pattern to mirror — `_default_baseline_journal_path()` free function → `~/.local/state/tradingagents/baseline_journal.sqlite`, frozen-dataclass field with `default_factory`, env `OPS_BASELINE_JOURNAL_PATH` in `load_config()`, `__post_init__` validation. `baseline_starting_cash: Decimal = 100000`. Already present: `memo_store_path`, `screen_store_path`, `journal_path`, `research_evidence_model`/`research_thesis_model` (spec strings `"provider:model[@base_url]"`).
- `tradingagents/memos/schema.py`: `Memo` has `memo_id, ticker, thesis_type, status ("open"|"passed"|"resolved"), conviction_tier ("starter"|"medium"|"high"), entry_price_ref: float, as_of_date, created_at, price_target_low/high: float, expected_holding_months, scenarios: list[ReturnScenario(probability, return_pct, description)], falsifiers, catalysts, precedent_memo_ids, resolution`. `Resolution(resolved_at: datetime, exit_price: float|None, realized_return_pct: float, benchmark_return_pct: float, holding_days: int, outcome_label: OutcomeLabel, falsifiers_tripped: list[int], catalysts_realized: list[int], narrative: str)`. `OutcomeLabel` = the 2×2: `thesis_right_made_money | thesis_right_lost_money | thesis_wrong_made_money | thesis_wrong_lost_money`. **No model-attribution field exists (Task 2 adds it).**
- `MemoStore` (`tradingagents/memos/store.py`): `open_memos()`, `resolved_corpus()` (resolved, oldest-first), `list(ticker=, status=, thesis_type=)` (newest-first), `get(memo_id)`, `resolve(memo_id, resolution) -> Memo` (raises KeyError/ValueError), `mark_passed`, `due_for_resolution(as_of=None)`. Index columns for raw SQL: `memo_id, ticker, thesis_type, status, conviction_tier, created_at, as_of_date, resolved_at, outcome_label`.
- `ops/research/brain.py`: memo assembly at `research_hit` — `Memo(ticker=symbol, as_of_date=today, entry_price_ref=float(price), evidence=kept, status="open", **draft.model_dump(exclude={"recommendation"}))` then `memo_store.save(memo)`. `research_hit(hit, *, evidence_llm, thesis_llm, memo_store, list_filings=None, fetch_text=None, price_fetcher=None, today=None)`. CLI call site in `ops/cli.py` `research_run` has `config.research_thesis_model` in scope.
- `PaperBroker.from_journal(*, journal, quote_source, starting_cash)` (`ops/broker/paper.py`); `QuoteSource = Callable[[str], Decimal]`; `broker.get_quote(symbol) -> Decimal` raises `QuoteUnavailable`; `broker.get_positions() -> list[Position]` (fields incl. `symbol`, `quantity`, `avg_entry_price` — verify in `ops/broker/base.py`); `broker.get_equity()`, `get_cash()`; `broker.close_position(symbol) -> Fill`; `broker.place_order(Order)` raises `QuoteUnavailable`/`InsufficientFunds`. `Order(client_order_id, symbol, side: Side.BUY/SELL, notional_dollars: Decimal > 0, order_type=OrderType.MARKET)` — `stop_pct` stays None (no broker stops on this sleeve; sell rules live in memos).
- Baseline precedent (`ops/research/baseline.py`): journal-scoped `with Journal(path) as j:` + `PaperBroker.from_journal` + coid `f"baseline-{asof}-{symbol}-{uuid4().hex[:8]}"` + `_MIN_ORDER_DOLLARS = Decimal("100")` + per-order `except QuoteUnavailable: continue` / `except InsufficientFunds: break` + `journal.record_equity_snapshot(kind="baseline_run", equity=..., cash=..., at=now)`.
- `_quantize_money(d: Decimal) -> Decimal` in `ops/strategy/post_earnings_momentum.py:33` (cents quantize) — copy the one-liner into sizing.py rather than importing across sleeves (verify its body first).
- Sector: `load_smallcap_members(*, fetch=None) -> list[SmallcapMember]` (`ops/universe/smallcap.py:98`), `SmallcapMember.sector: str`; quarterly-cached to `~/.cache/tradingagents/smallcap_universe.json`. No single-ticker sector fetch — a ticker absent from the cache gets sector `"UNKNOWN"`.
- ADV: `fetch_price_and_adv_from_yfinance(symbol) -> tuple[Decimal, Decimal] | None` (`ops/universe/filters.py:37`) returns `(last_price, avg_dollar_vol_20d)`, paced, None on failure.
- Journal (`ops/journal.py`): `record_event(kind, payload, *, at=None)`; `has_event_today(kind, *, now=None)`; `count_events(kind, *, since=None, payload_equals=None)` (string matching); `latest_event_payload_by_symbol(kind) -> dict[str, dict]` (last event per `payload["symbol"]`); `record_equity_snapshot(*, kind, equity, cash, at=None, note=None)`; `read_equity_snapshots() -> list[dict]` (`at, kind, equity, cash, note`, id order, caller filters by kind); `last_buy_fill_for(symbol) -> dict|None` (`"price"`, `"filled_at"`).
- Phase C events in the MAIN journal: `KIND_FALSIFIER_TRIPPED` payload has `memo_id`, `ticker`; `KIND_RESEARCH_MONITOR_RUN` gates the 16:20 monitor. Daemon jobs (`ops/main.py::_start_full_scheduler`): `daily_summary` 16:05, `research_monitor` 16:20 (registered only when `config is not None`; wrapper `_research_monitor_tick(journal, config)` pattern: gate inside try, errors → `KIND_RESEARCH_MONITOR_ERROR` event).
- Benchmark closes: `fetch_price_context(symbol)` (`ops/research/prices.py`) works for any ticker incl. `"IWM"`; `PriceContext.close_on_or_before(when)`. Nothing computes `benchmark_return_pct` today (test fixtures hardcode it).
- Report precedent: `ops/status.py` — `build_status(journal, config, *, now=None) -> dict` + `format_status(dict) -> str` + thin CLI. `decide-once` echoes literal `#`/`##` markdown headers.
- CLI research group: `write-off`, `run`, `monitor` — new subcommands follow `monitor`'s shape (load_config → scoped Journal → call function → echo).

---

### Task 1: Research-ledger config

**Files:**
- Modify: `ops/config.py`
- Test: extend `tests/ops/test_config.py`

**Interfaces (Tasks 5–8 rely on these exact names):**
- `OpsConfig.research_journal_path: str` — default `${XDG_STATE_HOME:-~/.local/state}/tradingagents/research_journal.sqlite` via a `_default_research_journal_path()` helper mirroring `_default_baseline_journal_path` EXACTLY; env override `OPS_RESEARCH_JOURNAL_PATH`.
- `OpsConfig.research_starting_cash: Decimal = Decimal("100000")`; env `OPS_RESEARCH_STARTING_CASH` through the file's `_env_decimal` helper; `__post_init__` rejects `<= 0` (mirror the `baseline_starting_cash` validation line).

- [ ] **Step 1: Failing tests.** Read `tests/ops/test_config.py` first; follow its env-override test pattern exactly. Append:

```python
def test_research_journal_env_overrides(monkeypatch):
    monkeypatch.setenv("OPS_RESEARCH_JOURNAL_PATH", "/tmp/research.sqlite")
    monkeypatch.setenv("OPS_RESEARCH_STARTING_CASH", "50000")
    config = load_config()
    assert config.research_journal_path == "/tmp/research.sqlite"
    assert config.research_starting_cash == Decimal("50000")


def test_research_journal_defaults(monkeypatch):
    monkeypatch.delenv("OPS_RESEARCH_JOURNAL_PATH", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/state")
    config = load_config()
    assert config.research_journal_path == "/tmp/state/tradingagents/research_journal.sqlite"
    assert config.research_starting_cash == Decimal("100000")


def test_nonpositive_research_cash_rejected():
    with pytest.raises(ValueError):
        OpsConfig(research_starting_cash=Decimal("0"))
```

(Adapt: if `_default_baseline_journal_path` does NOT honor `XDG_STATE_HOME`, mirror whatever it actually does and adjust the default-path test to match — the binding contract is "identical mechanism to the baseline journal path", not the literal XDG string.)

Run: `.venv/bin/python -m pytest tests/ops/test_config.py -v` — Expected: new tests FAIL (`AttributeError`/`TypeError`).

- [ ] **Step 2: Implement** — helper next to `_default_baseline_journal_path` (same body shape, `research_journal.sqlite` filename), dataclass fields after `baseline_starting_cash`, env blocks in `load_config()` next to the baseline ones, validation line in `__post_init__` next to the `baseline_starting_cash` one.

- [ ] **Step 3: Run tests, full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ops/test_config.py -v && .venv/bin/python -m pytest tests/ -q
ruff check ops/config.py tests/ops/test_config.py
git add ops/config.py tests/ops/test_config.py
git commit -m "feat(research): third ledger config — research_journal_path + research_starting_cash"
```

End every commit message in this plan with EXACTLY this trailer (verbatim, never substitute another model name):

```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

### Task 2: Per-model attribution field

**Files:**
- Modify: `tradingagents/memos/schema.py` (ONE additive field), `ops/research/brain.py`, `ops/cli.py` (research_run call site)
- Test: extend `tests/ops/research/test_brain.py`; extend `tests/test_memo_store.py` (backward-compat)

**Interfaces (Task 8 relies on these exact names):**
- `Memo.authored_by_model: str = ""` — the thesis-stage model spec string (`"provider:model[@base_url]"`), empty for pre-Phase-D memos. Placed with the other metadata fields; `Field(description=...)` matching the file's style. NOTHING else in the schema changes.
- `research_hit(..., thesis_model_spec: str | None = None)` — new optional keyword; when set, the assembled `Memo` gets `authored_by_model=thesis_model_spec`. The CLI threads `config.research_thesis_model`. **The config string is the source of truth — never introspect the LangChain object** (provider-specific attributes, unreliable).

- [ ] **Step 1: Failing tests.** Append to `tests/ops/research/test_brain.py` (reuse its `_run`/FakeLLM helpers — read the file first; note `_run` calls `research_hit` internally, so add the kwarg pass-through there or call `research_hit` directly in the new test):

```python
def test_authored_by_model_recorded(memo_store):
    thesis_llm = FakeLLM(["bear case", _draft()])
    outcome = research_hit(
        _hit(), evidence_llm=_good_evidence_llm(), thesis_llm=thesis_llm,
        memo_store=memo_store,
        list_filings=lambda ticker, **kw: FILINGS,
        fetch_text=lambda f, **kw: TEXTS[f.accession_number],
        price_fetcher=_price_fetcher, today=TODAY,
        thesis_model_spec="openai_compatible:deepseek-v4-flash@http://127.0.0.1:8000/v1",
    )
    assert outcome.status == "researched"
    memo = memo_store.get(outcome.memo_id)
    assert memo.authored_by_model == "openai_compatible:deepseek-v4-flash@http://127.0.0.1:8000/v1"


def test_authored_by_model_defaults_empty(memo_store):
    thesis_llm = FakeLLM(["bear case", _draft()])
    outcome = _run(_good_evidence_llm(), thesis_llm, memo_store)
    assert memo_store.get(outcome.memo_id).authored_by_model == ""
```

Append to `tests/test_memo_store.py` (backward compatibility — old rows must deserialize):

```python
def test_memo_without_authored_by_model_deserializes(tmp_path):
    # Simulate a pre-Phase-D stored payload: dump a memo, strip the new field,
    # write the row back raw, read it through the store.
    import json
    import sqlite3

    store = MemoStore(tmp_path / "memos.sqlite")
    memo = _memo()  # reuse the file's existing memo fixture/helper name
    store.save(memo)
    payload = json.loads(memo.model_dump_json())
    payload.pop("authored_by_model", None)
    with sqlite3.connect(tmp_path / "memos.sqlite") as conn:
        conn.execute("UPDATE memos SET payload = ? WHERE memo_id = ?",
                     (json.dumps(payload), memo.memo_id))
    loaded = store.get(memo.memo_id)
    assert loaded is not None
    assert loaded.authored_by_model == ""
```

(Adapt the memo fixture name to the file's existing helper.) Run both — Expected: FAIL.

- [ ] **Step 2: Implement.** Schema: one field after `precedent_memo_ids` (or wherever metadata fields cluster — read the class):

```python
    authored_by_model: str = Field(
        default="",
        description=(
            "Model spec (provider:model[@base_url]) of the thesis stage that "
            "authored this memo; empty for memos predating attribution. "
            "Report-time attribution only — never a sizing or monitoring input."
        ),
    )
```

Brain: add `thesis_model_spec: str | None = None` to `research_hit`'s signature; in the `Memo(...)` assembly add `authored_by_model=thesis_model_spec or ""`. CLI: in `research_run`, pass `thesis_model_spec=config.research_thesis_model` at the `research_hit(...)` call.

- [ ] **Step 3: Run tests, full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ops/research/test_brain.py tests/test_memo_store.py -v && .venv/bin/python -m pytest tests/ -q
ruff check tradingagents/memos/schema.py ops/research/brain.py ops/cli.py tests/ops/research/test_brain.py tests/test_memo_store.py
git add tradingagents/memos/schema.py ops/research/brain.py ops/cli.py tests/ops/research/test_brain.py tests/test_memo_store.py
git commit -m "feat(memos): authored_by_model attribution field, threaded from the thesis model spec"
```

---

### Task 3: Trading event kinds

**Files:**
- Modify: `ops/events.py`, `ops/notify/policy.py`
- Test: extend the same test file Task 3 of Phase C used for kind registration (find `test_phase_c_monitoring_kinds_registered` and add a sibling)

**Interfaces (Tasks 5–6 rely on these exact names):**

| constant | value | policy | payload builder kwargs |
|---|---|---|---|
| `KIND_RESEARCH_TRADE_RUN` | `"research_trade_run"` | POLICY: `_PUSH_ONLY` (push/normal) — the user learns the sleeve traded | `asof, entered: list[str], exited: list[str], skipped: list[str], equity: str, cash: str` |
| `KIND_RESEARCH_TRADE_ERROR` | `"research_trade_error"` | AUDIT_ONLY | `error: str` |
| `KIND_RESEARCH_POSITION_OPENED` | `"research_position_opened"` | AUDIT_ONLY (lives in the research journal; provenance) | `symbol, memo_id, conviction_tier, entry_date: str, client_order_id, notional: str` |
| `KIND_RESEARCH_POSITION_CLOSED` | `"research_position_closed"` | AUDIT_ONLY | `symbol, memo_id, reason, exit_date: str, price: str` |

`symbol`/`memo_id` as strings (json_extract / `latest_event_payload_by_symbol` compatibility). Builders/registration follow the file's exact conventions; the notified kind needs a `render()` branch only if the generic `_kv_body` fallback is unused for new kinds — check how Phase C's four notified kinds were rendered and match.

- [ ] **Step 1: Failing test** (sibling of the Phase C registration test):

```python
def test_phase_d_trading_kinds_registered():
    from ops import events
    from ops.notify.policy import POLICY

    assert POLICY[events.KIND_RESEARCH_TRADE_RUN].urgency == "normal"
    for kind in (
        events.KIND_RESEARCH_TRADE_ERROR,
        events.KIND_RESEARCH_POSITION_OPENED,
        events.KIND_RESEARCH_POSITION_CLOSED,
    ):
        assert kind in events.AUDIT_ONLY
        assert kind not in POLICY
    for kind in (
        events.KIND_RESEARCH_TRADE_RUN, events.KIND_RESEARCH_TRADE_ERROR,
        events.KIND_RESEARCH_POSITION_OPENED, events.KIND_RESEARCH_POSITION_CLOSED,
    ):
        assert kind in events.BUILDERS
```

Run — Expected: FAIL (`AttributeError`).

- [ ] **Step 2: Implement** (constants group `# --- Research sleeve trading (Phase D) ---`, four builders, BUILDERS entries, AUDIT_ONLY additions, one POLICY entry with a comment). Confirm the notify enforcement test passes (it walks POLICY/BUILDERS automatically).

- [ ] **Step 3: Run tests, full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ops/notify/ -v && .venv/bin/python -m pytest tests/ -q
ruff check ops/events.py ops/notify/policy.py
git add ops/events.py ops/notify/policy.py <the test file from Step 1>
git commit -m "feat(ops): Phase D research-trading event kinds + notify policy"
```

(Replace `<the test file from Step 1>` with the real path you appended to.)

---

### Task 4: Sizing + the three fences (`ops/research/sizing.py`)

**Files:**
- Create: `ops/research/sizing.py`
- Test: `tests/ops/research/test_sizing.py`

**Interfaces (Task 5 relies on these exact names):**
- `TIER_SIZING: dict[str, Decimal]` = `{"starter": Decimal("0.02"), "medium": Decimal("0.04"), "high": Decimal("0.06")}` — within the spec bands (starter 1–2%, medium 3–5%, high 5–8%); conservative-mid picks.
- `NAME_CAP_PCT = Decimal("0.10")` (single name ≤10% of research equity **at cost**), `SECTOR_CAP_PCT = Decimal("0.25")`, `ADV_CAP_PCT = Decimal("0.05")` (position ≤5% of 20-day dollar ADV — exitable at small-cap liquidity), `MIN_ORDER_DOLLARS = Decimal("100")`.
- `@dataclass(frozen=True) SizingDecision: notional: Decimal; rejected: str | None` — `rejected` is a human-readable fence name + numbers when the order must NOT be placed; `notional` is the (possibly fence-clamped) amount when `rejected is None`.
- `size_entry(*, tier: str, equity: Decimal, cash: Decimal, cost_by_symbol: dict[str, Decimal], symbol: str, sector: str, cost_by_sector: dict[str, Decimal], adv_20d: Decimal | None) -> SizingDecision`
- `cost_basis(positions) -> tuple[dict[str, Decimal], Decimal]` — `({symbol: quantity × avg_entry_price}, total)`; sector aggregation happens in the caller (Task 5) which knows sectors.

**Fence semantics (binding):**
1. Base notional = `_quantize_money(equity × TIER_SIZING[tier])`, clamped to available `cash`.
2. **Name-at-cost:** `existing cost_by_symbol.get(symbol, 0) + notional ≤ NAME_CAP_PCT × equity`; clamp notional down to the remaining headroom; if headroom `< MIN_ORDER_DOLLARS` → rejected `"name cap"`.
3. **Sector:** `cost_by_sector.get(sector, 0) + notional ≤ SECTOR_CAP_PCT × equity`; clamp; below floor → rejected `"sector cap"`. Sector `"UNKNOWN"` is a real bucket (unknown-sector names compete for the same 25% — conservative, no network).
4. **ADV:** `notional ≤ ADV_CAP_PCT × adv_20d`; clamp; below floor → rejected `"adv cap"`. `adv_20d is None` (fetch failed) → rejected `"adv unavailable"` — never enter blind on liquidity.
5. Anything below `MIN_ORDER_DOLLARS` after all clamps → rejected. Order of application: tier → cash → name → sector → ADV (each clamps the running notional).

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for research-sleeve sizing fences (pure, no I/O)."""

from decimal import Decimal

import pytest

from ops.research.sizing import (
    ADV_CAP_PCT,
    MIN_ORDER_DOLLARS,
    NAME_CAP_PCT,
    SECTOR_CAP_PCT,
    TIER_SIZING,
    SizingDecision,
    size_entry,
)

pytestmark = pytest.mark.unit

EQUITY = Decimal("100000")


def _size(**overrides):
    kwargs = dict(
        tier="medium", equity=EQUITY, cash=Decimal("50000"),
        cost_by_symbol={}, symbol="WIDG", sector="Industrials",
        cost_by_sector={}, adv_20d=Decimal("5000000"),
    )
    kwargs.update(overrides)
    return size_entry(**kwargs)


def test_tier_sizing_within_spec_bands():
    assert Decimal("0.01") <= TIER_SIZING["starter"] <= Decimal("0.02")
    assert Decimal("0.03") <= TIER_SIZING["medium"] <= Decimal("0.05")
    assert Decimal("0.05") <= TIER_SIZING["high"] <= Decimal("0.08")


def test_base_sizing_by_tier():
    assert _size(tier="starter").notional == Decimal("2000.00")
    assert _size(tier="medium").notional == Decimal("4000.00")
    assert _size(tier="high").notional == Decimal("6000.00")
    assert _size().rejected is None


def test_cash_clamps():
    d = _size(tier="high", cash=Decimal("2500"))
    assert d.notional == Decimal("2500.00") and d.rejected is None


def test_name_cap_at_cost_clamps_then_rejects():
    # Existing WIDG cost 9k of a 10k cap: headroom 1k >= floor -> clamp.
    d = _size(cost_by_symbol={"WIDG": Decimal("9000")})
    assert d.notional == Decimal("1000.00") and d.rejected is None
    # 9.95k of 10k: headroom 50 < MIN_ORDER_DOLLARS -> reject.
    d = _size(cost_by_symbol={"WIDG": Decimal("9950")})
    assert d.rejected is not None and "name" in d.rejected


def test_sector_cap():
    d = _size(cost_by_sector={"Industrials": Decimal("24000")})
    assert d.notional == Decimal("1000.00")
    d = _size(cost_by_sector={"Industrials": Decimal("25000")})
    assert d.rejected is not None and "sector" in d.rejected
    # Different sector unaffected.
    assert _size(cost_by_sector={"Tech": Decimal("25000")}).rejected is None


def test_adv_cap_and_unavailable():
    # 5% of 60k ADV = 3k < the 4k medium base -> clamp.
    d = _size(adv_20d=Decimal("60000"))
    assert d.notional == Decimal("3000.00")
    d = _size(adv_20d=Decimal("1000"))  # 5% = 50 < floor
    assert d.rejected is not None and "adv" in d.rejected
    d = _size(adv_20d=None)
    assert d.rejected is not None and "unavailable" in d.rejected


def test_unknown_sector_is_a_real_bucket():
    d = _size(sector="UNKNOWN", cost_by_sector={"UNKNOWN": Decimal("25000")})
    assert d.rejected is not None


def test_unknown_tier_rejected():
    d = _size(tier="yolo")
    assert d.rejected is not None and "tier" in d.rejected
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest tests/ops/research/test_sizing.py -v` — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""Conviction-tier sizing + hard fences for the research sleeve (Phase D).

The spec's "guardrails profile": implemented as a pure fence module rather
than ops/guardrails Rule subclasses because the Rule chain's context
(order, broker, config) cannot see sector, ADV, or the memo — and the
sleeve's sell rules live in memos, not broker stops. Every fence is
mechanical; LLM-stated probabilities are NEVER inputs (locked decision) —
the only research-quality signal used is the memo's conviction_tier.

All money in Decimal. Rejections carry the fence name + numbers so the
trade-run summary can say exactly why a memo produced no position.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

TIER_SIZING: dict[str, Decimal] = {
    "starter": Decimal("0.02"),
    "medium": Decimal("0.04"),
    "high": Decimal("0.06"),
}
NAME_CAP_PCT = Decimal("0.10")     # single name <= 10% of research equity at cost
SECTOR_CAP_PCT = Decimal("0.25")   # sector <= 25% at cost
ADV_CAP_PCT = Decimal("0.05")      # position <= 5% of 20-day dollar ADV
MIN_ORDER_DOLLARS = Decimal("100")


def _quantize_money(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"))


@dataclass(frozen=True)
class SizingDecision:
    notional: Decimal
    rejected: str | None = None


def cost_basis(positions) -> tuple[dict[str, Decimal], Decimal]:
    """{symbol: quantity * avg_entry_price} and the total, from live positions."""
    by_symbol = {p.symbol: p.quantity * p.avg_entry_price for p in positions}
    return by_symbol, sum(by_symbol.values(), Decimal("0"))


def size_entry(
    *,
    tier: str,
    equity: Decimal,
    cash: Decimal,
    cost_by_symbol: dict[str, Decimal],
    symbol: str,
    sector: str,
    cost_by_sector: dict[str, Decimal],
    adv_20d: Decimal | None,
) -> SizingDecision:
    pct = TIER_SIZING.get(tier)
    if pct is None:
        return SizingDecision(Decimal("0"), f"unknown tier {tier!r}")
    notional = _quantize_money(equity * pct)
    notional = min(notional, cash)

    name_room = NAME_CAP_PCT * equity - cost_by_symbol.get(symbol, Decimal("0"))
    if name_room < MIN_ORDER_DOLLARS:
        return SizingDecision(Decimal("0"), (
            f"name cap: {symbol} cost {cost_by_symbol.get(symbol, Decimal('0'))} "
            f"leaves {name_room:.2f} of {NAME_CAP_PCT * equity:.2f}"
        ))
    notional = min(notional, _quantize_money(name_room))

    sector_room = SECTOR_CAP_PCT * equity - cost_by_sector.get(sector, Decimal("0"))
    if sector_room < MIN_ORDER_DOLLARS:
        return SizingDecision(Decimal("0"), (
            f"sector cap: {sector} cost {cost_by_sector.get(sector, Decimal('0'))} "
            f"leaves {sector_room:.2f} of {SECTOR_CAP_PCT * equity:.2f}"
        ))
    notional = min(notional, _quantize_money(sector_room))

    if adv_20d is None:
        return SizingDecision(Decimal("0"), f"adv unavailable for {symbol}")
    adv_room = _quantize_money(ADV_CAP_PCT * adv_20d)
    if adv_room < MIN_ORDER_DOLLARS:
        return SizingDecision(Decimal("0"), (
            f"adv cap: 5% of 20d ADV {adv_20d:.0f} = {adv_room} below floor"
        ))
    notional = min(notional, adv_room)

    if notional < MIN_ORDER_DOLLARS:
        return SizingDecision(Decimal("0"), f"below floor after fences ({notional})")
    return SizingDecision(notional)
```

- [ ] **Step 4: Run tests to verify they pass** — Expected: 9 passed.

- [ ] **Step 5: Full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ -q
ruff check ops/research/sizing.py tests/ops/research/test_sizing.py
git add ops/research/sizing.py tests/ops/research/test_sizing.py
git commit -m "feat(research): conviction-tier sizing + name/sector/ADV hard fences"
```

---

### Task 5: The trade step (`ops/research/trading.py`)

**Files:**
- Create: `ops/research/trading.py`
- Test: `tests/ops/research/test_trading.py`

**Interfaces:**
- Consumes: `size_entry`/`cost_basis`/`TIER_SIZING` (Task 4); event kinds (Task 3); `MemoStore.open_memos`/`get`; `PaperBroker.from_journal`; `latest_event_payload_by_symbol`; `count_events` (falsifier trips from the MAIN journal); `load_smallcap_members` (sector); `fetch_price_and_adv_from_yfinance` (ADV).
- Produces (Task 6 relies on these exact names):
  - `@dataclass TradeOutcome: asof: str; entered: list[str] = field(default_factory=list); exited: list[str] = field(default_factory=list); skipped: list[str] = field(default_factory=list); errors: list[str] = field(default_factory=list); equity: Decimal = Decimal("0"); cash: Decimal = Decimal("0")` — `skipped` entries are `"SYMBOL: reason"` strings (fence rejections, quote failures).
  - `trade_research_sleeve(*, memo_store, research_journal, main_journal, quote_source, starting_cash: Decimal, asof: date, now: datetime | None = None, sector_lookup=None, adv_fetcher=None) -> TradeOutcome`

**Behavior (binding):**

1. Build `broker = PaperBroker.from_journal(journal=research_journal, quote_source=quote_source, starting_cash=starting_cash)`. Load provenance: `prov = research_journal.latest_event_payload_by_symbol(events.KIND_RESEARCH_POSITION_OPENED)`.
2. **Exits first.** For every held position: find its memo via `prov.get(symbol, {}).get("memo_id")` → `memo_store.get(memo_id)`. Exit reasons, first match wins:
   - `memo is None or memo.status == "resolved"` → reason `"resolved"` (or `"memo missing"`),
   - the MAIN journal has a falsifier trip for this memo since entry: `main_journal.count_events(events.KIND_FALSIFIER_TRIPPED, payload_equals={"memo_id": memo_id}) > 0` → reason `"falsifier tripped"`,
   - current quote ≥ `memo.price_target_high` → reason `"target hit"` (quote via `broker.get_quote(symbol)`; `QuoteUnavailable` → skip with note, the Phase C delist machinery is baseline-only and human handles research delistings v1).
   Exit = `broker.close_position(symbol)`; journal `KIND_RESEARCH_POSITION_CLOSED` (research journal, `at=now`, price from the returned Fill — verify the `Fill` field name for price in `ops/broker/types.py`; if absent use the pre-close quote). One `except QuoteUnavailable`/generic per position: note in `errors`, continue.
3. **Entries.** For every memo in `memo_store.open_memos()` (oldest-first — sort by `created_at`) whose ticker has NO current position and NO exit this run: sector = `sector_lookup(ticker)`, adv = `adv_fetcher(ticker)`; build `cost_by_symbol`/total via `cost_basis(broker.get_positions())` and `cost_by_sector` by mapping each held symbol through `sector_lookup` (memoize lookups); `decision = size_entry(tier=memo.conviction_tier, equity=broker.get_equity(), cash=broker.get_cash(), ...)`. Rejected → `skipped.append(f"{ticker}: {decision.rejected}")`. Else place `Order(client_order_id=f"research-{asof.isoformat()}-{ticker}-{uuid4().hex[:8]}", symbol=ticker, side=Side.BUY, notional_dollars=decision.notional, order_type=OrderType.MARKET)`; journal `KIND_RESEARCH_POSITION_OPENED` (research journal, `at=now`, memo_id/tier/entry_date/coid/notional). `QuoteUnavailable` → skip+note; `InsufficientFunds` → stop entering. Recompute cost dicts after each fill (the fences see the running book).
4. **Snapshot + summary.** `research_journal.record_equity_snapshot(kind="research_run", equity=broker.get_equity(), cash=broker.get_cash(), at=now)`; then `main_journal.record_event(events.KIND_RESEARCH_TRADE_RUN, ..., at=now)` (this event is ALSO the daemon's once-per-day gate and the user's push). Equity via a quote-failure-tolerant path: if `get_equity()` raises `QuoteUnavailable`, fall back to cash + cost basis with a note (mirror the spirit of baseline's `_equity_with_fallback` — read it first).
5. Defaults: `sector_lookup` = build once from `load_smallcap_members()` → `{m.symbol: m.sector}` with `.get(ticker, "UNKNOWN")` (lazy import, wrapped: on ANY exception every ticker is `"UNKNOWN"` + one note); `adv_fetcher` = `lambda t: (fetch_price_and_adv_from_yfinance(t) or (None, None))[1]` (lazy import). All record_event calls pass `at=now`.

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the research-sleeve trade step (no network; injected quotes)."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ops import events
from ops.journal import Journal
from ops.research.trading import TradeOutcome, trade_research_sleeve
from tradingagents.memos.schema import EvidenceItem, Falsifier, Memo, ValueThesis
from tradingagents.memos.store import MemoStore

pytestmark = pytest.mark.unit

TODAY = date(2026, 7, 7)
NOW = datetime(2026, 7, 7, 20, 30, tzinfo=timezone.utc)


def _memo(ticker="WIDG", *, tier="medium", targets=(15.0, 20.0)):
    return Memo(
        ticker=ticker, as_of_date=date(2026, 7, 1), thesis_type="value",
        thesis="Mispriced.", conviction_tier=tier,
        evidence=[EvidenceItem(claim="c", source_type="filing", source_ref="a:mdna")],
        value_block=ValueThesis(
            why_cheap="x", change_trigger="y",
            normalized_earnings_view="z", quality_assessment="q",
        ),
        entry_price_ref=10.0, price_target_low=targets[0], price_target_high=targets[1],
        expected_holding_months=12, must_be_true=["m"],
        falsifiers=[Falsifier(description="d", check_type="price",
                              metric="drawdown_from_cost_pct", operator="<",
                              threshold=-30.0)],
    )


@pytest.fixture
def env(tmp_path):
    memo_store = MemoStore(tmp_path / "memos.sqlite")
    research_journal = Journal(str(tmp_path / "research.sqlite"))
    main_journal = Journal(str(tmp_path / "main.sqlite"))
    return memo_store, research_journal, main_journal


QUOTES = {"WIDG": Decimal("10"), "SPIN": Decimal("5")}


def _trade(env, *, quotes=None, adv=Decimal("5000000"), sectors=None, asof=TODAY):
    memo_store, research_journal, main_journal = env
    q = quotes or dict(QUOTES)

    def quote_source(symbol):
        from ops.broker.base import QuoteUnavailable
        if symbol not in q:
            raise QuoteUnavailable(symbol)
        return q[symbol]

    return trade_research_sleeve(
        memo_store=memo_store, research_journal=research_journal,
        main_journal=main_journal, quote_source=quote_source,
        starting_cash=Decimal("100000"), asof=asof, now=NOW,
        sector_lookup=lambda t: (sectors or {}).get(t, "Industrials"),
        adv_fetcher=lambda t: adv,
    )


def test_enters_open_memos_tier_sized(env):
    memo_store, research_journal, main_journal = env
    memo_store.save(_memo("WIDG", tier="medium"))
    outcome = _trade(env)
    assert outcome.entered == ["WIDG"]
    prov = research_journal.latest_event_payload_by_symbol(
        events.KIND_RESEARCH_POSITION_OPENED)
    assert prov["WIDG"]["memo_id"] == memo_store.list(ticker="WIDG")[0].memo_id
    assert Decimal(prov["WIDG"]["notional"]) == Decimal("4000.00")  # 4% of 100k
    # Summary event in the MAIN journal (the dispatcher's journal).
    kinds = [e["kind"] for e in main_journal.read_events()]
    assert kinds == [events.KIND_RESEARCH_TRADE_RUN]
    # Equity snapshot in the RESEARCH journal.
    snaps = [s for s in research_journal.read_equity_snapshots()
             if s["kind"] == "research_run"]
    assert len(snaps) == 1


def test_no_double_entry_and_passed_memos_ignored(env):
    memo_store, _, _ = env
    memo_store.save(_memo("WIDG"))
    passed = _memo("SPIN")
    memo_store.save(passed)
    memo_store.mark_passed(passed.memo_id)
    _trade(env)
    outcome2 = _trade(env)  # second run: WIDG already held, SPIN passed
    assert outcome2.entered == []
    assert outcome2.exited == []


def test_exit_on_falsifier_trip(env):
    memo_store, research_journal, main_journal = env
    memo_store.save(_memo("WIDG"))
    _trade(env)
    memo_id = memo_store.list(ticker="WIDG")[0].memo_id
    main_journal.record_event(
        events.KIND_FALSIFIER_TRIPPED,
        events.falsifier_tripped_payload(
            memo_id=memo_id, ticker="WIDG", falsifier_index="0",
            description="d", metric="drawdown_from_cost_pct",
            observed="-31.0", threshold="-30.0", consecutive_periods=1,
        ),
    )
    outcome = _trade(env)
    assert outcome.exited == ["WIDG"]
    closed = [e for e in research_journal.read_events()
              if e["kind"] == events.KIND_RESEARCH_POSITION_CLOSED]
    assert closed[0]["payload"]["reason"] == "falsifier tripped"
    # And it does not re-enter in the same run.
    assert outcome.entered == []


def test_exit_on_target_hit_and_resolution(env):
    memo_store, research_journal, _ = env
    memo_store.save(_memo("WIDG", targets=(15.0, 20.0)))
    _trade(env)
    outcome = _trade(env, quotes={"WIDG": Decimal("21")})  # >= 20 target
    assert outcome.exited == ["WIDG"]
    closed = [e for e in research_journal.read_events()
              if e["kind"] == events.KIND_RESEARCH_POSITION_CLOSED]
    assert closed[-1]["payload"]["reason"] == "target hit"


def test_fence_rejection_skips_with_reason(env):
    memo_store, _, _ = env
    memo_store.save(_memo("WIDG"))
    outcome = _trade(env, adv=Decimal("1000"))  # 5% of ADV = $50 < floor
    assert outcome.entered == []
    assert any("WIDG" in s and "adv" in s for s in outcome.skipped)


def test_quote_failure_skips_and_continues(env):
    memo_store, _, _ = env
    memo_store.save(_memo("DEAD"))
    memo_store.save(_memo("WIDG"))
    outcome = _trade(env)  # DEAD not in QUOTES -> QuoteUnavailable
    assert outcome.entered == ["WIDG"]
    assert any("DEAD" in e for e in outcome.errors + outcome.skipped)


def test_empty_book_and_no_memos_is_clean_noop(env):
    _, research_journal, main_journal = env
    outcome = _trade(env)
    assert isinstance(outcome, TradeOutcome)
    assert outcome.entered == [] and outcome.exited == []
    assert [e["kind"] for e in main_journal.read_events()] == [
        events.KIND_RESEARCH_TRADE_RUN]
```

- [ ] **Step 2: Run to verify failure** — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement** — module docstring (spec mapping: entries by tier under hard caps; exits from Phase C signals; third ledger isolation; no LLM), then the code per the binding behavior. Structure: `_exit_pass(...)`, `_entry_pass(...)`, small and readable; all journaling with `at=now`; lazy default fetchers. `Fill` price field: read `ops/broker/types.py` first. Memo ordering for entries: `sorted(memo_store.open_memos(), key=lambda m: m.created_at)`.

- [ ] **Step 4: Run tests to verify they pass** — Expected: 7 passed.

- [ ] **Step 5: Full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ -q
ruff check ops/research/trading.py tests/ops/research/test_trading.py
git add ops/research/trading.py tests/ops/research/test_trading.py
git commit -m "feat(research): post-close trade step — memo-driven entries/exits on the third ledger"
```

---

### Task 6: Daemon job + `ops research trade` CLI

**Files:**
- Modify: `ops/main.py`, `ops/cli.py`
- Test: extend `tests/ops/test_main.py`; create `tests/ops/test_cli_research_trade.py`

**Interfaces:**
- `_research_trade_tick(journal, config) -> None` in `ops/main.py` — mirrors `_research_monitor_tick` EXACTLY: gate `journal.has_event_today(events.KIND_RESEARCH_TRADE_RUN)` inside the try; opens `Journal(config.research_journal_path)` as a scoped context; calls `trade_research_sleeve(memo_store=MemoStore(config.memo_store_path), research_journal=<scoped>, main_journal=journal, quote_source=make_yfinance_quote_source(), starting_cash=config.research_starting_cash, asof=date.today())`; ALL exceptions → `KIND_RESEARCH_TRADE_ERROR` event. Lazy imports inside.
- Scheduler: job id `"research_trade"`, `CronTrigger(hour=16, minute=25, day_of_week="mon-fri")` (5 minutes after the monitor — exits react to the falsifier trips the 16:20 pass just journaled), `max_instances=1, misfire_grace_time=300`, registered in `_start_full_scheduler` ONLY when `config is not None` (same guard as research_monitor); NOT in `_start_guardian_only`.
- CLI `ops research trade` — manual one-shot mirroring `research monitor`'s shape: echoes `"trade {asof}: entered [...], exited [...], N skipped"` + each skipped/error line; exit 0.

- [ ] **Step 1: Failing tests.** Append to `tests/ops/test_main.py` (siblings of the research_monitor job tests — read them and mirror):

```python
def test_research_trade_job_registered_and_gated(tmp_path):
    from unittest.mock import MagicMock
    from ops.main import _start_full_scheduler

    journal = MagicMock()
    journal.has_event_today.return_value = True
    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), MagicMock(), journal, MagicMock(),
        config=MagicMock(),
    )
    try:
        job = sched.get_job("research_trade")
        assert job is not None
        job.func()  # gate returns early; must not raise
    finally:
        sched.shutdown(wait=False)


def test_research_trade_tick_records_error_instead_of_raising(tmp_path):
    from unittest.mock import MagicMock
    from ops import events
    from ops.main import _research_trade_tick

    journal = MagicMock()
    journal.has_event_today.return_value = False
    config = MagicMock()
    config.research_journal_path = object()  # guaranteed failure downstream
    _research_trade_tick(journal, config)  # must not raise
    kinds = [c.args[0] for c in journal.record_event.call_args_list]
    assert events.KIND_RESEARCH_TRADE_ERROR in kinds
```

Create `tests/ops/test_cli_research_trade.py` (mirror `test_cli_research_monitor.py`'s env fixture + source-module monkeypatch pattern):

```python
"""Unit tests for `ops research trade` (trade core faked)."""

from decimal import Decimal

import pytest
from click.testing import CliRunner

import ops.cli as cli_mod
from ops.research.trading import TradeOutcome

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "journal.sqlite"))
    monkeypatch.setenv("OPS_RESEARCH_JOURNAL_PATH", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    return tmp_path


def test_trade_echoes_summary(env, monkeypatch):
    outcome = TradeOutcome(
        asof="2026-07-07", entered=["WIDG"], exited=["SPIN"],
        skipped=["AAA: adv cap"], errors=[],
        equity=Decimal("101000"), cash=Decimal("60000"),
    )
    monkeypatch.setattr("ops.research.trading.trade_research_sleeve",
                        lambda **kw: outcome)
    result = CliRunner().invoke(cli_mod.cli, ["research", "trade"])
    assert result.exit_code == 0, result.output
    assert "WIDG" in result.output and "SPIN" in result.output
    assert "adv cap" in result.output


def test_trade_empty_everything_clean_exit(env, monkeypatch):
    # Real stores/journals, but a no-network quote source: no memos -> no quotes needed.
    monkeypatch.setattr(
        "ops.quotes.make_yfinance_quote_source",
        lambda: (lambda s: (_ for _ in ()).throw(AssertionError("no quotes needed"))),
    )
    result = CliRunner().invoke(cli_mod.cli, ["research", "trade"])
    assert result.exit_code == 0, result.output
```

Run — Expected: FAIL. (Verify `make_yfinance_quote_source` lives in `ops.quotes` and how the CLI should import it so the monkeypatch intercepts — lazy `from ops.quotes import make_yfinance_quote_source` inside the command body binds at call time from the patched module.)

- [ ] **Step 2: Implement** `_research_trade_tick` + scheduler registration + CLI command (lazy imports; the CLI builds the same objects as the tick but echoes instead of gating).

- [ ] **Step 3: Run tests, full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ops/test_main.py tests/ops/test_cli_research_trade.py -v && .venv/bin/python -m pytest tests/ -q
ruff check ops/main.py ops/cli.py tests/ops/test_main.py tests/ops/test_cli_research_trade.py
git add ops/main.py ops/cli.py tests/ops/test_main.py tests/ops/test_cli_research_trade.py
git commit -m "feat(ops): post-close research-trade job (16:25) + ops research trade CLI"
```

---

### Task 7: `ops research resolve` — computed Resolution, human judgment

**Files:**
- Create: `ops/research/resolution.py`
- Modify: `ops/cli.py`
- Test: `tests/ops/research/test_resolution.py`; CLI test appended to it or a small `tests/ops/test_cli_research_resolve.py`

Without an ergonomic resolve step the corpus never grows — Phase C surfaces `resolution_due` pushes, but resolving today requires hand-writing a `Resolution` in Python. This task automates the arithmetic and leaves the human exactly two inputs: the outcome label (right/wrong process judgment) and the narrative.

**Interfaces:**
- `BENCHMARK_SYMBOL = "IWM"` (Russell 2000 proxy for the small/mid-cap universe; module comment notes the schema docstring's "e.g. Russell 2000 Value" and that swapping tickers is a constant change).
- `build_resolution(memo, *, research_journal, price_fetcher=None, now=None, exit_price: float | None = None) -> Resolution`... returns a Resolution with everything computed EXCEPT `outcome_label`/`narrative` — so make it `compute_resolution_numbers(memo, *, research_journal, price_fetcher=None, now=None, exit_price=None) -> dict` returning `{"resolved_at", "exit_price", "realized_return_pct", "benchmark_return_pct", "holding_days"}`; the CLI assembles `Resolution(**numbers, outcome_label=..., falsifiers_tripped=[], catalysts_realized=[], narrative=...)`.
- Semantics: `exit_price` — explicit flag wins; else the research journal's last SELL fill price for the ticker (find the journal's fill-read API — `read_events`? fills live in the fills table; check `ops/journal.py` for a sell-fill reader; if only `last_buy_fill_for` exists, read fills via the journal's public read API or fall back to current price via `price_fetcher(ticker).close_on_or_before(today)`); else current close (shadow-tracked "passed" memos have no fills — schema says their `exit_price` may be None: for passed memos leave `exit_price=None` and use current close for the return math). `realized_return_pct = (exit - memo.entry_price_ref) / memo.entry_price_ref` (float, fraction not %— MATCH the schema docstring/test fixtures: read `tests/test_memo_store.py`'s `_resolution` helper to confirm whether 0.08 means 8%; mirror that convention). `benchmark_return_pct`: IWM close_on_or_before(memo.as_of_date) → close_on_or_before(today), same fraction convention. `holding_days = (now - memo.created_at).days`.
- CLI: `ops research resolve MEMO_ID --label [thesis_right_made_money|thesis_right_lost_money|thesis_wrong_made_money|thesis_wrong_lost_money] --narrative TEXT [--exit-price P]` — `click.Choice` for the label; computes numbers, calls `MemoStore.resolve`, echoes the resolved summary. Errors (unknown memo, already resolved, no benchmark data) → clean ClickException, exit nonzero.

- [ ] **Step 1: Failing tests** (inject `price_fetcher` returning canned `PriceContext`s keyed by symbol; seed a memo + optional SELL fill in a tmp research journal; assert each computed number; assert the fraction convention matches the schema fixture; CLI test with monkeypatched `compute_resolution_numbers` + real MemoStore asserting status flips to resolved and the label round-trips). Write ~6 tests covering: explicit exit price, sell-fill exit price, passed-memo current-close path, benchmark math, already-resolved error, label choice validation.

- [ ] **Step 2: Implement** `ops/research/resolution.py` + the CLI command (mirror `research monitor`'s shape; `MemoStore(config.memo_store_path)`, `Journal(config.research_journal_path)` scoped).

- [ ] **Step 3: Run tests, full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ops/research/test_resolution.py -v && .venv/bin/python -m pytest tests/ -q
ruff check ops/research/resolution.py ops/cli.py tests/ops/research/test_resolution.py
git add ops/research/resolution.py ops/cli.py tests/ops/research/test_resolution.py
git commit -m "feat(research): ops research resolve — computed resolution numbers, human label + narrative"
```

(If a separate CLI test file was created, stage it too.)

---

### Task 8: The calibration report (`ops/research/report.py` + `ops research report`)

**Files:**
- Create: `ops/research/report.py`
- Modify: `ops/cli.py`
- Test: `tests/ops/research/test_report.py`

**Interfaces:**
- `build_report(*, memo_store, research_journal, baseline_journal, now: datetime | None = None) -> dict` — journal/store reads ONLY, no network (the `ops/status.py` discipline: refuse quotes).
- `format_report(report: dict) -> str` — markdown (`#`/`##` headers + pipe tables), stdout-ready.
- CLI: `ops research report [--output FILE]` — echoes to stdout or writes the file.

**Report sections (binding — each a key in the dict and a `##` in the markdown):**
1. **Corpus**: counts by status (open/passed/resolved), by thesis_type, by conviction_tier; oldest/newest memo dates.
2. **Outcome 2×2**: for the resolved corpus — a 2×2 table of `outcome_label` counts plus mean `realized_return_pct` per cell. The off-diagonals called out (`thesis_wrong_made_money` = luck).
3. **Scenario calibration**: per resolved memo, stated expected return `Σ(p × return_pct)` vs realized; report the mean signed gap and mean absolute gap, plus a directional hit rate (stated-positive vs realized-positive agreement). Fewer than 5 resolved memos → the section renders `"corpus too small (n=N < 5) — numbers are noise"` instead of statistics (design doc: below ~30–50 the corpus is noise; be honest at any n).
4. **Bought vs passed**: mean realized return of resolved memos that had positions (a `KIND_RESEARCH_POSITION_OPENED` event exists for their memo_id in the research journal — build the memo_id set from `read_events`) vs resolved memos never positioned (the shadow track). Counts + means.
5. **Sleeve vs baseline**: from `read_equity_snapshots()` filtered to `kind="research_run"` (research journal) and `kind="baseline_run"` (baseline journal): first/last equity + simple return over the overlapping window (max of the two first-timestamps → min of the two last-timestamps; note the window in the output; either series empty → `"no data yet"`).
6. **Per-model attribution**: resolved memos grouped by `authored_by_model` (empty string rendered as `"(unattributed)"`) — count, mean realized return, 2×2 counts per model.

- [ ] **Step 1: Failing tests** (~7): seed a tmp MemoStore with a mix (2 resolved with different labels/models/scenarios, 1 open, 1 passed-resolved with no position events), tmp journals with equity snapshots + one position-opened event; assert each section's numbers exactly; assert the small-corpus honesty string; assert `format_report` contains the `##` headers and renders without exceptions on an EMPTY store (every section `"no data yet"` — the report must run on day one).

- [ ] **Step 2: Implement** — pure aggregation; floats fine; no quotes/network anywhere (assert by design: no broker, no fetchers imported).

- [ ] **Step 3: CLI command** (`--output` via `click.Path`; default stdout) + run tests, full suite, lint, commit

```bash
.venv/bin/python -m pytest tests/ops/research/test_report.py -v && .venv/bin/python -m pytest tests/ -q
ruff check ops/research/report.py ops/cli.py tests/ops/research/test_report.py
git add ops/research/report.py ops/cli.py tests/ops/research/test_report.py
git commit -m "feat(research): ops research report — calibration corpus report (2x2, scenarios, sleeve vs baseline, per-model)"
```

---

### Task 9: Docs, final review, PR

- [ ] **Step 1: Write `docs/research_trading.md`** — runbook: what trades when (16:25 job after the 16:20 monitor; exits react same-day to falsifier trips), the tier table + three fences with constants, the third-ledger isolation rule, provenance events, `ops research trade`/`resolve`/`report` usage, the resolve workflow (push arrives → `ops research resolve MEMO_ID --label ... --narrative ...`), and inspection queries against `research_journal.sqlite`. Cross-link from `docs/research_monitor.md` (one line: escalations/resolutions now feed the trading step + resolve command).

- [ ] **Step 2: Update `docs/long_horizon_research.md`** — mark step 7 done (checkmark style); step 8: mark the calibration-report half done, embedding lookup explicitly still deferred (adjust the line, don't fake a full checkmark).

- [ ] **Step 3: Full suite, lint, commit, push, PR**

```bash
.venv/bin/python -m pytest tests/ -q
git add docs/research_trading.md docs/long_horizon_research.md docs/research_monitor.md
git commit -m "docs(research): trading runbook + build-order step 7 checkmark"
git push -u origin feat/phase-d-sizing
gh pr create --repo CWFred/TradingAgents --base feat/phase-c-loop --head feat/phase-d-sizing \
  --title "feat(research): phase D — sizing + calibration (third ledger, tier sizing, memo-driven trades, resolve, report)" \
  --body "Implements Phase D of docs/superpowers/specs/2026-07-06-finish-research-system-design.md, stacked on #16 (retarget to main after it merges): research_journal third ledger, conviction-tier sizing under name/sector/ADV hard fences, post-close memo-driven entries/exits, ops research resolve, and the quarterly calibration report.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Report the PR URL. (Base is `feat/phase-c-loop` while PR #16 is unmerged; the controller retargets to `main` after #16 merges.)

---

## Verification checklist (after all tasks)

1. `pytest tests/ -q` green on `feat/phase-d-sizing` (expect ~35–45 new tests over the 1443 baseline).
2. Third-ledger isolation: `grep -n "baseline_journal_path\|journal_path" ops/research/trading.py` shows ONLY `research_journal` usage — the trade step never opens the momentum or baseline journals (main journal is passed in solely for the falsifier-trip read + summary event).
3. No LLM and no probability-sizing: `grep -rn "llm\|bind_structured\|scenario" ops/research/sizing.py ops/research/trading.py` — no LLM imports; `scenarios` never read by sizing/trading.
4. `ops research trade` and `ops research report` run clean on empty stores (day-one safe).
5. Old memos (no `authored_by_model` in payload) still load; suite proves it.
6. Spec coverage: third ledger (T1), tier sizing + three fences (T4), entries by tier / exits from Phase C signals reading sell rules from the memo (T5–T6), calibration report with all four spec'd comparisons (T8) + attribution field it needs (T2), resolution ergonomics supporting the corpus (T7). Non-goal intact: no embedding lookup (corpus too small).
7. Daemon: `research_trade` registered at 16:25 only with config; guardian-only path untouched; monitor at 16:20 unchanged.
