# Pipeline Activity: live "now working on", run history, next-work forecast

**Date:** 2026-07-14
**Status:** Approved (design review with user)
**Branch target:** `feat/opsdash-react-ui` (or follow-on branch)

## Goal

The ops dashboard should answer, at a glance:

1. **What is the model working on right now?** ("daily cycle — analyzing BAH (3 of 8)")
2. **When did the pipeline last run, and why?** (recent runs with reason + outcome + duration)
3. **When will it next do real work, and for what purpose?** (gate-aware forecast, not raw
   cron fire times — most scheduler fires no-op through gates)

Motivation: know whether the ds4 box is busy or idle, and keep it maximally utilized.

## Scope decisions (user-confirmed)

- **Coverage:** everything that holds ds4 — the market-hours daily cycle (analyses) and the
  overnight research window (drain, graph-vetting, short pass, insider memo-lite).
  Mechanical jobs (trade ticks, insider scan, summaries) are out of scope; the design
  extends to them trivially if wanted later.
- **Live granularity:** name + stage breadcrumbs (per-symbol / per-memo), not agent-level
  graph introspection, not a bare busy/idle bit.
- **Next-run:** gate-aware "next real work" prediction only, not raw cron times.
- **History:** a recent-runs list (~20 rows), not per-job summaries, not a full browser.
- **Transport (approach A):** structured journal events power both the live view and the
  history. No sidecar status file, no log parsing.

## Section 1 — Activity events (service side)

Two new event kinds in `ops/events.py`, with payload helpers per existing convention:

- `activity_started`: `{scope, job, stage, symbol?, seq?, reason?}`
- `activity_finished`: same identity fields plus `{ok, duration_s, outcome?}`

`scope` ∈ `{"job", "item"}`:

- **job** — a whole ds4-holding window. `job` ∈ `{"daily_cycle", "overnight"}`.
  Start carries `reason` (e.g. "attempt 2 of 3, retrying failed cycle" / "screen due; 3
  hits pending; 2 memos to vet"). Finish carries `outcome` (e.g. "analyzed 5, bought 2" /
  "researched 4, vetted 2, 1 failed, hit deadline").
- **item** — one unit inside a job: one symbol analysis, one memo vetting, one drain name,
  one insider memo. `stage` names the work ("analyzing", "vetting", "researching",
  "authoring_memo"), `seq` is "3/8" style when the loop knows its total.

### ActivityReporter (`ops/activity.py`, new)

A small class wrapping a `Journal`, exposing two context managers:

```python
with reporter.job("overnight", reason=...):        # emits job start/finish
    with reporter.item("overnight", stage="vetting", symbol="CRC"):
        ...
```

- Emits the start event on enter, the finish on exit; computes `duration_s` in-process.
- On exception: emits finish with `ok=False` and **re-raises** — the reporter never
  swallows or alters control flow.
- `NullReporter` no-op twin is the default everywhere (tests, CLI paths unchanged).
- Journal write failures inside the reporter must not kill the wrapped work: reporter
  emit errors are caught and printed to stderr (the one place breadcrumbs are best-effort).

### Instrumentation points

- **`Orchestrator._tick_impl`** — wraps the cycle body in `reporter.job("daily_cycle",
  reason=...)`; reason derived from the attempt count it already computes.
- **`TradingAgentsPipelineAdapter`** — gains an optional reporter; brackets each
  `propagate(symbol)` with an item event. This covers daily-cycle analyses AND overnight
  graph-vetting with one touchpoint.
- **`_research_overnight_tick`** (`ops/main.py`) — wraps the window in
  `reporter.job("overnight", reason=...)` built from screen-due state + queue depths it
  already reads. Job start is emitted only when the tick decides real work exists (the
  half-hourly no-op fires journal nothing — the runs list must not fill with no-ops).
- **`drain_pending`** and **`author_pending_memos`** — accept an optional reporter for
  per-name item events (default `NullReporter`).

Volume: ~50–150 rows/day. Breadcrumbs appear in the Activity feed like any event and are
filterable by its existing kind dropdown.

## Section 2 — Snapshot additions

`/api/snapshot` grows a top-level `activity` section (same exception-isolation as the rest):

```
"activity": {
  "current": { job, stage, symbol, seq, started_at, age_seconds, reason, stale } | null,
  "recent_runs": [ { job, reason, started_at, finished_at|null, ok|null,
                     duration_s|null, outcome|null }, ... up to 20 ],
  "next_work": [ { at, job, purpose }, ... ]
}
```

- **current** — latest activity event: a start means busy (item-level preferred over its
  enclosing job when both are open), a finish means idle. Guards: if the health verdict is
  not `RUNNING`, or the dangling start is older than a 4 h sanity cap, report `null` /
  `stale: true` — never a phantom "analyzing BAH" from a crashed process.
- **recent_runs** — last 20 job-scope starts, each joined to its matching job-scope finish
  (next finish for the same job after it). A dangling start with a later `service_started`
  renders `ok: false, outcome: "interrupted"` — crashes are visible.
- **next_work** — pure function in `ops/dashboard/forecast.py` (new), `now` injected:
  - *Daily cycle:* next market-hours tick where the cycle would actually run. Skipped if
    completed today or `MAX_DAILY_CYCLE_ATTEMPTS` used; shown as "retry, attempt N of 3"
    when the last run failed. Uses `MarketCalendar` for the next trading session.
  - *Overnight:* next 00:00 window (weekend windows extend to Monday's deadline per
    `_overnight_deadline` semantics), purpose assembled from live queue state: screen due
    (days since last run vs `research_screen_interval_days`), pending screen hits (long +
    short), memos pending vetting (long + short), insider entries awaiting memos — or
    "likely idle: queues empty, screen not due until <day>".
  - Inputs are all read-only: journals, screen/memo/signal stores (mode=ro), calendar.

### Shared schedule constants (`ops/scheduler/times.py`, new)

The cron facts (tick minutes/hours/days, afternoon train times, overnight fire cadence)
move into one constants module imported by both `ops/main.py` (to register jobs) and
`forecast.py` (to predict) — one source of truth, no drift.

## Section 3 — UI (dashboard-ui)

- **`NowStrip`** (new component) — full-width strip under the header, above sleeve cards.
  Busy: pulsing dot + "**daily cycle — analyzing BAH (3 of 8)** · started 12:40 · 6m ago".
  Idle: "idle — next: **tonight 00:00 overnight** — screen due · 3 hits · 2 memos".
  Stale/stopped: existing warn styling. Always one line.
- **`RunsPanel`** (new component) — right column, between Overnight and Logs: recent-runs
  table (time, job, reason, outcome, duration), red-tinted rows for failed/interrupted.
  Footer renders the full `next_work` list.
- `types.ts` gains `Activity` section types; `App.tsx` wires both components. Formatting
  logic lives in pure helpers under `lib/` (testable). No polling changes.

## Error handling / edge cases

- Crashed service mid-item → stale-guarded strip, "interrupted" run row (never a phantom).
- `activity` section error → existing `Section<T>`/`isErr` panel error state; rest of the
  dashboard unaffected.
- Restart mid-window → new job start supersedes the dangling one.
- No-op scheduler fires journal nothing → runs list stays signal, not noise.
- Reporter journal-write failure → work proceeds; error to stderr.

## Testing

- `ops/activity.py`: pair emission, `ok=False` on raise + re-raise, durations, NullReporter.
- Orchestrator/overnight: extend existing tests — job events with correct reason; adapter
  emits item events per propagate; no job event on no-op fires.
- `forecast.py`: frozen-clock scenarios — cycle done today, retry pending, attempts
  exhausted, Friday→Monday weekend window, screen due vs not, queues empty, holiday.
- Snapshot: section shape, stale guard, interrupted-run join, section isolation.
- UI: vitest units for strip/panel formatting helpers; shipped-bundle test extended to the
  new panels.
- Out of scope: the 11 pre-existing `test_main.py` failures on main.
