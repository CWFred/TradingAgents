# Backtest & Learning Loop — Design

Date: 2026-07-15
Status: Approved pending user review
Supersedes: the scrapped `ops/backtest/` trade-level-replay scaffold from
earlier on 2026-07-15 (source deleted deliberately; only `__pycache__`
remains and can be removed).

## 1. Motivation and goals

The #1 reason for this system: **accelerated learning**. Without it, knowing
whether a sleeve works means waiting ~2 years of live trading. With it, the
feedback loop compresses to about a week for a new memo-driven sleeve and to
minutes for settings changes and mechanical sleeves.

Concrete goals, in priority order:

1. **Sleeve triage.** Codify a new sleeve (or tweak settings on an existing
   one), run one command over June 2025 → today, and get a report that says
   whether it's worth exploring or dead in the water.
2. **A verdict corpus.** 30–50 memos + the buy/sell/hold decisions made on
   them, each labeled good/bad on both outcome and process, so the system
   (and Fred) can learn from them.
3. **A learning loop that provably works.** Lessons distilled from
   post-mortems feed the research brain; a holdout experiment measures
   whether lessons actually improve memos.
4. **One pipeline for backtest and live.** The verdict/post-mortem machinery
   consumes (memo, decisions, outcomes) triples regardless of origin, so the
   corpus grows automatically from live trading too.

Non-goals (v1): capital-constrained portfolio simulation (displacement
between simultaneous sim holdings is approximated, not simulated); short and
insider sleeves (the interface supports them; wiring is later work);
optimizing for in-sample returns (this is a truth machine, not a curve
fitter).

## 2. Hard constraints

- **Strict post-cutoff cases only.** ds4 (DeepSeek V4 Flash) training cutoff
  is **May 2025** (user-supplied). No case may have `asof < 2025-06-01`.
  The gate is a config constant (`OPS_BACKTEST_CUTOFF=2025-06-01`), enforced
  at case-creation time, and validated once empirically: a probe script asks
  ds4 about a handful of major post-May-2025 market events; if it knows any,
  the cutoff moves forward and the probe result is recorded in the run
  metadata.
- **Point-in-time discipline everywhere.** Every context input (filings,
  news, prices, fundamentals) must be dated on-or-before the case's `asof`.
  Reuse the existing look-ahead-safe fetchers; anything that can't prove its
  as-of date is excluded from historical context.
- **Local models only** for memo generation, same as live
  (`OPS_RESEARCH_*_MODEL`). Post-mortem/lesson distillation may use a
  cheaper/faster model since it never touches trade decisions.
- **Cost tiers are load-bearing.** LLM work (memo generation, post-mortems)
  runs once per (symbol, asof) and is cached forever. Everything a user
  iterates on — settings, exits, sizing, mechanical sleeves — must run with
  zero LLM calls.

## 3. Architecture

Two planes sharing one store:

```
GENERATION PLANE (expensive, once per case)
  screener@asof ──> cases ──> PIT context ──> research brain ──> frozen memos
                                                                    │
                                              memo cache: (symbol, asof)
                                                                    │
REPLAY PLANE (cheap, run constantly)                                ▼
  sleeve + settings + date range ──> decision replay ──> decisions journal
                                                              │
                                     prices/benchmark ──> verdict engine
                                                              │
              ┌───────────────┬───────────────┬───────────────┤
              ▼               ▼               ▼               ▼
        triage report   calibration     falsifier      post-mortems +
        (go / no-go)      curve         scorecard      lesson distiller
```

### 3.1 Primary UX

```
ops backtest run --sleeve research --start 2025-06-01 --end today \
    [--settings overrides.toml] [--cases 40]
```

- If every needed memo is cached: completes in minutes, prints the triage
  report.
- If memos are missing (new sleeve/universe/dates): prints the generation
  plan (N names × ~X min on ds4), runs generation resumably (`ops backtest
  generate` can also be invoked directly, e.g. overnight), then replays.
- `ops backtest report <run-id>` re-renders any past run.
- `ops backtest postmortem <run-id>` runs/updates the LLM post-mortems and
  lesson distillation for a run's decided cases.

### 3.2 Sleeve interface (the adaptability requirement)

A backtestable sleeve is four small pieces, each defaulting to the live
implementation where one exists:

| Piece | Contract | Research-sleeve binding |
|---|---|---|
| case source | `dates -> [(symbol, asof, trigger)]` | live screener run at historical dates |
| context builder | `(symbol, asof) -> PIT context` | research brain's section reads, date-gated |
| decider | `(case, context) -> decision + conviction` | frozen memo + conviction v2 rating (LLM, cached) or pure-mechanical (free) |
| exit policy | `(position, daily bars) -> hold/sell` | `ops.exits.engine.evaluate_exits` + falsifier trips |

The live `Strategy.propose_orders(..., asof_date=...)` protocol already
takes an as-of date; the replay drives the same interface rather than a
parallel one. A new sleeve that implements these four pieces is backtestable
with no harness changes; a mechanical sleeve simply has a free decider and
lives entirely in the replay plane.

### 3.3 Case selection

- Run the actual screener at dates sampled every ~2 weeks across the window
  (June 2025 → ~3 months ago, so most cases have a 3-month verdict; newer
  cases are allowed but their long-horizon verdicts show as pending).
- Take top hits per date until the target (default 40, range 30–50) is
  reached; cap per-date takes so cases spread across time and regimes.
- Screener-selected, never hand-picked — hand-picked cases measure Fred's
  stock-picking, not the system's.
- **Rejections are cases too.** If the brain's guardrails reject a memo or
  the pipeline says no-BUY, that is recorded as a `pass` decision and graded
  like any other — a passed stock that mooned is a miss worth learning from.
- Risk: if the live screener can't cleanly run at a historical date (data
  source won't time-travel), fall back to reconstructing its criteria over
  PIT price/fundamental data and record the substitution in run metadata.

### 3.4 Frozen memo corpus

- Separate SQLite store (`backtest.sqlite`), never the live memo store.
  Keyed by (symbol, asof, brain-version). Generation is resumable
  (crash-safe, oldest-first), same pattern as the live research queue.
- Memos are generated by the unmodified research brain with the PIT context
  builder swapped in, and pass the same mechanical validation gates as live
  memos. A frozen memo records the model IDs, prompt/brain version, and
  context manifest (which documents, with dates) that produced it.

### 3.5 Decision replay

- Entry: decider output → next-session execution at the following trading
  day's price (no same-day fills), position sized by the sleeve's live
  sizing rules under a fixed notional per case (trade-level, not a shared
  capital pool).
- Then step forward day-by-day applying the sleeve's real exit policy and
  machine-checkable falsifiers to emit hold/sell decisions, journaled with
  the same shape as the live journal.
- Every decision row: case, action, date, price, reason, and the
  settings-hash that produced it — so two runs with different settings are
  diffable decision-by-decision.

### 3.6 Verdict engine

Two independent labels per decision:

- **Outcome label** (mechanical, free): excess return vs SPY (return beyond
  just holding the index) at 1w / 1m / 3m / 6m horizons, using adjusted
  closes from a persistent price cache. Primary horizon 3m. Wash band:
  |excess| < 3% at 3m is `wash`, not win/loss, so noise isn't graded.
  Thresholds and benchmark are config.
- **Process label** (LLM, cached): post-mortem compares the memo's thesis
  and falsifiers to what actually happened (using only post-asof facts) and
  places the decision on the process×outcome quadrant: *right-thesis-worked /
  right-thesis-unlucky / wrong-thesis-lucky / wrong-thesis-lost*.
  Wrong-but-lucky is flagged loudest — it's how strategies learn bad habits.

### 3.7 Learning outputs

1. **Triage report** (per run): per-trade table, hit rate, mean/median
   excess return, quadrant counts, max drawdown per trade, and a blunt
   verdict line (promising / mixed / dead) with the evidence for it.
2. **Conviction calibration curve**: realized excess return grouped by
   conviction tier. If Tier-1 doesn't beat Tier-3, the rating is decoration
   — say so.
3. **Falsifier scorecard**: for each losing trade, did a falsifier fire
   before the damage, after, or never? For each firing, was it a true save
   or a false alarm?
4. **Lesson distiller**: post-mortems → short, general lessons written into
   the per-sleeve memo store (tagged `backtest-lesson`, source-linked to
   their cases) so future memos are conditioned on them.
5. **Lesson efficacy experiment**: hold out ~10 cases; regenerate their
   memos with lessons injected vs. without; compare decision quality. This
   is the proof the self-improvement mechanism improves anything.

### 3.8 Live unification

The verdict engine and post-mortem machinery accept any (memo, decisions,
outcomes) triple. A thin adapter maps live journal entries + live memo store
rows into the same shapes, so live trades get post-mortems on the same
cadence and the corpus grows forward automatically (each passing month adds
clean post-cutoff history).

## 4. Error handling

- Generation is resumable; a crashed run continues where it stopped.
- Missing/insufficient price history for a case → case marked `unpriceable`
  and excluded from stats, never silently dropped from the report.
- Memo generation failure/rejection → recorded with the guardrail reason;
  rejected-memo cases still get outcome labels (see "rejections are cases").
- The cutoff gate raises on any pre-cutoff case; there is no override flag.
- Data sources that won't time-travel are excluded from context and listed
  in the run's context manifest rather than approximated silently.

## 5. Testing

- Unit tests for the replay plane run on synthetic fixtures (deterministic
  price paths + hand-written memos): entry timing, exit rules, verdict
  labels, wash band, calibration grouping. No network, no LLM.
- Cutoff gate and PIT context builder get adversarial tests (document dated
  one day after asof must be excluded).
- One slow integration test (marked, opt-in) drives a single real case
  end-to-end against a local model.
- The existing pre-commit/test conventions apply; new tests live under
  `tests/ops/backtest/`.

## 6. Decisions log

- Prior trade-level scaffold deliberately scrapped: it measured returns but
  had no learning loop, which is the point of the system.
- Strict post-cutoff only (no contaminated deep history, no anonymization).
- Frozen-corpus + cheap-replay over full portfolio simulation: 30–50
  independent samples beat one path; portfolio realism can be layered on
  later over the same corpus.
- All four learning outputs are in scope; lesson efficacy must be measured,
  not assumed.
- ds4 cutoff May 2025 per Fred; probe validates rather than establishes it.
