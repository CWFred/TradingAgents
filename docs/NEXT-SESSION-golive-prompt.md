# Next-session prompt (copy the block below into a fresh Claude Code session)

---

Continue the TradingAgents live-trading buildout at ~/Code/TradingAgents. The entire v1
codebase is built and merged (PRs #1–#8, main @ 17b6154, 448 tests passing). Read these
first — they carry the full context: memories `project_tradingagents`, `project_spot_blackout`,
`plan-3c-bootstrap`, `mcp-live-client`, `feedback-planning-autonomy`; the master spec
`docs/superpowers/specs/2026-06-30-tradingagents-live-v1-design.md`; the go-live runbook
`docs/RUNBOOK-paper-golive.md`; and the round-2 review
`docs/superpowers/plans/2026-07-04-ops-review-round2-fixes.md`. Per-task history is in
`.superpowers/sdd/progress.md` (gitignored).

**Goal of this session:** get to the master spec's v1 gate — an eyes-on-glass paper smoke
test on a real trading day (forced BUY passes guardrails → fill → push; forced stop → push;
daily summary → email) — and set up the 8-week paper run that gates graduation to live.

**Do this in order:**

1. **Fix the notification round-2 findings first** (they bite the moment notifications are
   on): M1 (dispatcher poison-pill wedges all notifications), M2 (first-enable storm replays
   history as pushes), M4 (kill-switch push renders an empty body — the single most important
   alert), M5 (SMTP creds ride an unverified TLS channel), then M3 (daily-summary stale-baseline
   P&L + holiday emails). All are in `ops/notify/` and fully specified in the round-2 doc.
   Use the same workflow as the prior rounds: `superpowers:subagent-driven-development` — one
   commit + two-stage review per finding, on a branch off main, then a whole-branch review and
   a PR (this repo is a fork: `gh pr create --repo CWFred/TradingAgents`). C1 and C2 are
   **live-mode only** — they do NOT block the paper smoke; leave them for the pre-live session.

2. **Walk me through the paper smoke test** using `docs/RUNBOOK-paper-golive.md`: Phase A
   (`ops decide-once --stub-pipeline-buy AAPL` to validate the guarded engine + a SPOT/TQQQ
   rejection), then help me set the `OPS_NOTIFY_*` env vars and verify delivery with
   `ops notify-once`, then the full `ops run` eyes-on-glass pass. I'll run the interactive/live
   commands myself — hand them to me as `! <command>` blocks.

**Hard constraints (do not violate):**
- SPOT is a contractual blackout — never weaken `DenyListRule` or `RobinhoodBroker._enforce_spot_hard_check`.
- Stay in **paper mode** this whole session. Do NOT set `OPS_BROKER_MODE=robinhood` and do
  NOT place any live order. The live flip is a separate, deliberate, post-graduation step
  (needs C1+C2 fixed, buying power in the agentic account — currently $0 — and my explicit go-ahead).
- The Robinhood MCP is connected in-session (tools `mcp__robinhood-trading__*`); the agentic
  account is `502163744` (the only `agentic_allowed=true` one). Read-only use only.

**Autonomy:** once a design/approach is agreed, don't pause on process gates — write, commit,
review, and proceed (per my `feedback-planning-autonomy` preference). Substantive design forks
still deserve a question. There are also two unrelated uncommitted edits in my working tree
(`main.py`, `tradingagents/dataflows/reddit.py`) — leave them alone; they're mine.

---
