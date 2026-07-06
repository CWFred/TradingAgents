# Finishing the Long-Horizon Research System — Design

Date: 2026-07-06. Status: approved by user (brainstorm session, same date).
Companion docs: `docs/long_horizon_research.md` (strategy rationale),
`docs/superpowers/plans/2026-07-06-universe-screener-baseline.md` (the plan
that built the screener), `docs/research_screener.md` (screener runbook).

This spec covers everything remaining to take the system from "screener +
null baseline merged (PR #12, conflicted)" to a fully unattended two-sleeve
paper-trading system with the deep-research brain, monitoring loop, and
conviction-based sizing. It is split into four phases; **each phase gets its
own implementation plan** (Phase A's plan accompanies this spec; B/C/D plans
are written after their predecessors land, against real file states).

## Decisions locked during brainstorming (with reasons)

1. **Momentum sleeve stays on**, through its 8-week paper gate, with a formal
   sunset review at gate end (~2026-08-30): keep / pause / retire decided by
   its paper track record. Rationale: it is the only *daily* exerciser of the
   trading chassis (orders, fills, stops, exits, restarts, notifications);
   the research sleeve trades ~monthly and would surface infra bugs months
   later. Its marginal cost is capped (~8 LLM analyses/day, once-daily gate,
   local model). Expectation going in: the likely outcome is retirement —
   treat it as scaffolding with a scheduled demolition date. Consequence: the
   S&P 500 EPS-beat universe code (one of two candidate feeds into the
   momentum strategy) is NOT deleted; it lives and dies with the sleeve.
2. **No historical backtest.** User chose forward-only validation: the null
   baseline is the yardstick. (Reasons a backtest was optional anyway:
   survivorship bias makes results an upper bound, and the LLM layer cannot
   be honestly backtested at all — a model trained through 2025 already
   knows how 2021's stories ended; the model weights themselves are
   lookahead.)
3. **Local models only** for the deep-research stage (LM Studio /
   ds4-DwarfStar backends). Free and private; the known risk is judgment
   quality (the Gemma e2e sim HOLD-ed everything). Mitigations baked into
   Phase B's design: tool-based bounded reading (no long-context stuffing),
   strict structured outputs, citation enforcement, machine-checkable
   falsifier validation, and per-stage model configuration so any single
   stage can be pointed at an API model later by config change only. The
   null baseline is the honest test of whether local judgment adds value.
4. **Daemon left as-is until Phase A ships deploy isolation** (it is paper
   money, currently data-blind, and running pre-momentum code — harmless).

## The 2026-07-06 incident (evidence that shaped Phase A)

Diagnosed live during the brainstorm; root causes verified:

- **Silent data blindness:** the morning sweep logged
  `[earnings] skipped <SYM>: KeyError: ['Earnings Date']` for every S&P 500
  name. Traced into yfinance 1.5.1 `base.py:718`
  (`df.dropna(subset="Earnings Date")`): when Yahoo serves a degraded
  (rate-limited) payload the scraped table lacks the column and yfinance
  raises; our per-name skip handler correctly ate the error 500 times → the
  universe was empty → zero candidates, zero trades, zero notifications. A
  single-name repro after recovery worked fine — transient, likely
  rate-limiting under ~500 rapid calls at open. **There is no alarm that
  distinguishes "I looked and found nothing" from "I could not see."**
- **Deploy hazard:** the launchd daemon runs `.venv` (editable install)
  against this working directory; a mid-day branch checkout meant the
  daemon's 14:39 ET relaunch (no clean-shutdown event; launchd KeepAlive)
  came up running pre-momentum research-branch code. Development and
  production share one checkout.
- **Branch divergence:** PR #11 (momentum sleeve) merged into main hours
  after the research branch forked; PR #12 is CONFLICTING.

## Phase A — Consolidate, verify, deploy

Goal: one reconciled codebase, both sleeves, daemon isolated from dev,
data failures loud, data coverage measured, research funnel scheduled.

### A1. Branch reconciliation
Merge `main` into `claude/smallcap-research-coverage-dervpt`, resolve
conflicts (expected: `ops/config.py`, `ops/events.py`, `ops/cli.py`,
`tests/ops/test_config.py` — both sides added fields/kinds/commands; the
resolution is the union of both), full suite green, merge PR #12 into main.
All subsequent Phase A work happens on a new branch off main
(`feat/phase-a-hardening`), one PR at the end.

### A2. Deploy isolation
- Production checkout: git worktree `~/Code/TradingAgents-live` pinned to
  `main`, with its own venv (`python -m venv .venv && .venv/bin/pip install -e .`).
- The launchd plist is re-rendered to point at the live worktree's
  interpreter and WorkingDirectory. Service swap (unload/load) is a
  user-confirmed step.
- Redeploy recipe documented in the runbook:
  `git -C ~/Code/TradingAgents-live pull --ff-only && launchctl kickstart -k gui/$(id -u)/com.tradingagents.ops`
  (plus `pip install -e .` when dependencies changed).
- Rule: the dev checkout (`~/Code/TradingAgents`) never runs services.

### A3. Data-blindness alarms
- New journal event `universe_diagnostics` emitted once per daily cycle with
  counts: members checked, fetch failures, candidates produced, per source
  (earnings, momentum). Producers count failures instead of only printing
  them to stderr.
- Alarm event `universe_blind` (notify: push, high priority) when
  candidates == 0 AND fetch-failure rate > 50% — i.e. "empty because blind,"
  not "empty because quiet." Registered in BUILDERS + notify POLICY.
- Screen-side equivalent: `run_screen` already counts per-name errors; a
  sweep where errors > 50% of universe (or universe build itself fails)
  produces a failure notification (see A7) and a nonzero exit code.

### A4. Sweep pacing + retry
Shared helper for batch yfinance calls (earnings finder, momentum
leaderboard, ADV filter, price context): global min-interval throttle
(~0.15 s) + up to 2 retries with exponential backoff (5 s, 25 s) on
exception. Turns a transient Yahoo degradation into a slower sweep instead
of a blind day. Constants, not config.

### A5. Data-quality debts
- **Split-adjustment fix (P/E-history bar):** Yahoo closes are
  split-adjusted; XBRL EPS is as-reported. Fix: fetch split actions in the
  same 6-year history call (`actions=True`, "Stock Splits" column);
  `PriceContext` gains the split series and an
  `unadjusted_close_on_or_before(when)` = adjusted close × Π(split ratios
  after `when`); `run_screen` uses it for fiscal-year-end prices (current
  price needs no adjustment — no future splits). One-time re-check of
  accumulated pending hits after the fix lands.
- **Delisted write-off (manual v1):** `ops research write-off SYMBOL --price P`
  records a synthetic SELL order+fill at P directly via the baseline
  journal (PaperBroker.close_position would quote and fail), journals a
  `baseline_writeoff` event (AUDIT_ONLY). Automation follows in Phase C.

### A6. Coverage telemetry + live calibration run
- `run_screen` aggregates per-bar computability across the sweep (for each
  of the 6 bars: computed vs "missing:") and reports it in the summary and
  the stored run row.
- Then a real (network) calibration run: `ops screen --dry-run` with the SEC
  user agent, capturing the coverage table. Acceptance gate: if EV/EBIT or
  FCF-yield coverage < 60% of screened names, a follow-up task tunes concept
  fallbacks before Phase B is planned. Results recorded in the runbook.

### A7. Weekly screen as a scheduled job
- `ops screen --notify` flag: on completion sends a Pushover summary
  (passers, coverage, errors) through the existing notify transports; on
  blindness/failure sends a high-priority alert. Env-gated like the daemon's
  notifications.
- Second launchd plist `com.tradingagents.screen` (Saturday 10:00 local,
  StartCalendarInterval) rendered by `ops install-screen-service`,
  mirroring `install-service`, run from the live worktree.

### A8. decide-once composite parity
`ops decide-once` (non-forced path) still uses the earnings-only universe —
the momentum merge's documented known limitation. Switch it to the composite
universe so smoke runs exercise what the daemon runs.

## Phase B — The brain (build-order steps 4–5)

Goal: consume the pending queue, emit structured memos, local models only.

- **Filing-reader tools** (`tradingagents/dataflows/` + agent tool
  wrappers): `read_filing_section(ticker, accession, section)` (deterministic
  section extraction, bounded output), `diff_filing_sections(ticker, section,
  year_a, year_b)` (aligned YoY diff — the design doc ranks this above any
  embedding index), `get_insider_transactions(ticker)` (Form 4 XML parser:
  open-market buys vs sales vs grants, 10b5-1 flag — this unlocks the
  deferred insider-cluster trigger), `get_past_memos(ticker)` (MemoStore).
- **Two-stage memo graph** (fits small context windows):
  1. *Evidence passes*: per filing section, the local model produces cited
     bullet evidence (accession + section required on every claim; uncited
     evidence is stripped mechanically, not by prompt hope).
  2. *Thesis passes*: bear-case-first debate ("why is it cheap" must name a
     specific reason), falsifier drafting, then memo emission through the
     existing `structured.py` path into the Pydantic `Memo` schema.
- **Weak-model guardrails:** memo rejected (hit marked `failed`, not
  `researched`) if falsifiers aren't machine-checkable
  (metric/operator/threshold), if evidence citations don't resolve, or if
  mandatory fields (precedent lookup — "none found" is an explicit finding)
  are missing. One retry, then surface for human review.
- **Per-stage model config:** `OPS_RESEARCH_EVIDENCE_MODEL`,
  `OPS_RESEARCH_THESIS_MODEL` (provider/model strings through the existing
  provider registry). Upgrading any stage to an API model later is config.
- **Batch entry point:** `ops research run [--max-names N]` — nightly-safe
  batch: pull pending hits (oldest first), research, save memo, mark hit
  researched, shadow-track passes. Scheduled later (Phase C job or cron);
  manual first.

## Phase C — The loop (build-order step 6)

Goal: positions and memos are watched mechanically; humans get exceptions.

- Daily post-close job (in the ops service scheduler, alongside the daily
  summary): for every open memo — evaluate machine-checkable falsifiers
  against latest facts/prices; check calendar catalysts (event sleeve);
  surface `due_for_resolution` memos (push notification with the memo's
  exit checklist); escalate on falsifier trip or −30% drawdown by queueing a
  re-research hit and notifying.
- Automated delisted-position handling for the baseline and (later) the
  research sleeve: detect via quote failures on N consecutive runs, write
  off at last-trade/deal price with a journaled event (replaces the manual
  A5 command as default path; the command remains as override).
- Monitoring events registered in BUILDERS + POLICY (falsifier_tripped:
  push/high; resolution_due: push/normal; research_escalation: push/high).

## Phase D — Sizing + calibration (build-order steps 7–8)

Goal: memos become sized paper positions under hard caps; the corpus
becomes reports.

- **Third ledger:** the research sleeve trades its own paper journal
  (`research_journal.sqlite`, config + env parallel to the baseline's) —
  never the momentum journal, never the baseline journal.
- **Conviction-tier sizing** (LLM probabilities are NEVER sizing inputs):
  tier from the memo — starter 1–2%, medium 3–5%, high 5–8% of research
  equity; hard fences enforced by a guardrails profile: single name ≤10% at
  cost, sector ≤25%, position ≤ (X% of 20-day ADV) so the paper position is
  realistically exitable at small-cap liquidity.
- Entries are placed from `open` memos by tier; exits come from Phase C
  (falsifier trips, targets, resolution) — sell rules read from the memo.
- **Calibration reports:** `ops research report` — quarterly: stated
  scenario probabilities vs realized outcomes (the 2×2
  right/wrong-process × made/lost-money), bought-vs-passed comparison,
  null-baseline-vs-research-sleeve return comparison over identical
  windows, per-model attribution (which local model wrote which memos).
  Output: markdown to stdout/file; no dashboards until the corpus is real.

## Non-goals (explicit)

- No historical backtesting (decision 2). No embedding/similar-situation
  index until ~30–50 resolved memos (design doc). No API-model spend
  (decision 3; config escape hatch only). No real money — live remains
  gated far beyond all four phases. No new notification channels. No
  earnings-call transcript ingestion (deferred in design doc).

## Success criteria

- Phase A: PR #12 conflicts resolved and merged; daemon runs momentum from
  the live worktree while dev continues here; a yfinance outage produces a
  push alert within one cycle instead of silence; screen runs weekly by
  itself; coverage table exists with ≥60% computability on the headline
  valuation bars (or a tuning follow-up filed); split-adjusted P/E bar
  verified by test; suite green throughout.
- Phase B: `ops research run` turns a pending hit into a schema-valid,
  citation-resolving memo with machine-checkable falsifiers using only
  local models; rejects garbage rather than storing it.
- Phase C: a falsifier trip or lapsed holding period reaches the user's
  phone without any human polling; delisted names resolve themselves.
- Phase D: research positions exist on their own journal, sized by tier,
  never exceeding caps; the first calibration report renders.
