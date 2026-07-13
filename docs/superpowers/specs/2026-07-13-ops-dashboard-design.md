# Ops Dashboard — Design

**Date:** 2026-07-13
**Status:** Approved pending user spec review

## Problem

The operator has no visibility into the always-on ops service except asking
Claude to inspect logs and journals. They need a single place that answers,
at any time: is everything alive, what is the system doing right now, what
is queued in the research funnel, and how is each sleeve performing.

## Decisions (settled during brainstorming)

1. **Strictly read-only.** The dashboard observes; it never mutates trading
   or research state. Controls stay in the CLI / flag files.
2. **Journal-only data.** Same rule as `ops status` and the daily overview:
   no broker, no MCP, no OAuth, no quotes, no LLM. P&L is journal-derived
   (equity snapshots + fills). Unrealized P&L is "as of the last journaled
   snapshot", not marked to live market prices.
3. **Architecture: separate read-only process, zero-dependency stack.**
   A sibling launchd agent running a stdlib HTTP server on `127.0.0.1`,
   serving a no-build vanilla HTML/JS frontend that polls a JSON snapshot
   endpoint every ~5 seconds. Rejected alternatives: an in-process thread
   inside `ops run` (dashboard goes dark exactly when the service crashes;
   shares fds/runtime with trading) and FastAPI+React (build step that can
   fail at boot; large dependency surface for a localhost read-only page).

## Data sources (all read-only, all existing)

Under `${XDG_STATE_HOME:-~/.local/state}/tradingagents/` (paths come from
`OpsConfig`, never hard-coded):

| Store | Contents |
|---|---|
| `ops_journal.sqlite` | momentum sleeve: events, orders, fills, equity snapshots, cursors |
| `research_journal.sqlite` | research sleeve ledger |
| `baseline_journal.sqlite` | baseline sleeve ledger |
| `memos.sqlite` | memo corpus + vetting statuses (open/passed/rejected) |
| `research_screen.sqlite` | screener runs + hits queue (pending/researched/failed/expired) |
| `logs/ops.out.log`, `logs/ops.err.log` | service stdout/stderr |

SQLite opened with `file:...?mode=ro` URIs; WAL mode makes concurrent reads
beside the live writer safe. `mode=ro` is a hard guarantee the dashboard can
never hold a write lock or block the service.

**One new data source (the only ops-service change in this design):**
guardian pass recency currently lives only in memory
(`PositionGuardian.last_pass_started_at`, monotonic clock) — a journal
reader cannot see it, and journaling a per-minute pass event would be
noise. Instead the guardian best-effort-touches (`os.utime`, wrapped so a
failure can never disturb a guardian pass) a liveness file
`${state}/tradingagents/guardian.alive` at the start of each pass; the
dashboard reads its mtime. If the file is absent (service predates it),
the health strip shows guardian liveness as "unknown" rather than guessing
from log mtime.

## Panels

Ordered by operator priority (health → activity → funnel → money):

1. **Health strip** (top, always visible). Computed verdict — ● RUNNING /
   ○ STOPPED / ⚠ STALE — derived from last `service_started`/
   `service_stopping` events and guardian-pass recency. Also: broker mode,
   live-gate status (flip marker, fills used/remaining, cap), guardian last
   pass age (from the `guardian.alive` liveness file; "unknown" if absent),
   last daily cycle run/completed (successful orchestrator ticks are not
   journaled per-tick; the daily cycle events are the meaningful signal),
   recent heartbeat errors, notify dispatch lag.
   Red full-width banner when: service down, guardian stale, daily halt,
   kill switch, startup halted, or research pause flag set.
2. **Activity feed.** The three journals' event streams merged newest-first;
   each kind rendered as a one-line human sentence; kind filter. Overnight
   window card during research hours: stage (vet/drain), progress, deadline
   countdown, pause-flag state. Collapsible raw tail of the two log files.
3. **Research funnel.** Screener (last run, hits pending → researched →
   failed) → memos (open/passed/rejected counts; open-memo list with ticker,
   age, authoring model, resolution/catalyst due dates) → positions opened.
   Escalations, falsifiers tripped, resolutions due soon.
4. **Sleeves & P&L.** Per sleeve (momentum, research, baseline): latest
   equity + cash, day-over-day P&L (journal convention), equity sparkline
   from snapshot history. Combined open-positions table (symbol, qty, entry,
   stop, sleeve). Fills today across sleeves.
5. **Anomalies (7-day).** Count + last-seen per anomaly kind (guardian
   errors, stop failures, inconsistencies, quote failures, …).
6. **Market clock** (header). Market open/closed, next open/close, time
   until the overnight research window (via `ops.scheduler.market_calendar`
   / `ops.trading_time`).

## Components

New package `ops/dashboard/`:

- **`snapshot.py`** — `build_snapshot(config, *, now=None) -> dict`, modeled
  on `ops/status.py::build_status`. One JSON-safe dict covering every panel.
  Reuses existing pure logic (`build_status`, overview section builders,
  `ScreenStore` queries) instead of re-deriving. Each top-level section is
  built independently and exception-wrapped: a failing section becomes
  `{"error": "<message>"}` while the rest of the snapshot proceeds.
  Money serialized as strings (Decimal-faithful), timestamps as ISO-8601.
- **`events_view.py`** — merge + human-render journal events. Per-kind
  renderers; unknown kinds fall back to `kind + compact payload` so new
  event kinds never break the feed.
- **`server.py`** — stdlib `ThreadingHTTPServer`. Host hard-coded
  `127.0.0.1` (not configurable). Port from `OPS_DASHBOARD_PORT`, default
  8321. Routes:
  - `GET /` + static assets (from package dir, resolved-path check)
  - `GET /api/snapshot` — full snapshot, built fresh per request
  - `GET /api/events?after_id=&kinds=` — incremental feed
  - `GET /api/logs?file=out|err&lines=N` — tail; `file` is an enum key
    mapped to the two known paths, never a path parameter
- **`static/`** — `index.html`, `app.js`, `style.css`. Vanilla JS, no
  frameworks, no CDN, no build step. Poll `/api/snapshot` every 5s and
  re-render; per-panel error chips; "dashboard disconnected" banner with
  last-successful-fetch time when polling fails.
- **CLI:** new `ops dashboard` subcommand runs the server in the foreground
  (launchd owns backgrounding, same as `ops run`).

Per-request connections: open read-only, read, close. No long-lived handles
(no fd accumulation, no stale state).

## Error handling

- Missing/locked/mid-migration store → that section reports its error;
  every other panel still renders. Partial dashboard beats blank page.
- Dashboard process down → browser shows disconnected banner (silence must
  never look like calm).
- Ops service down → dashboard keeps working (it reads files, not the
  service) and the health strip computes STOPPED/STALE itself.

## Security

- Loopback-only bind, hard-coded — cannot be exposed by config typo.
- Read-only by construction: no mutating routes, no broker/OAuth imports.
- Logs endpoint: enum-keyed files only; static files: resolved-path check
  under the package static dir. No secrets (heartbeat URL, tokens) in any
  response.
- No auth: localhost-only, single-user machine, no sessions/cookies exist.

## Deployment / lifecycle

- `ops install-service` additionally renders
  `com.tradingagents.dashboard.plist` from a template in `ops/deploy/`
  (same KeepAlive `{Crashed: true, SuccessfulExit: false}`, same 60s
  restart throttle) and prints both `launchctl bootstrap` commands. It
  never runs launchctl itself (house rule).
- Logs to `~/.local/state/tradingagents/logs/dashboard.{out,err}.log`.
- The two agents boot together and restart independently. No build step:
  `git pull` + restart is the full upgrade path.

## Testing

- Snapshot/eventview logic: temp journals + stores seeded with known
  events; assert on the returned dicts (house pattern from
  `tests/ops` for `build_status` / overview).
- Server: a handful of tests against a real server on an ephemeral port —
  valid JSON snapshot, 404 unknown routes, logs endpoint rejects bad keys,
  socket bound to loopback only.
- Section isolation: corrupt/missing store yields per-section error while
  the snapshot still returns 200.

## Out of scope (explicit)

- Any control/write action from the UI.
- Live market quotes / intraday mark-to-market.
- Remote (non-localhost) access, auth, TLS.
- Websockets/SSE — 5s polling is sufficient at current tick cadences;
  revisit only if it ever isn't.
