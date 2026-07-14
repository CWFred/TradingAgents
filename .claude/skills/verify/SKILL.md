---
name: verify
description: Drive the ops dashboard (server + built React frontend) for end-to-end verification
---

# Verifying the ops dashboard

Serve this checkout's code + committed static build on a scratch port (reads the REAL live stores read-only — safe):

    python -c "from ops.dashboard.server import serve; serve(8397)" &

Drive it:
- `curl http://127.0.0.1:8397/` → 200 text/html, module script `/assets/app.js`
- `/assets/app.js` → 200 text/javascript; `/assets/app.css` → 200 text/css
- `/api/snapshot` → JSON with sections health/sleeves/funnel/anomalies_7d/market; 5 sleeves (momentum research baseline short insider)
- `/api/events?limit=200` → merged events; sources should span the sleeves
- `/api/logs?file=out&lines=5` → {"file","text"}

Probes that must hold: `--path-as-is /../server.py` → 404; `?file=../../etc/passwd` → 400; LAN-IP curl → connection refused (loopback-only bind).

Gotchas: `?limit=zzz` → 500 (pre-existing int() parse, known); frontend dev-mode uses `npm run dev` in dashboard-ui/ (proxies /api to the live :8321 service). Kill scratch server with `pkill -f "serve(8397)"`.

Pixel-level render capture needs a real browser — no headless harness in this repo; eyeball via the live URL after deploy.
