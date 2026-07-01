import re
from decimal import Decimal
import pytest
from ops.broker.base import OrderRejected
from ops.broker.guarded import GuardedBroker
from ops.broker.paper import PaperBroker
from ops.broker.types import Order, Side, OrderType, Position
from ops.config import OpsConfig
from ops.guardrails.base import Rule, RuleContext, RuleResult
from ops.guardrails.engine import RuleEngine
from ops.guardrails.static_rules import DenyListRule
from ops.journal import Journal


class _RejectSymbol(Rule):
    def __init__(self, symbol): self._sym = symbol
    @property
    def name(self): return "RejectSymbol"
    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.symbol == self._sym:
            return RuleResult.reject(f"reject {self._sym}")
        return RuleResult.allow()


def _stack(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    paper = PaperBroker(journal=j, quote_source=lambda s: Decimal("200"),
                       starting_cash=Decimal("250"))
    engine = RuleEngine([_RejectSymbol("BANNED")])
    return j, paper, GuardedBroker(inner=paper, engine=engine, journal=j, config=OpsConfig())


def test_guarded_allows_passing_order(tmp_path):
    j, paper, guarded = _stack(tmp_path)
    fill = guarded.place_order(Order(
        client_order_id="c1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    assert fill.symbol == "AAPL"
    assert paper.get_positions()[0].symbol == "AAPL"


def test_guarded_rejects_and_journals_rejection(tmp_path):
    j, paper, guarded = _stack(tmp_path)
    with pytest.raises(OrderRejected) as exc:
        guarded.place_order(Order(
            client_order_id="c1", symbol="BANNED", side=Side.BUY,
            notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
            stop_loss_price=Decimal("184"),
        ))
    assert exc.value.rule_name == "RejectSymbol"
    # Inner broker was not touched
    assert paper.get_positions() == []
    assert paper.get_cash() == Decimal("250")
    # The rejection event is in the journal
    events = j.read_events()
    rejections = [e for e in events if e["kind"] == "order_rejected"]
    assert len(rejections) == 1
    assert rejections[0]["payload"]["rule"] == "RejectSymbol"
    assert rejections[0]["payload"]["symbol"] == "BANNED"


def test_guarded_passes_through_read_methods(tmp_path):
    _, paper, guarded = _stack(tmp_path)
    assert guarded.get_cash() == paper.get_cash()
    assert guarded.get_equity() == paper.get_equity()
    assert guarded.get_quote("AAPL") == paper.get_quote("AAPL")


def test_guarded_exposes_journal(tmp_path):
    j, paper, guarded = _stack(tmp_path)
    assert guarded.journal is j


class _SettableQuoteSource:
    """A quote source you can update mid-test, unlike a fixed lambda."""

    def __init__(self):
        self._quotes: dict[str, Decimal] = {}

    def set(self, symbol: str, price: Decimal) -> None:
        self._quotes[symbol] = price

    def __call__(self, symbol: str) -> Decimal:
        return self._quotes[symbol]


@pytest.fixture
def quote_source():
    return _SettableQuoteSource()


@pytest.fixture
def journal(tmp_path):
    return Journal(str(tmp_path / "j.sqlite"))


@pytest.fixture
def inner(journal, quote_source):
    return PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("250"))


@pytest.fixture
def guarded(inner, journal):
    engine = RuleEngine([_RejectSymbol("BANNED")])
    return GuardedBroker(inner=inner, engine=engine, journal=journal, config=OpsConfig())


def test_guarded_close_position_delegates_to_inner(guarded, inner, quote_source):
    quote_source.set("AAPL", Decimal("10"))
    guarded.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9"),
    ))
    fill = guarded.close_position("AAPL")
    assert fill.side == Side.SELL
    assert guarded.get_positions() == []


def test_guarded_close_position_denylist_still_blocks(journal, quote_source):
    """If SPOT somehow ends up in the paper book (e.g. via test seed), close_position
    still runs the rule chain — DenyListRule blocks it, OrderRejected raised."""
    quote_source.set("SPOT", Decimal("400"))
    inner = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("250"))
    # Seed the SPOT position directly on the inner broker — DenyListRule would
    # have blocked a normal guarded BUY, so this simulates a pre-existing
    # position (e.g. carried over before the denylist existed).
    inner._positions["SPOT"] = Position(
        symbol="SPOT", quantity=Decimal("1"), avg_entry_price=Decimal("400"),
    )
    engine = RuleEngine([DenyListRule()])
    guarded_denylist_spot = GuardedBroker(inner=inner, engine=engine, journal=journal, config=OpsConfig())

    with pytest.raises(OrderRejected) as exc:
        guarded_denylist_spot.close_position("SPOT")
    assert exc.value.rule_name == "DenyListRule"
    # Inner position untouched — the rejection happened before delegation.
    assert inner.get_positions()[0].symbol == "SPOT"
    assert inner.get_positions()[0].quantity == Decimal("1")


def test_guarded_close_position_races_with_concurrent_buy(guarded, inner, quote_source):
    """A BUY on the same symbol arriving during a close_position must serialise:
    if close runs first the position is empty and BUY re-opens; if BUY runs first
    the close sells the new bigger qty. No mid-close top-up."""
    import threading
    quote_source.set("AAPL", Decimal("10"))
    guarded.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9"),
    ))
    barrier = threading.Barrier(2)
    close_result = {}
    buy_result = {}

    def do_close():
        barrier.wait()
        try:
            close_result["fill"] = guarded.close_position("AAPL")
        except Exception as e:
            close_result["exc"] = e

    def do_buy():
        barrier.wait()
        try:
            buy_result["fill"] = guarded.place_order(Order(
                client_order_id="b-2", symbol="AAPL", side=Side.BUY,
                notional_dollars=Decimal("20"), order_type=OrderType.MARKET,
                stop_loss_price=Decimal("9"),
            ))
        except Exception as e:
            buy_result["exc"] = e

    t1 = threading.Thread(target=do_close)
    t2 = threading.Thread(target=do_buy)
    t1.start(); t2.start()
    t1.join(); t2.join()

    positions = guarded.get_positions()
    # Both operations must succeed — the lock serialises them, it never
    # rejects either. Exactly one of two valid interleavings happened, and
    # neither leaves a partial/torn state.
    assert "fill" in close_result, close_result.get("exc")
    assert "fill" in buy_result, buy_result.get("exc")
    assert close_result["fill"].side == Side.SELL

    if close_result["fill"].quantity == Decimal("5"):
        # close ran first (sold the original 5 shares), BUY ran second and
        # re-opened a fresh 2-share position.
        assert len(positions) == 1
        assert positions[0].quantity == Decimal("2")
    else:
        # BUY ran first (position became 7 shares), close ran second and
        # sold the full top-ped-up quantity — nothing left over.
        assert close_result["fill"].quantity == Decimal("7")
        assert positions == []


_CLOSE_ID_RE = re.compile(r"^close-[A-Za-z]+-[0-9a-f]{8}$")


def test_guarded_close_position_client_order_id_continuity_on_rejection(journal, quote_source):
    """GuardedBroker mints the close's client_order_id once. On rejection, the id
    journaled in order_rejected must be in the guarded layer's own
    close-{symbol}-{hex8} format (it is minted before the rule chain runs, so a
    rejection never reaches the inner broker to mint a different one)."""
    quote_source.set("SPOT", Decimal("400"))
    inner = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("250"))
    inner._positions["SPOT"] = Position(
        symbol="SPOT", quantity=Decimal("1"), avg_entry_price=Decimal("400"),
    )
    from ops.guardrails.static_rules import DenyListRule
    engine = RuleEngine([DenyListRule()])
    guarded_denylist = GuardedBroker(inner=inner, engine=engine, journal=journal, config=OpsConfig())

    with pytest.raises(OrderRejected):
        guarded_denylist.close_position("SPOT")

    events = journal.read_events()
    rejections = [e for e in events if e["kind"] == "order_rejected"]
    assert len(rejections) == 1
    rejected_id = rejections[0]["payload"]["client_order_id"]
    assert _CLOSE_ID_RE.match(rejected_id), rejected_id


def test_guarded_close_position_client_order_id_continuity_on_success(guarded, inner, quote_source, journal):
    """On success, the id minted by GuardedBroker is passed through to the inner
    broker so it appears — unchanged — on both the journaled order row and the
    fill, matching the guarded layer's close-{symbol}-{hex8} format."""
    quote_source.set("AAPL", Decimal("10"))
    guarded.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9"),
    ))
    fill = guarded.close_position("AAPL")

    assert _CLOSE_ID_RE.match(fill.client_order_id), fill.client_order_id
    orders = journal.read_orders()
    close_orders = [o for o in orders if o["client_order_id"] == fill.client_order_id]
    assert len(close_orders) == 1
    assert close_orders[0]["side"] == "SELL"
