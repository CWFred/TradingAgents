# Ops Dashboard: React UI rebuild + opsdash.test hostname

**Date:** 2026-07-14
**Status:** Approved (user, 2026-07-14)
**Source design:** claude.ai/design project `adcf5f91-1845-4e62-855e-f7a9f2fc8a45`, file `Ops Dashboard.dc.html` ("Modern dark mode UI design")

## Goal

Replace the vanilla-JS dashboard frontend (`ops/dashboard/static/`) with a
React implementation of the approved visual design, and make the dashboard
reachable at `http://opsdash.test` instead of `http://127.0.0.1:8321`.

Out of scope: the design mock's demo scenario switcher (dropped — live data
only), any change to the server's read-only/loopback security posture, any
new snapshot sections.

## Decisions (user-confirmed)

- **Hostname:** `http://opsdash.test` via `/etc/hosts` + pf port-80→8321
  redirect. Not `opsdash.com` (real public domain, not ours).
- **Demo mode:** dropped entirely.
- **Stack:** delegated to implementer → Vite + React 18 + TypeScript.

## Architecture

### Frontend (`dashboard-ui/`, new top-level directory)

Vite + React 18 + TypeScript SPA. `npm run build` emits static assets into
`ops/dashboard/static/` (the existing server's static root). Node is
build-time only; the deployed service remains the single loopback-only
Python process. Built assets are **committed** so the live checkout and
`install-service` need no Node toolchain. `dashboard-ui/README.md` documents
`npm ci && npm run build`.

Visuals match `Ops Dashboard.dc.html`: IBM Plex Sans/Mono, dark palette
(`--bg:#0c0f14` etc. as CSS variables), sparkline sleeve cards, slide-in
drill drawer, pill badges, banner treatments. The design's fixed
`repeat(5,1fr)` sleeve grid becomes responsive (`auto-fit, minmax`).

Component inventory (one file each, `dashboard-ui/src/components/`):

| Component | Design element | Data source |
|---|---|---|
| `HeaderBar` | sticky health bar: verdict dot, broker, guardian age, market chip, updated-at | `health`, `market` |
| `DisconnectedBanner` | red sticky banner | poll-failure state |
| `AlertBanner` | ALERT/NOTICE banner with condition list | `health.halts`, verdict, `health.research_paused` |
| `SleeveCards` | 5 cards: equity, day/life pnl, cash, sparkline; amber UNAVAIL on error | `sleeves.*` |
| `PositionsPanel` | per-sleeve accordion of positions tables | `sleeves.*.positions` |
| `FillsPanel` | merged fills-today table | `sleeves.*.fills_today` |
| `FunnelPanel` | screener last run, memo pills, signal pills, open-memos table | `funnel` |
| `ActivityFeed` | event list + kind `<select>` filter | `/api/events` |
| `OvernightPanel` | vetting/drain times, WINDOW ACTIVE / PAUSED badge | `funnel.overnight` |
| `AnomaliesPanel` | 7d anomaly counts table | `anomalies_7d` |
| `LogsPanel` | collapsible ops.out.log / ops.err.log tails, lazy fetch + refresh | `/api/logs` |
| `SleeveDrillDrawer` | right slide-in: big chart, positions, fills for one sleeve | `sleeves.<name>` |

### Data layer (`dashboard-ui/src/data/`)

- `usePoll` hook: fetch `/api/snapshot` + `/api/events` every 5s;
  skip a tick while a fetch is in flight (same as current app);
  3 consecutive failures (~15s) → disconnected banner with last-good
  timestamp.
- **Money is decimal strings end-to-end.** The API serializes `Decimal` as
  strings deliberately; display formatting is string arithmetic (round,
  group thousands, `$`/sign) — never `parseFloat` on money.
- **No `dangerouslySetInnerHTML` anywhere.** Journal payloads carry
  arbitrary operator/ticker text; React's default escaping is the defense.
- **Per-section error isolation preserved:** any section or sleeve carrying
  `{"error": ...}` renders the design's UNAVAIL chip; the rest of the page
  renders normally. A partial dashboard beats a blank page.
- Derived-state mappers are pure functions (formatting, pnl coloring,
  event-kind color/bg, alert-condition derivation) so they unit-test
  without DOM.

### Backend change (small)

`ops/dashboard/server.py::_api_events` merges only momentum/research/
baseline journals. Add `short` (`config.short_journal_path`) and `insider`
(`config.insider_journal_path`) so the activity feed covers all five live
sleeves. Plus a test. No other server changes; bind stays hardcoded
`127.0.0.1`.

### Hostname (`ops/deploy/setup_opsdash.sh` + LaunchDaemon)

Idempotent script, run once with sudo:

1. `/etc/hosts`: add `127.0.0.1 opsdash.test` (skip if present). IPv4
   only, on purpose: the pf rule below is `inet`, and an `::1` mapping
   would send browsers to an unredirected IPv6 port 80 first.
2. pf anchor file `/etc/pf.anchors/com.tradingagents.opsdash`:
   `rdr pass on lo0 inet proto tcp from any to 127.0.0.1 port 80 -> 127.0.0.1 port 8321`
3. LaunchDaemon `com.tradingagents.opsdash-pf.plist`: at boot, load the
   anchor (`pfctl -a com.apple/250.opsdash -f <anchor>` and `pfctl -E`).
   Loaded into the `com.apple/*` namespace, not `com.tradingagents.opsdash`,
   because stock macOS `/etc/pf.conf` only evaluates
   `rdr-anchor "com.apple/*"` — anything outside that namespace never gets
   pulled in. This also avoids editing `/etc/pf.conf` (macOS updates can
   clobber it).

Server still binds `127.0.0.1:8321` only; pf redirects loopback port-80
traffic. Nothing becomes network-reachable. `http://opsdash.test` works in
any local browser; uninstall = remove hosts line, anchor file, plist.

## Error handling

- Poll failure: disconnected banner (red, sticky) with last-update time;
  panels keep last-good data.
- Sectional `{"error": ...}`: UNAVAIL chip per panel/card (amber tag +
  message), as in the design's sleeve-error treatment.
- Log fetch failure: inline error text in the `<pre>`, panel stays usable.

## Testing

- **Vitest** (`dashboard-ui/`): pure mappers — decimal-string formatting,
  pnl sign/color, event kind→color, alert-condition derivation,
  disconnected reducer. TDD.
- **Python** (`tests/ops/dashboard/`): extend events test for the two new
  journal paths; smoke test that built assets serve via `_static`
  (existing route tests keep passing).
- Manual: /verify against the live snapshot on this machine, plus
  `curl -H 'Host: opsdash.test' http://127.0.0.1/` after pf setup.

## Deployment

Built assets land in `ops/dashboard/static/` in the repo → normal PR →
pull in the live checkout (`TradingAgents-live`) → restart the dashboard
service gracefully (never `kickstart -k`). Hostname script is a one-time
manual sudo step, documented in the README.
