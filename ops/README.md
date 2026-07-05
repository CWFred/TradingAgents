## Robinhood MCP (live broker)

The live broker connects to Robinhood's official MCP endpoint at
`https://agent.robinhood.com/mcp/trading`. First run performs an OAuth
browser flow; the token is cached at `~/.config/tradingagents/robinhood_token.json`
with `0600` perms. Override via `OPS_RH_TOKEN_PATH`.

**Plan 3a ships the broker plumbing but NOT the always-on orchestration.**
`broker_mode` defaults to `paper`. The `build_guarded_robinhood_broker`
factory exists so the Plan 3b orchestrator can consume it.

### Running opt-in live tests

Read-only integration tests against the real MCP (never place orders) are
skipped by default. To run them:

```bash
OPS_RH_LIVE_TESTS=1 .venv/bin/pytest tests/ops/broker/test_robinhood_live.py -v
```

First invocation performs the OAuth browser flow. Subsequent runs reuse
the cached token until it expires.

### Constraints

- SPOT is contractually restricted. Both `DenyListRule` and a hard-coded
  check inside `RobinhoodBroker` reject any SPOT order. Do not remove
  either gate.

## Running the orchestrator service

The `ops run` command starts the always-on orchestrator + guardian in the
foreground. Keep it in a terminal, tmux, or a persistent shell of your
choice — SIGINT (Ctrl-C) triggers a graceful shutdown that drains
in-flight jobs and closes the journal cleanly.

```bash
# Paper mode (default): safe to run anytime.
.venv/bin/python -m ops.cli run

# Live mode: opt-in via env var.
OPS_BROKER_MODE=robinhood .venv/bin/python -m ops.cli run
```

### Startup behavior

1. Load config from `OPS_*` env vars and the built-in defaults.
2. Open the journal at `$OPS_JOURNAL_PATH` (default `ops_journal.sqlite`)
   and record a `service_started` event (broker mode, journal path, pid,
   git sha when available). On shutdown a `service_stopping` event records
   the exit code — together these are the uptime record the graduation
   evaluation ("ran on ≥80% of trading days") reads back.
3. Build the guarded broker for the configured mode.
4. **Live-flip ritual (robinhood mode only):** the FIRST live start prints
   the account equity and the live-gate parameters and requires you to type
   the equity figure back verbatim at a real terminal. Non-interactive
   stdin, EOF, or a mismatch refuses startup with exit code 4 (journal:
   `live_flip_refused`) and schedules nothing. Once the flip marker exists,
   restarts skip the prompt entirely — launchd restarts are unattended.
5. **Reconciliation gate:** compare journal-replayed state against the live
   broker's positions and cash.
   - No diffs → normal startup. Orchestrator ticks every 30 minutes during
     NYSE market hours; guardian polls every 60 seconds.
   - Diffs found → journal `inconsistency` + `startup_halted` events;
     orchestrator job does NOT start; guardian keeps running so existing
     stops are enforced. Exit code 2 on clean shutdown so a shell wrapper
     can distinguish this from a normal exit.

### Halt semantics

- **Daily drawdown** ≤ config threshold (default −7%): guardian records
  `daily_halt`; orchestrator no-ops for the rest of the day.
- **Weekly drawdown** ≤ config threshold (default −15%): guardian records
  `kill_switch`. Paper mode: guardian auto-closes all positions. Live mode:
  guardian halts only; user handles positions manually.

### Running as a service (launchd)

A terminal foreground process dies with the laptop lid: laptop sleep, a
crash, or a closed terminal silently stops everything — including the
guardian — and a dead process sends no notifications. Run the service
under launchd instead:

```bash
# Render the plist from ops/deploy/com.tradingagents.ops.plist.template
# with this checkout's paths. Writes the file and prints the load
# command — it never runs launchctl for you.
.venv/bin/python -m ops.cli install-service
# (default output: ~/Library/LaunchAgents/com.tradingagents.ops.plist)

# Then load it yourself:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tradingagents.ops.plist
# ... and unload with:
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.tradingagents.ops.plist
```

The rendered agent runs `ops run` in paper mode (`OPS_BROKER_MODE` is
deliberately not set in the template — the first live flip must go through
the interactive ritual, never a supervisor), restarts it on crash or
nonzero exit (`KeepAlive: {Crashed: true, SuccessfulExit: false}`), and
throttles restarts to one per 60s so exit code 3 (broker unreachable)
cannot hot-loop. stdout/stderr land under
`~/.local/state/tradingagents/logs/` (`install-service` creates the
directory).

**Sleep caveat:** launchd cannot run jobs on a sleeping laptop. If the
machine may be asleep at the open, consider a wake schedule (your call to
apply):

```bash
sudo pmset repeat wakeorpoweron MTWRF 09:20:00
```

### Dead-man's switch (external heartbeat)

Silence is indistinguishable from health: a dead process cannot push a
notification about its own death. Set `OPS_HEARTBEAT_URL` to a
healthchecks.io-style check URL and the service pings it every 60 seconds
— but only while the guardian loop has actually started a pass within the
last 3 minutes. A wedged or dead guardian stops the pings, and the
external service alerts you. Ping failures never disturb trading; they are
journaled as `heartbeat_error` (throttled email) at most once per 10
minutes. Unset (the default) = feature off.

### Where things get logged

Journal (`sqlite3 ops_journal.sqlite`) — the events, orders, fills, and
equity snapshots tables carry the full audit trail. Notifications (push +
email) arrive in Plan 3c; until then, `sqlite3` queries against the journal
are the way to inspect state.
- Live order placement in tests is forbidden. If the CI runs live tests
  and this file grows a `place_order` call, revert.

## Guardian process isolation (deferred — decision record)

Decision (2026-07-05, spec A2): one process means a pipeline OOM or
scheduler wedge kills stop-loss enforcement together with the thing that
needed stopping. Splitting the guardian into its own process is
**deferred** until either (a) the account exceeds ~$1,000, or (b) the live
flip — whichever comes first. Do not implement before then.

Hard constraint — write-down so nobody designs around it wrongly: process
isolation is only straightforward in **robinhood mode**, where positions
and cash live at the broker and any process can read them via MCP. In
**paper mode the book is in-memory inside PaperBroker**; a separate
guardian process cannot see it without moving the paper book into SQLite
(a real change to PaperBroker's write path, not a deployment tweak). The
deferred design is therefore: a separate `ops guardian` entrypoint,
live-mode-only, sharing the journal via WAL (multi-process safe),
constructing its own GuardedBroker + MCP client, with no
orchestrator/pipeline imports. The in-process guardian remains for paper
mode permanently. When implementing, the two processes must not both
auto-close on kill-switch (split: the separate process owns stops + kill
switch; the in-process guardian is disabled via config when the external
one is registered — a design detail for that future task, not now).

Full record: `docs/superpowers/plans/2026-07-05-ops-operational-architecture.md`,
section A2.
