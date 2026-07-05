# Design: Daily Cross-Sectional Momentum Sleeve + Loosened Risk Envelope

**Date:** 2026-07-05
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

## Non-Goals / Out of Scope

- **Momentum-specific exit logic.** Deferred to a future spec (see "Deferred"
  below). Momentum positions will exit only via the existing shared guardrails.
- **Widening membership** beyond the S&P 500. Future lever; not needed now.
- **Changing the pipeline, guardrails, broker, or scheduler cadence.**
- **A re-entry cooldown** for recently-exited names.

## Architecture Overview

Two independent design changes ship together:

1. **A second candidate sleeve** — a daily cross-sectional momentum screen that
   is populated every trading day, merged with the earnings sleeve into one
   ranked, capped daily shortlist.
2. **A modestly loosened risk envelope** — more concurrent positions and
   slightly larger position sizing, so the now-fuller funnel actually produces
   more concurrent trades (throughput), while stops and kill-switches are
   unchanged.

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
cost dial; risk is capped separately and globally).

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

Data: reuse the yfinance daily-bar path used by the liquidity filter, extended
to ~6 months of history (`period` long enough to compute both the 6-mo return
and the 200-day MA). Apply the same Decimal-at-the-boundary discipline
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
    earnings: EarningsHit | None = None      # populated iff source == EARNINGS
    momentum: MomentumHit | None = None      # populated iff source == MOMENTUM
```

Invariant: exactly one of `earnings`/`momentum` is set, consistent with
`source`. Follows the existing codebase ethic — optional means genuinely absent,
never a fabricated zero.

## Component 3: Composite Universe Builder

A new builder composes both sleeves and preserves the existing
`universe_builder(asof_date=…, config=…) -> list[Candidate]` signature the
orchestrator already calls (`orchestrator.py:44`), so no orchestrator change is
required.

Algorithm:

1. Build the **earnings** candidates (existing path).
2. Build the **momentum** candidates (new path): members → deny-list →
   `find_momentum_leaders` → liquidity → ranked list.
3. **Merge + dedup by symbol.** On overlap (a name that is both a fresh earnings
   beat and a momentum leader), **earnings wins** — it is the higher-conviction,
   event-driven signal — and the candidate keeps `source == EARNINGS`.
4. **Rank the merged list.** Earnings candidates first (event-driven priority),
   then momentum candidates by 6-mo return descending. (Exact intra/inter-sleeve
   ordering is an implementation detail; the invariant is: earnings names are
   never starved by momentum names under the cap.)
5. **Cap at the daily analysis budget (8).** Return at most 8 candidates.

Interaction with the orchestrator's existing held-name filter
(`orchestrator.py:46`, `fresh_candidates = [c for c in candidates if c.symbol
not in held]`): the orchestrator drops currently-held symbols AFTER the builder
returns. To ensure the daily budget funds genuinely-new analysis rather than
being partly consumed by names that will be dropped, the composite builder
should account for held symbols when applying the cap. Concretely: the builder
gains access to the current held set (either passed through, or the cap is
applied to a slightly larger ranked list so ≥8 fresh names typically survive the
orchestrator's filter). Final wiring decided in the implementation plan; the
requirement is: **up to 8 fresh (non-held) names reach the pipeline per day.**

Wire the composite builder into `main.py:_wire` in place of the bare
`build_universe`.

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

## Data Flow (per decide-once tick)

```
members (S&P 500)
   ├─ earnings sleeve ─→ EPS-beat hits (last 2 trading days) ─┐
   └─ momentum sleeve ─→ 6-mo leaders > 200d MA ──────────────┤
                                                              ▼
                          deny-list + liquidity filter (shared)
                                                              ▼
                    merge + dedup (earnings wins) + rank + cap(8)
                                                              ▼
              orchestrator drops held names → up to 8 fresh candidates
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
- **Composite builder** — merge, symbol dedup with earnings-wins-on-overlap,
  earnings-not-starved ordering, and the 8-name cap (including the held-name
  interaction so the budget funds fresh analysis).
- **Source-aware reason string** — earnings vs momentum branches.
- **Config** — the new defaults load, and the derived effective-concurrent count
  is 7; env overrides still apply.

## Deferred (future specs)

- **Momentum-decay exit.** When does a momentum position sell? Today it exits
  only via the shared −8% stop and the drawdown kill-switches — there is no
  "sell when it falls off the leaderboard / drops below its 200-day MA" rule.
  Explicitly out of scope here; tracked as the next follow-up.
- **Membership widening** (S&P 1500 / Russell 1000) for still more candidates.
- **Extending the earnings lookback window** (2 → ~10 trading days) to harvest
  more of the PEAD drift within the existing thesis.
- **Re-entry cooldown** for recently stopped-out names, if churn appears.
```
