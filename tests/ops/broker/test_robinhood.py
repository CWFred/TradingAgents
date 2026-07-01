from decimal import Decimal

import pytest

from ops.broker.base import BrokerError, NoSuchPosition, OrderRejected
from ops.broker.mcp_client import MCPUnavailable, RobinhoodMCPClient
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
