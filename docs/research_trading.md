# Research Trading Runbook (Phase D — sizing and execution)

Phase D of docs/superpowers/specs/2026-07-06-finish-research-system-design.md.
Memos drive entries and exits. The trading step sizes under hard conviction-tier
fences and closes positions against multiple mechanical sell rules. Resolved
memos feed the calibration report.

## What runs when

| job | where | when | what |
|---|---|---|---|
| research_trade | ops daemon (APScheduler) | 16:25 ET mon-fri | entries by tier; exits on memo resolution, falsifier trip, or price target |
| research_monitor | ops daemon (APScheduler) | 16:20 ET mon-fri | falsifiers, drawdown, catalysts, resolution-due (happens before trading) |
| ops research resolve | manual | — | record resolution outcome and exit price for a closed position |
| ops research report | manual only | — | quarterly calibration report on resolved memos; no daemon job runs this |

The gate for `research_trade` is the `research_trade_run` event in the **ops
journal** (the main journal for the daemon). Manual: `ops research trade` (safe
anywhere; empty stores are a no-op).

**Resolve-before-fill note:** `research_monitor` runs first (16:20) and can
raise a `resolution_due` signal that a human acts on with `ops research
resolve` — which pins the corpus return to that day's close. The mechanical
ledger exit (falsifier trip / target hit / "memo missing") doesn't happen
until the next `research_trade` tick (16:25, or the next trading day if the
resolve happened outside that window). So the recorded corpus return
(resolve-day close) can diverge slightly from the ledger's realized P&L
(the actual fill price on the next 16:25 run).

## Conviction tiers and position sizing

Every open memo carries a `conviction_tier` (starter, medium, high). The trading
step sizes new entries under these rules:

| tier | portfolio % | rationale |
|---|---|---|
| starter | 2% | discovery phase; highest failure rate |
| medium | 4% | filtered through falsifiers; medium conviction |
| high | 6% | core thesis; confidence in both selection and timing |

These are **portfolio percentages** of the research equity bucket (independent
of the baseline equal-weight control portfolio), applied against equity at the
time of entry (`ops/research/sizing.py::TIER_SIZING`). There is no tier-sum
constraint — each entry is sized independently, subject only to the hard
fences below and the cash on hand.

## Hard fences (per-position limits)

Three immovable caps apply to every new entry (`ops/research/sizing.py`):

1. **Name-at-cost ≤ 10%** of research equity (`NAME_CAP_PCT`). A single
   thesis, no matter how high-conviction, cannot dominate the portfolio.
2. **Sector ≤ 25%** of research equity (`SECTOR_CAP_PCT`). Concentration
   guard for correlated risks. `UNKNOWN` is a real bucket from the smallcap
   universe cache and counts toward the sector limit.
3. **Position ≤ 5% of 20-day dollar ADV** (`ADV_CAP_PCT`). Liquidity fence.
   The market can absorb this size without material impact; larger sizes
   face execution risk and wider spreads. (Shorthand: ≤5% ADV.)

**Order floor:** `MIN_ORDER_DOLLARS` = $100; orders below this after all
fences are not placed (operational friction below this threshold).

All three fences are checked **at entry only** — they are not re-evaluated
against existing positions after that (no continuous rebalancing/trimming).
The first fence that binds rejects the order and records the reason in
`outcome.skipped`.

## Entries

Every memo with `status == "open"` is a candidate, oldest-first by
`created_at` (`memo_store.open_memos()`, sorted in `_entry_pass`). There is
no age cutoff — an old open memo is still eligible for entry until it is
resolved, exited, or closed.

For each candidate, in order:
1. Skip if the ticker is already held, or was closed earlier in this same
   run (same-run wash guard).
2. Skip if this memo's `memo_id` already has a `research_position_closed`
   event in the research journal — **one memo drives at most one position
   lifecycle.** A falsifier trip or target hit closes the position but does
   not resolve the memo (it stays `open`); without this guard the next run
   would re-enter the same memo and re-trip the same historical falsifier
   or target, thrashing enter/exit indefinitely until a human resolves it.
   Recorded as `"{ticker}: memo already had a position (closed)"` in
   `outcome.skipped`.
3. All three fences must pass (name ≤ 10%, sector ≤ 25%, position ≤ 5% ADV),
   sized by tier.

Orders are placed as **market orders at the current yfinance last price**
(`ops.quotes.make_yfinance_quote_source`, via `PaperBroker`) — there is no
midpoint calculation and no quote-freshness check. If no quote is available
(halt, delisting), the entry is skipped this run with a `"quote unavailable"`
reason and retried automatically on the next run.

**Note:** one unquotable HELD position (e.g. a delisting yfinance can no
longer price) blocks all *new* entries for that run once cash/fence room
runs out or the run otherwise wedges on it — v1 has no automatic recovery
for delistings; a human must resolve/write off the memo. When this happens,
`outcome.skipped` lines may name other candidate tickers that lost their
fence/cash room rather than the actual failing held position, so check
`outcome.errors` too when a run looks stuck.

Positions are recorded in the research journal (`research_journal.sqlite`)
with a `research_position_opened` event; `memo_id` links the journal entry to
the memo.

## Exits (first match wins, checked before entries each run)

Positions close immediately when any of these conditions is met
(`_exit_reason` in `ops/research/trading.py`):

1. **Memo missing:** the position's `memo_id` no longer resolves in the memo
   store (journal surgery or data loss). Reason recorded: `"memo missing"`.
2. **Memo resolved:** the memo's `status == "resolved"` (a human ran
   `ops research resolve`). Reason recorded: `"resolved"`.
3. **Falsifier tripped:** the main journal has a `falsifier_tripped` event
   for this memo's `memo_id`. Reason recorded: `"falsifier tripped"`.
4. **Price target hit:** the latest quote is ≥ `price_target_high` from the
   memo. Reason recorded: `"target hit"`.

Exits run **before** entries each trade run, so a name whose memo just
resolved (or whose falsifier just tripped) frees its sizing room in the same
run — but per the entry-pass guard above, that memo can never re-enter, even
in a later run.

All exits record a `research_position_closed` event in the research journal,
including the exit reason and fill price.

## Third ledger: research_journal.sqlite

The research portfolio is a separate, isolated ledger from the baseline
(equal-weight control) and momentum (post-earnings) systems. It lives in
`research_journal.sqlite` at the path `OPS_RESEARCH_JOURNAL_PATH` (see
ops/config.py). The trading step opens/replays only this journal via
`PaperBroker.from_journal`; the main/ops journal is touched for exactly two
things — reading `falsifier_tripped` events (written there by Phase C) and
writing the single `research_trade_run` summary event.

**Starting capital:** `OPS_RESEARCH_STARTING_CASH`, default 100000. This is the
pool from which all research positions are sized.

**Provenance events** (all in the research journal):
- `research_position_opened`: entry order filled; payload has `symbol`,
  `memo_id`, `conviction_tier`, `entry_date`, `client_order_id`, `notional`.
- `research_position_closed`: position closed; payload has `symbol`,
  `memo_id`, `reason` (one of `resolved`, `memo missing`,
  `falsifier tripped`, `target hit`), `exit_date`, `price`.

**Summary event** (in the main/ops journal):
- `research_trade_run`: recorded every run, whether or not anything traded.
  Payload: `{asof, entered, exited, skipped, equity, cash}` (`entered`/
  `exited`/`skipped` are lists of strings). Notification policy sends it as
  a `push` channel, `normal` urgency notification unconditionally
  (`ops/notify/policy.py`) — there is no size-based urgency escalation.

## Resolving positions: ops research resolve

When the monitor pushes a `resolution_due` event or when you manually close a
position (for any reason other than the mechanical exits above), record the
outcome and exit price:

```
ops research resolve MEMO_ID --label <LABEL> --narrative "..." [--exit-price P]
```

**Labels** (mutually exclusive 2×2, exact `click.Choice` values):
- `thesis_right_made_money`: Thesis held; position was profitable.
- `thesis_right_lost_money`: Thesis held; position was unprofitable.
- `thesis_wrong_made_money`: Thesis broken; position was profitable (luck).
- `thesis_wrong_lost_money`: Thesis broken; position was unprofitable.

**Numbers** are auto-computed if not provided (`ops/research/resolution.py`):
- **Exit price ladder:** explicit `--exit-price` > last research SELL fill by
  `filled_at` > current close. (If exits were never filled, we use today's
  close.)
- **Benchmark:** IWM (Russell 2000) over the identical holding window.
- **Return fractions:** 0.08 = 8%; computed as (exit − entry) / entry.

**Passed memos** (researched but not bought) are resolved the same way, but
the stored `exit_price` is unconditionally `None` (no fill exists to report),
even when `--exit-price` was supplied — the corpus records "what would have
happened" using that price for the return math, not a market execution that
never occurred.

## Inspecting positions and events

Research positions and their events live in the research journal:

```bash
sqlite3 ${XDG_STATE_HOME:-~/.local/state}/tradingagents/research_journal.sqlite \
  "SELECT at, kind, payload FROM events WHERE kind IN ('research_position_opened', 'research_position_closed') ORDER BY id DESC LIMIT 20"
```

The research_trade_run summary event (and all other trader signals) goes into the
main ops journal:

```bash
sqlite3 ${XDG_STATE_HOME:-~/.local/state}/tradingagents/ops_journal.sqlite \
  "SELECT at, kind, payload FROM events WHERE kind = 'research_trade_run' ORDER BY id DESC LIMIT 10"
```

To inspect a specific memo's positions:

```bash
sqlite3 ${XDG_STATE_HOME:-~/.local/state}/tradingagents/research_journal.sqlite \
  "SELECT payload FROM events WHERE kind IN ('research_position_opened', 'research_position_closed') AND json_extract(payload, '$.memo_id') = 'MEMO-ID-HERE'"
```

## Calibration report: ops research report

`ops research report [--output FILE]` is **manual only** — no daemon job
runs it. It reads only the memo store and the research/baseline equity
journals (no broker, no quotes, no LLM), so it is safe to run any time,
including day one before any memo has resolved.

Six sections (`ops/research/report.py`):

1. **Corpus:** counts by status/thesis_type/conviction_tier, oldest/newest
   memo date.
2. **Outcome 2x2:** the right/wrong-process × made/lost-money matrix, with
   counts and mean realized return per cell, plus the off-diagonal
   ("luck, not skill") callout.
3. **Scenario calibration:** stated (Σ probability × return) vs realized
   return, directional hit rate. Below `n=5` resolved+scored memos the
   section renders the honesty string `"corpus too small (n=N < 5) —
   numbers are noise"` instead of numbers.
4. **Bought vs passed:** mean realized return for positions actually opened
   vs memos that were shadow-tracked but never bought.
5. **Sleeve vs baseline:** equity return over the overlapping snapshot
   window between the research sleeve and the baseline (equal-weight
   control) portfolio.
6. **Per-model attribution:** resolved-memo stats (count, mean return,
   outcome-cell counts) grouped by `authored_by_model`.

Every section degrades to a literal "no data yet" when its inputs are empty.
