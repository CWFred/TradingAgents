from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ops.broker.base import BrokerError, NoSuchPosition, OrderRejected
from ops.broker.mcp_client import (
    MCPUnavailable,
    RealRobinhoodMCPClient,
    RobinhoodMCPClient,
)
from ops.broker.robinhood import RobinhoodBroker
from ops.broker.types import Order, OrderType, Side
from ops.journal import Journal
from tests.ops.broker.fakes import FakeMCPClient


@pytest.fixture
def fake_client():
    return FakeMCPClient()


@pytest.fixture
def journal(tmp_path):
    return Journal(str(tmp_path / "j.sqlite"))


def test_fake_client_satisfies_protocol():
    client: RobinhoodMCPClient = FakeMCPClient()
    assert isinstance(client, RobinhoodMCPClient)


def test_get_cash_maps_from_account(fake_client, journal):
    fake_client.seed_position("AAPL", Decimal("5"), Decimal("10"))
    fake_client.set_quote("AAPL", Decimal("11"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    assert broker.get_cash() == fake_client.get_account().cash


def test_get_equity_maps_from_account(fake_client, journal):
    fake_client.seed_position("AAPL", Decimal("5"), Decimal("10"))
    fake_client.set_quote("AAPL", Decimal("11"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    assert broker.get_equity() == fake_client.get_account().equity


def test_get_positions_maps_mcp_positions(fake_client, journal):
    fake_client.seed_position("AAPL", Decimal("5"), Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].quantity == Decimal("5")
    assert positions[0].avg_entry_price == Decimal("10")
    assert positions[0].stop_loss_price is None


def test_get_positions_attaches_stop_from_journal(fake_client, journal):
    """A journaled BUY with stop → RobinhoodBroker.get_positions() carries it."""
    fake_client.seed_position("AAPL", Decimal("5"), Decimal("10"))
    ts = datetime(2026, 7, 2, tzinfo=timezone.utc)
    journal.record_fill(order_id="o-1", client_order_id="b-1", symbol="AAPL",
                        side="BUY", quantity=Decimal("5"), price=Decimal("10"),
                        filled_at=ts, stop_loss_price=Decimal("9.2"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    positions = broker.get_positions()
    assert positions[0].stop_loss_price == Decimal("9.2")


def test_get_positions_stop_none_when_no_journaled_buy(fake_client, journal):
    """Manual (non-journaled) position in RH → stop_loss_price=None."""
    fake_client.seed_position("MSFT", Decimal("2"), Decimal("300"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    positions = broker.get_positions()
    assert positions[0].stop_loss_price is None


def test_get_positions_stop_none_when_journaled_buy_lacks_stop(fake_client, journal):
    fake_client.seed_position("NVDA", Decimal("1"), Decimal("500"))
    ts = datetime(2026, 7, 2, tzinfo=timezone.utc)
    journal.record_fill(order_id="o-1", client_order_id="b-1", symbol="NVDA",
                        side="BUY", quantity=Decimal("1"), price=Decimal("500"),
                        filled_at=ts, stop_loss_price=None)
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    assert broker.get_positions()[0].stop_loss_price is None


def test_get_quote_delegates_to_client(fake_client, journal):
    fake_client.set_quote("AAPL", Decimal("11"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    assert broker.get_quote("AAPL") == Decimal("11")


def test_place_order_buy_calls_mcp(fake_client, journal):
    fake_client.set_quote("AAPL", Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    fill = broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9"),
    ))
    assert fill.side == Side.BUY
    assert fill.quantity == Decimal("5")
    assert len(fake_client.placed) == 1
    assert fake_client.placed[0].notional == Decimal("50")


def test_place_order_journals_order_and_fill(fake_client, journal):
    fake_client.set_quote("AAPL", Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9"),
    ))
    orders = journal.read_orders()
    fills = journal.read_fills()
    assert len(orders) == 1
    assert orders[0]["client_order_id"] == "b-1"
    assert len(fills) == 1
    assert fills[0]["symbol"] == "AAPL"


def test_close_position_places_quantity_sell(fake_client, journal):
    fake_client.set_quote("AAPL", Decimal("10"))
    fake_client.seed_position("AAPL", Decimal("5"), Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    fill = broker.close_position("AAPL")
    assert fill.side == Side.SELL
    assert fill.quantity == Decimal("5")
    ack = fake_client.placed[-1]
    assert ack.quantity == Decimal("5")
    assert ack.notional is None


def test_close_position_journals_order_before_fill(journal, fake_client):
    fake_client.set_quote("AAPL", Decimal("10"))
    fake_client.seed_position("AAPL", Decimal("5"), Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    broker.close_position("AAPL")
    orders = journal.read_orders()
    close_orders = [o for o in orders if o["client_order_id"].startswith("close-AAPL-")]
    assert len(close_orders) == 1
    assert close_orders[0]["side"] == "SELL"
    assert close_orders[0]["notional_dollars"] == Decimal("50")
    fills = journal.read_fills()
    close_fills = [f for f in fills if f["client_order_id"] == close_orders[0]["client_order_id"]]
    assert len(close_fills) == 1


def test_place_order_sell_calls_mcp(fake_client, journal):
    fake_client.set_quote("AAPL", Decimal("10"))
    fake_client.seed_position("AAPL", Decimal("5"), Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    fill = broker.place_order(Order(
        client_order_id="s-1", symbol="AAPL", side=Side.SELL,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=None,
    ))
    assert fill.side == Side.SELL
    assert fill.quantity == Decimal("5")
    assert len(fake_client.placed) == 1
    assert fake_client.placed[0].side == Side.SELL
    assert fake_client.placed[0].notional == Decimal("50")


def test_close_position_missing_raises(fake_client, journal):
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    with pytest.raises(NoSuchPosition):
        broker.close_position("NVDA")


def test_mcp_unavailable_wraps_as_broker_error(fake_client, journal):
    fake_client.set_quote("AAPL", Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    fake_client.fail_next(MCPUnavailable("network"))
    with pytest.raises(BrokerError):
        broker.place_order(Order(
            client_order_id="b-1", symbol="AAPL", side=Side.BUY,
            notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
            stop_loss_price=Decimal("9"),
        ))


def test_mcp_unavailable_wraps_on_get_cash(fake_client, journal):
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    fake_client.fail_next(MCPUnavailable("network"))
    with pytest.raises(BrokerError):
        broker.get_cash()


def test_mcp_unavailable_wraps_on_close_position(fake_client, journal):
    fake_client.seed_position("AAPL", Decimal("5"), Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    fake_client.fail_next(MCPUnavailable("network"))
    with pytest.raises(BrokerError):
        broker.close_position("AAPL")


def test_get_equity_wraps_mcp_unavailable(fake_client, journal):
    fake_client.fail_next(MCPUnavailable("net"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    with pytest.raises(BrokerError):
        broker.get_equity()


def test_get_positions_wraps_mcp_unavailable(fake_client, journal):
    fake_client.fail_next(MCPUnavailable("net"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    with pytest.raises(BrokerError):
        broker.get_positions()


def test_get_quote_wraps_mcp_unavailable(fake_client, journal):
    fake_client.fail_next(MCPUnavailable("net"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    with pytest.raises(BrokerError):
        broker.get_quote("AAPL")


def test_place_order_spot_hard_check_rejects(fake_client, journal):
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    with pytest.raises(OrderRejected) as exc:
        broker.place_order(Order(
            client_order_id="b-1", symbol="SPOT", side=Side.BUY,
            notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
            stop_loss_price=Decimal("9"),
        ))
    assert exc.value.rule_name == "SpotDenyList"
    assert len(fake_client.placed) == 0


def test_close_position_spot_hard_check_rejects(fake_client, journal):
    fake_client.seed_position("SPOT", Decimal("5"), Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    with pytest.raises(OrderRejected) as exc:
        broker.close_position("SPOT")
    assert exc.value.rule_name == "SpotDenyList"
    assert len(fake_client.placed) == 0


def test_token_path_defaults_to_home(monkeypatch, tmp_path):
    from ops.broker.mcp_client import _resolve_token_path
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPS_RH_TOKEN_PATH", raising=False)
    path = _resolve_token_path()
    assert path == tmp_path / ".config" / "tradingagents" / "robinhood_token.json"


def test_token_path_env_override(monkeypatch, tmp_path):
    from ops.broker.mcp_client import _resolve_token_path
    override = tmp_path / "custom.json"
    monkeypatch.setenv("OPS_RH_TOKEN_PATH", str(override))
    assert _resolve_token_path() == override


def test_write_token_creates_dir_with_0600_perms(tmp_path):
    from ops.broker.mcp_client import _write_token
    path = tmp_path / "sub" / "token.json"
    _write_token(path, {"access_token": "xyz", "expires_at": "..."})
    assert path.exists()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


# --- RealRobinhoodMCPClient error mapping -----------------------------------
#
# `_call_tool` bridges to the SDK's async `ClientSession.call_tool`. Any
# exception raised there (network, auth, protocol) must surface as
# MCPUnavailable with the original exception chained via `__cause__`, so
# callers can log/inspect the root cause without catching SDK-specific types.


class _StubSessionThatFails:
    async def call_tool(self, name, arguments):
        raise RuntimeError("mcp died")


def test_get_account_wraps_session_error(tmp_path):
    client = RealRobinhoodMCPClient(token_path=tmp_path / "fake.json")
    client._session = _StubSessionThatFails()
    with pytest.raises(MCPUnavailable) as exc_info:
        client.get_account()
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_get_positions_wraps_session_error(tmp_path):
    client = RealRobinhoodMCPClient(token_path=tmp_path / "fake.json")
    client._session = _StubSessionThatFails()
    with pytest.raises(MCPUnavailable) as exc_info:
        client.get_positions()
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_get_quote_wraps_session_error(tmp_path):
    client = RealRobinhoodMCPClient(token_path=tmp_path / "fake.json")
    client._session = _StubSessionThatFails()
    with pytest.raises(MCPUnavailable) as exc_info:
        client.get_quote("AAPL")
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_place_equity_order_wraps_session_error(tmp_path):
    client = RealRobinhoodMCPClient(token_path=tmp_path / "fake.json")
    client._session = _StubSessionThatFails()
    with pytest.raises(MCPUnavailable) as exc_info:
        client.place_equity_order(
            symbol="AAPL", side=Side.BUY,
            notional=Decimal("50"), quantity=None,
            order_type=OrderType.MARKET, limit_price=None,
            client_order_id="b-1",
        )
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_cancel_equity_order_wraps_session_error(tmp_path):
    client = RealRobinhoodMCPClient(token_path=tmp_path / "fake.json")
    client._session = _StubSessionThatFails()
    with pytest.raises(MCPUnavailable) as exc_info:
        client.cancel_equity_order("order-1")
    assert isinstance(exc_info.value.__cause__, RuntimeError)
