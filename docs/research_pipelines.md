# Two analysis pipelines, one spine — and why

There are **two** LLM analysis pipelines in this system, and they are easy to
confuse because both "read stuff and reason about a stock." This document
exists so you don't have to re-derive which is which, what they share, and —
with measured evidence — why they are deliberately kept separate. If you ever
think "why isn't this all one pipeline?", read the [decision](#why-two-not-one)
section: the question was tested, not assumed.

## The one-paragraph version

The **momentum pipeline** is the upstream TradingAgents multi-agent graph — 4
analysts + a bull/bear + risk debate over LLM tool-calls — and it emits a fast
daily **BUY/HOLD/SELL signal**. The **research brain** is a purpose-built
two-stage pipeline that emits a deep weekly **structured memo** (thesis, cited
evidence, machine-checkable falsifiers, targets, conviction tier). They are
different tools for different jobs. **Everything downstream of their output is
shared and does not care which produced it.**

## The shared spine (producer-agnostic)

This is the important part for de-confusing things: almost the entire system is
**shared** and sits *below* both producers. A memo is a memo no matter who
wrote it; a decision is a decision. The shared pieces:

| Shared component | Path | Used by |
|---|---|---|
| **Memo schema** (the contract) | `tradingagents/memos/` | research brain writes it; Phase C/D read it |
| **EDGAR data + primitives** (filings, section extraction, YoY diff, Form 4, fundamentals) | `tradingagents/dataflows/edgar.py`, `edgar_sections.py`, `form4.py`, `fundamentals.py` | both |
| **Filing-reader `@tool` wrappers** | `tradingagents/agents/utils/filing_reader_tools.py` | available to the graph; the brain calls the primitives under them directly |
| **LLM provider registry + structured output + managed ds4 backend** | `tradingagents/llm_clients/`, `tradingagents/agents/utils/structured.py`, `ops/llm_backend.py` | both |
| **Mechanical validation gate** (anti-garbage) | `ops/research/memo_validation.py` | the research path (and any future memo producer) |
| **Screen queue** | `ops/research/store.py` | feeds the research brain |
| **Sizing, monitor, trade, resolve, report** (Phase C/D) | `ops/research/{sizing,monitor,trading,resolution,report}.py` | consume memos, **producer-agnostic** |

So the "two pipelines" are really **two thin producers on top of one large
shared base.** The split is at the *reasoning* layer only.

## The two producers

### 1. The momentum pipeline — the multi-agent graph

- **What it is:** the upstream `TradingAgentsGraph` (langgraph) — market /
  sentiment / news / fundamentals analysts, each a real `bind_tools` +
  `ToolNode` tool-calling loop, then a bull/bear research debate, a trader
  plan, and a risk debate. Entry point: `ops/pipeline_adapter.py` →
  `tradingagents/graph/`.
- **Its job:** a **fast BUY/HOLD/SELL signal** for one momentum candidate,
  **daily** (once per trading day via the `daily_cycle_run` gate).
- **Its output:** a decision word (via `SignalProcessor`). Not a memo.
- **Unchanged this system's lifetime** — `tradingagents/graph/` was not
  modified when the research sleeve was built.

### 2. The research brain — the deterministic memo pipeline

- **What it is:** `ops/research/brain.py`. Two stages: (1) per-filing-section
  **cited-evidence extraction** (`bind_structured` into `EvidenceBatch`, one
  bounded call per section, uncited items stripped mechanically), then
  (2) **bear-case-first thesis + memo emission** (`bind_structured` into
  `MemoDraft`). Plain Python decides what to read; the LLM only ever answers
  bounded structured-output prompts. **No agentic tool loop.**
- **Its job:** a deep **structured memo** for one screener passer, **weekly**
  (Saturday, `ops research run --max-names 10`).
- **Its output:** a validated `Memo` — thesis, cited evidence, machine-checkable
  falsifiers, price targets, conviction tier — or a rejection (garbage is not
  stored).

## Why two, not one?

This was the obvious question — "the graph is right there, why not use it for
research too?" — and it was **measured on ds4**, not assumed. Record of the
decision so it is not re-litigated:

### The original rationale is retired

The research brain's docstring historically cited "local models loop on
tools." **That is not true on ds4.** Measured 2026-07-08, full multi-agent
graph on ds4 @ 1M context:

| Name | Converged? | Wall-clock | ds4 requests |
|---|---|---|---|
| AAPL (mega-cap) | ✅ clean, HOLD | 26.4 min | 21 |
| AEO (mid-cap) | ✅ clean, HOLD | 32.4 min | 24 |

The tool-calling loop **converges cleanly** — bounded, decelerating (not
spiralling), robust even when a tool fails (Reddit rate-limited mid-run and the
loop continued). At 10 names/Saturday that is ~5 hours — affordable. So "ds4
can't loop on tools" is **false** and is not the reason for the split.

### The real reason: different jobs + measured efficiency

Head-to-head on the same name (AEO), same ds4:

| | Multi-agent graph | Research brain |
|---|---|---|
| Converges | ✅ | ✅ |
| Wall-clock | 32.4 min | **18.9 min** |
| ds4 requests | 24 | **14** |
| Output | a `HOLD` **word** | a **memo**: 32 cited evidence items, 4 machine-checkable falsifiers, tier, targets |

The brain is **~1.7× faster, ~1.7× cheaper, and already emits exactly the
structured cited memo the research sleeve needs.** The graph emits a trading
signal; to make it produce a memo you would have to *add* a memo-emitter stage,
costing **more** than 24 requests, plus solve keeping evidence citations
resolvable through an agentic pipeline. Migrating research onto the graph would
make it slower and more expensive to gain broader multi-agent reasoning — a
quality bet that was not worth the cost given the brain already does the job.

### The gate that makes weak-model output safe

Either way, the piece that earns its keep is the **mechanical validation gate**
(`memo_validation.py`). In the head-to-head run it caught the brain trying to
cite a bogus precedent id (`"none found"` as a literal string), **rejected the
memo, fed the error back, and the retry produced a clean one.** That is the
structural defense against confabulation — it belongs to the shared spine, not
to either producer.

## Naming — say which one

To stop the conflation, name them explicitly:

- **"the momentum pipeline"** or **"the multi-agent graph"** — the daily
  BUY/HOLD/SELL signal (`tradingagents/graph/`).
- **"the research brain"** or **"the two-stage memo pipeline"** — the weekly
  structured memo (`ops/research/brain.py`).

Avoid the bare phrase "the deep research pipeline" — it is ambiguous.

## When you would revisit this

The only reason to merge them onto the multi-agent base would be to buy
**reasoning breadth** for research quality — four analyst perspectives plus an
adversarial bull/bear/risk debate, rather than the brain's two stages. That is
a *quality* bet, and it was not measurable from the cost probes above. If you
ever decide it is worth it, the migration shape is known: run the multi-agent
graph for research, replace `SignalProcessor` with a **memo-emitter node** that
feeds the graph's accumulated state (the analyst reports + debate + trader plan)
into `bind_structured(MemoDraft)`, and keep the **validation gate** as the
acceptance layer. Everything downstream (sizing, monitoring, trading, resolve,
report) is already producer-agnostic and would not change.

Until then: two producers, one spine, on purpose.
