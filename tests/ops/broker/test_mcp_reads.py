"""Read-path DTO mapping + agentic-account resolution tests for
`RealRobinhoodMCPClient`, driven against the REAL response shapes captured
live 2026-07-04 (see docs/superpowers/specs/2026-07-04-tradingagents-mcp-live-design.md
§"Ground truth captured live").

These feed literal fixture dicts through a fake `ClientSession` (mimicking
`mcp.types.CallToolResult`'s `.isError`/`.structuredContent` shape) so the
real `_call_tool` unpacking path is exercised too — no network.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from ops.broker.mcp_client import (
    MCPProtocolError,
    MCPUnavailable,
    RealRobinhoodMCPClient,
)

# --- fixtures (verbatim from the design doc's "Ground truth captured live") --

ACCOUNTS_FIXTURE = {
    "data": {
        "accounts": [
            {
                "account_number": "5UK30936",
                "rhs_account_number": "5UK30936",
                "type": "margin",
                "agentic_allowed": False,
                "is_default": True,
                "option_level": "2",
            },
            {
                "account_number": "502163744",
                "rhs_account_number": "502163744",
                "nickname": "Agentic",
                "agentic_allowed": True,
                "type": "cash",
                "option_level": "",
                "is_default": False,
            },
        ]
    },
    "guide": "ignored prose",
}

ALL_NON_AGENTIC_FIXTURE = {
    "data": {
        "accounts": [
            {"account_number": "5UK30936", "type": "margin", "agentic_allowed": False},
            {"account_number": "502163744", "type": "cash", "agentic_allowed": False},
        ]
    },
    "guide": "ignored prose",
}

PORTFOLIO_FIXTURE = {
    "data": {
        "total_value": "238.07340825",
        "equity_value": "238.07340825",
        "options_value": "0",
        "cash": "0",
        "pending_deposits": "0",
        "buying_power": {
            "buying_power": "0.0000",
            "unleveraged_buying_power": "0.0000",
            "display_currency": "USD",
        },
    },
    "guide": "ignored prose",
}

POSITIONS_FIXTURE = {
    "data": {
        "positions": [
            {
                "symbol": "GLD",
                "quantity": "0.270317",
                "intraday_quantity": "0",
                "average_buy_price": "369.940000",
                "shares_available_for_sells": "0.270317",
                "shares_held_for_sells": "0",
                "type": "long",
            },
            {
                "symbol": "MU",
                "quantity": "0.138888",
                "intraday_quantity": "0",
                "average_buy_price": "1080.010000",
                "shares_available_for_sells": "0.138888",
                "shares_held_for_sells": "0",
                "type": "long",
            },
        ]
    },
    "guide": "ignored prose",
}


def _quote_fixture(**overrides) -> dict:
    quote = {
        "symbol": "AAPL",
        "last_trade_price": "308.240000",
        "last_non_reg_trade_price": "308.450000",
        "venue_last_trade_time": "2026-07-04T20:00:00Z",
        "bid_price": "304.01",
        "ask_price": "317.00",
        "previous_close": "294.38",
        "has_traded": True,
        "state": "active",
    }
    quote.update(overrides)
    return {
        "data": {"results": [{"quote": quote, "close": {"symbol": "AAPL", "price": "294.38"}}]},
        "guide": "ignored prose",
    }


QUOTE_FIXTURE = _quote_fixture()


# --- fake session (mimics mcp.types.CallToolResult) -------------------------


class _FakeResult:
    def __init__(self, structured_content, is_error=False):
        self.isError = is_error
        self.structuredContent = structured_content


class _FakeSession:
    """Routes call_tool(name, arguments) to a canned response dict per name."""

    def __init__(self, responses: dict[str, dict]):
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name not in self._responses:
            raise AssertionError(f"unexpected tool call: {name}({arguments!r})")
        return _FakeResult(self._responses[name])


def _client_with(**responses) -> tuple[RealRobinhoodMCPClient, _FakeSession]:
    client = RealRobinhoodMCPClient()
    session = _FakeSession(responses)
    client._session = session
    return client, session


# --- get_account → AccountInfo from get_portfolio ---------------------------


def test_get_account_maps_portfolio_to_account_info():
    client, session = _client_with(
        get_accounts=ACCOUNTS_FIXTURE, get_portfolio=PORTFOLIO_FIXTURE,
    )
    acct = client.get_account()
    assert acct.cash == Decimal("0")
    assert acct.equity == Decimal("238.07340825")
    assert acct.buying_power == Decimal("0.0000")
    # account resolved from get_accounts, threaded into get_portfolio
    portfolio_calls = [c for c in session.calls if c[0] == "get_portfolio"]
    assert portfolio_calls == [("get_portfolio", {"account_number": "502163744"})]


def test_get_account_falls_back_to_total_value_when_equity_value_missing():
    portfolio = {
        "data": {
            "total_value": "238.07340825",
            "cash": "0",
            "buying_power": {"buying_power": "0.0000"},
        },
    }
    client, _ = _client_with(get_accounts=ACCOUNTS_FIXTURE, get_portfolio=portfolio)
    acct = client.get_account()
    assert acct.equity == Decimal("238.07340825")


def test_get_account_missing_data_raises_protocol_error():
    client, _ = _client_with(get_accounts=ACCOUNTS_FIXTURE, get_portfolio={"guide": "no data key"})
    with pytest.raises(MCPProtocolError):
        client.get_account()


def test_get_account_missing_buying_power_raises_protocol_error():
    portfolio = {"data": {"equity_value": "1", "cash": "0"}}  # no buying_power at all
    client, _ = _client_with(get_accounts=ACCOUNTS_FIXTURE, get_portfolio=portfolio)
    with pytest.raises(MCPProtocolError):
        client.get_account()


# --- get_positions → MCPPosition[] incl. shares_available_for_sells --------


def test_get_positions_maps_real_shape():
    client, session = _client_with(
        get_accounts=ACCOUNTS_FIXTURE, get_equity_positions=POSITIONS_FIXTURE,
    )
    positions = client.get_positions()
    assert len(positions) == 2
    gld = next(p for p in positions if p.symbol == "GLD")
    assert gld.quantity == Decimal("0.270317")
    assert gld.avg_price == Decimal("369.940000")
    assert gld.shares_available_for_sells == Decimal("0.270317")
    positions_calls = [c for c in session.calls if c[0] == "get_equity_positions"]
    assert positions_calls == [("get_equity_positions", {"account_number": "502163744"})]


def test_get_positions_missing_data_raises_protocol_error():
    client, _ = _client_with(
        get_accounts=ACCOUNTS_FIXTURE, get_equity_positions={"guide": "no data"},
    )
    with pytest.raises(MCPProtocolError):
        client.get_positions()


def test_get_positions_row_missing_field_raises_protocol_error():
    positions = {"data": {"positions": [{"symbol": "GLD", "quantity": "1"}]}}  # no average_buy_price
    client, _ = _client_with(get_accounts=ACCOUNTS_FIXTURE, get_equity_positions=positions)
    with pytest.raises(MCPProtocolError):
        client.get_positions()


# --- get_quote → picks price, rejects unreliable quotes ---------------------


def test_get_quote_picks_last_trade_price_when_present():
    client, session = _client_with(get_equity_quotes=QUOTE_FIXTURE)
    price = client.get_quote("AAPL")
    assert price == Decimal("308.240000")
    quote_calls = [c for c in session.calls if c[0] == "get_equity_quotes"]
    assert quote_calls == [("get_equity_quotes", {"symbols": ["AAPL"]})]


def test_get_quote_falls_back_to_non_reg_price_when_last_trade_price_missing():
    # Real schema (design doc, "Ground truth captured live" § Quotes) has no
    # per-price timestamp to compare — last_non_reg_trade_price is only ever
    # used when last_trade_price itself is absent.
    fixture = _quote_fixture(last_trade_price=None)
    client, _ = _client_with(get_equity_quotes=fixture)
    assert client.get_quote("AAPL") == Decimal("308.450000")


def test_get_quote_rejects_when_has_traded_false():
    fixture = _quote_fixture(has_traded=False)
    client, _ = _client_with(get_equity_quotes=fixture)
    with pytest.raises(MCPUnavailable):
        client.get_quote("AAPL")


def test_get_quote_rejects_when_state_not_active():
    fixture = _quote_fixture(state="halted")
    client, _ = _client_with(get_equity_quotes=fixture)
    with pytest.raises(MCPUnavailable):
        client.get_quote("AAPL")


def test_get_quote_missing_data_raises_protocol_error():
    client, _ = _client_with(get_equity_quotes={"guide": "no data"})
    with pytest.raises(MCPProtocolError):
        client.get_quote("AAPL")


def test_get_quote_no_results_raises_protocol_error():
    client, _ = _client_with(get_equity_quotes={"data": {"results": []}})
    with pytest.raises(MCPProtocolError):
        client.get_quote("AAPL")


# --- agentic-account resolution ---------------------------------------------


def test_resolve_account_picks_agentic_allowed():
    client, session = _client_with(get_accounts=ACCOUNTS_FIXTURE)
    assert client._resolve_account() == "502163744"
    assert len([c for c in session.calls if c[0] == "get_accounts"]) == 1


def test_resolve_account_memoizes_across_calls():
    client, session = _client_with(get_accounts=ACCOUNTS_FIXTURE)
    assert client._resolve_account() == "502163744"
    assert client._resolve_account() == "502163744"
    assert len([c for c in session.calls if c[0] == "get_accounts"]) == 1


def test_resolve_account_refuses_when_all_non_agentic():
    client, _ = _client_with(get_accounts=ALL_NON_AGENTIC_FIXTURE)
    with pytest.raises(MCPUnavailable, match="agentic"):
        client._resolve_account()


def test_resolve_account_missing_data_raises_protocol_error():
    client, _ = _client_with(get_accounts={"guide": "no data"})
    with pytest.raises(MCPProtocolError):
        client._resolve_account()


def test_resolve_account_env_override_honored_when_agentic(monkeypatch):
    monkeypatch.setenv("OPS_RH_ACCOUNT", "502163744")
    client, _ = _client_with(get_accounts=ACCOUNTS_FIXTURE)
    assert client._resolve_account() == "502163744"


def test_resolve_account_env_override_refused_when_not_agentic(monkeypatch):
    monkeypatch.setenv("OPS_RH_ACCOUNT", "5UK30936")
    client, _ = _client_with(get_accounts=ACCOUNTS_FIXTURE)
    with pytest.raises(MCPUnavailable):
        client._resolve_account()


def test_resolve_account_env_override_unknown_account_refused(monkeypatch):
    monkeypatch.setenv("OPS_RH_ACCOUNT", "does-not-exist")
    client, _ = _client_with(get_accounts=ACCOUNTS_FIXTURE)
    with pytest.raises(MCPUnavailable):
        client._resolve_account()
