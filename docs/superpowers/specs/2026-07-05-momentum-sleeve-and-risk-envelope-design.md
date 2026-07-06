# Design: Daily Cross-Sectional Momentum Sleeve + Loosened Risk Envelope + Exit Engine

**Date:** 2026-07-05 (rev 2, same day — exit engine, stop-out cooldown, and
slot-aware analysis budget folded in; originally deferred exits made in-scope)
**Status:** Approved (design), pending implementation plan
**Author:** frednick + Claude

## Problem

The `/ops` service builds its trading universe as:

> S&P 500 membership ∩ earnings reported in the last 2 trading days with an EPS
> beat ∩ liquidity (≥$50M avg daily dollar volume, ≥$5 price).

The binding constraint is **"earnings reported in the last 2 trading days."**
Earnings are seasonal — they cluster into ~4–6 week seasons (mid-Jan, mid-Apr,
mid-Jul, mid-Oct). Outside those windows almost no S&P 500 name reports on a
given day, so `find_recent_earnings_beats` returns an empty list and there is
nothing to analyze or trade. The account can go multiple weeks with zero
activity, which makes it very slow to validate that the end-to-end system
works.

The current strategy (`PostEarningsMomentumStrategy`) bets on **PEAD** —
post-earnings-announcement drift, the documented tendency of a stock that beats
earnings to keep drifting for weeks. The 2-trading-day window discards most of
that ~60-day drift window, so there is also unused headroom in the existing
thesis — but the core goal here is to keep the analysis/trade loop fed on the
days (most of the year) when no fresh earnings exist.

## Goal

Raise the frequency of analysis and trades so the system produces observations
nearly every trading day — **without** increasing per-trade or portfolio risk
character ("not too aggressive"). Faster validation is the objective; number of
observations matters more than size of bets.

The book must **turn over**, not merely fill. Without exits, the momentum
sleeve fills all slots within roughly a week and activity stops — the only
exits would be −8% stop-outs. Every position therefore gets a defined
lifecycle: entries via the sleeves, exits via the exit engine (Component 6),
so freed slots are refilled daily and the buy/sell loop runs continuously.

## Non-Goals / Out of Scope

- **Widening membership** beyond the S&P 500. Future lever; not needed now.
- **Changing the pipeline, guardrails, broker, or scheduler cadence.** The
  orchestrator's daily tick gains an exit step, but stops, kill-switches, the
  guardian's 60s loop, and the schedule itself are untouched.
- **Partial exits / trimming.** Every exit closes the whole position.
- **LLM involvement in sell decisions.** Exits are mechanical, cheap, and
  deterministic — no pipeline runs on the sell side.
- **Profit targets.** The momentum thesis is "let winners run"; the decay
  rules are the sell discipline.

## Architecture Overview

Three design changes ship together:

1. **A second candidate sleeve** — a daily cross-sectional momentum screen that
   is populated every trading day, merged with the earnings sleeve into one
   ranked, capped daily shortlist.
2. **A modestly loosened risk envelope** — more concurrent positions and
   slightly larger position sizing, so the now-fuller funnel actually produces
   more concurrent trades (throughput), while stops and kill-switches are
   unchanged.
3. **A position exit engine** (Component 6) — daily, rule-based sell decisions
   (momentum decay, PEAD max-hold) evaluated before buys, plus a stop-out
   re-entry cooldown and a slot-aware analysis budget. This is what makes the
   first two changes produce *sustained* daily activity instead of one good
   week followed by a full, static book.

### Key framing: a "sleeve" is a candidate feeder, not a decision engine

The pipeline (`TradingAgentsGraph` via `PipelineAdapter`) still does the deep
per-symbol analysis and the final BUY/HOLD gate. The guardrails still size and
cap every order. A sleeve only decides **which symbols get fed into the
pipeline each day.** We are adding a second feeder, not a second decision
engine — everything downstream is untouched.

### Cost constraint

Every candidate that reaches the pipeline is a full `TradingAgentsGraph` run
(many LLM calls). The earnings sleeve provided a naturally short shortlist for
free. The momentum sleeve is populated every day, so it MUST impose its own cap.
The **daily analysis budget is 8 names** (chosen by the account owner as the
cost dial; risk is capped separately and globally). The budget is additionally
gated by free position slots — `min(8, free_slots)`, see Component 3 — so a
full book costs zero pipeline runs rather than 8 guaranteed-reject analyses.

## Component 1: Cross-Sectional Momentum Sleeve

### `ops/universe/momentum.py` (new)

`find_momentum_leaders(members, asof_date, *, fetch=…) -> list[MomentumHit]`,
structured like `find_recent_earnings_beats` — a pure function with an
injectable fetcher so it is unit-testable with fakes and has no import-time I/O.

Screen definition (per symbol):

- **Ranking signal:** trailing **6-month total return**. (6mo is deliberately
  chosen over 3mo (noisier, higher turnover) and 12mo (sluggish); it captures an
  established trend without being stale. The lookback is a named constant so it
  is easy to tune later.)
- **Uptrend gate:** last close **> 200-day moving average**. This is the "buy
  strength, never catch a falling knife" filter that keeps the sleeve
  conservative and coherent with the earnings (momentum-family) bet.
- **Liquidity:** the SAME filter already used by the earnings sleeve
  (≥$50M 20-day avg dollar volume, ≥$5 price). Reuse `apply_liquidity_filter`.
- **Rank** surviving names by 6-month return, descending.

`find_momentum_leaders` returns the **full ranked list** of every name passing
the gates, not just a top-N slice. It has two consumers: the composite builder
takes the top of the list for entries, and the exit engine (Component 6) looks
up the ranks of held names. One computation per tick, two consumers.

Data: reuse the yfinance daily-bar path used by the liquidity filter, extended
to **~10 months of history** — a 200-day moving average needs ~200 *trading*
days ≈ 9.5 calendar months, so 6 months of bars cannot compute it; a symbol
with fewer than 200 bars is skipped by the insufficient-history rule below.
Apply the same Decimal-at-the-boundary discipline
(`_safe_decimal`) as `earnings.py`/`filters.py`. Never fabricate absent data —
a symbol with insufficient history is skipped, not zero-filled.

`MomentumHit` (frozen dataclass) carries at least: `symbol`, `asof_date`,
`trailing_return_6m: Decimal`, `close: Decimal`, `sma_200: Decimal`.

## Component 2: Generalized `Candidate`

`ops.universe.Candidate` currently **requires** an `EarningsHit`, and
`PostEarningsMomentumStrategy` reads `cand.earnings.eps_actual` for its reason
string. A momentum candidate has no earnings event.

Change: add a `source` field and make the sleeve-specific payloads optional:

```python
class CandidateSource(str, Enum):
    EARNINGS = "EARNINGS"
    MOMENTUM = "MOMENTUM"

@dataclass(frozen=True)
class Candidate:
    symbol: str
    source: CandidateSource
    last_price: Decimal
    avg_dollar_volume_20d: Decimal
    earnings: EarningsHit | None = None      # set when an earnings event exists
    momentum: MomentumHit | None = None      # set when on today's leaderboard
```

Invariant: **at least one** payload is set, and the payload matching `source`
is always set. A name that is both a fresh earnings beat and a momentum leader
carries **both** payloads with `source == EARNINGS` (the primary thesis).
Retaining the momentum payload is not cosmetic: the exit engine's provenance
record (Component 6) needs the entry rank/MA context even for earnings-sourced
overlap names. Follows the existing codebase ethic — optional means genuinely
absent, never a fabricated zero.

## Component 3: Composite Universe Builder

A new builder composes both sleeves. The orchestrator calls the builder at
`ops/scheduler/orchestrator.py:44`; the builder's signature grows to accept the
current held set and free-slot count (wiring detail below).

Algorithm:

1. Build the **earnings** candidates (existing path).
2. Build the **momentum** candidates (new path): members → deny-list →
   `find_momentum_leaders` → liquidity → ranked list.
3. **Exclude ineligible names:** currently-held symbols and symbols inside the
   stop-out re-entry cooldown (Component 6). Doing the held-name exclusion in
   the builder (rather than relying only on the orchestrator's post-hoc filter
   at `ops/scheduler/orchestrator.py:46`, which stays as a belt-and-suspenders
   check) means the cap below funds genuinely-new analysis.
4. **Merge + dedup by symbol.** On overlap (a name that is both a fresh
   earnings beat and a momentum leader), the candidate keeps
   `source == EARNINGS` — the higher-conviction, event-driven signal — and
   carries **both** payloads (Component 2).
5. **Rank the merged list.** Earnings candidates first (event-driven priority),
   then momentum candidates by 6-mo return descending. (Exact intra/inter-sleeve
   ordering is an implementation detail; the invariant is: earnings names are
   never starved by momentum names under the cap.)
6. **Cap at `min(daily analysis budget (8), free slots)`.** Free slots =
   `max_open_positions − currently held`, computed AFTER the exit engine has
   run (exits execute before buys — see Data Flow). Every candidate returned
   costs a full pipeline run, so with zero free slots the builder returns an
   empty list and the day costs zero LLM runs — instead of 8 analyses whose
   orders are all guaranteed rejects at the guardrail
   (`ops/guardrails/sizing_rules.py:55` fires only after the LLM spend).

Wire the composite builder into `_wire` (`ops/main.py:248`) in place of the
bare `build_universe`. (Note: repo-root `main.py` is an unrelated demo script.)

## Component 4: Strategy Reason String

`PostEarningsMomentumStrategy.propose_orders` builds a `reason` from
`cand.earnings.*`. Make it **source-aware**:

- `EARNINGS`: unchanged — `"post-earnings beat (EPS … vs est …); pipeline BUY"`.
- `MOMENTUM`: `"6-mo momentum leader (ret …, > 200d MA); pipeline BUY"`.

Sizing, stop (`stop_pct`), order construction, and all guardrail interaction are
**identical** for both sleeves. (Strategy may be renamed to reflect that it now
serves both sleeves; not required for correctness.)

## Component 5: Loosened Risk Envelope

Change three `OpsConfig` defaults (all already `OPS_*` env-overridable):

| Param | From | To | Env var |
|---|---|---|---|
| `max_open_positions` | 5 | **7** | `OPS_MAX_OPEN_POSITIONS` |
| `per_position_cap_pct` | 0.10 | **0.12** | `OPS_PER_POSITION_CAP_PCT` |
| `cash_reserve_pct` | 0.20 | **0.16** | `OPS_CASH_RESERVE_PCT` |

Derivation. Effective concurrent positions =
`min(max_open_positions, floor(deployable / per_position_cap_pct))`, where
`deployable = 1 - cash_reserve_pct`.

- Before: `deployable = 0.80`; `floor(0.80/0.10) = 8`; `min(5, 8) = 5` positions,
  ≤50% deployed.
- After: `deployable = 0.84`; `floor(0.84/0.12) = 7`; `min(7, 7) = 7` positions,
  ≤84% deployed.

The three values are internally consistent — the position count and the sizing
are matched so neither dial is cosmetic (a common failure mode: raising the
count while raising size so much that cash-reserve caps the count *below* the
old value).

**Unchanged** (deliberately — loosening these would trade away safety for zero
throughput gain, and it is all paper during validation; live positions remain
hard-capped at `live_max_position = $10`):

- `per_position_stop_pct = -0.08`
- `daily_drawdown_pct = -0.07`
- `weekly_drawdown_pct = -0.15`

Impact: adding the momentum sleeve does not change per-trade or portfolio risk
character — the global guardrails bound everything regardless of which sleeve
sourced a candidate. The envelope change raises *throughput* (5 → 7 concurrent
positions, more capital working) so the fuller funnel produces more concurrent
trades and thus more observations, faster.

Stated honestly, the envelope itself IS hotter: the worst case where every
position stops out simultaneously grows from 5 × 10% × 8% = **4.0%** of equity
to 7 × 12% × 8% ≈ **6.7%**, which sits just under the −7% daily-drawdown halt.
That proximity is accepted — this is paper-mode validation and the kill-switch
still bounds the day — but it should be revisited before any live-capital
increase.

## Component 6: Position Exit Engine

A new pure module, `ops/exits/`, with one entry point:

```python
evaluate_exits(positions, provenance, leaderboard, bars_fetch, config,
               asof_date) -> list[ExitDecision]
```

Called by the orchestrator once per daily tick, **before** building buys, so a
slot freed in the morning is refilled the same morning. Sell orders go through
the existing `GuardedBroker`. `PositionGuardian` is untouched — it keeps its
single job (stop enforcement every 60s) and remains the safety net if this
engine ever breaks. Same pattern as the sleeves: pure function, injectable
data, no import-time I/O.

### Exit rules (per held position, checked once per daily tick)

**Momentum-sourced** — exit when either fires:

1. **Rank decay:** the symbol is not in the top `momentum_exit_rank`
   (default **25**) of today's full ranked leaderboard. Entry is top-8; the
   8 → 25 gap is deliberate hysteresis so a name oscillating around the entry
   boundary is not churned (bought and sold repeatedly, paying the spread each
   round trip).
2. **Trend break:** the last **two consecutive** daily closes are below the
   200-day MA. Two closes, not one — a single-close rule whipsaws on names
   oscillating around their MA. Computed statelessly from the bar history
   already fetched for the leaderboard.

**Earnings-sourced (PEAD)** — exit after `earnings_max_hold_days` (default
**40 trading days**, ≈ the documented ~60-calendar-day drift window), counted
via the market calendar. The thesis has an expiration date; enforce it. This
rule applies to every `EARNINGS`-sourced position, including overlap names
that also carried a momentum payload at entry — they were bought on the event
thesis, so they exit on it; the retained momentum payload is for observability
and future rules, not for switching exit regimes mid-hold.

**Unknown provenance** (position predates this feature, or metadata is
missing): apply the momentum rules — "does this name still deserve a slot" is
the sensible general test — and journal a warning so it is visible.

**Never sell on missing data.** If bars for a held symbol cannot be fetched,
skip its evaluation this tick and journal `exit_skipped_missing_data`. A data
outage must not liquidate the book. Implementation note: the rank rule is
"not in the top 25," and a fetch failure would ALSO present as "not in the
list" — the engine must distinguish *evaluated-and-ranked-low* from
*could-not-evaluate*.

### Position provenance

The exit engine needs each position's source sleeve and entry date. At order
placement the orchestrator journals a typed `position_opened` event (symbol,
source, entry date, and for momentum names the entry rank). The engine
reconstructs provenance for currently-held symbols from the journal — no new
state store; follows the existing typed-events pattern.

### Stop-out re-entry cooldown

The composite builder (Component 3) excludes any symbol whose most recent exit
was a **stop-out** within `stopout_reentry_cooldown_days` (default **10
trading days**), via a journal lookup. Rationale: a name that drops 8% can
still be a 6-month leader above its MA, so without a cooldown the screen
mechanically re-buys it the next morning — buy, stop out, re-buy is pure bleed.
Decay exits carry **no** cooldown: by construction they cannot be immediately
re-bought (a name below rank 25 is not top-8; a name below its MA fails the
entry gate), and a name that legitimately re-qualifies should be allowed back.

### Safety interactions & error handling

- Decay/max-hold exits **respect** the drawdown halts — they are discretionary
  trades, not safety actions. The guardian's stop enforcement keeps running
  during halts regardless; that hierarchy is unchanged.
- A failed sell order journals the error and retries naturally on the next
  tick: the position is still held and the condition still true — the engine
  is idempotent by design.
- An engine crash is caught, journaled (`exit_check_error`), and never kills
  the tick — same discipline as `PositionGuardian.check_stops_once`.
- New typed events: `position_opened`, `exit_decision` (which rule fired plus
  evidence: rank, closes vs MA, days held), `exit_order_placed`,
  `exit_skipped_missing_data`, `exit_check_error`.

### New `OpsConfig` fields

All env-overridable and validated like existing fields:

| Param | Default | Env var |
|---|---|---|
| `momentum_exit_rank` | 25 | `OPS_MOMENTUM_EXIT_RANK` |
| `earnings_max_hold_days` | 40 | `OPS_EARNINGS_MAX_HOLD_DAYS` |
| `stopout_reentry_cooldown_days` | 10 | `OPS_STOPOUT_REENTRY_COOLDOWN_DAYS` |

Validation: `momentum_exit_rank` must exceed the entry cap (8) — an exit rank
at or below the entry rank removes the hysteresis band and guarantees churn;
day counts must be > 0.

## Data Flow (once per trading day — first open, un-halted tick)

The `daily_cycle_run` journal event (recorded before the cycle runs, not
after) gates this whole flow to the FIRST open, un-halted orchestrator tick
of each trading day, enforcing the daily analysis-budget ceiling against the
30-minute scheduler cadence — every other tick that day short-circuits here.

```
market open? → kill-switch halts? (existing gates)
                                                              ▼
        EXIT ENGINE: held positions × (rank decay | MA break | max hold)
                  → sell decisions → GuardedBroker sells
                                                              ▼
                free slots = max_open_positions − still held
                                                              ▼
members (S&P 500)
   ├─ earnings sleeve ─→ EPS-beat hits (last 2 trading days) ─┐
   └─ momentum sleeve ─→ 6-mo leaders > 200d MA (full ranked) ┤
                                                              ▼
        deny-list + liquidity filter (shared) + held/cooldown exclusion
                                                              ▼
       merge + dedup (earnings wins, both payloads kept) + rank
                        + cap( min(8, free slots) )
                                                              ▼
                   pipeline.propagate() per candidate → BUY/HOLD
                                                              ▼
             strategy sizes (12% cap) → guardrails (7 max, 16% reserve,
                            −8% stop, drawdown kill-switches)
                                                              ▼
                                   broker.place_order
```

## Testing

Reuse the existing fake-fetcher unit-test pattern (see the StockTwits/Reddit and
earnings fakes):

- **Momentum ranking** — injected bar fetcher; assert 6-mo return computation,
  200-day MA gate (rejects below-MA names), liquidity reuse, descending rank,
  and skip-on-insufficient-history (no zero-fill).
- **Composite builder** — merge, symbol dedup with earnings-wins-on-overlap
  (both payloads retained), earnings-not-starved ordering, held/cooldown
  exclusion, and the slot-aware cap: `min(8, free_slots)`, including
  zero-free-slots → empty list → zero pipeline runs.
- **Source-aware reason string** — earnings vs momentum branches.
- **Exit engine** — rank decay fires at rank 26 and does not fire at rank 25
  (boundary: "in the top 25" survives); MA break needs two consecutive closes
  (one close does not fire);
  earnings max-hold fires on trading day 40 and not 39, counted via the market
  calendar; missing bars → skip + `exit_skipped_missing_data`, never a sell;
  unknown provenance → momentum rules + warning; engine crash is journaled and
  does not kill the tick.
- **Cooldown** — a stopped-out name is excluded for exactly 10 trading days;
  decay-exited names are not excluded.
- **Tick integration** — exits run before buys: an exit frees a slot and a
  same-tick buy can fill it.
- **Config** — the new defaults load, and the derived effective-concurrent count
  is 7; env overrides still apply; `momentum_exit_rank ≤ 8` is rejected.

## Deferred (future specs)

- **Membership widening** (S&P 1500 / Russell 1000) for still more candidates.
- **Extending the earnings lookback window** (2 → ~10 trading days) to harvest
  more of the PEAD drift within the existing thesis.
- **Partial exits / profit-taking rules**, if whole-position exits prove too
  coarse once real data accumulates.
