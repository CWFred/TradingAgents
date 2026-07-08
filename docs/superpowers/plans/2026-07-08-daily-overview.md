# Daily Overview — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A single "everything that happened today" overview across both sleeves — momentum (which names were analyzed and the BUY/HOLD/SELL verdict, orders/rejections/exits, universe health), research (memos written, monitor findings, trades, falsifier trips/escalations/resolutions), the null baseline, and anomalies — delivered as a Pushover headline + a full markdown file, plus an on-demand CLI. Runs after close on weekdays and Saturday evening (after the research brain finishes).

**Architecture:** One new **audit** journal event (`analysis_decision`) closes the only instrumentation gap — the momentum pipeline's per-name verdict, currently discarded inside `propose_orders`; it's threaded out via an optional decision sink and journaled by the orchestrator. Everything else already lives in the append-only journals. A pure builder (`ops/notify/overview.py`, mirroring `ops/status.py`'s dict-builder + string-renderer split) reads the day's events across the **three** journals (main / baseline / research) + memos-created-today, and renders markdown + a one-line headline. Delivery is a `_daily_overview_tick` in the always-on daemon (weekday 16:35 ET + Saturday 18:00 ET, once-per-day gated) plus an `ops digest` CLI. **No LLM anywhere; read-only except the two new audit events.**

**Tech Stack:** Python 3.10+, stdlib, existing journals/stores, APScheduler (existing daemon), the existing Pushover transport, pytest (all I/O injected).

## Global Constraints

- Branch `feat/daily-overview` off current `main` (already cut, @ c2e1c54 = A+B+C+D). Never commit to `main`; never `git checkout main` (deploy worktree holds it).
- Unrelated user files (`main.py` at repo root, `tradingagents/dataflows/reddit.py`) in NO commit; explicit file lists only.
- `ruff check <touched files>` clean (line-length 100, py310+); pre-existing errors in untouched files are not yours.
- New test modules set `pytestmark = pytest.mark.unit`; ALL network/quotes/journals injected. Full suite green before every commit: `.venv/bin/python -m pytest tests/ -q` (baseline: **1502 passed, 13 skipped**). Use `.venv/bin/python -m pytest`.
- Money in `Decimal`; overview percentages may be float.
- Every new event kind in `ops/events.py` `BUILDERS` + exactly one of `POLICY`/`AUDIT_ONLY` (enforcement test in `tests/ops/notify/test_policy.py`).
- **No LLM.** The overview is 100% read-and-render; the daemon tick catches all exceptions into a `daily_overview_error` event (never raises — it's an APScheduler job, same pattern as `_daily_summary_tick`/`_research_monitor_tick`).
- The existing 16:05 `daily_summary` stays as-is (thin equity snapshot); the overview is additive, not a replacement.
- Never run `launchctl`. No plist changes in this plan (the overview rides the existing daemon scheduler; the Saturday slot is a scheduler trigger, not a launchd job).
- End every commit message with EXACTLY:
```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

## Key repo facts (verified 2026-07-08 @ c2e1c54)

- **Momentum decision seam** (`ops/strategy/post_earnings_momentum.py:53` `propose_orders`): loops `candidates`, calls `pipeline.propagate(symbol, asof_date) -> PipelineResult(symbol, date, decision: PipelineDecision[BUY|HOLD|SELL], raw: dict)`, keeps only BUYs. HOLD/SELL are discarded — this is the gap. `Candidate` has `.symbol`, `.source` (an enum with `.value`), `.momentum` (may be None, has `.rank`). Orchestrator calls `propose_orders(...)` at `ops/scheduler/orchestrator.py:~121` inside `with self._pipeline_adapter.session():` and has `self._journal`.
- **Journals** (`ops/journal.py`): `read_events() -> list[dict]` (each `{"at": datetime, "kind": str, "payload": dict, ...}`), `read_fills()`, `read_equity_snapshots()`, `record_event(kind, payload, *, at=None)`, `has_event_today(kind, *, now=None)`, `get_latest_equity_snapshot(kind=, since=)`. `trading_day_start(when)` from `ops/trading_time.py`. Filter the day's events in Python by `at >= trading_day_start(now)`.
- **Three journals**: main = `config.journal_path` (momentum + research monitor/trade summary events + falsifier/escalation/resolution/catalyst events + analysis_decision); baseline = `config.baseline_journal_path` (baseline_screen_run/exit/writeoff/quote_failure); research = `config.research_journal_path` (research_position_opened/closed). Memos = `MemoStore(config.memo_store_path)`, filter `created_at >= day_start`.
- **Event kinds available for the day** (all in `ops/events.py`): momentum → `daily_cycle_run, universe_diagnostics, universe_blind, position_opened, exit_decision (symbol,rule,evidence), exit_order_placed, exit_skipped_missing_data, exit_check_error, fill, order_rejected, stop_hit, daily_halt, kill_switch`; research → `research_monitor_run (memos_checked,falsifiers_evaluated,tripped,unevaluable,escalations,resolution_due,catalyst_due,errors), research_trade_run (entered,exited,skipped,equity,cash), falsifier_tripped, research_escalation, resolution_due, catalyst_due, research_position_opened/closed`; baseline → `baseline_screen_run (asof,passers,buys,exits,skipped,equity), baseline_exit, baseline_writeoff, baseline_auto_writeoff, baseline_quote_failure`.
- **Report/CLI precedent**: `ops/status.py` `build_status(journal, config, *, now=None) -> dict` + `format_status(dict) -> str` + thin CLI (`ops/cli.py` `status`). `ops/research/report.py` for the build/format markdown split. Pushover: the `screen --notify` block in `ops/cli.py` shows `from ops.notify.push import build_push_transport; from ops.notify.config import load_notify_config; from ops.notify.transport import NotifyMessage; build_push_transport(load_notify_config()).send(NotifyMessage(title=, body=, urgency=))`.
- **Daemon wiring** (`ops/main.py`): `_daily_summary_tick(journal, broker, calendar=None)` and `_research_monitor_tick(journal, config)` show the scheduler-safe wrapper (gate via `has_event_today`, catch-all → `*_error` event). `_start_full_scheduler(orchestrator, guardian, dispatcher, journal, broker, calendar=None, heartbeat_job=None, config=None)` registers jobs (research_monitor at 16:20, research_trade at 16:25) only when `config is not None`; `run()` passes `config=config`. `_start_guardian_only` must NOT get the overview job.
- Logs dir: `~/.local/state/tradingagents/logs/` (from the plists' StandardOutPath dir); config has no explicit logs path — default the overview file under `${XDG_STATE_HOME:-~/.local/state}/tradingagents/overviews/` (create dir).

---

### Task 1: Journal the momentum per-name analysis decision

**Files:** Modify `ops/events.py`, `ops/strategy/post_earnings_momentum.py`, `ops/scheduler/orchestrator.py`. Test: extend `tests/ops/notify/test_policy.py` (kind registration), `tests/ops/strategy/test_post_earnings_momentum.py` (sink), and the orchestrator test (find it: `grep -rl "Orchestrator(" tests/`).

**Interfaces (Task 2 reads these):**
- `KIND_ANALYSIS_DECISION = "analysis_decision"`, **AUDIT_ONLY** (per-name log line, not a push). `analysis_decision_payload(*, symbol: str, decision: str, source: str, asof: str, rank: int | None = None) -> dict` (`decision` ∈ `"BUY"|"HOLD"|"SELL"`).
- `propose_orders(..., decision_sink: list | None = None)` — when a list is passed, append one `PipelineResult` (or a `(symbol, decision, source, rank)` tuple — implementer's call, keep it simple and typed) for **every** analyzed candidate, BUY or not. Default `None` = current behavior exactly (existing callers/tests unaffected).
- Orchestrator: pass a fresh `decisions: list = []` into `propose_orders`, then after the order loop journal one `KIND_ANALYSIS_DECISION` per collected decision (`at` defaulted). `source` from `cand.source.value`; `rank` from `cand.momentum.rank if cand.momentum else None`.

- [ ] **Step 1: Failing tests.** (a) kind registration (sibling of `test_phase_d_trading_kinds_registered`): assert `KIND_ANALYSIS_DECISION in AUDIT_ONLY`, not in `POLICY`, in `BUILDERS`. (b) sink: call `propose_orders` with a stub pipeline returning BUY for one candidate, HOLD for another; assert the sink got **both**, and the returned orders got only the BUY. (c) orchestrator: after a tick with a mixed BUY/HOLD pipeline, assert the journal has `analysis_decision` events for every analyzed name with the right decision. Read the existing strategy + orchestrator tests first and mirror their fixtures.

- [ ] **Step 2:** run → fail. **Step 3:** implement (payload builder + BUILDERS/AUDIT_ONLY entry; `decision_sink` append in `propose_orders`; orchestrator collection + journaling). **Step 4:** run → pass. **Step 5:** full suite + `ruff check ops/events.py ops/strategy/post_earnings_momentum.py ops/scheduler/orchestrator.py <test files>`, commit `feat(ops): journal per-name momentum analysis decisions (analysis_decision event)`.

---

### Task 2: The overview builder + renderer (`ops/notify/overview.py`)

**Files:** Create `ops/notify/overview.py`; test `tests/ops/notify/test_overview.py`.

**Interfaces (Task 3 uses these):**
- `build_daily_overview(*, main_journal, baseline_journal, research_journal, memo_store, config, now: datetime | None = None) -> dict` — reads the day's slice of each journal (`at >= trading_day_start(now)`) + memos with `created_at >= day_start`. Pure/read-only (no quotes, no network, no LLM — assert by imports). Every source is optional-tolerant: a journal with no matching events yields an empty section, never an error.
- `format_daily_overview(report: dict) -> str` — markdown, `#`/`##` sections.
- `overview_headline(report: dict) -> str` — one line for the push, e.g. `"2026-07-08: momentum 2 buys/1 exit, research 3 memos/1 trip, equity $10,120 (+1.2%)"`.

**Sections (binding — dict keys + `##` headers):**
1. **Momentum** — cycle ran? (`daily_cycle_run`); universe health (`universe_diagnostics`: checked / fetch-failures / candidates; flag `universe_blind`); **analyzed → decided** (`analysis_decision` counts by verdict + the BUY/SELL names); buys filled (`fill` BUY) / rejected (`order_rejected` with reason); exits (`exit_decision`: symbol + rule); day equity + P&L (from main journal `open_day` snapshot).
2. **Research** — memos written today (from the memo store: ticker, thesis_type, tier, buy/pass recommendation is not stored — use status); monitor (`research_monitor_run` counts; list `falsifier_tripped` names, `research_escalation` names, `resolution_due` / `catalyst_due` names); trades (`research_trade_run`: entered/exited/skipped, equity, cash; `research_position_opened/closed` from the research journal).
3. **Baseline** — `baseline_screen_run` (passers/buys/exits/skipped/equity), `baseline_exit`, `baseline_auto_writeoff`/`baseline_writeoff`.
4. **Anomalies** — any of `daily_halt, kill_switch, stop_hit, order_rejected, *_error, universe_blind, research_*_error` today; empty → "none".
5. **Header** — date + a per-sleeve equity line (momentum from main `open_day`/latest snapshot; research from research journal `research_run` snapshot; baseline from baseline journal `baseline_run` snapshot; "n/a" when a sleeve has no snapshot yet).

Empty/quiet day (nothing happened) → renders a clean "Quiet day — no activity" without errors (the daemon runs this every weekday + Saturday from day one, so day-one must be safe).

- [ ] **Step 1: Failing tests** (~8): seed each journal (tmp) with a representative day (a couple analysis_decisions incl. a HOLD, a fill, an exit_decision, a research_monitor_run with 1 trip, a research_trade_run, a baseline_screen_run, one anomaly), + a memo `created_at` today and one from last week (excluded); assert each section's numbers/names; assert the empty-stores case renders "Quiet day" and every section without exceptions; assert `overview_headline` is a single line with the key counts; assert no network/quote import in the module.
- [ ] **Step 2:** run → fail. **Step 3:** implement (build/format/headline; small private `_momentum_section`/`_research_section`/etc.; day-slice helper). **Step 4:** run → pass. **Step 5:** full suite + ruff, commit `feat(notify): daily overview builder — full cross-sleeve day summary (read-only)`.

---

### Task 3: Delivery — `daily_overview` event, daemon tick (weekday + Saturday), `ops digest` CLI

**Files:** Modify `ops/events.py` (2 kinds), `ops/main.py`, `ops/cli.py`. Test: extend `tests/ops/notify/test_policy.py`, `tests/ops/test_main.py`; create `tests/ops/test_cli_digest.py`.

**Interfaces:**
- `KIND_DAILY_OVERVIEW = "daily_overview"` (AUDIT_ONLY — once-per-day gate), `daily_overview_payload(*, date: str, headline: str, path: str) -> dict`; `KIND_DAILY_OVERVIEW_ERROR = "daily_overview_error"` (AUDIT_ONLY), `daily_overview_error_payload(*, error: str) -> dict`.
- `_daily_overview_tick(journal, config) -> None` in `ops/main.py` — gate `journal.has_event_today(KIND_DAILY_OVERVIEW)` inside a try; open `MemoStore(config.memo_store_path)`, `Journal(config.baseline_journal_path)`, `Journal(config.research_journal_path)` (scoped `with`), build the overview (main_journal = the passed `journal`), write the markdown to `${state}/tradingagents/overviews/overview-YYYY-MM-DD.md`, push the headline via the Pushover transport when `OPS_NOTIFY_ENABLED`, then `journal.record_event(KIND_DAILY_OVERVIEW, ...)`; catch-all → `KIND_DAILY_OVERVIEW_ERROR`. Lazy imports inside. (Push failure must not prevent the file write or the gate event — wrap the push separately.)
- Scheduler (`_start_full_scheduler`, config-guarded block): register `daily_overview` **twice** — `CronTrigger(hour=16, minute=35, day_of_week="mon-fri")` and `CronTrigger(hour=18, minute=0, day_of_week="sat")` (Saturday after the noon brain), both `max_instances=1, misfire_grace_time=600`, both calling `lambda: _daily_overview_tick(journal, config)`. The once-per-day gate makes the double registration safe. NOT in `_start_guardian_only`.
- CLI `ops digest [--date YYYY-MM-DD] [--output FILE] [--push/--no-push]` (default no-push, stdout) — builds the overview for the given day (default today), prints the markdown (or writes `--output`), pushes the headline only with `--push`. Mirrors `ops research monitor`'s command shape; opens all three journals + memo store from config. Manual/debug — does NOT record the gate event (so it never suppresses the daemon's own run).

- [ ] **Step 1: Failing tests.** (a) both kinds registered AUDIT_ONLY + in BUILDERS. (b) `_start_full_scheduler(..., config=<mock>)` registers a job id `daily_overview` (mon-fri) and one for Saturday — assert `get_job` returns them and `job.func()` is callable with no args and doesn't raise when the gate returns True; assert absent when `config=None`. (c) `_daily_overview_tick` with a broken config path records `KIND_DAILY_OVERVIEW_ERROR` and does not raise. (d) CLI: monkeypatch `ops.notify.overview.build_daily_overview` to a canned dict, assert `ops digest` prints the formatted markdown, `--output` writes it, `--push` calls a faked transport once; empty real stores → clean exit 0. Read `tests/ops/test_cli_research_monitor.py` + the research_monitor job tests first and mirror.
- [ ] **Step 2:** run → fail. **Step 3:** implement. **Step 4:** run → pass. **Step 5:** full suite + `ruff check ops/events.py ops/main.py ops/cli.py <test files>`, commit `feat(ops): daily overview delivery — post-close + Saturday daemon job and ops digest CLI`.

---

### Task 4: Docs, final review, PR

- [ ] **Step 1:** Write `docs/daily_overview.md` — what the overview covers (both sleeves + baseline + anomalies), when it fires (weekday 16:35 ET, Saturday 18:00 ET, + `ops digest` on demand), where the files land (`overviews/overview-DATE.md`), and that the push is a headline with the full detail in the file. Add one line to `docs/research_trading.md` / the ops RUNBOOK cross-linking it.
- [ ] **Step 2:** Full suite green, commit docs `docs(ops): daily overview runbook`.
- [ ] **Step 3:** Push branch, open PR to `main`:
```bash
git push -u origin feat/daily-overview
gh pr create --repo CWFred/TradingAgents --base main --head feat/daily-overview \
  --title "feat(ops): daily overview — full cross-sleeve 'everything that happened today'" \
  --body "Post-close (weekday 16:35 ET) + Saturday-evening (18:00 ET, after the research brain) daily overview across momentum, research, and baseline sleeves — analyzed→decided names, orders/rejections/exits, universe health, memos written, monitor findings/trips/escalations, research trades, and anomalies. Pushover headline + full markdown file, plus \`ops digest\` on demand. One new instrumentation event (analysis_decision) closes the only gap; everything else reads the existing journals. No LLM.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```
Report the PR URL and WAIT for review.

---

## Verification checklist
1. `pytest tests/ -q` green (expect ~20-25 new tests over 1502).
2. `ops digest` on the freshly-reset (empty) system renders "Quiet day" cleanly, exit 0.
3. No LLM/quote/network import in `ops/notify/overview.py`.
4. Every analyzed momentum name (incl. HOLDs) produces an `analysis_decision` event; existing `propose_orders` callers unaffected when no sink passed.
5. `daily_overview` registered mon-fri 16:35 + sat 18:00, config-guarded, absent from guardian-only; once-per-day gate prevents double-emit.
6. Notify enforcement test green (analysis_decision, daily_overview, daily_overview_error all AUDIT_ONLY + BUILDERS).
7. The existing 16:05 `daily_summary` is untouched.
