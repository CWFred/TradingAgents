# dashboard-ui

React frontend for the local ops dashboard (`ops/dashboard/server.py`).

- `npm ci` — install (build-time only; the deployed service needs no Node)
- `npm run dev` — dev server; proxies `/api` to the live dashboard at 127.0.0.1:8321
- `npm test` — vitest unit tests (pure mappers, poll reducer)
- `npm run build` — typecheck + build into `ops/dashboard/static/` (commit the output)

Visual source of truth: `design/ops-dashboard.dc.html`.
Money is decimal strings end-to-end — never route money through floats.
