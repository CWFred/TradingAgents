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
2. Open the journal at `$OPS_JOURNAL_PATH` (default `ops_journal.sqlite`).
3. Build the guarded broker for the configured mode.
4. **Reconciliation gate:** compare journal-replayed state against the live
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

### Where things get logged

Journal (`sqlite3 ops_journal.sqlite`) — the events, orders, fills, and
equity snapshots tables carry the full audit trail. Notifications (push +
email) arrive in Plan 3c; until then, `sqlite3` queries against the journal
are the way to inspect state.
- Live order placement in tests is forbidden. If the CI runs live tests
  and this file grows a `place_order` call, revert.
