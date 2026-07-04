"""Opt-in live READ-ONLY smoke tests for `RealRobinhoodMCPClient`.

READ-ONLY. This file MUST NOT call place_equity_order / review_equity_order /
cancel_equity_order or any write tool. The live ORDER round-trip is a
separate, post-graduation, human-initiated step gated by the $10/first-20
LiveMaxPositionRule (see docs/superpowers/specs/2026-07-04-tradingagents-mcp-
live-design.md § "Testing & the live boundary"). Only get_account,
get_positions, get_quote, connect, and close are exercised here.

Gated on OPS_RH_LIVE_TESTS=1 (skipped by default — the normal suite must
never touch the network). Requires an OAuth-authenticated token file (first
run performs the browser flow interactively; cached thereafter). Mirrors the
opt-in gate pattern in tests/ops/broker/test_robinhood_live.py.
"""
import os
from decimal import Decimal

import pytest

from ops.broker.mcp_client import AccountInfo, MCPPosition, RealRobinhoodMCPClient

pytestmark = pytest.mark.skipif(
    os.environ.get("OPS_RH_LIVE_TESTS") != "1",
    reason="live Robinhood MCP tests are opt-in; set OPS_RH_LIVE_TESTS=1 to run",
)


@pytest.fixture(scope="module")
def client() -> RealRobinhoodMCPClient:
    c = RealRobinhoodMCPClient()
    c.connect()
    yield c
    c.close()


def test_get_account_returns_typed_account_info(client):
    acct = client.get_account()
    assert isinstance(acct, AccountInfo)
    assert isinstance(acct.cash, Decimal)
    assert isinstance(acct.equity, Decimal)
    assert isinstance(acct.buying_power, Decimal)
    assert acct.equity >= Decimal("0")


def test_get_positions_returns_list_of_mcp_positions(client):
    positions = client.get_positions()
    assert isinstance(positions, list)
    for p in positions:
        assert isinstance(p, MCPPosition)
        assert isinstance(p.symbol, str) and p.symbol
        assert isinstance(p.quantity, Decimal)
        assert isinstance(p.shares_available_for_sells, Decimal)


def test_get_quote_returns_positive_decimal(client):
    price = client.get_quote("AAPL")
    assert isinstance(price, Decimal)
    assert price > Decimal("0")


def test_agentic_account_resolved_exactly_once(client):
    # Proves account resolution + gating actually ran against the real
    # get_accounts list: exactly one agentic-allowed account was found and
    # memoized (see RealRobinhoodMCPClient._resolve_account).
    account_number = client._resolve_account()
    assert account_number
    assert client._account_number == account_number


def test_close_tears_down_cleanly():
    # Independent client/connect/close cycle so it doesn't interfere with
    # the module-scoped `client` fixture other tests depend on.
    c = RealRobinhoodMCPClient()
    c.connect()
    c.close()
    assert c._session is None
