# Runbook — Paper go-live (the v1 merge gate)

**Goal:** the master spec's stated v1 gate — *"run the orchestrator on a real trading day
with `BROKER=paper`; forced test BUY passes guardrails to the paper book, forced stop
hits, push + email arrive."* Once that eyes-on-glass pass is captured, v1 is "shipped" and
the 8-week paper-trading clock (graduation criteria) starts.

State at time of writing: `main @ 17b6154`, **448 tests passing**, entire v1 codebase built.
Nothing below is more building — it's configuring + running + watching.

---

## Which review findings block what

A round-2 review (`docs/superpowers/plans/2026-07-04-ops-review-round2-fixes.md`) found new items.
Triage for go-live:

| Finding | Blocks paper smoke? | Blocks 8-week paper run w/ notifications? | Blocks live? |
|---|---|---|---|
| **C1** live-gate rejects instead of caps | no (live only) | no | **YES** |
| **C2** MCP connect() thread race | no (live only) | no | **YES** |
| **M2** first-enable notification storm | no* | **YES** (if journal has history) | — |
| **M4** kill-switch push has empty body | no | **YES** (kill-switch is the #1 alert) | — |
| **M5** SMTP creds on unverified TLS | no (if push-only) | **YES** (if using email) | — |
| **M1** dispatcher poison-pill wedge | no | **YES** (one bad event kills all notifications) | — |
| **M3** daily-summary wrong P&L / holiday emails | no | should fix | — |

\* M2 is moot on a **fresh** journal (no history to replay).

**Bottom line:** the paper *smoke test* can be done today. A clean multi-week paper run
**with notifications** wants the notify batch (M1, M2, M4, M5, M3) fixed first. C1/C2 are
only prerequisites for eventually flipping to live.

---

## Phase A — Validate the engine now (no notifications, no scheduler)

Proves guardrails + fill + stop-check end-to-end in one synchronous pass. Safe anytime.

```bash
cd ~/Code/TradingAgents

# Forced BUY through the FULL guarded stack (no LLM calls).
# --force-candidate injects the symbol past the universe filters (earnings/liquidity)
# at its real quote — WITHOUT it, --stub-pipeline-buy no-ops unless the symbol
# happens to have a fresh earnings beat. Guardrails still fully apply.
.venv/bin/ops decide-once --date $(date +%Y-%m-%d) \
  --force-candidate AAPL --stub-pipeline-buy AAPL --starting-cash 250
```

Watch the printed output for: universe → the forced BUY → `FILLED qty=… @ $…` (guardrails
passed) → the guardian stop-check pass → end-of-pass equity + open positions.

Inspect the journal:
```bash
JOURNAL=~/.local/state/tradingagents/ops_journal.sqlite   # 'ops' prints the resolved path at startup
sqlite3 "$JOURNAL" "SELECT at, kind FROM events ORDER BY id DESC LIMIT 20;"
sqlite3 "$JOURNAL" "SELECT symbol, side, quantity, price FROM fills ORDER BY id DESC LIMIT 5;"
```

Expected event kinds: a `fill` (from the BUY). Try a deny-listed symbol to see the guard fire.
`--force-candidate` is required here too: without it the universe deny-list silently drops
the symbol before the `DenyListRule` guardrail is ever reached, so nothing visible happens.
```bash
.venv/bin/ops decide-once --date $(date +%Y-%m-%d) \
  --force-candidate SPOT --stub-pipeline-buy SPOT   # must print REJECTED [DenyListRule] (SPOT blackout)
.venv/bin/ops decide-once --date $(date +%Y-%m-%d) \
  --force-candidate TQQQ --stub-pipeline-buy TQQQ   # must print REJECTED [DenyListRule] (leveraged ETF)
```

If Phase A looks right, the trading engine is sound.

---

## Phase B — Fix the notification findings (recommended before Phase C)

Fresh session, same SDD flow as the prior rounds. Fix, in order: **M1, M2, M4, M5, M3**
from the round-2 doc (all in `ops/notify/`). See the next-session prompt below — it drives this.
Skip this only if you want a bare push-only smoke on a **fresh** journal and accept an
empty kill-switch body.

---

## Phase C — Configure notifications + full eyes-on-glass run

### 1. Pushover (push)
- Create/open a Pushover account → note your **User Key**.
- Create a Pushover **Application** → note its **API Token**.

### 2. SMTP (email) — optional but in the spec
- Use an app-specific password (e.g. Gmail app password), host `smtp.gmail.com`, port `587`.
- **Do not enable email until M5 is fixed** (STARTTLS currently unverified — MITM can grab the password).

### 3. Environment (put in a `.env` or your shell profile; the service reads `OPS_*`)
```bash
export OPS_BROKER_MODE=paper            # stays paper — do NOT set robinhood yet
export OPS_NOTIFY_ENABLED=1
export OPS_PUSHOVER_USER_KEY=…
export OPS_PUSHOVER_APP_TOKEN=…
# email (only after M5):
export OPS_SMTP_HOST=smtp.gmail.com
export OPS_SMTP_PORT=587
export OPS_SMTP_USER=you@gmail.com
export OPS_SMTP_PASSWORD=…              # app password
export OPS_SMTP_FROM=you@gmail.com
export OPS_SMTP_TO=you@gmail.com
```

### 4. Verify delivery in isolation
```bash
# After a Phase-A fill exists in the journal, force one dispatch pass:
.venv/bin/ops notify-once
```
Confirm a push (and email) arrives. A disabled/misconfigured transport is a silent no-op
(it will just say "dispatched 0 message(s)" or skip) — check the creds if nothing arrives.

### 5. Full service, on a real trading day
```bash
# Keep it in tmux/a dedicated terminal. Ctrl-C = graceful shutdown (drains jobs, closes journal).
.venv/bin/ops run
```
Startup prints the resolved journal path and reconciliation result. Then: orchestrator ticks
every 30 min during NYSE hours; guardian polls every 60s; notify dispatcher every 20s;
daily summary at 16:05 ET.

**Eyes-on-glass checklist (capture screenshots/logs for the record):**
- [ ] Service starts, prints journal path, reconciliation clean (no `startup_halted`).
- [ ] A scheduled tick runs (or force activity with `decide-once --stub-pipeline-buy` against the **same** journal in another shell).
- [ ] A forced BUY passes guardrails → `fill` event → **push arrives**.
- [ ] Force a stop: seed a BUY, then a later guardian pass on a price ≤ entry×0.92 → `stop_hit` → **push arrives**.
- [ ] `daily_summary` at close → push (one-line) + email (full).
- [ ] Ctrl-C → clean shutdown (exit 0), journal intact.

Watch live:
```bash
watch -n 30 'sqlite3 ~/.local/state/tradingagents/ops_journal.sqlite \
  "SELECT at, kind FROM events ORDER BY id DESC LIMIT 15;"'
```

---

## After the smoke: the 8-week paper period (graduation criteria)

Leave `ops run` running in paper mode. Graduation to live requires (spec §Graduation):
1. ≥ 8 continuous weeks; orchestrator error-free on ≥ 80% of trading days.
2. Cumulative paper P&L > 0.
3. ≤ 1 kill-switch trigger in the window.

**Only after** those are met, and **after C1 + C2 are fixed**, do the live flip:
`OPS_BROKER_MODE=robinhood`, `LIVE_MAX_POSITION=$10`, and note the agentic account needs
spendable buying power (currently $0). The first 20 live fills are code-capped at $10.

---

## Rollback / safety
- Ctrl-C anytime → graceful shutdown. The journal is the source of truth; a restart rebuilds state.
- Paper mode touches no real money. SPOT is dual-blocked. The kill switch auto-closes in paper.
- To reset for a clean smoke: point `OPS_JOURNAL_PATH` at a fresh file (avoids M2 replay storm).

## Deploy (live worktree)

The daemon and the weekly screen run from `~/Code/TradingAgents-live` (a git
worktree pinned to `main`, with its own venv) — NEVER from the dev checkout.
This is the fix for the 2026-07-06 deploy hazard: a branch switch in the dev
checkout used to change what the daemon ran on its next relaunch. Both plists
(`com.tradingagents.ops`, `com.tradingagents.screen`) point at the live
worktree's interpreter and WorkingDirectory; their `EnvironmentVariables`
blocks carry the notify/heartbeat/LLM credentials — re-rendering with
`install-service`/`install-screen-service` resets that block to template
defaults, so re-apply the env keys after any re-render.

Redeploy after merging to main:

    git -C ~/Code/TradingAgents-live pull --ff-only
    ~/Code/TradingAgents-live/.venv/bin/pip install -e ~/Code/TradingAgents-live   # only if deps changed
    launchctl kickstart -k gui/$(id -u)/com.tradingagents.ops

The screen job (`com.tradingagents.screen`, Saturday 10:00 local) picks up
new code on its next run automatically; kickstart it manually to run early.
Rule: the dev checkout (`~/Code/TradingAgents`) never runs services.

Momentum sunset review due ~2026-08-30 (8-week paper gate): keep / pause /
retire on its track record. See
docs/superpowers/specs/2026-07-06-finish-research-system-design.md.

## Resolved: `ops decide-once` composite parity (was a known limitation)

Fixed in Phase A (PR #13): `ops decide-once` (non-forced) now builds the
composite universe (earnings + momentum sleeves), matching what the daemon
runs. It still does not run the exit engine — the always-on service remains
the only wired path for exits and the once-daily cycle gate.

Deferred follow-up (from the whole-branch review): the composite builder
has no sanity floor on leaderboard cardinality — under a *partial* yfinance
outage, "top-8" means top-8 of whatever fetched, so entry quality can silently
degrade (exits are hold-biased and unaffected). Consider skipping momentum
*entries* when the leaderboard is implausibly small (e.g. under half of
expected membership), journaling why.
