# Research screener + null-baseline portfolio

Build-order step 3 of docs/long_horizon_research.md.

## What it does

`ops screen` runs the funnel's cheap stages: quarterly-cached small/mid-cap
universe ($300M-$10B, price > $5, 20-day ADV > $2M, no financials/biotech) →
point-in-time fundamental screen (2-of-3 valuation bars AND 2-of-3 quality
bars AND ≥1 change trigger) → writes passers to the deep-research queue
(`research_screen.sqlite`) → updates the null-baseline paper portfolio
(equal-weight every passer, 12-month holds, its own journal).

The baseline is the control for the whole system: LLM stages must beat it by
more than the token bill (design doc, "the mandatory null baseline").

## Running

    SEC_EDGAR_USER_AGENT="Your Name you@email.com" ops screen

First run of a quarter is slow (one yfinance history call per universe name
for ADV, then per-name company-facts + price history). Subsequent runs reuse
the quarterly universe cache. Smoke-test with `--limit 25 --dry-run`.

Cadence: weekly, outside market hours. Example launchd/cron: Saturday 09:00
local. There is deliberately no always-on service for this yet — the
monitoring loop is build-order step 6.

## Env vars

| Var | Default | Meaning |
|---|---|---|
| `SEC_EDGAR_USER_AGENT` | (required) | SEC fair-access contact string |
| `OPS_SCREEN_STORE_PATH` | `~/.local/state/tradingagents/research_screen.sqlite` | screen runs + deep-research queue |
| `OPS_BASELINE_JOURNAL_PATH` | `~/.local/state/tradingagents/baseline_journal.sqlite` | baseline portfolio journal |
| `OPS_BASELINE_STARTING_CASH` | `100000` | baseline paper cash |

## Backdated runs

`--asof` is a debug knob, not a backtest: it re-dates the point-in-time
fundamentals lookup, but the universe membership and the baseline's fill
prices are still TODAY's. Backdating changes what the screen *sees*, not
what it *trades against*. Running `--asof` earlier than today without
`--dry-run` prints a warning to say so.

A delisted baseline position (tender, acquisition — no live quote) is marked
at its last journaled fill price so the control keeps accruing instead of
crashing; it requires manual resolution until the monitoring loop
(build-order step 6) lands.

## Data notes

P/E-history prices are split-unadjusted (as-traded) to match as-reported XBRL EPS; Yahoo's split-adjusted closes are corrected using the split actions from the same history call.

## Form 4 note

Insider-cluster triggers are deferred to build-order step 4 (needs the Form 4
XML parser to separate open-market buys from routine sales/grants). EDGAR
triggers today: 13D/13D-A, notable 8-K items, 10-12B spinoffs, tenders,
going-private. Plus the price trigger: close ≥25% below the 60-day high.

## Calibration runs

### 2026-07-06 (Phase A, Task 9 — first live run, pre-tuning)

`ops screen --limit 200 --dry-run` (first 200 universe names, network live):
universe 200, screened 198, passed 15, errors 2 (both SEC companyfacts 404s
for recently-listed names: AIBZ, AYA).

| bar | computed | coverage |
|---|---|---|
| ev_ebit_vs_sector | 87/198 | 43% |
| fcf_yield | 142/198 | 71% |
| pe_vs_own_history | 84/198 | 42% |
| roic_5y | 132/198 | 66% |
| debt_to_ebitda | 79/198 | 39% |
| gross_margin_stability | 86/198 | 43% |

**A6 gate (ev_ebit & fcf_yield ≥ 60%): FAILED on ev_ebit_vs_sector (43%).**

### 2026-07-07 (Phase B, Task 0 — after Task 0 tuning)

`ops screen --limit 200 --dry-run` re-run after the honest-diagnostics split
(`unprofitable:`/`not-meaningful:` no longer count as missing) and the EBIT
pretax+interest fallback chain: universe 200, screened 198, passed 15,
errors 2 (same two SEC companyfacts 404s: AIBZ, AYA).

| bar | computed | coverage |
|---|---|---|
| ev_ebit_vs_sector | 141/198 | 71% |
| fcf_yield | 142/198 | 71% |
| pe_vs_own_history | 147/198 | 74% |
| roic_5y | 134/198 | 67% |
| debt_to_ebitda | 105/198 | 53% |
| gross_margin_stability | 86/198 | 43% |

**A6 gate (ev_ebit & fcf_yield ≥ 60%): PASSED (71% / 71%).**
(`debt_to_ebitda` and `gross_margin_stability` remain below 60% but are not
part of the gate; residual gaps are true XBRL data gaps.)

## Follow-ups

- A6 gate follow-up (2026-07-06): ev_ebit coverage 43% < 60%. Two known
  causes: (1) the coverage metric counts unprofitable names (EBIT ≤ 0) as
  "missing" although the screener saw the data and judged the bar; (2)
  `EBIT_CONCEPTS` is a single tag (`OperatingIncomeLoss`) with no fallback
  for filers that reconstruct via pretax + interest. Tuning task written up
  as Task 0 of docs/superpowers/plans/2026-07-06-phase-b-brain.md — must be
  resolved before the Phase B brain runs on real hits.
