# Graph-vetted research memos — brain researches, agents decide

**Date:** 2026-07-10
**Status:** design — awaiting review
**Author:** brainstormed with the operator

## Problem

The research sleeve's memos are authored solely by the **research brain**
(`ops/research/brain.py`) — a two-stage, single-shot structured pipeline. The
multi-agent **TradingAgents graph** (`tradingagents/graph/`: four analysts +
bull/bear + risk debate) — the system's more trusted reasoning engine — is used
only by the momentum sleeve and never touches a memo. The brain was chosen for
research on **cost/format grounds** (measured ~1.7× cheaper than the graph and it
emits the memo shape directly), **not** because it reasons as well as the debate.
Two problems follow: (1) the brain is brittle — its whole thesis is one
structured call, so on a large, messy filing set it returns an empty memo (ACM /
AECOM did exactly this, 2026-07-09); (2) the memo reflects one model's judgment
with no adversarial review. The operator trusts the multi-agent debate to make
the *call* and wants it in the loop.

## Insight

Play each component to its strength instead of choosing one:

- **Brain = researcher.** Cheap, thorough at pulling **cited evidence out of
  filings** (the analysts' most tool-heavy job) and producing a structured
  first-cut memo.
- **Graph = decider.** Four perspectives + adversarial bull/bear/risk debate make
  the actual buy/no-buy call, with the brain's evidence injected as context.

The brain's memo becomes high-value **input** to the graph, not the final word.

## Goal

A two-stage funnel: the brain researches **every** screen passer into an
evidence-rich first-cut memo; the graph then **vets only the brain's `buy`
memos**, with the memo distilled and injected as context, and returns a verdict
(confirm / reject / adjust conviction) plus risk-driven falsifiers. Only
**graph-confirmed** memos become tradeable. Everything downstream (sizing,
monitoring, resolution, calibration) is unchanged.

## Non-goals

- No change to the momentum sleeve or to the graph's analyst/debate logic itself
  (only a new injected-context field + a memo-emitting read of final state).
- No change to sizing, fences, monitoring, resolution, or the calibration report.
- No change to what the screener screens or to the brain's evidence/thesis stages.
- Not retiring the brain — it stays as the researcher/first-cut producer.

## Current state (verified 2026-07-10)

- **Brain** drains pending screen hits into memos overnight (`_research_overnight_tick`,
  the deadline-boxed drain shipped 2026-07-09). A stored `Memo` *is* a buy thesis;
  `MemoStatus = Literal["open", "passed", "resolved"]`; a memo defaults to `open`.
  The brain's intermediate `MemoDraft` carries a `recommendation`; only
  recommended theses are stored.
- **Trade gate:** `ops/research/trading.py::_entry_pass` iterates
  `memo_store.open_memos()` (status `open`), sizes by `conviction_tier`
  (starter/medium/high), and opens paper positions. So **any `open` memo trades**.
- **Graph:** `TradingAgentsGraph.propagate(company_name, trade_date)`
  (`tradingagents/graph/trading_graph.py`) builds initial state via
  `Propagator.create_initial_state(...)`, runs the analyst→debate→risk graph, and
  returns `(final_state, process_signal(final_state["final_trade_decision"]))`.
  `final_state` exposes `market_report`, `fundamentals_report`, `sentiment_report`,
  `news_report`, `investment_debate_state` (bull/bear/judge), `risk_debate_state`.
- **Injection reality:** `past_context` is **already occupied** — set from
  `memory_log.get_past_context()` and consumed only by the portfolio manager. It is
  **not** a free slot and does not reach the analysts. A dedicated field is needed.
- **Adapter:** `ops/pipeline_adapter.py::TradingAgentsPipelineAdapter.propagate(
  symbol, asof_date)` wraps the graph and returns a `PipelineResult` with a
  BUY/HOLD/SELL `PipelineDecision`. It does not thread any research context today.
- ds4 is a single ~86 GB resource; the full graph is ~30 min/name. The overnight
  window is 00:00–08:00 (deadline-boxed, must free ds4 before the 09:00 momentum
  tick).

## Design

### 1. The funnel

```
Screen (~90 passers)
  └─ BRAIN (all passers, ~19min) — emits MemoDraft.recommendation ∈ {buy, pass}
        ├─ "pass" ─→ status = passed          (corpus / calibration; UNCHANGED from today)
        └─ "buy"  ─→ status = pending_vetting  (NOT traded; the vetting queue)
              └─ GRAPH VETTING (brain-buys only, ~30min)
                   memo distilled → injected as context → analysts+debate (augment)
                   → verdict: confirm | reject ; conviction ; risk falsifiers
                       ├─ confirm ─→ status = open      (tradeable; enrichment merged)
                       └─ reject  ─→ status = rejected  (never traded; corpus)
```

The brain **already** distinguishes buy vs pass by status (today: buy→`open`,
pass→`passed` via `mark_passed`). The only change to the brain is that a **buy is
written `pending_vetting` instead of `open`**; `pass` behavior is untouched. Only
`open` memos trade, so the graph becomes a required gate purely by that one status
change — the vetting stage is the only thing that promotes `pending_vetting`→`open`.
`_entry_pass` (`open_memos()`) is unchanged. No new recommendation field is needed:
`pending_vetting` *is* the set of brain-buys awaiting the graph.

### 2. Memo lifecycle + schema (`tradingagents/memos/schema.py`, `store.py`)

- Extend `MemoStatus` to `Literal["pending_vetting", "open", "rejected", "passed",
  "resolved"]` (adds `pending_vetting` and `rejected`).
- The brain writes a **buy** memo as `pending_vetting` (was `open`); the `pass`
  path (`mark_passed` → `passed`) is unchanged. This is a one-line change at
  `brain.py`'s save (status `"open"` → `"pending_vetting"` for the buy branch).
- Add `MemoStore.pending_vetting_memos()` returning all `pending_vetting` memos
  oldest-first — the vetting queue. Every `pending_vetting` memo is a brain-buy by
  construction, so no recommendation filter/field is required.
- Add an optional provenance block to `Memo`:
  `vetting: VettingResult | None = None` where `VettingResult` records the graph's
  `verdict` (`"confirm"|"reject"`), `decision` (BUY/HOLD/SELL word),
  `conviction_before`/`conviction_after`, `added_falsifier_indices`, a short
  `rationale` (judge decision summary), and `vetted_by_model`. Report-time
  provenance only — never a sizing input (mirrors `authored_by_model`).
- `open_memos()` unchanged (`status == "open"`). Monitoring/resolution unchanged.

### 3. Memo distillation → research brief (deterministic, no LLM)

New `ops/research/memo_brief.py`: `build_research_brief(memo: Memo) -> str`.
Selects from the already-structured memo — thesis, the type block (value/event),
`must_be_true`, the top-N cited `evidence` items (claim + source_ref + quote),
`falsifiers`, and the bear/why-cheap framing — into a compact, labelled string.
Bounded length (top-N evidence, truncation) so it never blows ds4's context. Pure
function, fully unit-testable.

### 4. Injection channel (graph augment mode)

- Add `research_memo_context: str` to the graph state
  (`tradingagents/agents/utils/agent_states.py`), defaulting to `""`.
- Thread it into `Propagator.create_initial_state(..., research_memo_context="")`
  and into `TradingAgentsGraph.propagate(..., research_memo_context="")` (kept
  separate from the memory-`past_context` path).
- Reference it in the **fundamentals analyst** prompt (its head-start on filings
  evidence) and the **bull and bear researcher** prompts (the debate reasons over
  the brain's cited evidence). When empty, prompts render exactly as today
  (momentum path unaffected — this is the key backward-compat guarantee).
- **Augment, not replace:** analysts still run their own tool loops. The brain
  covers filings/fundamentals; the graph's market/news/sentiment analysts add
  dimensions the brain never sees, and the debate stress-tests the brain's thesis.
- Extend `PipelineAdapter.propagate` to accept an optional
  `research_context: str = ""` and pass it through. `StubPipelineAdapter` accepts
  and ignores it (keeps tests/dry-runs cheap).

### 5. Adjudication output

`ops/research/vetting.py` orchestrates one memo's vetting:

1. `brief = build_research_brief(memo)`.
2. `result = adapter.propagate(memo.ticker, memo.as_of_date, research_context=brief)`
   → `PipelineDecision` (BUY/HOLD/SELL) + `final_state`.
3. **Verdict:** BUY → `confirm`; HOLD/SELL → `reject` (conservative: the debate
   must actively affirm the buy).
4. **Conviction + risk falsifiers:** a single bounded `bind_structured` call over
   the graph's `risk_debate_state` + judge decision → `{conviction_tier, added_
   falsifiers[]}`. Reuses the brain's structured-output plumbing and the
   **mechanical validation gate** (`ops/research/memo_validation.py`) so a
   malformed enrichment is rejected, not stored (same anti-garbage guarantee).
5. **Merge:** on `confirm`, set `conviction_tier` to the graph's call, append the
   validated risk falsifiers to `memo.falsifiers`, attach `VettingResult`, set
   status `open`. On `reject`, attach `VettingResult`, set status `rejected`.

No brain-recommendation field is needed on `Memo`: the vetting queue is exactly the
`pending_vetting` set (all brain-buys), since `pass` memos go straight to `passed`
and are never enqueued.

### 6. Scheduling (`ops/main.py`)

Graph vetting is a second overnight stage inside the existing 00:00–08:00 window,
running **after** the brain drain within `_research_overnight_tick` (or a sibling
tick sharing the same deadline): after the drain returns, vet `pending_vetting`
buy memos oldest-first until the **same 08:00 deadline** / queue empty / shutdown.
Reuses the deadline/shutdown-boxed loop shape from `ops/research/drain.py` (extract
a shared bounded-iteration helper if clean). ds4 is already up from the drain; one
`ensure_up`/`shutdown` brackets both stages. Whatever isn't vetted tonight carries
to the next night (memo stays `pending_vetting`). A `research_vetting_run` /
`research_vetting_error` audit event mirrors the drain events.

Budget note: brain (~19min) + graph (~30min) per confirmed name means fewer names
clear per night than brain-only. Acceptable — the funnel deliberately trades
throughput for decision quality, and the deadline guarantees ds4 frees before
09:00.

### 7. Downstream unchanged

Sizing, monitoring, resolution, calibration consume a `Memo` and are
producer-agnostic. A graph-vetted memo is still a `Memo`; only its provenance
(`vetting`, `authored_by_model`) differs.

## Data flow

```
overnight 00:00 (ops run)
  ├─ [screen if ≥3d]  → pending screen hits
  ├─ BRAIN drain      → buy → Memo(status=pending_vetting) ; pass → status=passed
  └─ GRAPH vetting (buys, until 08:00 deadline)
        build_research_brief → adapter.propagate(research_context=brief)
        verdict + conviction + risk falsifiers  (validated)
        → Memo.status = open | rejected ; VettingResult attached
weekday 16:25 (existing)
  └─ _entry_pass: open_memos() → size → paper position   (now only graph-confirmed)
```

## Error handling

- Vetting is scheduler-safe: any failure records `research_vetting_error` and
  leaves the memo `pending_vetting` (retried next night) — never raises out of the
  tick, never promotes a memo on error.
- A graph run that fails/empties for one memo marks nothing; the memo stays
  `pending_vetting`. Repeated failures on one name are visible via the audit event
  (a later change could add an attempt cap → `rejected` with reason).
- The validation gate rejects malformed enrichment; the memo stays
  `pending_vetting` for retry rather than storing garbage.
- ds4 backend torn down in a `finally` bracketing both overnight stages.

## Testing

- **Brief:** `build_research_brief` selects the right fields, bounds length, is
  deterministic; empty/edge memos handled.
- **Injection backward-compat:** with `research_memo_context=""`, initial state and
  rendered analyst/debater prompts are byte-identical to today (momentum path
  untouched); non-empty context appears in the fundamentals + bull/bear prompts.
- **Adapter:** `propagate(..., research_context=...)` threads through to
  `create_initial_state`; `StubPipelineAdapter` ignores it.
- **Verdict mapping:** BUY→confirm, HOLD/SELL→reject.
- **Merge:** confirm sets status `open`, applies graph conviction, appends
  validated falsifiers, attaches `VettingResult`; reject sets `rejected`, appends
  nothing, still attaches provenance.
- **Gate:** a `pending_vetting` memo is NOT returned by `open_memos()` (not traded);
  becomes tradeable only after confirm. `_entry_pass` unchanged behavior against
  the new lifecycle.
- **Validation:** malformed enrichment → memo stays `pending_vetting`, no promote.
- **Scheduling:** vetting runs after drain within the deadline, deadline/shutdown
  stops between memos, error recorded not raised, ds4 shutdown in finally.
- **Store:** `pending_vetting_memos()` returns only pending-vetting buys oldest-first.

## Rollout (ordering matters)

The brain switching to `pending_vetting` **stops the sleeve from trading** until
the vetting stage promotes memos. Therefore:

1. Land schema + store + brief + injection + adapter + vetting + scheduling
   **together** (one branch), so the moment the brain writes `pending_vetting`, the
   vetting stage exists to promote.
2. Grandfather the existing AEO `open` memo (already confirmed-equivalent under the
   old flow) — leave it `open` so nothing already trading is disrupted; a one-off
   note, not a migration.
3. Deploy to the live daemon; the next overnight cycle produces brain memos and
   graph-vets them in the same window.

## Open questions / risks

- **Throughput.** ~50 min/confirmed name halves nightly clearance vs brain-only.
  Accepted (quality over throughput). Revisit if the vetting backlog grows
  unboundedly — a per-night name cap or a smaller screen are the levers.
- **Graph brittleness on the same hard names.** ACM-class large-caps that break the
  brain won't even reach vetting (no buy memo). The graph is more robust
  (incremental tool state) but not immune; failures leave the memo
  `pending_vetting`, surfaced by the audit event.
- **Conviction source.** Graph conviction comes from a bounded structured pass over
  the risk debate, not the brain's tier — deliberate (the debate is the decider).
