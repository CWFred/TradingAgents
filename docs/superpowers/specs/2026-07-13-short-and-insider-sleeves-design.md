# Short Sleeve + Insider-Cluster Sleeve — Design

Two new paper sleeves alongside the existing three (momentum, research,
baseline). Each gets its own journal, its own bucket of paper cash, and its
own scorecard — the established isolation pattern ("third-ledger isolation",
`ops/research/trading.py`). Purpose, per the operator: more trades and more
resolvable memos, faster, to stress-test the system while everything is
still paper money.

- **Short sleeve** — bets on stocks going *down*, sourced from EDGAR red
  flags. The aggressive one: it exercises machinery nothing currently tests
  (a bear-authored thesis, inverted falsifiers, positions whose loss is
  unbounded).
- **Insider-cluster sleeve** — mechanically buys small-caps where several
  insiders just bought with their own cash. The throughput one: cheap
  signals, fast resolution (~90 trading days), lots of corpus entries.

Glossary used throughout: *short* = sell borrowed shares now, buy them back
("cover") later; profit if the price fell. *Cluster buy* = ≥2 distinct
insiders making open-market purchases (SEC Form 4, transaction code P) not
under a 10b5-1 plan (a pre-scheduled, non-discretionary trading plan —
those buys carry no information). *ADV* = average daily dollar volume, the
liquidity yardstick all position caps key off.

## Decisions that shape everything else

1. **Per-sleeve memo stores, not a shared one.** The research trade step
   enters from `memo_store.open_memos()` with no thesis-type filter
   (`ops/research/trading.py:160`), and the vetting stage sweeps every
   `pending_vetting` memo. Putting short or insider memos in the shared
   store would cause the research sleeve to buy them long. Each new sleeve
   gets its own sqlite `MemoStore` path in `OpsConfig` (mirrors how
   `baseline_journal_path` / `research_journal_path` already work), with
   `OPS_*` env overrides. Rejected alternative: a `sleeve` discriminator
   field on `Memo` — touches every existing query site and risks silent
   contamination if one site forgets to filter; separate files fail closed.

2. **A dedicated `ShortPaperBroker`, not signed quantities in
   `PaperBroker`.** `PaperBroker` is long-only by construction: `_fill_sell`
   raises without an existing position, journal replay assumes
   buy-then-sell, the position guardian and reconciler assume long books.
   Retrofitting signed quantities puts a live paper system's replay
   correctness at risk. A separate broker class with short-native semantics,
   writing only to the short journal, cannot break the other four books.

3. **New sleeves follow the existing job pattern.** APScheduler jobs in
   `ops/main.py`, scheduler-safe wrappers (errors journaled as events, never
   raised), once-per-day gates via a summary event in the main journal, and
   the daily overview + dashboard pick the new journals up by config path.

4. **Both sleeves respect the deny-list** (`ops/universe` deny rules) at
   entry, same as every other sleeve.

5. **LLM budget note (ds4).** The short sleeve's brain/vetting stages run
   inside the existing overnight window (00:00–pre-market drain deadline)
   sharing the queue-and-drain machinery; the insider sleeve's memo pass is
   one cheap structured call per entry. Nothing new runs during market
   hours.

---

## Sleeve 4 — Short sleeve

### Thesis and edge

The system's stated edge — reading filings at scale where nobody else does —
cuts hardest on the short side: thin-coverage small-caps hide negative
information in footnotes. Sources of thesis: going-concern doubt, 8-K item
4.02 (non-reliance on past financials — a restatement), 1.03 (bankruptcy),
3.01 (delisting notice), CFO departures (5.02), insider *sell* clusters,
deteriorating fundamentals behind promotional optics.

### Broker: `ops/broker/short_paper.py`

New `ShortPaperBroker` implementing the `Broker` interface against the
short journal only.

- **Sides.** Two new `Side` enum members: `SHORT` (sell-to-open) and
  `COVER` (buy-to-close). Additive — every existing comparison is an
  explicit `BUY`/`SELL` check, so no behavior change elsewhere.
- **Cash model.** A short fill credits proceeds to cash and opens a
  negative-exposure position. Equity = cash − Σ(qty × current price) over
  open shorts. Example: $10,000 cash, short $400 of XYZ → cash $10,400,
  liability $400, equity still $10,000; XYZ −10% → liability $360, equity
  $10,040.
- **Replay.** `from_journal` replays SHORT/COVER fills symmetrically to
  BUY/SELL (same notional-from-order-row discipline, same orphan-event
  journaling for a COVER with no prior SHORT).
- **Stops invert — and live in the trade step, not the broker.** A short's
  stop sits *above* entry. Like the research sleeve (whose orders never set
  `stop_pct`; sell rules are memo-driven), the short sleeve enforces its
  hard stop in the trade step from the position's average entry price.
  `Order.stop_pct` stays long-only; SHORT/COVER orders reject it.
- **Unrealized P&L** for a short position: `(avg_entry − current) /
  avg_entry` (sign-flipped from long).

Guardrails: the sleeve uses a pure fence module (below) like the research
sleeve, not the `ops/guardrails` Rule chain — same precedent, same reason
(the chain can't see sector/ADV/memo).

### Screen: inverted bars + mandatory red flag

Runs in the overnight tick over the same small/mid-cap universe cache the
long screen uses. A name is a short candidate only when it is
(a) statistically expensive/deteriorating AND (b) carries a **red-flag
trigger** — the mirror of "cheap AND change trigger". Bars (tunable
constants in `ops/research/short_screen.py`):

- EV/EBIT > 20× or EBIT ≤ 0 with market cap > $500M
- net debt/EBITDA > 4×
- gross margin declining ≥ 3pp year-over-year
- pass = expensive/deteriorating on ≥ 2 bars

(All three computable from the `Fundamentals` fields the long screen and
metrics registry already use; an FCF-yield bar can be added later if the
fundamentals layer grows an FCF series.)

Red-flag triggers (≥ 1 required, 90-day lookback):

- 8-K items 4.02, 1.03, 3.01 (already labeled in `edgar.NOTABLE_8K_ITEMS`)
- CFO departure (5.02 filtered to principal-financial-officer language)
- insider sell cluster: ≥ 3 distinct insiders, open-market sells (code S),
  non-10b5-1 — higher bar than the buy cluster because routine selling is
  common
- going-concern language via EDGAR full-text search over the latest 10-K/10-Q

### Memo: `thesis_type="short"`

- `ThesisType` becomes `Literal["value", "event", "short"]`; new
  `ShortThesis` block (`short_block`), `block_matches_type()` updated:
  - `overvaluation_mechanism` — why the market prices it too high, named
    specifically (the mirror of `why_cheap`)
  - `red_flags` — the specific disclosures driving the thesis (each must
    also appear in `evidence` with accession citations)
  - `why_now` — the trigger; shorts bleed carry, so timing is part of the
    thesis
  - `squeeze_risk` — crowded-short/low-float assessment (prose; no borrow
    data in paper)
  - `downside_scenario` — what the stock is worth if the thesis plays out
- **Price-target semantics invert:** `price_target_low` is the profit
  target (cover when quote ≤ target_low); `price_target_high` is the
  thesis-wrong level. The short trade step encodes this; the schema fields
  are unchanged.
- Falsifiers are written normally (metric/operator/threshold) — for a short
  they describe *improvement* (e.g. `gross_margin_pct > X` for 2 quarters).
  Machine-checking is direction-agnostic, so `evaluate_falsifier` needs no
  change.
- Monitor: `DRAWDOWN_ESCALATION_PCT` must become direction-aware — for a
  short, "drawdown" = price *rising* vs cost. `MetricContext` gains the
  memo's direction; the −30% escalation applies to the adverse move.

### Brain + vetting

- Brain: a short-specific debate prompt set in `ops/research/brain.py`
  (the bear authors, the bull attacks — roles swap). Same two-stage
  evidence→draft orchestration, writing `pending_vetting` memos to the
  short store.
- Vetting: same graph, **inverted mapping** in code (no agent prompt
  changes, same philosophy as today): graph rating Sell → confirm/high,
  Underweight → confirm/medium, anything else → reject. The brief presents
  the stock neutrally; the graph rates the stock, and a Sell rating is
  what confirms a short.

### Sizing fences: `ops/research/short_sizing.py`

| fence | value | vs research sleeve | rationale |
|---|---|---|---|
| tiers | starter 1% / medium 2% / high 3% | half | unbounded loss |
| name cap | 5% at cost | 10% | one squeeze can't dominate |
| sector cap | 15% | 25% | short sectors correlate in squeezes |
| ADV cap | 2% of 20-day dollar ADV | 5% | must be able to cover fast |
| gross short exposure | ≤ 50% of sleeve equity | n/a | margin discipline |
| min order | $100 | same | |
| price floor | ≥ $5 | same (universe) | sub-$5 borrow is fiction |

### Trade step + exits

`ops/research/short_trading.py`, mirroring `trading.py` structure (exits
before entries, one memo = one position lifecycle, closed-memo guard).
Exit reasons, first match wins:

1. memo missing / resolved
2. falsifier tripped (journaled by monitor)
3. **hard stop**: price ≥ entry × 1.25
4. **target hit**: price ≤ `price_target_low`
5. **time stop**: held > min(expected_holding_months, 9 months) — shorts
   are never buy-and-hold

Daemon jobs: short stages join the existing overnight window (screen →
brain queue → vet → drain, sharing the deadline/pause machinery); the
trade step runs post-close alongside `research_trade` (e.g. 16:27 ET).

### Paper-fidelity caveats (recorded, not solved)

Paper shorting ignores borrow cost/availability, hard-to-borrow lists, and
squeeze dynamics — results will flatter the sleeve. Acceptable: the goal is
exercising the machinery and building the corpus, and these are exactly the
lessons to have learned *before* any live shorting is ever contemplated
(which is far out of scope).

---

## Sleeve 5 — Insider-cluster sleeve

### Thesis and edge

Officers and directors buying their own small-cap with personal cash,
several of them at once, outside scheduled plans, is one of the
best-documented public signals. The system already detects it
(`ops/research/triggers.py::find_insider_cluster_trigger`, form4 pipeline)
but only as a *trigger* feeding deep research. This sleeve trades the
signal directly and mechanically — which also makes it the experiment that
measures what the expensive LLM funnel adds over a raw signal.

### Signal scan: `ops/insider/scan.py`

Per-ticker Form 4 polling across ~1500 names nightly would hammer SEC rate
limits. Instead, scan EDGAR's **daily index** once per night: list the
day's Form 4 filings, intersect issuer CIKs with the small/mid-cap universe
cache, fetch and parse only those XMLs (reusing
`tradingagents/dataflows/form4.py`), and persist transactions to a rolling
signal store (sqlite, `insider_signals`). Cluster detection then runs over
the store's 30-day window:

- ≥ 2 distinct insiders, each with ≥ 1 open-market buy (code P),
  non-10b5-1, each buy ≥ $10k
- cluster strength: **STRONG** if ≥ 3 buyers, or aggregate ≥ $250k, or the
  CEO/CFO participated; else **BASIC**
- per-name cooldown: a name that produced an entry can't re-signal for 90
  days

### Entry/exit: `ops/insider/trading.py` (mechanical, no LLM gate)

Entries (post-close job, e.g. 16:29 ET), standard fences first:
deny-list, min order $100, ADV cap 5%, name cap 5% at cost, max 25 open
positions, cash clamp.

- BASIC cluster → 3% of sleeve equity; STRONG → 5%
- Exits, first match wins: **stop** −20% from fill; **target** +40%;
  **time stop** 90 trading days. No discretion anywhere.

### Memo-lite (non-gating)

Every entry also produces a compact memo — one structured LLM call, using
the existing `Memo` schema with `thesis_type="event"`,
`event_type="insider_cluster"` (already in the taxonomy), written to the
**insider memo store**, status `open`, `vetting=None`. Evidence cites the
Form 4 accessions; falsifiers restate the mechanical exits. The memo NEVER
gates the trade — if authoring fails, the trade stands and an error event
is journaled. Timing: the trade fills at the post-close tick, but the memo
is authored the following night *inside the shared overnight window* — ds4
must never be spun up outside that bracket (it is the sole guard against
two models holding ds4 at once). At exit the memo is resolved mechanically (realized vs
benchmark over the window, outcome label). This is what feeds the corpus at
high speed and lets calibration reporting later compare LLM-authored
conviction against a sleeve where conviction was never consulted.

---

## Configuration

New `OpsConfig` fields, each with `OPS_*` env override, validated > 0 where
money: `short_journal_path`, `short_memo_store_path`,
`short_screen_store_path` (the short screen keeps its own pending-hits
queue, same isolation logic as the memo stores), `short_starting_cash`
(default $10,000), `insider_journal_path`, `insider_memo_store_path`,
`insider_signal_store_path`, `insider_starting_cash` (default $10,000).

## Measurement

- Daily overview gains both sleeves' equity/cash/positions lines (it is
  already cross-sleeve; new journals are new inputs).
- Dashboard: `_sleeves_section` iterates configured journal paths — add the
  two new paths (coordinate with the in-flight ops-dashboard worktree).
- Scorecards: each sleeve vs its natural null — the short sleeve vs doing
  nothing (a short book must beat holding cash, since the market drifts
  up); the insider sleeve's memo-lite corpus vs the research sleeve's
  vetted corpus in the quarterly calibration report (per-store report
  runs).

## Error handling

Same discipline as every existing sleeve: per-name failures logged and
skipped (a sweep never dies on name #937), scheduler wrappers journal
`*_error` events instead of raising, quote outages fall back to last-fill
marks (short equity uses the same `_equity_with_fallback` pattern —
conservatively marking a short at last fill when unquotable), and the
baseline's majority-outage guard pattern applies to any auto-write-off
logic.

## Testing

TDD throughout, mirroring the existing test layout (`tests/ops/research/`,
`tests/ops/broker/`):

- `ShortPaperBroker`: fill/cover math, equity under price moves, replay
  round-trip (including orphan COVER), stop resolution above fill
- schema: `thesis_type="short"` block matching, validation
- vetting: inverted rating map
- short screen bars + each red-flag trigger with canned filings
- short sizing fences (each cap, exposure cap)
- short trade step: every exit reason, closed-memo guard, exits-free-room
- insider scan: daily-index parse, CIK intersection, cluster + strength +
  cooldown, 10b5-1/sale exclusion
- insider trading: sizing tiers, every exit, memo-lite failure is non-fatal
- config: new fields + env overrides

## Build order

1. `Side.SHORT`/`COVER` + `ShortPaperBroker` (+ replay) — pure, no wiring
2. Memo schema `short` thesis type + `ShortThesis` + validation
3. Short screen (inverted bars + red-flag triggers)
4. Short brain prompts + inverted vetting map
5. Short sizing + trade step + monitor direction-awareness
6. Config + daemon jobs + overnight wiring for the short sleeve
7. Insider signal store + daily-index scan + clustering
8. Insider trade step + memo-lite + config + jobs
9. Daily overview + dashboard + runbook/docs updates

Steps 1–6 and 7–8 are independent tracks; 9 lands last.

## Out of scope

Live shorting (ever, without a separate design), borrow-cost modeling,
short-interest data feeds, options/leverage, changes to the existing three
sleeves' behavior, the position guardian / `ops/guardrails` Rule chain.
