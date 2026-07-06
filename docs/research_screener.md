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

## Form 4 note

Insider-cluster triggers are deferred to build-order step 4 (needs the Form 4
XML parser to separate open-market buys from routine sales/grants). EDGAR
triggers today: 13D/13D-A, notable 8-K items, 10-12B spinoffs, tenders,
going-private. Plus the price trigger: close ≥25% below the 60-day high.
