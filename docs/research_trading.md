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
| ops research report | manual or daemon | — | quarterly calibration report on resolved memos |

The gate for `research_trade` is the `research_trade_run` event in the **ops
journal** (the main journal for the daemon). Manual: `ops research trade` (safe
anywhere; empty stores are a no-op).

## Conviction tiers and position sizing

Every open memo carries a `conviction_tier` (starter, medium, high). The trading
step sizes new entries and existing positions under these rules:

| tier | portfolio % | constraint | rationale |
|---|---|---|---|
| starter | 2% | tier-sum constraint | discovery phase; highest failure rate |
| medium | 4% | tier-sum constraint | filtered through falsifiers; medium conviction |
| high | 6% | tier-sum constraint | core thesis; confidence in both selection and timing |

These are **portfolio percentages** of the research equity bucket (independent
of the baseline equal-weight control portfolio). The tier percentages are
constants in `ops/research/sizing.py`.

## Hard fences (per-position limits)

Three immovable caps apply to every position, regardless of tier:

1. **Name-at-cost ≤ 10%** of research equity. A single thesis, no matter how
   high-conviction, cannot dominate the portfolio.
2. **Sector ≤ 25%** of research equity. Concentration guard for correlated
   risks. `UNKNOWN` is a real bucket from the smallcap universe cache and
   counts toward the sector limit.
3. **Position ≤ 5% of 20-day dollar ADV.** Liquidity fence. The market can
   absorb this size without material impact; larger sizes face execution risk
   and wider spreads. (Shorthand: ≤5% ADV.)

**Order floor:** orders under $100 are not placed (operational friction below
this threshold).

All three fences are checked at entry and continuously monitored. The first
fence that binds rejects the order.

## Entries

Memos in state `open` with `created_at` within the past 12 months drive entries.
The trading step stages orders at the current quote midpoint. If no quote exists
(trading halt, delisted), the entry is silently deferred — try again next day.

Entry checks (in order):
1. Quote must exist and be recent (within 15 seconds).
2. Position does not already exist.
3. Memo is in `open` state.
4. All three fences pass (name ≤ 10%, sector ≤ 25%, position ≤ 5% ADV).
5. Tier-sum constraint passes: the new size + existing tier-sum ≤ tier %.

Positions are recorded in the research journal (`research_journal.sqlite`) with
a `research_position_opened` event; the position ID links the journal entry to
the memo.

## Exits (first match wins)

Positions close immediately when any of these conditions is met:

1. **Memo resolved or missing:** When a `resolution_due` event arrives or
   journal inspection finds no corresponding memo, the position closes with
   status "memo missing" or "resolved". **Risk:** un-provenanced positions
   (journal surgery or data loss) close as "memo missing" and record no exit
   reason — note this hazard in operational runbooks.
2. **Falsifier tripped:** The main journal has a `falsifier_tripped` event for
   this memo's `memo_id`. The position closes on the same day.
3. **Price target hit:** The latest quote is ≥ `price_target_high` from the
   memo. The position closes at that quote.

All exits record a `research_position_closed` event in the research journal,
including the exit reason and fill price.

## Third ledger: research_journal.sqlite

The research portfolio is a separate, isolated ledger from the baseline
(equal-weight control) and momentum (post-earnings) systems. It lives in
`research_journal.sqlite` at the path `OPS_RESEARCH_JOURNAL_PATH` (see
ops/config.py).

**Starting capital:** `OPS_RESEARCH_STARTING_CASH`, default 100000. This is the
pool from which all research positions are sized.

**Provenance events** (all in the research journal):
- `research_position_opened`: entry order filled, memo_id linked.
- `research_position_closed`: position closed, reason (memo_resolved,
  memo_missing, falsifier_tripped, price_target_hit), exit_price recorded.

**Summary event** (in the main/ops journal):
- `research_trade_run`: nightly push summary after all entries/exits complete.
  Payload: {count_opened, count_closed, count_pending, ...}. Push type is
  "normal" on day-to-day runs, "push" (high urgency) if any position size
  exceeds tier allocation.

## Resolving positions: ops research resolve

When the monitor pushes a `resolution_due` event or when you manually close a
position (for any reason other than the mechanical exits above), record the
outcome and exit price:

```
ops research resolve MEMO_ID --label <LABEL> --narrative "..." [--exit-price P]
```

**Labels** (mutually exclusive 2×2):
- `thesis_correct_made_money`: Thesis held; position was profitable.
- `thesis_correct_lost_money`: Thesis held; position was unprofitable.
- `thesis_wrong_made_money`: Thesis broken; position was profitable (luck).
- `thesis_wrong_lost_money`: Thesis broken; position was unprofitable.

**Numbers** are auto-computed if not provided:
- **Exit price ladder:** explicit `--exit-price` > last research SELL fill by
  `filled_at` > current close. (If exits were never filled, we use today's
  close.)
- **Benchmark:** IWM (Russell 2000) over the identical holding window.
- **Return fractions:** 0.08 = 8%; computed as (exit − entry) / entry.

**Passed memos** (researched but not bought) are resolved the same way, always
recording `exit_price=None` (no fill to compare). The memo ID exists but no
position record does.

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

After the first batch of positions resolves (typically 6–12 weeks), run:

```
ops research report [--output FILE]
```

This generates a quarterly calibration report with six sections:

1. **Portfolio return vs benchmark:** realized return, benchmark return (IWM),
   alpha.
2. **Selection skill:** bought-vs-passed outcomes (positions outperform passed
   memos).
3. **Sizing accuracy:** whether tier sizing matches realized conviction (do
   high-conviction bets actually outperform starter?).
4. **Falsifier accuracy:** rate at which mechanical stops trigger vs memo
   misjudgment as the root cause of losses.
5. **Outcome label distribution:** the 2×2 matrix (correct/wrong × made/lost),
   with emphasis on off-diagonal luck.
6. **Calibration of stated probabilities:** scenario probabilities from memos
   vs realized outcomes, after corpus reaches n ≥ 5 resolved memos. Below that,
   the report renders an honesty string: "n=[count] — probabilities not yet
   calibrated."

The report excludes memos with no stated scenarios (counted as "unscored" in the
summary). Once calibration is proven (typically after 20–30 resolved memos),
these metrics feed back into sizing (Kelly-style bet sizing becomes permissible)
and memo evaluation (similar situations surface by embedding lookup; ~30–50
resolved memos is the threshold).
