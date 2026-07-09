# Research sleeve: every-3-days screen + overnight drain

**Date:** 2026-07-09
**Status:** design — awaiting review
**Author:** brainstormed with the operator

## Problem

The research sleeve (the deterministic two-stage memo brain, `ops/research/brain.py`)
is the part of the system the operator actually values — an LLM specialized to
study one stock and emit a cited, falsifiable memo. But it only *produces* memos
on Saturday, and today the stores are empty (`memos.sqlite`: 0 memos,
`research_screen.sqlite`: 0 hits/0 runs), so the weekday monitor/trade jobs have
nothing to act on. The sleeve looks dormant. The operator wants to (a) see it run
end-to-end today, and (b) run it far more often — on a fixed cadence — so it
accumulates a labeled track record faster.

### Why widen intake (the real motivation)

Not returns — **learning rate**. The resolution + calibration corpus report
(`ops/research/{resolution,report}.py`) is a measurement engine whose statistics
only become trustworthy with dozens of *resolved* memos. At 0 memos it says
nothing. Widening intake buys statistical power on the system's own accuracy, at
the cost of ds4 compute (the binding constraint — ~19 min/name). Portfolio risk
is mild: the sleeve is $100k paper, conviction-tier sized (starter 2% / medium 4%
/ high 6% of equity) behind hard fences (name ≤10%, sector ≤25%, position ≤5% of
20-day dollar ADV, min $100/order). More names → smaller average positions, not
more tail risk. Caveat: only resolved memos teach, and resolution lags weeks.

## Goal

Change the research sleeve's cadence from **weekly, name-budgeted** to
**every-3-days screen + nightly deadline-boxed drain**, consolidated into the
always-on `ops run` service, with a per-symbol research TTL that makes frequent
screening non-wasteful. Plus a clean one-shot to kick the whole chain by hand.

## Non-goals

- No change to the momentum sleeve, capital allocation across sleeves, or the
  research brain's two-stage reasoning. (A momentum→research strategic pivot was
  explicitly deferred.)
- No change to sizing, fences, monitoring, resolution, or the calibration report.
- No change to what the screener screens (the small/mid-cap fundamental screen).

## Current state (verified 2026-07-09)

- **Screen**: launchd plist `com.tradingagents.screen`, Saturday 10:00 →
  `ops screen --notify`. Fills `screen_hits` (dedup: skips symbols already
  `pending`).
- **Research brain**: launchd plist `com.tradingagents.research`, Saturday 12:00 →
  `ops research run --max-names 3 --notify`. Drains pending hits into memos.
  **Already manages ds4 itself**: `build_managed_backend(...)` → `ensure_up()` →
  loop → `shutdown()` in a `finally` (managed backend runs `lms unload --all`
  first — the RAM-crash mitigation).
- **Monitor / trade / overview**: jobs inside the `ops run` scheduler
  (`ops/main.py::_start_full_scheduler`), weekdays 16:20 / 16:25 / 16:35.
  Mechanical, memo-driven, **no LLM**.
- ds4 is a single ~86 GB resource; the managed backend only kills a server it
  started, and two big models resident at once has crashed the machine before.
  Non-overlap of any LLM job with any other is the invariant.

## Design

### 1. Consolidate the two Saturday plists into `ops run`

Retire `com.tradingagents.screen` and `com.tradingagents.research` (and their
`render_*_plist` deploy helpers + `install-*-service` CLI commands). Their work
moves into one nightly job in `_start_full_scheduler`, so a single process owns
ds4 lifecycle and graceful shutdown. This removes the cross-process ds4 race the
separate-plist approach carries.

### 2. One nightly job: `_research_overnight_tick`, `CronTrigger(hour=0, minute=0)`

Ordered steps, each guarded so a failure records an event instead of killing the
APScheduler job (same pattern as the existing `_research_*_tick` wrappers):

1. **Maybe screen (3-day gate).** If `ScreenStore.last_run()` is `None` or its
   `created_at` is ≥ `research_screen_interval_days` (default 3) old, run the
   screener (`run_screen`) to refill the queue. Gate reads the store's own
   `last_run`, not a journal event — downtime-safe and self-correcting.
2. **Drain by deadline.** Research pending hits oldest-first until the queue is
   empty **or** the local-time deadline `research_drain_deadline_hour` (default
   08:00 America/New_York) is reached **or** shutdown is requested. Check the
   deadline and the module `_shutdown_event` **between names** (a name already
   in flight finishes — worst-case ~19 min overrun, still far before the 09:30
   first momentum tick). ds4 is brought up once for the drain and torn down in a
   `finally` (reuse the existing `ops research run` backend discipline).

Because the drain is deadline-boxed rather than name-capped, "process everything
between screens" falls out: ~25 names/night capacity clears a typical screen
batch in one night; later nights no-op until the next 3-day screen.

### 3. Deadline-boxed drain mode

Extract the drain loop from `research_run` (cli.py) into a reusable function so
both the CLI and the overnight tick share it. New parameters:

- `deadline: datetime | None` — stop before starting a name once passed.
- `should_stop: Callable[[], bool] | None` — shutdown check (wired to
  `_shutdown_event.is_set` in the service).
- Drains **all** pending hits (no `--max-names`) when deadline-boxed.

The existing `ops research run --max-names N` behavior is preserved for manual
use (no deadline, name-capped).

### 4. Per-symbol research TTL (7 days, configurable) — keyed on last screened

Enforced at **screen-enqueue** time, entirely within `ScreenStore` (no
`MemoStore` dependency). In `record_run` (and `enqueue_hit`), in addition to the
existing "skip already-pending" rule, skip any symbol that already has **any
`screen_hit` whose `created_at` is newer than `research_screen_ttl_days`**
(default 7), regardless of that hit's status. This means a name screened in the
last week is not re-queued — whether it produced a memo, is still pending, or
*failed* research — which is exactly "don't reanalyze the same securities in the
same week." A single indexed query on `screen_hits(symbol, created_at)`.
Complements — does not replace — the existing "one memo = one position lifecycle"
trading dedup.

### 5. Config (ops/config.py)

- `research_screen_interval_days: int = 3`
- `research_drain_deadline_hour: int = 8`  (local, America/New_York)
- `research_screen_ttl_days: int = 7`  (skip symbols screened within this window)

All env-overridable via the existing `_env_*` pattern, all validated (> 0;
deadline hour in 0–23).

### 6. Kick-it-live-today one-shot

A CLI command (e.g. `ops research kick`) that runs the full chain synchronously
for a manual demo: screen (ignoring the 3-day gate) → drain all pending
(no deadline) → run the research trade step so paper positions appear the same
run. Prints a per-stage summary. This is the "watch it work now" path and is
independent of the schedule.

## Data flow

```
00:00 nightly (ops run scheduler)
  ├─ last screen ≥3d? ──► run_screen ──► screen_hits (TTL-filtered enqueue)
  └─ drain until 08:00 / empty / shutdown
         ├─ ds4 ensure_up()  (lms unload --all first)
         ├─ per hit: research_hit ──► MemoStore  (mark_researched / mark_failed)
         └─ ds4 shutdown()  (finally)
weekday 16:25 (existing)
  └─ trade_research_sleeve ──► paper positions from fresh memos
```

## Error handling

- Every step in the nightly tick is wrapped: failures record an event
  (`research_*_error`) and do not raise, so the APScheduler job survives (matches
  `_research_monitor_tick` / `_research_trade_tick`).
- ds4 teardown is in a `finally` — a mid-drain crash still frees the 86 GB before
  market open.
- Deadline + shutdown checks between names guarantee the drain yields for
  `sched.shutdown(wait=True)` and never bleeds ds4 into momentum-tick hours.
- Screener per-name failures already skip-and-continue (unchanged).

## Testing

- **TTL:** `record_run` with a symbol whose most recent `screen_hit` is 3 days
  old → skipped (any status); 8 days old → enqueued; never screened → enqueued.
- **3-day screen gate:** `last_run` None / 2 days / 3 days → screen runs on
  None and ≥3, skips at 2.
- **Deadline drain:** fake clock past deadline after N names → stops between
  names, leaves the rest pending; empty queue → no-op; `should_stop` true → stops.
- **ds4 teardown:** an exception mid-drain still calls `backend.shutdown()`.
- **Kick one-shot:** screen → drain → trade produces a memo and a paper position
  on seeded fixtures (mocked ds4 stage LLMs).
- **Config validation:** non-positive interval/ttl and out-of-range hour raise.

## Rollout

1. Land code + tests.
2. Update `ops run` plist (already deployed) — no schedule change needed; the
   nightly job is internal. Remove the two retired launchd plists via
   `launchctl unload` + delete.
3. Run `ops research kick` once by hand to seed the first memos and confirm the
   full chain (the operator's "today" ask).
4. Let the nightly job take over.

## Open questions

None blocking. Defaults (3-day / 08:00 / 7-day) are the operator's stated
numbers.
