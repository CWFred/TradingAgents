# Execute: Phase A — Consolidate, Verify, Deploy

## Setup
Check out the branch and read the two governing documents FIRST, in this order:

    git fetch origin claude/smallcap-research-coverage-dervpt
    git checkout claude/smallcap-research-coverage-dervpt   # expect HEAD ≈ e9b4294
    # 1) docs/superpowers/specs/2026-07-06-finish-research-system-design.md  (the why + all locked decisions)
    # 2) docs/superpowers/plans/2026-07-06-phase-a-hardening.md              (the how — your task list)

The plan is authoritative and self-contained: 9 tasks, each with exact file
paths, complete test + implementation code, expected command output, and a
commit step. Execute it task-by-task with
superpowers:subagent-driven-development (preferred) or
superpowers:executing-plans. Track progress in the ledger at
`.superpowers/sdd/progress.md` (append a new "# Phase A" section; prior
sections show the expected format).

## Context in one paragraph
This repo runs a paper-trading system with two sleeves on one ops chassis:
a momentum sleeve (merged to main as PR #11) and a long-horizon research
screener + null-baseline portfolio (PR #12 — currently OPEN and CONFLICTING,
because #11 merged after the research branch forked). On 2026-07-06 the
daemon went silently blind for a day (Yahoo rate-limiting made yfinance
raise per-name; the universe came back empty; no alarm exists) and a branch
switch in the dev checkout changed which code the daemon runs. Phase A fixes
all of this: reconcile the branches (Task 1), add pacing/retry + blindness
alarms + coverage telemetry + two data-quality fixes + a weekly screen job
(Tasks 2–8), then move the daemon to an isolated release worktree and run a
live calibration sweep (Task 9).

## Hard rules (violating any of these is a failed run)
1. The working tree has unrelated user edits in `main.py` (repo root) and
   `tradingagents/dataflows/reddit.py`. NEVER stage them. Always `git add`
   explicit file lists — never `git add -A` / `git add .`.
2. Task 1 merge: conflicts are expected ONLY in the 8 files the plan lists.
   A conflict anywhere else → STOP, report BLOCKED with the file list.
3. USER GATES — stop and ask before: (a) `gh pr merge 12` (push the merged
   branch first, then ask); (b) every `launchctl` command in Task 9; (c) the
   Phase A PR merge in Task 9 Step 1.
4. The launchd daemon (`com.tradingagents.ops`) is running. Do not stop,
   restart, or reconfigure it before Task 9's user gate.
5. `SEC_EDGAR_USER_AGENT` is needed only for Task 9's calibration run —
   source it from the gitignored `.env`; never commit or print credentials.
6. Full suite green (`pytest tests/ -q`) before every task's commit;
   `ruff check` clean on files you touched (≈26 pre-existing errors in
   untouched ops/ files are NOT yours). Baseline pre-merge: 1146 passed /
   13 skipped (skips are opt-in live tests).
7. If a plan instruction contradicts what you find in the code, STOP and
   report — do not improvise. The escalation rule is in the plan's Global
   Constraints.

## Definition of done
All 9 plan tasks committed; PR #12 merged (after user gate); Phase A PR open
with the verification checklist at the bottom of the plan satisfied; final
report includes: PR URLs, daemon + screen-job `launchctl` status, the
calibration coverage table, and whether the 60% coverage gate passed.

## After Phase A (not yours — context only)
Phases B (memo brain), C (monitoring loop), D (sizing + calibration) are
specced in the design doc and get their own plans once Phase A lands.
Momentum sunset review is due ~2026-08-30.
