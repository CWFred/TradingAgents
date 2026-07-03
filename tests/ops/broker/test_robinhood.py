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
        stop_pct=Decimal("-0.1"),
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
        stop_pct=Decimal("-0.1"),
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
            stop_pct=Decimal("-0.1"),
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
            stop_pct=Decimal("-0.1"),
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


# --- fill journaling carries the ordered stop (live rehydration fix) --------


def test_place_order_buy_journals_stop_on_fill(fake_client, journal):
    """The BUY fill row must carry the order's stop_loss_price — it is what
    get_positions/last_buy_fill_for rehydrate the stop from after a restart."""
    fake_client.set_quote("AAPL", Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    ))
    fills = journal.read_fills()
    assert len(fills) == 1
    assert fills[0]["stop_loss_price"] == Decimal("9.2")


def test_buy_then_get_positions_rehydrates_stop(fake_client, journal):
    """End-to-end: place a live BUY with a stop, then get_positions() must
    report that stop (not None) — the guardian enforces the ordered stop."""
    fake_client.set_quote("AAPL", Decimal("10"))
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    ))
    positions = broker.get_positions()
    assert positions[0].symbol == "AAPL"
    assert positions[0].stop_loss_price == Decimal("9.2")


# --- ack.status enforcement: only real fills are journaled as fills ---------


class _CannedAckClient:
    """Protocol impl that returns one canned ack from place_equity_order.
    Positions/quotes are seedable so close_position paths work."""

    def __init__(self, ack, *, positions=None, quote=Decimal("10")):
        self._ack = ack
        self._positions = positions or []
        self._quote = quote

    def get_account(self):
        raise AssertionError("not used in these tests")

    def get_positions(self):
        return list(self._positions)

    def get_quote(self, symbol):
        return self._quote

    def place_equity_order(self, **kwargs):
        return self._ack

    def cancel_equity_order(self, order_id):
        pass


def _ack(status, *, quantity=None, fill_price=None):
    from ops.broker.mcp_client import MCPOrderAck
    return MCPOrderAck(
        order_id="rh-1", client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        quantity=quantity, notional=Decimal("50"), status=status,
        fill_price=fill_price,
    )


def _buy_order(client_order_id="b-1"):
    return Order(
        client_order_id=client_order_id, symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )


# --- M2: stop is entry-relative, resolved from the actual fill price -------


def test_stop_computed_from_actual_fill_price(fake_client, journal):
    """The absolute stop journaled on the fill must be derived from the
    real ack.fill_price, not any pre-trade reference price."""
    fake_client.set_quote("AAPL", Decimal("91"))  # gapped down from a stale reference
    broker = RobinhoodBroker(client=fake_client, journal=journal)
    broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    ))
    fills = journal.read_fills()
    expected = Decimal("91") * (Decimal("1") + Decimal("-0.08"))
    assert fills[0]["stop_loss_price"] == expected
    assert expected < Decimal("91")
    positions = broker.get_positions()
    assert positions[0].stop_loss_price == expected


def test_queued_ack_raises_and_journals_no_fill(journal):
    client = _CannedAckClient(_ack("queued"))
    broker = RobinhoodBroker(client=client, journal=journal)
    with pytest.raises(BrokerError):
        broker.place_order(_buy_order())
    assert journal.read_fills() == []
    kinds = [e["kind"] for e in journal.read_events()]
    assert "order_not_filled" in kinds


def test_queued_ack_event_carries_broker_order_id(journal):
    """The order_not_filled event must carry the broker-side order id and
    status so the pending order can be found/cancelled manually."""
    client = _CannedAckClient(_ack("queued"))
    broker = RobinhoodBroker(client=client, journal=journal)
    with pytest.raises(BrokerError):
        broker.place_order(_buy_order())
    ev = [e for e in journal.read_events() if e["kind"] == "order_not_filled"][0]
    assert ev["payload"]["order_id"] == "rh-1"
    assert ev["payload"]["status"] == "queued"


def test_rejected_ack_raises_and_journals_no_fill(journal):
    client = _CannedAckClient(_ack("rejected"))
    broker = RobinhoodBroker(client=client, journal=journal)
    with pytest.raises(BrokerError):
        broker.place_order(_buy_order())
    assert journal.read_fills() == []


def test_filled_ack_without_price_or_qty_raises(journal):
    """A 'filled' ack missing fill_price/quantity must NOT journal a
    qty=0/price=0 fill — that corrupts replayed cash and positions."""
    client = _CannedAckClient(_ack("filled", quantity=Decimal("5"), fill_price=None))
    broker = RobinhoodBroker(client=client, journal=journal)
    with pytest.raises(BrokerError):
        broker.place_order(_buy_order(client_order_id="b-1"))
    assert journal.read_fills() == []

    client = _CannedAckClient(_ack("filled", quantity=None, fill_price=Decimal("10")))
    broker = RobinhoodBroker(client=client, journal=journal)
    with pytest.raises(BrokerError):
        broker.place_order(_buy_order(client_order_id="b-2"))
    assert journal.read_fills() == []


def test_close_position_queued_ack_raises_and_journals_no_fill(journal):
    from ops.broker.mcp_client import MCPPosition
    client = _CannedAckClient(
        _ack("queued"),
        positions=[MCPPosition(symbol="AAPL", quantity=Decimal("5"), avg_price=Decimal("10"))],
    )
    broker = RobinhoodBroker(client=client, journal=journal)
    with pytest.raises(BrokerError):
        broker.close_position("AAPL")
    assert journal.read_fills() == []
    kinds = [e["kind"] for e in journal.read_events()]
    assert "order_not_filled" in kinds
