# dashboard-ui

React frontend for the local ops dashboard (`ops/dashboard/server.py`).

- `npm ci` — install (build-time only; the deployed service needs no Node)
- `npm run dev` — dev server; proxies `/api` to the live dashboard at 127.0.0.1:8321
- `npm test` — vitest unit tests (pure mappers, poll reducer)
- `npm run build` — typecheck + build into `ops/dashboard/static/` (commit the output)

Visual source of truth: `design/ops-dashboard.dc.html`.
Money is decimal strings end-to-end — never route money through floats.

## Access via http://opsdash.test

One-time setup (adds a hosts entry, a pf loopback redirect :80→:8321, and a
boot-persistent LaunchDaemon; see comments in the script):

    sudo bash ops/deploy/setup_opsdash.sh

Uninstall: remove the `opsdash.test` line from /etc/hosts, then
`sudo launchctl bootout system /Library/LaunchDaemons/com.tradingagents.opsdash-pf.plist`,
delete that plist plus /etc/pf.anchors/com.tradingagents.opsdash, and run
`sudo pfctl -a "com.apple/250.opsdash" -F all` to flush the loaded anchor
immediately (otherwise the redirect rule stays live until reboot).
