# TradingAgents — News-Driven Discovery Funnel

**Date:** 2026-07-05
**Status:** Design approved by user, ready for implementation plan
**Scope:** Research-only discovery layer for the ops service; no broker integration in this iteration

## Summary

Add a once-daily, pre-market discovery sweep to the always-on ops service. The sweep reads global financial news, funnels ticker mentions through hard liquidity/deny filters and a cheap LLM triage pass, runs the top 3–5 candidates through the existing TradingAgents multi-agent pipeline, and persists the resulting structured theses (rating, price target, time horizon, thesis, trigger conditions) to a new `theses` table in the ops journal. Output is a push/email digest plus archived per-symbol markdown reports. The funnel is **research-only**: it never imports the broker layer or places orders, but the data model and a single well-defined seam make later promotion to the trading loop a small, additive change.

## Background

Today the ops universe is a deterministic screen: S&P 500 members → deny list → recent earnings beats → liquidity floors. It can only surface companies matching rules written in advance, and only around earnings events. News-driven discovery adds an interpretive channel: any liquid US equity can surface when a materially interesting story breaks — including under-covered names the screen would never find.

The pipeline's Portfolio Manager already produces nuanced structured output (rating on a 5-point scale, executive summary, thesis, price target, time horizon — see `tradingagents/agents/schemas.py`), but `ops/pipeline_adapter.py` collapses it to BUY/HOLD/SELL and discards the rest. Nothing is persisted, so there is no track record to evaluate whether the pipeline's conviction levels predict anything. This design fixes both: the nuance gets a durable home, and discovery gets a disciplined, cost-capped funnel.

Design constraints that shaped this spec:

- **Interpretation, not reaction.** By the time news is public, the fast money has traded it. The value here is second-order reading and coverage of less-followed names, so a daily cadence is sufficient.
- **Cost discipline dominates.** Each full pipeline run is a multi-agent LLM pass. Every stage of the funnel exists to spend the expensive step on at most a handful of names per day, with hard caps.
- **Painless future broker integration.** The orchestrator already receives candidates through one injected callable (`universe_builder`). Discovery output carries every field a `Candidate` needs so promotion later is one new function at that seam, with all existing guardrails applying unchanged downstream.

## Goals

- A daily discovery sweep that can surface any liquid US equity, not just S&P 500 earnings beats.
- Hard, configurable cost caps at every stage; worst-case daily spend is fixed and known.
- A persistent `theses` store capturing the full structured PM output — the measurement infrastructure for future outcome scoring.
- Digest notifications through the existing notify dispatcher; full markdown reports archived per sweep.
- Structural (not flag-based) separation from order placement.
- A clean, documented promotion seam for later broker integration.

## Non-Goals (this iteration)

Recorded here so they are not lost; see "Deferred work" for how each is prepared for.

- Condition-triggered re-analysis (watching `trigger_conditions` against live quotes).
- Prior-thesis diffing on re-analysis (blind re-run + compare step).
- Broker promotion (`theses_candidate_source` feeding the trading loop).
- Outcome scoring (grading price targets against realized prices).
- Intraday discovery cadence.

## Architecture

A new `ops/discovery/` package hosts a `DiscoveryJob`, scheduled once per trading day pre-market (~8:00 AM ET) on the existing APScheduler, alongside the orchestrator tick and position guardian. A manual `ops discover --once` CLI entry runs the same job on demand.

### Data flow

```
get_global_news (Alpha Vantage, last 24h)      ~50–200 articles
  → aggregate ticker mentions                  AV tags articles with tickers,
                                               relevance scores, sentiment
  → hard filters                               deny list, SPOT blackout, held
                                               positions, 14-day cooldown,
                                               min price / min ADV; yfinance
                                               lookups capped at top ~30
                                               mentions by relevance weight
  → LLM triage (one batched cheap-model call)  materiality score 0–10 +
                                               catalyst type + rationale per
                                               candidate; rank
  → full pipeline for survivors                score ≥ triage_min_score,
                                               capped at max_full_runs (5),
                                               existing PipelineAdapter,
                                               run serially
  → persist + notify                           theses table + markdown report
                                               + journal events → digest
```

### Structural rules

- **Journal is the source of truth.** Every stage outcome lands as journal events (`discovery_sweep_started`, `discovery_triage`, `discovery_thesis`, `discovery_sweep_error`, `discovery_digest`). The notify dispatcher renders `discovery_digest` exactly like existing notification kinds.
- **No broker imports.** `ops/discovery/` never imports `ops/broker`; research-only is enforced by structure, not configuration.
- **Pipeline adapter exposes structured output.** `ops/pipeline_adapter.py` gains a method returning the structured PM report (rating, price target, time horizon, thesis/summary) alongside the existing collapsed decision. The trading path's conservative BUY/HOLD/SELL collapse is untouched.

### Promotion seam (future broker integration)

The orchestrator's `universe_builder` parameter is the single point where candidates enter the trading loop. To make later promotion additive:

- Thesis rows persist `last_price` and `avg_dollar_volume_20d` (already computed by the liquidity stage), so a thesis contains everything a `Candidate` needs.
- **Prep change in this iteration:** generalize the `Candidate` dataclass's earnings-specific field (`earnings: EarningsHit`) into a tagged `origin` field (e.g. `origin: EarningsHit | DiscoveryHit`). Mechanical change to `ops/universe/__init__.py` and the one strategy that reads it.
- Future integration is then one new function, `theses_candidate_source(asof_date, config) -> list[Candidate]`, reading active theses and composed with `build_universe` at the injection point. Strategy sizing, deny list, live-gate caps, and the guarded broker apply to discovered names identically — no guardrail changes.

## Components

Each stage is a small module in `ops/discovery/`, testable in isolation:

| Module | Responsibility |
|---|---|
| `feeds.py` | Wrap `tradingagents/dataflows/alpha_vantage_news.get_global_news`; normalize raw payloads into typed `Article` records (title, summary, url, published_at, ticker tags with relevance + sentiment). Only module that knows AV's JSON shape. |
| `mentions.py` | Pure function: articles → one `TickerMention` per symbol (mention count, max relevance, aggregate sentiment, contributing articles). Drops crypto/forex/ETF tags. |
| `screen.py` | Hard filters, reusing `apply_deny_list` and `fetch_price_and_adv_from_yfinance` from `ops/universe/filters`: deny list, held positions, cooldown (via theses table), min price / min ADV. Caps yfinance lookups at `max_screen_symbols`. Held positions arrive via an injected `held_symbols_provider: Callable[[], set[str]]` wired up at service composition time — `ops/discovery/` itself never imports `ops/broker`, keeping the no-broker-imports rule intact. |
| `triage.py` | One batched call to a cheap model with a strict JSON response schema: per-candidate materiality score 0–10, catalyst type (earnings, M&A, regulatory, product, macro, other), one-line rationale. LLM client injected. |
| `job.py` | `DiscoveryJob`: wires the stages, enforces `max_full_runs`, writes journal events, invokes the pipeline serially, triggers report rendering and digest emission. |
| `reports.py` | Renders full PM report + triage context to `<reports_dir>/YYYY-MM-DD/SYMBOL.md`. |

### Thesis store

New `theses` table in the existing journal SQLite, accessed through a `ThesesStore` class in `ops/theses.py`:

```
theses:
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  symbol TEXT NOT NULL,
  source TEXT NOT NULL,            -- 'discovery' | 'screen'
  rating TEXT,                     -- Buy / Overweight / Hold / Underweight / Sell
  price_target TEXT,               -- Decimal as text, journal convention
  time_horizon TEXT,               -- e.g. "3-6 months"
  thesis_summary TEXT,
  triage_score INTEGER,
  triage_rationale TEXT,
  source_articles TEXT,            -- JSON list (title, url, published_at)
  last_price TEXT,
  avg_dollar_volume TEXT,
  report_path TEXT,
  trigger_conditions TEXT,         -- JSON; parsed entry/exit conditions
  review_after TEXT,               -- DATE derived from time_horizon
  status TEXT NOT NULL             -- 'active' | 'superseded' | 'expired'
```

`trigger_conditions` and `review_after` are populated on write but consumed by nothing in this iteration — they exist so the deferred re-analysis watcher starts with data instead of blind.

### Configuration

`DiscoveryConfig`, env-overridable following the `OpsConfig` pattern:

| Field | Default | Purpose |
|---|---|---|
| `enabled` | `False` | Master switch; job not scheduled when off |
| `max_full_runs` | `5` | Hard cap on daily full-pipeline runs |
| `triage_model` | upstream `quick_think_llm` setting | Model used for the batched triage call; defaults to the pipeline's existing quick-thinking model config so provider choice stays in one place |
| `triage_min_score` | `6` | Materiality floor (0–10); candidates below it never advance, even if `max_full_runs` budget remains |
| `relevance_threshold` | `0.5` | Min AV relevance for a mention to count |
| `cooldown_days` | `14` | No re-analysis of a symbol within this window |
| `max_screen_symbols` | `30` | Cap on yfinance lookups per sweep |
| `reports_dir` | `${XDG_STATE_HOME:-~/.local/state}/tradingagents/discovery_reports` | Root for markdown report archive (same base-dir convention as the journal) |

### Digest

`discovery_digest` journal event carrying, per analyzed name: symbol, rating, price target, one-line thesis, report path. A new entry in `ops/notify/policy.py` renders it for push + email; the existing SPOT scrub applies.

## Error handling

- The scheduler entry point wraps the sweep; any unhandled failure lands as `discovery_sweep_error`. A discovery failure never affects the trading service.
- News fetch fails or returns garbage → abort sweep with event.
- One yfinance lookup fails → drop that symbol, continue.
- Triage returns malformed JSON → one retry, then abort sweep (no un-triaged guessing).
- One pipeline run fails → record event, continue with remaining survivors.
- ≥1 thesis produced → digest sent; zero → no digest (quiet days are silent).

## Cost controls

Worst-case daily spend is fixed: one news API call, ≤`max_screen_symbols` quote lookups, one batched triage call, ≤`max_full_runs` pipeline runs. The cooldown prevents a hot story from re-buying analysis daily.

## Testing

- `feeds.py`: recorded Alpha Vantage fixtures; malformed-payload cases.
- `mentions.py` / `screen.py`: pure-function unit tests with synthetic inputs, reusing existing filter test patterns.
- `triage.py`: stub LLM client with canned JSON, including malformed-JSON retry/abort path.
- `ThesesStore`: in-memory SQLite.
- `job.py`: integration-style test wiring all stubs plus `StubPipelineAdapter` (same pattern as orchestrator tests), asserting budget enforcement, cooldown behavior, journal event sequence, and digest emission.
- One opt-in live smoke test against the real AV feed, read-only, gated by env var (mirroring `OPS_RH_LIVE_TESTS`).

## Deferred work

| Item | Prepared by |
|---|---|
| Condition-triggered re-analysis (watcher on orchestrator tick evaluating `trigger_conditions` against quotes) | `trigger_conditions` populated from day one |
| Prior-thesis diffing (blind re-run, then cheap compare marking intact / strengthened / broken) | Theses persisted with full summaries; `review_after` set |
| Broker promotion | Promotion seam + `Candidate.origin` generalization (this iteration) |
| Outcome scoring (grade price targets vs. realized prices after horizon) | `price_target`, `time_horizon`, `created_at` persisted |
