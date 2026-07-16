# Backtest & Learning Loop — Implementation Plan

Date: 2026-07-15
Design: `docs/superpowers/specs/2026-07-15-backtest-learning-loop-design.md`

## Goal

Build a strict point-in-time, memo-cached backtest system in which expensive
generation and post-mortem work is reusable while settings, exits, sizing, and
reports replay without an LLM. The same canonical `(memo, decisions, outcomes)`
shapes will also accept live research records.

## Decisions made concrete

- Cases and every context item are gated by the effective cutoff, initially
  `2025-06-01`; there is no override. The model probe may only advance it.
- A decision observed on session D executes at the next NYSE regular-session
  open. A same-day fill is never allowed.
- Outcome horizons are 5, 21, 63, and 126 exchange sessions. The 63-session
  outcome is primary; `abs(excess) < 3%` is a wash. Exact +/-3% is not a wash.
- BUY utility follows asset excess return. PASS/SELL utility reverses it, so a
  passed stock that materially outperforms is a miss.
- Run statistics use one row per case. Daily HOLD decisions remain in the audit
  journal but never increase the sample weight of a long-lived position.
- Strategy P&L through the actual exit and fixed-horizon counterfactual outcomes
  are both reported; they are not conflated.
- Historical case reconstruction from a current universe is stored and rendered
  as `exploratory/current-universe-reconstruction`, never as a clean PIT screen.
- Frozen memo identity includes brain/prompt/model/context/lesson fingerprints,
  not just `(symbol, asof)`. Replay settings are not part of the memo key.
- Post-mortem thesis assessment is cached independently of replay settings. A
  deterministic cross with each run's outcome produces the four quadrants.
- Holdout membership is fixed before lesson distillation. Holdout post-mortems
  cannot source lessons; treated and control memos use identical pinned inputs.

## Architecture and ownership

`ops/backtest/` is a separate package and `backtest.sqlite` is a separate store.
The live memo DB and live journals are read-only inputs to the future adapter.

### Task 1 — Domain model, configuration, and schema

Files: `ops/backtest/models.py`, `ops/backtest/store.py`, `ops/config.py`,
`tests/ops/backtest/test_config.py`, `tests/ops/backtest/test_store.py`.

- Add typed cases, bars, context items/manifests, decisions, executions, horizon
  outcomes, case results, thesis assessments, lessons, and experiment records.
- Add XDG-aware DB path, cutoff, benchmark, horizons, wash band, case count,
  primary horizon, notional, and triage thresholds to `OpsConfig`.
- Create a versioned SQLite schema with foreign keys, WAL, busy timeout,
  canonical JSON, stable hashes, and transactional state changes.
- Enforce the configured cutoff both when constructing and when inserting a
  case; validate persisted cases again before replay.

Acceptance: exact-cutoff cases persist, earlier cases fail closed, reopening is
idempotent, foreign keys are active, and the live memo store is untouched.

### Task 2 — Case dates, selection, and PIT manifests

Files: `ops/backtest/cases.py`, `ops/backtest/context.py`,
`tests/ops/backtest/test_cases.py`, `tests/ops/backtest/test_context.py`.

- Sample exchange sessions roughly every two weeks, spread takes across dates,
  cap each date, deduplicate `(symbol, asof)`, and use stable score/symbol ties.
- Define protocols for true historical sources and the explicitly labeled
  current-universe reconstruction fallback.
- Filter every artifact by a proved `available_at <= asof`; exclude undated,
  malformed, and later items with a manifest reason.
- Wrap the existing brain's filing reader so newest eligible 10-K/10-Q and
  trigger accessions cannot cross `asof`; filter precedent memos and lessons by
  temporal eligibility.

Acceptance: adversarial one-day-later documents, future amendments, trigger
accessions, precedents, lessons, and price bars cannot enter a prompt or hash.

### Task 3 — Persistent price cache and exchange-session semantics

Files: `ops/backtest/prices.py`, `ops/scheduler/market_calendar.py`,
`tests/ops/backtest/test_prices.py`.

- Persist raw and adjusted OHLC, volume, dividends, splits, provider, and fetch
  timestamp for symbols and benchmark.
- Add injected fetch/update paths; replay and tests only read cached bars.
- Align symbol and benchmark on the same sessions, bound split adjustment by the
  case as-of date, and distinguish pending, unpriceable, stale, and terminal.

Acceptance: weekend/holiday next-open behavior, splits, dividends, missing bars,
benchmark gaps, and future-bar exclusion are deterministic and offline-tested.

### Task 4 — Frozen generation queue

Files: `ops/backtest/generate.py`, `ops/backtest/context.py`,
`tests/ops/backtest/test_generate.py`, `tests/ops/backtest/test_integration_local.py`.

- Plan missing artifacts, claim jobs oldest-first, resume stale/crashed jobs, and
  atomically store memo, context manifest, guardrail result, prompt/brain version,
  both model IDs, and conditioning hash.
- Reuse `research_hit` with `today=case.asof`, an as-of-gated filing wrapper, a
  case-sliced price context, and an isolated memo sink.
- Fail closed unless memo-generation model specs point to loopback/local
  providers. Keep one opt-in local-model integration case.

Acceptance: a second run hits the frozen cache, crash recovery does not duplicate
work, rejected/failed generations remain gradeable cases, and default tests make
zero network or model calls.

### Task 5 — Sleeve protocol and research/live policy extraction

Files: `ops/backtest/sleeves.py`, `ops/research/policy.py`,
`ops/research/trading.py`, `ops/research/monitor.py`,
`tests/ops/backtest/test_sleeves.py`, `tests/ops/research/test_policy.py`.

- Define the four-piece case-source/context/decider/exit-policy contract.
- Bind the research sleeve to frozen recommendation/conviction, the live
  `size_entry` function under fixed per-case notional, and a new pure research
  target/falsifier/status exit policy shared by live and replay.
- Do not route research through the momentum-only `evaluate_exits` function.

Acceptance: live and replay research policy fixtures produce the same action,
sizing, and exit reason without broker, journal, LLM, or network dependencies.

### Task 6 — Deterministic replay and decision journal

Files: `ops/backtest/replay.py`, `tests/ops/backtest/test_replay.py`.

- Replay BUY/PASS at the next session open, step cached daily bars, evaluate
  exits after the close, and execute exits at the next open.
- Store every decision and execution with reason, observed price, sequence, and
  settings hash. Use fixed notional per case rather than shared capital.
- Store falsifier observations/trips for scorecards and diff two settings runs
  decision by decision.

Acceptance: synthetic paths prove no same-day fills/look-ahead, deterministic
sizing, exit precedence, missing-bar handling, settings hashing, and zero LLM
calls on replay.

### Task 7 — Mechanical verdicts and learning reports

Files: `ops/backtest/verdicts.py`, `ops/backtest/report.py`,
`tests/ops/backtest/test_verdicts.py`, `tests/ops/backtest/test_report.py`.

- Compute fixed-horizon stock/benchmark/excess/utility labels plus actual replay
  return and maximum drawdown.
- Build one-case-per-row triage statistics, conviction calibration, falsifier
  early/late/never and save/false-alarm scorecards, quadrant counts, exclusions,
  coverage, and reproducibility metadata.
- Configure and persist blunt promising/mixed/dead thresholds and the minimum
  mature sample; do not emit an authoritative verdict below that sample.

Acceptance: exact wash boundaries, PASS polarity, pending denominators, sparse
calibration, empty reports, and rerender stability are covered.

### Task 8 — Post-mortems, lessons, and efficacy experiment

Files: `ops/backtest/postmortem.py`, `ops/backtest/lessons.py`,
`tests/ops/backtest/test_postmortem.py`, `tests/ops/backtest/test_lessons.py`.

- Cache a structured thesis-correct assessment using facts only through the
  adjudication date, then deterministically map assessment + outcome to the four
  process/outcome quadrants.
- Distill short source-linked lessons from training cases only; assign
  `eligible_from` and inject only lessons eligible at a future case's as-of.
- Run a deterministic seeded holdout experiment with paired control/treated
  memos and report paired metric deltas rather than claiming significance from
  ten samples.

Acceptance: cache/version idempotency, evidence cutoff, source links, failed-call
atomicity, and holdout leakage prevention are offline-tested with fake adapters.

### Task 9 — CLI orchestration and cutoff probe

Files: `ops/cli.py`, `ops/backtest/service.py`,
`scripts/probe_backtest_cutoff.py`, `tests/ops/backtest/test_cli.py`.

- Implement `ops backtest run`, `generate`, `report`, and `postmortem`.
- Resolve `today` once, validate date/case/settings inputs, print generation
  plans, resume work, and rerender existing runs read-only.
- Record sealed cutoff-probe prompts, raw replies, model fingerprint, rubric,
  effective cutoff, git commit/dirty state, resolved config, substitutions, and
  source coverage. An unsafe probe fails closed and advances future eligibility.

Acceptance: cached/missing/invalid/unknown-run CLI paths have stable output and
useful nonzero exit codes; `report` never creates or migrates a missing DB.

### Task 10 — Live triple adapter

Files: `ops/backtest/live_adapter.py`,
`tests/ops/backtest/test_live_adapter.py`.

- Normalize live `MemoStore` rows and research journal events into the canonical
  memo/decision/outcome shapes without changing live records.
- Make imports idempotent with source IDs and preserve unknown/missing provenance
  explicitly.

Acceptance: repeated imports add nothing, event ordering is stable, and live DBs
remain byte-for-byte unchanged.

### Task 11 — Verification and independent review

- Run `python -m pytest tests/ops/backtest -q` and focused shared-policy tests.
- Run `python -m ruff check ops/backtest tests/ops/backtest ops/cli.py ops/config.py`.
- Run the full unit suite and compare against the recorded baseline: 1851 passed,
  13 failed, 8 sandbox socket errors, 13 skipped. The pre-existing main-loop
  date-window failures and sandbox-bound socket tests are not backtest regressions.
- Have an independent reviewer inspect PIT discipline, cache boundaries, SQLite
  atomicity, outcome semantics, CLI behavior, and accidental edits outside scope;
  fix every actionable finding and rerun the relevant tests.

## Delivery slices

1. Deterministic foundation: Tasks 1-3, 6-7, plus CLI report/run over preloaded
   cases. This is useful immediately for settings and mechanical-sleeve testing.
2. Memo corpus: Tasks 2, 4-5, and generation CLI.
3. Learning proof: Tasks 8-10 and the opt-in real-case integration test.

Each slice keeps `backtest.sqlite` forward-migratable and must not weaken the
cutoff or PIT gates to make incomplete data look usable.
