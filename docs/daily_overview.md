# Daily Overview Runbook

"Everything that happened today" across all three sleeves — momentum, research, and
baseline — in one report. Read-only over the existing journals + memo store; no broker,
no quotes, no LLM, so it is safe to run on a schedule (or by hand) regardless of broker
reachability.

## What runs when

| job | where | when | what |
|---|---|---|---|
| daily_overview | ops daemon (APScheduler) | 16:35 ET mon-fri | full-day cross-sleeve overview |
| daily_overview_saturday | ops daemon (APScheduler) | Sat 18:00 ET | same overview, after the Saturday research brain (screen 10:00, research run 12:00) |
| ops digest | manual | — | on-demand overview for any date; debug/inspection companion |

Both daemon jobs call the same `_daily_overview_tick`; the daily-vs-Saturday split just
puts the Saturday run after that day's research batch jobs so it can see them. Both jobs
are registered only in the full scheduler (`_start_full_scheduler`) and only when a
`config` is available — they are **not** registered in the degraded guardian-only
scheduler (`_start_guardian_only`), so a broker-down day still gets guardian stop-checks
but not the overview.

The existing 16:05 ET `daily_summary` job (thin per-account equity + open-positions
snapshot) is unchanged and keeps firing — the overview is additive, not a replacement.

## Once-per-day gate

The daemon jobs are gated on the `daily_overview` event for that day in the main ops
journal (`journal.has_event_today(events.KIND_DAILY_OVERVIEW)`): if it's already been
recorded today, the tick is a silent no-op. This is what makes the weekday + Saturday
registrations safe together and makes restarts idempotent. Errors are journaled as
`daily_overview_error` rather than raised, so a bad tick can't kill the scheduler.

`ops digest` (below) deliberately does **not** record the gate event, so a manual run
never suppresses the daemon's own scheduled run later that day.

## Manual command: `ops digest`

```
ops digest [--date YYYY-MM-DD] [--output FILE] [--push/--no-push]
```

- `--date` — overview for that calendar date (ET); defaults to today.
- `--output FILE` — write the markdown to `FILE` instead of stdout.
- `--push/--no-push` — also push the headline via Pushover; **off by default**.

Reads the same three journals + memo store as the daemon job. Safe to run anywhere,
including against empty/fresh stores (renders a "Quiet day" banner, exit 0).

## Delivery (daemon jobs)

Each daemon-triggered run:

1. Writes the full markdown report to
   `${XDG_STATE_HOME:-~/.local/state}/tradingagents/overviews/overview-YYYY-MM-DD.md`.
2. Pushes a **one-line headline** via Pushover (title "Daily overview") — the push is a
   summary; the full detail lives only in the file. A push failure does not prevent the
   file from being written (the file write happens first) and does not block the gate
   event from being recorded.

## Report sections

Exact wording lives in `ops/notify/overview.py` (`format_daily_overview`); this is the
section-by-section summary.

- **Header** — per-sleeve last equity snapshot (momentum `open_day`, research
  `research_run`, baseline `baseline_run`) with its timestamp, or "n/a (no snapshot yet)".
  The momentum "day equity" figure here (and in Momentum, below) is **not** intraday
  mark-to-market P&L — the sleeve only journals one equity point per day (`open_day`,
  taken before that day's cycle runs), so the reported "(+X%)" is the day-over-day change
  vs. the most recent prior `open_day` snapshot. It's `None`/`n/a` when there's no prior
  snapshot to compare against (including day one).
- **Momentum** — whether the daily cycle ran; universe health (names checked, fetch
  failures, candidates) or "no diagnostics today"; a `UNIVERSE BLIND` flag if that
  happened; analyzed→decided counts split BUY/HOLD/SELL with names listed for BUY/SELL;
  buys filled (with symbols); rejected orders (symbol + reason); exits (symbol + exit
  rule); day equity (see above).
- **Research** — memos written today (ticker, thesis type, conviction tier, status);
  monitor run counts (memos checked, falsifiers evaluated, tripped, unevaluable,
  escalations, resolution-due, catalyst-due) or "none today"; falsifiers
  tripped/escalations/resolution-due/catalyst-due ticker lists; trades entered/exited/
  skipped plus resulting equity/cash, or "no research_trade_run today"; positions opened
  (symbol + tier) and closed (symbol + reason) today.
- **Baseline** — screen run (passers, buys, exits, skipped, equity) or "none today";
  exits (symbol + days held); write-offs (symbol + auto/manual).
- **Anomalies** — halts, kill-switch trips, stop hits/failures, order rejections,
  universe-blind, guardian/orchestrator/exit-check/heartbeat errors, broker-unreachable
  (main journal); research monitor/trade errors (research journal). No baseline-journal
  kind currently qualifies as an anomaly. Renders "none" when the list is empty.

A day with no activity anywhere (all sections empty, no anomalies) adds a "Quiet day --
no activity." banner right under the title; every section still renders its own
skeleton/empty state underneath rather than being omitted. Sample (fresh/empty stores,
via `ops digest`):

```
# Daily overview -- 2026-07-08

Quiet day -- no activity.

## Header
Date: 2026-07-08
Momentum: n/a (no snapshot yet)
Research: n/a (no snapshot yet)
Baseline: n/a (no snapshot yet)

## Momentum
Daily cycle ran: no
Universe: no diagnostics today
Analyzed -> decided: 0 (BUY 0, HOLD 0, SELL 0)
Buys filled: 0
Rejected: 0
Exits: 0
Day equity: n/a

## Research
Memos written today: 0
Monitor run: none today
Falsifiers tripped: none
Escalations: none
Resolution due: none
Catalyst due: none
Trades: no research_trade_run today

## Baseline
Screen run: none today

## Anomalies
none
```

## Inspecting

The gate/error events live in the main ops journal:

    sqlite3 ${XDG_STATE_HOME:-~/.local/state}/tradingagents/ops_journal.sqlite \
      "SELECT at, kind, payload FROM events WHERE kind IN ('daily_overview', 'daily_overview_error') ORDER BY id DESC LIMIT 10"

Written report files:

    ls ${XDG_STATE_HOME:-~/.local/state}/tradingagents/overviews/

## Data sources, not a fourth source of truth

The overview reads only what the other three sleeves already journal (main/momentum
journal, research journal, baseline journal) plus the memo store — it introduces no new
persistent state of its own beyond the daily gate/error events. The one new
instrumentation event this feature adds is `analysis_decision`, journaled per analyzed
momentum name (including HOLDs) so the Momentum section's analyzed→decided counts have
something to read; existing `propose_orders` callers that don't pass an event sink are
unaffected.
