# Conviction-Weighted Momentum Sleeve (v2 posture)

**Date:** 2026-07-14
**Status:** Approved (design review with operator)
**Scope:** Momentum sleeve only (main ops journal). Research, short, and
insider sleeves are untouched. All sleeves remain paper; broker mode and the
live gate do not change.

## Problem

The v1 "conservative posture" throws away the pipeline's own mid-conviction
signal, so the momentum sleeve barely trades and generates almost no outcome
data to learn from.

Evidence from the live journals (2026-07-13 → 2026-07-14):

- 27 full-pipeline analyses → **24 HOLD, 2 SELL, 1 BUY** (one fill: DAL).
- **Zero guardrail rejections** across all sleeve journals. The hard
  guardrails (position cap, max positions, cash reserve, drawdown rules)
  have never blocked a trade — the conservatism is upstream.
- Root cause 1: `ops/pipeline_adapter.py` collapses Overweight and
  Underweight to HOLD; only outright Buy/Sell act. LLMs hedge toward the
  middle of 5-point scales, so much of the discarded signal is likely
  actionable.
- Root cause 2: the `analysis_decision` journal event does not record the
  native 5-tier rating, so the discarded signal cannot even be measured.
- Root cause 3: `daily_analysis_budget = 8` caps attempts; at the observed
  ~4% buy-conversion that is ~1 trade every 3 days.

## Goals

- More trades and more strategy diversity on the momentum sleeve, to
  generate learning data.
- Portfolio allocation reflects lead quality: high-conviction leads get
  more of the pie, and can displace low-conviction holdings when cash is
  short ("Moderate" posture, chosen at design review).
- Every trade keeps a one-line auditable explanation.

## Non-goals

- No change to drawdown kill switches (−7% daily / −15% weekly), the
  per-position stop (−8%), the deny list, or the no-margin / no-options /
  no-crypto rules at the momentum sleeve's guarded boundary.
- The momentum sleeve stays long-only (`LongOnlyRule` = cannot sell more
  than held). The short sleeve keeps shorting through its own isolated
  `ShortPaperBroker` path; nothing here touches it.
- No continuous conviction score / optimizer (Approach C). LLM-emitted
  numeric scores are uncalibrated; revisit once this design has produced
  calibration data.
- No change to the research drain nightly cap (15): the 00:00–08:00 ds4
  window is already near capacity at ~30 min/name.

## Design

### 1. Act on the full rating scale (`ops/pipeline_adapter.py`)

Replace the collapse with:

| Native rating | Pipeline decision | Effect |
|---|---|---|
| Buy | BUY, tier `high` | Enter at full size |
| Overweight | BUY, tier `starter` | Enter at starter size |
| Hold | HOLD | Nothing |
| Underweight | TRIM | Sell 50% of held quantity, only if the symbol is currently held; otherwise HOLD |
| Sell | SELL | Exit as today |

Unknown/missing rating still defaults to HOLD. The adapter's result gains a
`tier` field alongside the decision. The `analysis_decision` journal event
gains a `rating` field (the native 5-tier word) — this observability fix
ships regardless of anything else.

TRIM only fires when a held symbol is re-analyzed, so it is expected to be
rare; it exists so the Underweight signal is no longer discarded.

### 2. Entry ladder (`ops/strategy/post_earnings_momentum.py` + config)

- Buy-rated → `per_position_cap_pct` (12%, unchanged).
- Overweight-rated → `starter_position_pct` (new config, default **0.05**).
- The `position_opened` journal payload gains a `tier` field so the
  displacement engine can distinguish starters later.
- Positions opened before this feature have no recorded tier and are
  treated as `high` (never displaceable) — safe default for e.g. the
  existing DAL position.

### 3. Displacement engine (new module, wired into the daily cycle)

Trigger: a Buy-rated (tier `high`) entry cannot be fully funded from free
cash after the 16% cash reserve.

Mechanics:

- Trim **starter-tier holdings only**, oldest first.
- Each trim sells min(position value, remaining shortfall) — partial trims
  allowed, up to full exit. Move to the next-oldest starter if still short.
- Guards (all config):
  - `displacement_max_trims_per_day: 2`
  - `displacement_min_holding_age_days: 3` (trading days; use
    `ops/trading_time.py`)
  - Up-ladder only: displacement funds `high`-tier entries exclusively.
    A starter can never displace anything.
- If the shortfall remains after guards are exhausted, the buy is skipped
  and a journal event records why.
- Every trim journals a `displacement_trim` event:
  `{trimmed_symbol, trimmed_tier, notional, funded_symbol, remaining_shortfall}`
  — the one-line explanation is "trimmed X (starter) to fund Buy-rated Y".
- Displacement SELLs flow through the guardrail engine like any other
  order (SELLs are always allowed by drawdown rules).

### 4. Capacity raises (`ops/config.py`)

| Knob | v1 | v2 | Rationale |
|---|---|---|---|
| `max_open_positions` | 7 | **12** | Room for starters alongside full positions |
| `daily_analysis_budget` | 8 | **12** | More candidates/day; capped because each full analysis costs ~30 min of ds4 time and the single managed backend bracket is load-bearing |
| `starter_position_pct` | — | **0.05** | New |
| `displacement_max_trims_per_day` | — | **2** | New |
| `displacement_min_holding_age_days` | — | **3** | New |
| `cash_reserve_pct` | 0.16 | 0.16 | Unchanged (Moderate posture) |
| `per_position_cap_pct` | 0.12 | 0.12 | Unchanged |

All new knobs get OPS_* env overrides and `__post_init__` validation
(starter pct in (0, per_position_cap_pct]; trim/age counts positive).

### 5. Explicitly unchanged

Drawdown rules, per-position stop, deny list / full-blackout set,
NoMargin/NoOptions/NoCrypto/StopAttached/FractionalSharesOnly rules, the
live gate (`live_max_position`, `live_fill_gate_count`), the research /
short / insider sleeves and their tier maps (`ops/research/vetting.py`
already confirms Overweight at medium tier — that path is independent).

## Error handling

- Displacement quote failures: skip that holding, log, continue to the
  next candidate starter; never block the daily cycle.
- TRIM on an unquotable held symbol: skip with a journal event (same
  posture as the exit engine).
- Config validation failures fail fast at startup, as today.

## Testing

- Unit: rating→(decision, tier) mapping for all five ratings + unknown.
- Unit: entry ladder sizing (Buy vs Overweight notional; dollar floor
  interaction).
- Unit: displacement — trim cap/day, holding-age gate, oldest-first order,
  partial vs full trim, up-ladder-only, shortfall-remaining skip,
  legacy-position (no tier) immunity.
- Unit: journal payloads (`rating` on analysis_decision, `tier` on
  position_opened, `displacement_trim` shape).
- Known noise: 11 pre-existing `test_main.py` failures on main; overnight
  research tests only pass 00:00–08:00 local.

## Rollout

Paper only. Deploy to the `TradingAgents-live` checkout and restart the
daemon gracefully (never `kickstart -k`). Watch the first week's
`analysis_decision.rating` distribution to measure how much signal the v1
collapse was discarding.
