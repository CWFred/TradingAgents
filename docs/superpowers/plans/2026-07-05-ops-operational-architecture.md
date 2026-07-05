# Ops operational-architecture improvements — spec for implementation

**Date:** 2026-07-05
**Baseline:** branch `fix/ops-review-round2` (3a80b0e) — both prior review docs
(`2026-07-02-ops-review-remaining-fixes.md`, `2026-07-04-ops-review-round2-fixes.md`)
are fully implemented. This document is not a bug list: the code architecture is
sound. It addresses the OPERATIONAL gaps around it — supervision, liveness,
contracts, and the live-flip ritual — identified in the post-fix architecture
review. A follow-up review will verify this work against this document.

Conventions: TDD (failing test first) for everything with runtime behavior.
Single-user system on macOS — do not add infrastructure beyond what is specified
here (see "Explicitly out of scope" at the bottom; it is binding).

Priorities: A1 and A5 before the paper go-live; A3 and A4 during the paper
window; A2 is a decision record now, implementation gated on account size;
A6 whenever, high value.

---

## A1. Supervision + dead-man's switch (highest value item in this doc)

**Problem:** `ops run` is a foreground process in a terminal. Laptop sleep, a
crash, or a closed lid silently stops everything — including the guardian, the
last line of defense — and the notification system cannot report it because a
dead process sends no pushes. Silence is indistinguishable from health. The
graduation criterion ("ran on ≥80% of trading days") is unmeasurable without
uptime records.

### A1.1 launchd agent

Add `ops/deploy/com.tradingagents.ops.plist.template` plus a short "Running as
a service" section in `ops/README.md`. Template contents:

- `ProgramArguments`: absolute venv python, `-m`, `ops.cli`, `run`.
- `WorkingDirectory`: repo root (placeholder in template).
- `EnvironmentVariables`: placeholder block for `OPS_*` (journal path is already
  absolute by default; broker mode deliberately NOT set here — paper is the
  default and live must go through the A5 ritual interactively).
- `KeepAlive`: `{Crashed = true, SuccessfulExit = false}`; `ThrottleInterval`: 60
  (exit code 3 — broker unreachable — must not hot-loop).
- `StandardOutPath`/`StandardErrorPath`: under
  `~/.local/state/tradingagents/logs/` (create dir in template docs).
- `RunAtLoad`: true.

Do NOT auto-install anything. Optionally add `ops install-service` that renders
the template with resolved paths and prints (not runs) the `launchctl bootstrap`
command. README must also document the sleep problem: launchd cannot run a
sleeping laptop; recommend `pmset repeat wakeorpoweron MTWRF 09:20:00` (user's
call to apply). Tests: template renders with valid plist syntax
(`plutil -lint` via subprocess, skipped off-macOS); `install-service` writes the
rendered file and does not invoke `launchctl`.

### A1.2 Startup/shutdown journal events (uptime record)

`run()` journals `service_started` (payload: broker_mode, journal path,
pid, version/git-sha if cheaply available) immediately after opening the
journal, and `service_stopping` (payload: exit code) in the `finally` before
close. Benefit: `ops status` (A4) and the graduation evaluation can compute
actual trading-day coverage from the journal instead of guessing.
Neither kind goes in the notify POLICY (audit-only). Tests: both events
present with correct payloads across a normal run; `service_stopping`
carries exit code 2 on the reconcile-halt path.

### A1.3 Dead-man's switch (external heartbeat)

New env `OPS_HEARTBEAT_URL` (default None → feature off) on `NotifyConfig`
(it is delivery config, not risk config). When set, `_start_full_scheduler`
AND `_start_guardian_only` add a `heartbeat` job (IntervalTrigger, 60s,
`max_instances=1`) that pings the URL — the intended target is a
healthchecks.io-style check that alerts the user when pings STOP.

Liveness semantics — the ping must mean "the safety loop is actually
running," not merely "the Python process exists":

- `PositionGuardian.check_stops_once` records `self.last_pass_started_at =
  time.monotonic()` as its FIRST statement — before the market-hours gate, so
  overnight/weekend passes still count as liveness (the loop runs 24/7 even
  though it only trades in RTH).
- The heartbeat job pings ONLY if `monotonic() - guardian.last_pass_started_at
  < 180`. A wedged or dead guardian job ⇒ pings stop ⇒ external alert. That is
  the entire point: this is the one alarm that fires when the process CANNOT
  speak for itself.
- Ping = `requests.get(url, timeout=5)`. Failures are swallowed (never let a
  monitoring outage disturb trading) and journaled as `heartbeat_error` at most
  once per 10 minutes (reuse the dispatcher-style cooldown idea, in-job state).
  `heartbeat_error` policy: email, throttled.

Tests: no URL → no job registered; wedged-guardian simulation (stale
`last_pass_started_at`) → no ping sent; fresh pass → ping sent; ping exception
→ swallowed + journaled once within the cooldown window. Use a fake transport
function injected into the job (follow the `sleep_fn`/`clock_fn` injection
pattern from `_await_fill`).

## A2. Guardian process isolation — DECISION RECORD, implementation deferred

**Problem:** one process means a pipeline OOM or scheduler wedge kills stop-loss
enforcement together with the thing that needed stopping.

**Decision to record (in this doc and `ops/README.md`):** deferred until either
(a) the account exceeds ~$1,000, or (b) the live flip — whichever first. Do not
implement now.

**Hard constraint discovered during review — write this down so nobody designs
around it wrongly:** process isolation is only straightforward in **robinhood
mode**, where positions/cash live at the broker and any process can read them
via MCP. In **paper mode the book is in-memory inside PaperBroker**; a separate
guardian process cannot see it without moving the paper book into SQLite (a
real change to PaperBroker's write path, not a deployment tweak). Therefore the
deferred design is: a separate `ops guardian` entrypoint, live-mode-only,
sharing the journal via WAL (multi-process safe), constructing its own
GuardedBroker + MCP client, no orchestrator/pipeline imports. The in-process
guardian remains for paper mode permanently. When implementing, the two
processes must not both auto-close on kill-switch (split: separate process owns
stops + kill switch; in-process guardian disabled via config when the external
one is registered — design detail for that future task, not now).

Deliverable now: this section verbatim as a decision record + a pointer in
`ops/README.md`. No code.

## A3. Typed event contracts (kills a recurring bug class)

**Problem:** journal event kinds and payload shapes are stringly-typed and
agreed on by vibes between producers (~25 `record_event` call sites), the
notify policy table, and the renderers. This exact disease caused three shipped
bugs already: `daily_halt` consumed-but-never-produced, the kill-switch
notification rendering an empty body (`payload["reason"]` never written), and
fills lacking `broker_mode`.

**Required change:** new module `ops/events.py`:

- One constant per kind (module-level `KIND_KILL_SWITCH = "kill_switch"` etc.,
  or a `StrEnum` — implementer's choice, but string VALUES must not change:
  the journal already contains them).
- One builder function per kind that takes typed kwargs and returns the payload
  dict, e.g. `kill_switch_payload(*, mode: str, equity_now: Decimal,
  equity_open_week: Decimal, pct: Decimal, threshold: Decimal) -> dict[str, str]`.
  Builders stringify Decimals (matching current storage convention).
- Migrate every producer (`position_guardian`, `guarded`, `main`, `reconcile`,
  `robinhood`, `paper`, `dispatcher`, `summary`, `live_gate`) to
  `journal.record_event(KIND_X, x_payload(...))`. `Journal.record_event`
  itself stays generic — the journal must not import event semantics.
- `ops/notify/policy.py` keys `POLICY` and `render` on the same constants.

**The enforcement test (the actual point):** in `tests/ops/notify/test_policy.py`,
a table mapping every kind in `POLICY` to a sample call of its builder, then
assert `render(kind, builder(...))` produces a non-empty title AND body with no
literal `None` in either. Plus: every kind in POLICY has a builder, and every
builder's kind is either in POLICY or in an explicit `AUDIT_ONLY` set in
`ops/events.py` (so a new event kind forces a conscious notify decision — the
`daily_halt` bug becomes impossible to reintroduce silently).

Pure refactor for producers (existing tests must stay green); the enforcement
test is new and must FAIL if someone adds a policy kind without a builder.

## A4. `ops status` CLI

**Problem:** inspecting the running system means hand-written sqlite3 queries.

**Required change:** `ops status [--journal PATH]` (default: configured journal
path), reading the JOURNAL ONLY — no broker, no MCP, no OAuth, no quotes — so
it is always safe to run beside the live service (WAL concurrent reads) and
works when the broker is unreachable. Print, plainly:

1. Journal path + broker mode (from config) + last `service_started` /
   `service_stopping` events (A1.2) with ages.
2. Positions per journal replay (`PaperBroker.from_journal`) with entry, stop,
   quantity — labeled "journal view" (live truth may differ; that's what
   reconciliation is for).
3. Cash per replay; latest `open_day` / `open_week` equity snapshots with
   timestamps, flagged `(stale)` when outside the current ET day/week
   (reuse `ops.trading_time`).
4. Halt states: `daily_halt` today? `kill_switch` this week? (existing
   `has_event_today` / `has_event_since_last_monday`.)
5. Fills today (ET boundary, by `filled_at`) and last fill.
6. Notify: cursor lag (`last_event_id_before(now)`-style max id minus cursor),
   count of `notify_event_skipped` / `notify_render_error` ever.
7. Live gate: flip marker present?, `count_live_buy_fills`, remaining, cap.
8. Recent anomalies: count + last-timestamp for `guardian_check_error`,
   `orchestrator_tick_error`, `stop_failed`, `guardian_blind`, `inconsistency`
   in the last 7 days.

Implementation: a pure function `build_status(journal, config) -> dict` in
`ops/status.py` with the CLI as a thin renderer — tests assert on the dict
(seeded journal fixtures per section), not on formatted text. One test proves
no attribute of the broker/MCP layer is touched (construct status against a
journal only).

## A5. Live-flip ritual (spec graduation criterion #4, still unimplemented)

**Problem:** `OPS_BROKER_MODE=robinhood` is one stale shell export away from
live trading. The parent spec requires "a CLI prompt requiring the user to type
the account value verbatim."

**Required change:** in `ops/main.py::run()`, after `_build_broker` succeeds in
robinhood mode and BEFORE `record_flip_marker` / reconcile / any scheduling:

- If a flip marker already exists (`ops.live_gate.flip_epoch`), skip the ritual
  (already graduated; restarts must be unattended — launchd).
- Else this is the FIRST live start: fetch `broker.get_equity()`, print it and
  the live-gate parameters ($cap × first N fills), and require the user to type
  the equity figure back verbatim (exact string match of the printed Decimal).
  Mismatch or EOF → journal `live_flip_refused` (audit-only) → exit code 4,
  nothing scheduled.
- **Non-TTY (`not sys.stdin.isatty()`) → refuse outright with the same exit
  code.** A supervisor must never be able to perform the first live flip; a
  human at a terminal does it once, then launchd restarts are unattended
  because the marker exists.
- Move `record_flip_marker` out of `_build_broker` to after the ritual passes
  (it currently fires on any robinhood build — that would silently satisfy
  "marker exists" on a refused attempt; the marker must mean "ritual passed").

Tests: first-live with correct input → marker recorded, startup proceeds;
wrong input → exit 4, no marker, no scheduler; non-TTY first-live → exit 4, no
marker; marker-already-present → no prompt (assert stdin never read). Document
exit code 4 in the module docstring alongside 0/2/3.

## A6. Crash-point recovery property tests

**Problem:** the entire design rests on "journal replay + reconciliation
recovers any crash," but nothing systematically proves it at every crash point.
Individual paths were hand-tested; the ordering bugs found in review (e.g.
kill-switch event journaled before closes complete) are exactly the class this
catches.

**Required change:** `tests/ops/test_crash_recovery.py`:

- `CrashingJournal`: wraps a real `Journal`, delegates everything, raises
  `SimulatedCrash` on the Nth write call (`record_event`, `record_order`,
  `record_fill`, `record_equity_snapshot`, `record_cash_adjustment`,
  `set_cursor` all count as writes).
- Scenario script (paper mode, fake quotes): seed → BUY fills → price drops →
  guardian pass (stop-sell) → price collapse → guardian pass (kill switch,
  sweep). Run once uncrashed to learn the total write count W.
- Parametrize N over 1..W: run the scenario with a crash at write N (swallow
  `SimulatedCrash` where the production catch-alls would — guardian and
  orchestrator wrappers already swallow; broker-path crashes propagate like
  real exceptions), then REOPEN the journal cold and assert the recovery
  invariant: `PaperBroker.from_journal` replays without exception, and
  `reconcile()` against a fresh replayed broker yields zero diffs (paper mode's
  own invariant), and a follow-up guardian pass completes the interrupted
  work (e.g. the kill-switch sweep resumes — already the designed behavior).
- Any N that violates the invariant is a real bug: fix the write ordering
  (journal-before-side-effect), don't weaken the test.

Keep runtime sane: one scenario, W is small (~15–25 writes); the whole
parametrized set must stay under a few seconds.

---

## Explicitly out of scope (binding — do not "improve" these)

- No asyncio rewrite; the thread + single-lock model stays.
- No Postgres/Redis/queues/brokers; SQLite WAL is the datastore.
- No web/Streamlit dashboard (v2, after 8 weeks of journal data exists).
- No second strategy module; the `Strategy` protocol seam is the extension
  point when the data justifies it.
- No paid market-data vendor.
- No A2 implementation — decision record only.
- Do not touch the user's uncommitted local edits (root `main.py`,
  `tradingagents/dataflows/reddit.py`) or their untracked
  `docs/NEXT-SESSION-golive-prompt.md` / `docs/RUNBOOK-paper-golive.md`.

## Suggested sequencing

1. **A1.2** (startup/shutdown events — tiny, everything else reads them),
   then **A1.3** (heartbeat), then **A1.1** (plist + docs).
2. **A5** (flip ritual) — required before any live experiment, small.
3. **A3** (event contracts) — before the journal grows more producers.
4. **A4** (`ops status`) — reads A1.2/A3 outputs; do after them.
5. **A6** (crash-point tests) — independent; slot in anywhere.
6. **A2** — copy the decision record into README, stop there.
