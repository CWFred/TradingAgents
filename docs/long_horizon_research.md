# Long-Horizon Research System — Design

This document records the design for evolving the project from a short-horizon
post-earnings momentum executor into a long-horizon fundamental research
system for small/mid-cap equities. It captures decisions, not aspirations:
each section states what we are building and why the alternative was rejected.

## Premise

The defensible edge for an LLM agent system in markets is **not** price
prediction. It is:

1. **Reading at scale where coverage is thin.** Nobody reads the 3000th
   Russell name's proxy statement. An LLM that reads every filing in the
   small/mid-cap universe holds information most market participants don't.
2. **Structural patience.** Funds with quarterly reporting can't hold theses
   that look wrong for six months. A personal account can. Holding periods of
   months to years put us in competition with fundamental analysts, not
   colocated HFT infrastructure.
3. **Capacity-free alpha.** Illiquid names institutions can't size into are
   accessible at personal-account scale. Illiquidity is a subsidy here, not a
   risk — position caps are set against ADV instead of excluding names.

Speed edges and pure-math edges (stat arb, options, intraday) are explicitly
out of scope: LLMs add nothing where the input is numbers, and the
competition there is fully professionalized.

## System shape: a funnel

Universe → screener → deep research → sizing → monitoring. Each stage narrows
the candidate set and costs more per name.

| Stage | Names | Cost | Mechanism |
|---|---|---|---|
| Universe | ~1500 | none | deterministic filters ($300M–$10B cap, >$2M ADV, >$5) |
| Screener | ~100 | cheap | valuation + quality bars AND ≥1 change trigger |
| Deep research | 5–15/wk | expensive | full agent team → structured memo |
| Portfolio | 15–25 | — | conviction tiers + correlation caps |
| Monitoring | continuous | cheap | mechanical falsifier checks, escalate on trip |

A name enters deep research only when it is (a) statistically cheap vs
quality and (b) has a **change trigger** — a reason to look *now*: insider
cluster, 13D, CEO change, guidance-cut selloff, spinoff. Looking at
everything all the time drowns in noise.

**The mandatory null baseline:** an equal-weight paper portfolio of
everything that passes the screener, running alongside the full system. The
question is never "did we beat the index" — it is "did the LLM stages beat
the screen alone by more than the token bill." Without this control the
system's value is unmeasurable.

## Two thesis sleeves, one chassis

- **Value + change trigger** (`thesis_type="value"`): mispriced earning
  power. Anchored on normalized earnings; monitored against quarterly
  fundamentals; horizon 12–24 months. The bear must answer *why is it cheap*
  with a specific named reason — cheap stocks are cheap for a reason most of
  the time, and the job is finding the exceptions.
- **Event / forced seller** (`thesis_type="event"`): a known non-economic
  seller (index funds dumping a spinoff, tender expirations, post-bankruptcy
  orphans). The mispricing mechanism is identified *ex ante*; monitored
  against hard calendar dates; horizon 3–9 months. Resolves 2–3× faster,
  which also feeds the calibration corpus sooner.

Both sleeves share the EDGAR pipeline, memo schema, monitoring loop, and
journal. Monitoring, sizing caps, and outcome analysis dispatch on
`thesis_type`.

## The memo (`tradingagents/memos/`)

The unit of research output is a **structured investment memo**, not a
buy/sell signal. Every deep-research pass produces one; every position links
to one; sell rules read from one. Key schema decisions:

- **Evidence must cite sources** (accession number + section). Uncited claims
  are stripped — the structural defense against debate-stage confabulation.
- **Falsifiers are pre-committed at entry** and machine-checkable where
  possible (`metric`/`operator`/`threshold`), so the "thesis violation" sell
  rule is enforceable and the weekly monitor is mostly mechanical.
- **Scenario probabilities are calibration data, never sizing inputs.**
  LLM-stated probabilities are uncalibrated; sizing uses conviction tiers
  (starter 1–2%, medium 3–5%, high 5–8%). Kelly-style sizing is allowed only
  after the resolved corpus proves the probabilities are calibrated.
- **Passed candidates are shadow-tracked.** A memo we researched but didn't
  buy still resolves; selection skill is measured by bought-vs-passed
  outcomes, and the corpus grows much faster than positions alone.

**Resolution** happens when the position closes (or the shadow window
lapses): realized return vs benchmark over the identical window, which
falsifiers tripped, which catalysts landed, and an outcome label on the
right/wrong-process × made/lost-money 2×2. The off-diagonal labels matter
most: `thesis_wrong_made_money` is luck and must not be rewarded when the
corpus is used for calibration or fine-tuning.

**Corpus usage is wired, not remembered:** precedent retrieval
(`precedent_memo_ids`) is a mandatory memo field — "none found" is an
explicit finding — and resolution surfacing (`due_for_resolution`) is a
scheduler job, not a human habit. Embedding-based similar-situation lookup is
deferred until ~30–50 resolved memos exist; below that the corpus is too
small to retrieve from and hit-rate statistics are noise.

## The data layer (`tradingagents/dataflows/edgar.py`)

EDGAR is the foundation because it is free, complete for every US filer, and
timestamped at filing time — point-in-time discipline (no restated-data
lookahead) for free. Tractability map for the event sleeve:

- **Fully tractable, one pipeline:** Form 4 insider clusters (structured XML,
  10b5-1 checkbox since 2023), SC 13D/A activists (Item 4 + letters in
  exhibits), 8-K item taxonomy (4.02 restatements, 5.02 departures), Form
  10-12B spinoffs, SC TO-I/TO-T tenders.
- **Free but not EDGAR:** index reconstitution (FTSE Russell / S&P press
  releases; scraped).
- **Deferred:** post-bankruptcy equity (PACER/RECAP, separate messy
  pipeline — highest alpha, built last); earnings call transcripts (not filed
  anywhere; paid feeds have the worst small-cap coverage — ASR on IR webcast
  audio is the single-person workaround).

For per-name research, **long-context reading beats RAG**: the signal is
often what *changed* in the language year-over-year, which snippet retrieval
destroys. The planned `diff_filing_sections` tool (deterministic section
alignment across filing years) ranks above any embedding index. EDGAR's own
full-text search API covers corpus-wide queries.

## What carries over from the existing system

The ops chassis is strategy-agnostic and survives nearly untouched: journal
(source of truth for money; memo store is the source of truth for
reasoning, linked by `memo_id`), guardrails engine, broker layer,
scheduler, notifications, LLM provider layer, agent graph machinery. The
things that change: universe (S&P 500 → small/mid-cap), cadence (30-minute
ticks → nightly/weekly batch), analysts (API summaries → actual filings),
and the strategy layer (a new sleeve implementing `ops.strategy.base`
alongside post-earnings momentum).

## Build order

1. ✅ Memo schema + store (`tradingagents/memos/`) — everything imports from it
2. ✅ EDGAR client (`tradingagents/dataflows/edgar.py`) — list/fetch/search + trigger classification
3. ✅ Small/mid-cap universe + point-in-time screener + null-baseline
   portfolio (`ops/universe/smallcap.py`, `ops/research/`, `ops screen` —
   see docs/research_screener.md)
4. ✅ Filing-reader agent tools (`read_filing_section`, `diff_filing_sections`,
   `get_insider_transactions`, `get_past_memos`)
5. ✅ Thesis-type-aware memo pipeline (ops/research/brain.py; two-stage, deterministic orchestration) — debate prompts per sleeve
6. Monitoring loop: mechanical falsifier checks + `due_for_resolution`
   scheduling, escalation to full re-analysis on trip or −30% drawdown
7. Sizing layer on the existing guardrails engine (tier caps, sector ≤25%,
   single name ≤10% at cost)
8. Later: similar-situation embedding lookup over the resolved corpus;
   quarterly calibration report (stated probabilities vs realized)

Two full years of paper trading before real money — not only to prove the
strategy, but to build the human pattern recognition needed to spot when the
system is confidently wrong.
