"""Opt-in live-network tests against the real Robinhood MCP.

Gated on OPS_RH_LIVE_TESTS=1. Requires an OAuth-authenticated token file
(first run performs the browser flow interactively). Read-only calls only.

Forward-looking note (Task 9 reviewer flagged): asyncio.run() inside
_call_tool spins a fresh event loop per call. If connect() opens the SDK's
async transport via a long-lived context manager, later _call_tool invocations
from a different loop may hit cross-loop stream bugs (anyio/asyncio streams
bound to origin loop). If first live-test run flakes on stream binding, check
the event-loop isolation in _call_tool vs. the SDK's transport lifecycle.
"""
import os
from decimal import Decimal
import pytest

from ops.broker.mcp_client import RealRobinhoodMCPClient

pytestmark = pytest.mark.skipif(
    os.environ.get("OPS_RH_LIVE_TESTS") != "1",
    reason="live Robinhood MCP tests are opt-in; set OPS_RH_LIVE_TESTS=1 to run",
)


@pytest.fixture(scope="module")
def client() -> RealRobinhoodMCPClient:
    c = RealRobinhoodMCPClient()
    c.connect()
    return c


def test_get_account_returns_positive_equity(client):
    acct = client.get_account()
    assert acct.equity > Decimal("0")
    assert acct.cash >= Decimal("0")


def test_get_positions_returns_list_of_mcp_positions(client):
    positions = client.get_positions()
    for p in positions:
        assert p.symbol.isupper()
        assert p.quantity > Decimal("0")


def test_get_quote_returns_decimal(client):
    q = client.get_quote("SPY")
    assert q > Decimal("0")


def test_token_file_has_0600_perms():
    from ops.broker.mcp_client import _resolve_token_path
    path = _resolve_token_path()
    assert path.exists(), "token file should be created after connect()"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600
