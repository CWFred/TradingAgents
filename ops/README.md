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
- Live order placement in tests is forbidden. If the CI runs live tests
  and this file grows a `place_order` call, revert.
