# Backtest → Self-Adapting Sleeve Tournaments — Roadmap

Date: 2026-07-25

**North star (user's words):** use the June 2025→now window (post ds4-training-cutoff,
genuinely out-of-sample) as extra data; make the system learn and adapt from backtests;
distinguish luck from real understanding (entries *and* sizing); ultimately pit many
sleeve variants against one another and let the best income-generator win.

This roadmap sequences that into phases with hard decision gates. Each phase is a
separate implementation plan producing working, testable software on its own. Later
phases are deliberately NOT detailed yet — their designs depend on artifacts and
verdicts the earlier phases produce.

## Phase 0 — First matured verdict (OPERATIONAL, in flight 2026-07-25)

No new code. Uses the `feat/backtest-price-backfill` branch.

1. `backtest generate --source reconstruction --start 2025-06-01 --end 2026-03-31 --enqueue`
   — screening sweep running now; cases + queued memo jobs land in `backtest.sqlite`.
2. Live daemon drains ~40 memo jobs through ds4 (weekend continuous; weekday
   nights outside the pre-market→16:45 ET blackout). Progress:
   `sqlite3 ~/.local/state/tradingagents/backtest.sqlite "SELECT status, count(*) FROM generation_jobs GROUP BY status;"`
3. `backtest prices --start 2025-06-01 --end 2026-03-31` (after cases exist; idempotent, re-run weekly).
4. `backtest run --start 2025-06-01 --end 2026-03-31` → first verdict with mature
   63-session outcomes.
5. Merge this branch to main (PR), deploy to `TradingAgents-live` so the daemon and
   the CLI agree on code.

**Gate G0:** ≥20 mature cases and a non-INSUFFICIENT verdict line. Caveat stays
visible: reconstruction corpus is survivorship-biased (today's universe membership).

**Known gap to fix opportunistically:** nothing writes `price_series_state`
(delistings → TERMINAL); stale names currently under-count losses.

## Phase 1 — Close the learning loop (PLANNED IN DETAIL)

Plan: `docs/superpowers/plans/2026-07-25-backtest-learning-loop-close.md`

- ds4-backed post-mortem adapter → `backtest postmortem --execute` fills the
  process/outcome quadrants (skill vs. luck, per case).
- `backtest lessons`: fixed holdout split (seeded, persisted FIRST), then ds4
  distills source-linked lessons from training-case assessments only.
- Lesson injection: eligible lessons (`eligible_from < case.asof`) enter memo
  generation; memo cache key already isolates lesson-conditioned variants.
- `backtest experiment`: paired control/treated memos over the untouched holdout;
  report mean paired delta (descriptive, no significance claims at n≈10).

**Gate G1:** paired experiment shows lessons don't *hurt* (mean_delta ≥ 0) before any
lesson ever conditions a live memo. If lessons hurt, iterate on distillation prompts —
the caches make re-runs cheap.

## Phase 2 — Live results join the corpus

- `backtest import-live` CLI over the existing `live_adapter.normalize_live_research`
  (built, tested, uncalled): live memos + research journal → canonical triples,
  idempotent by source id, live DBs byte-for-byte untouched.
- Recorded live cases then mature on their own clock and continuously grow the corpus
  alongside reconstruction cases; reports already label sources separately.

**Gate G2:** repeated import adds zero rows; live July-2026 cases score once mature
(first maturities ~Oct 2026 for the 63-session horizon).

## Phase 3 — Multi-sleeve bindings

- Implement `BacktestSleeve` bindings for short and insider sleeves (the four-piece
  contract in `ops/backtest/sleeves.py`: case source / context / decider / exit
  policy), mirroring how research binds frozen recommendation + live sizing + pure
  exit policy. Unlock `--sleeve` choices in the CLI (currently hardcoded `research`).
- Short sleeve replay needs short-side P&L semantics in replay (entry sell/exit
  buy); insider needs its case source (Form 4 triggers) made as-of-gated.

**Gate G3:** each sleeve's live policy fixtures produce identical action/sizing/exit
in live and replay paths (same acceptance the research sleeve already meets).

## Phase 4 — Sleeve tournaments

- A tournament = a matrix of (sleeve binding × settings variant × lesson set) replayed
  over the same frozen corpus; a leaderboard ranks by mean excess utility at the
  primary horizon with the wash band, calibration, and drawdown shown alongside.
- Because replay is zero-LLM, a tournament over N settings variants is cheap; only
  new lesson sets or prompt versions cost ds4 time (new frozen memo variants).
- Deliverables: `backtest tournament` CLI (declare variants in a TOML), leaderboard
  report, and a promotion rule — a variant must beat the incumbent on BOTH the
  reconstruction corpus and the (unbiased, slower-growing) live-import corpus before
  its settings are proposed for live deployment. Human merge stays the deploy gate.

**Gate G4 (self-adaptation, bounded):** the loop generate→postmortem→lessons→
experiment→tournament runs on a schedule (weekend queue), and its output is a
*proposal* (settings diff + evidence report), never an unattended live change.

## Sequencing constraints

- Phase 1 needs Phase 0's completed run (assessments need outcomes to quadrant
  against). Phase 4 needs Phase 3's bindings and is far stronger after Phase 2
  gives an unbiased corpus. Phase 2 and Phase 3 are independent of each other.
- ds4 compute is the scarce resource; everything is designed so LLM work is frozen
  and reused (memos, assessments, lessons are all cached by content hash).
