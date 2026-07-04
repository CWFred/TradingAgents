"""Order placement mapping + fill-polling lifecycle tests for
`RealRobinhoodMCPClient`, driven against the REAL response shapes captured
live 2026-07-04 (see docs/superpowers/specs/2026-07-04-tradingagents-mcp-live-design.md
§Design item 5 "Order lifecycle").

NO test in this file makes a real network call, places a real order, or
talks to the live Robinhood endpoint. `_call_tool` is intercepted by a fake
`ClientSession` (mimicking `mcp.types.CallToolResult`'s `.isError`/
`.structuredContent` shape) driven by scripted, hand-authored response
dicts. Polling never sleeps for real: `_await_fill` tests inject a no-op
`sleep_fn` and a deterministic `clock_fn`.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from ops.broker.mcp_client import (
    MCPProtocolError,
    MCPUnavailable,
    RealRobinhoodMCPClient,
)
from ops.broker.types import OrderType, Side

ACCOUNT_NUMBER = "502163744"
ORDER_ID = "6a444547-1111-4a11-8a11-111111111111"

# --- fixtures (real order shapes, verbatim per task's captured examples) ----

FILLED_ORDER = {
    "id": ORDER_ID,
    "symbol": "MU",
    "side": "buy",
    "type": "market",
    "state": "filled",
    "quantity": "0.138888",
    "cumulative_quantity": "0.138888",
    "average_price": "1080.000000",
    "fees": "0.000000",
    "dollar_based_amount": {"amount": "150.000000", "currency_code": "USD"},
    "placed_agent": "user",
    "last_transaction_at": "2026-07-04T12:00:00Z",
    "executions": [{"price": "1080.000000", "quantity": "0.138888"}],
}

QUEUED_ORDER = {
    "id": ORDER_ID, "symbol": "MU", "side": "buy", "type": "market",
    "state": "queued", "quantity": None, "cumulative_quantity": None,
    "average_price": None,
}

PARTIAL_ORDER = {
    "id": ORDER_ID, "symbol": "MU", "side": "buy", "type": "market",
    "state": "partially_filled", "quantity": None,
    "cumulative_quantity": "0.05", "average_price": "1075.000000",
}

REJECTED_ORDER = {
    "id": ORDER_ID, "symbol": "MU", "side": "buy", "type": "market",
    "state": "rejected", "quantity": None, "cumulative_quantity": None,
    "average_price": None,
}

FILLED_ORDER_MISSING_AVG_PRICE = {
    "id": ORDER_ID, "symbol": "MU", "side": "buy", "type": "market",
    "state": "filled", "quantity": "0.138888", "cumulative_quantity": "0.138888",
    # no "average_price" at all — shape mismatch
}

FILLED_ORDER_NULL_CUMULATIVE_QTY = {
    "id": ORDER_ID, "symbol": "MU", "side": "sell", "type": "market",
    "state": "filled", "quantity": "10", "cumulative_quantity": None,
    # present-but-null cumulative_quantity on a filled, share-count SELL —
    # captured shape variant; must fall back to "quantity".
    "average_price": "1080.000000",
}

FILLED_ORDER_MISSING_QTY_AND_CUMULATIVE = {
    "id": ORDER_ID, "symbol": "MU", "side": "buy", "type": "market",
    "state": "filled", "quantity": None, "cumulative_quantity": None,
    "average_price": "1080.000000",
}


def _orders_response(order: dict) -> dict:
    return {"data": {"orders": [order]}, "guide": "ignored prose"}


def _place_response(order_id: str, state: str) -> dict:
    return {"data": {"id": order_id, "state": state}, "guide": "ignored prose"}


# --- fake session (mimics mcp.types.CallToolResult), scripted per tool -----


class _FakeResult:
    def __init__(self, structured_content, is_error=False):
        self.isError = is_error
        self.structuredContent = structured_content


class _ScriptedSession:
    """Routes call_tool(name, arguments) to canned responses per tool name.

    `responses[name]` may be a single dict (returned on every call) or a
    list of dicts (returned in call order; the last is repeated once the
    list is exhausted) — used to script a state transition across
    successive polls of the same tool.
    """

    def __init__(self, responses: dict[str, dict | list[dict]]):
        self._responses = responses
        self._call_counts: dict[str, int] = {}
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name not in self._responses:
            raise AssertionError(f"unexpected tool call: {name}({arguments!r})")
        entry = self._responses[name]
        if isinstance(entry, list):
            idx = self._call_counts.get(name, 0)
            self._call_counts[name] = idx + 1
            payload = entry[min(idx, len(entry) - 1)]
        else:
            payload = entry
        return _FakeResult(payload)


def _client_with(**responses) -> tuple[RealRobinhoodMCPClient, _ScriptedSession]:
    client = RealRobinhoodMCPClient()
    session = _ScriptedSession(responses)
    client._session = session
    client._account_number = ACCOUNT_NUMBER  # bypass get_accounts; covered in test_mcp_reads.py
    return client, session


def _calls_for(session: _ScriptedSession, name: str) -> list[dict]:
    return [args for (n, args) in session.calls if n == name]


# --- place_equity_order mapping ---------------------------------------------


def test_place_equity_order_notional_buy_maps_dollar_amount_and_forces_market():
    client, session = _client_with(
        place_equity_order=_place_response(ORDER_ID, "queued"),
        get_equity_orders=_orders_response(FILLED_ORDER),
    )
    ack = client.place_equity_order(
        symbol="MU", side=Side.BUY,
        notional=Decimal("150"), quantity=None,
        order_type=OrderType.MARKET, limit_price=None,
        client_order_id="pem-2026-07-04-ab12cd34",
    )
    place_calls = _calls_for(session, "place_equity_order")
    assert len(place_calls) == 1
    params = place_calls[0]
    assert params["account_number"] == ACCOUNT_NUMBER
    assert params["symbol"] == "MU"
    assert params["side"] == "buy"
    assert params["dollar_amount"] == "150"
    assert params["type"] == "market"
    assert params["time_in_force"] == "gfd"
    assert params["market_hours"] == "regular_hours"
    assert "quantity" not in params
    # ref_id must be a syntactically valid UUID (client_order_id itself is not one)
    uuid.UUID(params["ref_id"])

    assert ack.status == "filled"
    assert ack.order_id == ORDER_ID
    assert ack.client_order_id == "pem-2026-07-04-ab12cd34"
    assert ack.symbol == "MU"
    assert ack.side == Side.BUY
    assert ack.quantity == Decimal("0.138888")
    assert ack.fill_price == Decimal("1080.000000")
    assert ack.notional == Decimal("150.000000")


def test_place_equity_order_quantity_sell_maps_quantity_and_order_type():
    client, session = _client_with(
        place_equity_order=_place_response(ORDER_ID, "queued"),
        get_equity_orders=_orders_response(FILLED_ORDER),
    )
    client.place_equity_order(
        symbol="MU", side=Side.SELL,
        notional=None, quantity=Decimal("0.138888"),
        order_type=OrderType.MARKET, limit_price=None,
        client_order_id="close-MU-deadbeef",
    )
    params = _calls_for(session, "place_equity_order")[0]
    assert params["quantity"] == "0.138888"
    assert params["side"] == "sell"
    assert params["type"] == "market"
    assert "dollar_amount" not in params


def test_place_equity_order_limit_maps_limit_price_and_type():
    client, session = _client_with(
        place_equity_order=_place_response(ORDER_ID, "queued"),
        get_equity_orders=_orders_response(FILLED_ORDER),
    )
    client.place_equity_order(
        symbol="MU", side=Side.SELL,
        notional=None, quantity=Decimal("1"),
        order_type=OrderType.LIMIT, limit_price=Decimal("1090.50"),
        client_order_id="close-MU-limit",
    )
    params = _calls_for(session, "place_equity_order")[0]
    assert params["type"] == "limit"
    assert params["limit_price"] == "1090.50"


def test_place_equity_order_ref_id_stable_for_same_client_order_id():
    client, session = _client_with(
        place_equity_order=_place_response(ORDER_ID, "queued"),
        get_equity_orders=_orders_response(FILLED_ORDER),
    )
    for _ in range(2):
        client.place_equity_order(
            symbol="MU", side=Side.BUY,
            notional=Decimal("150"), quantity=None,
            order_type=OrderType.MARKET, limit_price=None,
            client_order_id="pem-same-id",
        )
    client.place_equity_order(
        symbol="MU", side=Side.BUY,
        notional=Decimal("150"), quantity=None,
        order_type=OrderType.MARKET, limit_price=None,
        client_order_id="pem-different-id",
    )
    ref_ids = [p["ref_id"] for p in _calls_for(session, "place_equity_order")]
    assert ref_ids[0] == ref_ids[1]
    assert ref_ids[2] != ref_ids[0]


def test_place_equity_order_requires_exactly_one_of_notional_or_quantity():
    client, _ = _client_with()
    with pytest.raises(ValueError):
        client.place_equity_order(
            symbol="MU", side=Side.BUY, notional=None, quantity=None,
            order_type=OrderType.MARKET, limit_price=None, client_order_id="x",
        )
    with pytest.raises(ValueError):
        client.place_equity_order(
            symbol="MU", side=Side.BUY,
            notional=Decimal("1"), quantity=Decimal("1"),
            order_type=OrderType.MARKET, limit_price=None, client_order_id="x",
        )


def test_place_equity_order_notional_with_limit_raises_value_error():
    """Dollar-notional orders are always market on the real dollar_amount
    API (fractional shares are market-only) — a caller that ever wires
    LIMIT+notional through must fail loud, not silently drop price
    protection by forcing market underneath it (Finding 3, final review)."""
    client, session = _client_with()
    with pytest.raises(ValueError, match="dollar-notional orders must be market"):
        client.place_equity_order(
            symbol="MU", side=Side.BUY,
            notional=Decimal("150"), quantity=None,
            order_type=OrderType.LIMIT, limit_price=Decimal("100"),
            client_order_id="pem-bad-notional-limit",
        )
    # Never reached the network — no tool call was made.
    assert session.calls == []


def test_place_equity_order_rejected_on_placement_raises_unavailable_without_polling():
    client, session = _client_with(
        place_equity_order=_place_response(ORDER_ID, "rejected"),
    )
    with pytest.raises(MCPUnavailable):
        client.place_equity_order(
            symbol="MU", side=Side.BUY,
            notional=Decimal("150"), quantity=None,
            order_type=OrderType.MARKET, limit_price=None,
            client_order_id="pem-1",
        )
    assert _calls_for(session, "get_equity_orders") == []


def test_place_equity_order_filled_missing_average_price_raises_protocol_error():
    client, _ = _client_with(
        place_equity_order=_place_response(ORDER_ID, "queued"),
        get_equity_orders=_orders_response(FILLED_ORDER_MISSING_AVG_PRICE),
    )
    with pytest.raises(MCPProtocolError):
        client.place_equity_order(
            symbol="MU", side=Side.BUY,
            notional=Decimal("150"), quantity=None,
            order_type=OrderType.MARKET, limit_price=None,
            client_order_id="pem-1",
        )


def test_place_equity_order_filled_null_cumulative_quantity_falls_back_to_quantity():
    """Present-but-null `cumulative_quantity` (real captured shape on some
    filled share-count orders) must fall back to `quantity`, not raise —
    `dict.get(key, default)` only falls back when the key is ABSENT, so a
    present `None` needs falsy-fallback (`or`) semantics instead."""
    client, _ = _client_with(
        place_equity_order=_place_response(ORDER_ID, "queued"),
        get_equity_orders=_orders_response(FILLED_ORDER_NULL_CUMULATIVE_QTY),
    )
    ack = client.place_equity_order(
        symbol="MU", side=Side.SELL,
        notional=None, quantity=Decimal("10"),
        order_type=OrderType.MARKET, limit_price=None,
        client_order_id="close-MU-null-cumqty",
    )
    assert ack.status == "filled"
    assert ack.quantity == Decimal("10")
    assert ack.fill_price == Decimal("1080.000000")


def test_place_equity_order_filled_missing_quantity_and_cumulative_quantity_raises_protocol_error():
    client, _ = _client_with(
        place_equity_order=_place_response(ORDER_ID, "queued"),
        get_equity_orders=_orders_response(FILLED_ORDER_MISSING_QTY_AND_CUMULATIVE),
    )
    with pytest.raises(MCPProtocolError):
        client.place_equity_order(
            symbol="MU", side=Side.BUY,
            notional=Decimal("150"), quantity=None,
            order_type=OrderType.MARKET, limit_price=None,
            client_order_id="pem-1",
        )


# --- _await_fill state machine (fast, injected sleep/clock) -----------------


def _fast_clock_never_times_out():
    return 0.0


def _no_sleep(_seconds):
    return None


def test_await_fill_queued_then_filled():
    client, session = _client_with(
        get_equity_orders=[_orders_response(QUEUED_ORDER), _orders_response(FILLED_ORDER)],
    )
    order = client._await_fill(
        ORDER_ID, account_number=ACCOUNT_NUMBER,
        sleep_fn=_no_sleep, clock_fn=_fast_clock_never_times_out,
    )
    assert order["state"] == "filled"
    assert order["cumulative_quantity"] == "0.138888"
    assert order["average_price"] == "1080.000000"
    assert len(_calls_for(session, "get_equity_orders")) == 2


def test_await_fill_partially_filled_then_filled():
    client, session = _client_with(
        get_equity_orders=[_orders_response(PARTIAL_ORDER), _orders_response(FILLED_ORDER)],
    )
    order = client._await_fill(
        ORDER_ID, account_number=ACCOUNT_NUMBER,
        sleep_fn=_no_sleep, clock_fn=_fast_clock_never_times_out,
    )
    assert order["state"] == "filled"
    assert len(_calls_for(session, "get_equity_orders")) == 2


def test_await_fill_rejected_raises_unavailable_with_no_infinite_loop():
    client, session = _client_with(
        get_equity_orders=_orders_response(REJECTED_ORDER),
    )
    with pytest.raises(MCPUnavailable):
        client._await_fill(
            ORDER_ID, account_number=ACCOUNT_NUMBER,
            sleep_fn=_no_sleep, clock_fn=_fast_clock_never_times_out,
        )
    assert len(_calls_for(session, "get_equity_orders")) == 1


class _FakeClock:
    """Advances by `step` seconds on every call — deterministic timeout."""

    def __init__(self, step: float):
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        self._t += self._step
        return self._t


def test_await_fill_timeout_cancels_and_raises():
    client, session = _client_with(
        get_equity_orders=_orders_response(QUEUED_ORDER),  # never terminal
        cancel_equity_order={"data": {}, "guide": "ignored"},
    )
    clock = _FakeClock(step=6.0)  # deadline = 0+6+10=16; polls at t=12 (continue), t=18 (stop)
    sleeps: list[float] = []
    with pytest.raises(MCPUnavailable, match="did not fill"):
        client._await_fill(
            ORDER_ID, account_number=ACCOUNT_NUMBER,
            window_s=10.0, poll_interval_s=1.0,
            sleep_fn=sleeps.append, clock_fn=clock,
        )
    assert len(_calls_for(session, "get_equity_orders")) == 2
    assert sleeps == [1.0]
    cancel_calls = _calls_for(session, "cancel_equity_order")
    assert len(cancel_calls) == 1
    assert cancel_calls[0]["order_id"] == ORDER_ID
    assert cancel_calls[0]["account_number"] == ACCOUNT_NUMBER


def test_await_fill_timeout_survives_cancel_protocol_error():
    """A failing best-effort cancel must never mask the timeout signal —
    not just an MCPUnavailable cancel failure (already covered above), but
    also an MCPProtocolError one (e.g. a shape mismatch in the cancel
    response) (Finding 2, final review)."""
    client, session = _client_with(
        get_equity_orders=_orders_response(QUEUED_ORDER),  # never terminal
    )
    cancel_calls: list[str] = []

    def _cancel_raises_protocol_error(order_id):
        cancel_calls.append(order_id)
        raise MCPProtocolError("cancel_equity_order response missing 'data'")

    client.cancel_equity_order = _cancel_raises_protocol_error
    clock = _FakeClock(step=6.0)  # deadline = 0+6+10=16; polls at t=12 (continue), t=18 (stop)
    with pytest.raises(MCPUnavailable, match="did not fill"):
        client._await_fill(
            ORDER_ID, account_number=ACCOUNT_NUMBER,
            window_s=10.0, poll_interval_s=1.0,
            sleep_fn=lambda _s: None, clock_fn=clock,
        )
    assert len(_calls_for(session, "get_equity_orders")) == 2
    assert cancel_calls == [ORDER_ID]
