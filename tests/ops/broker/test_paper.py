from datetime import datetime, timezone
from decimal import Decimal
import pytest
from ops.broker.types import Order, Side, OrderType
from ops.broker.base import InsufficientFunds, NoSuchPosition
from ops.broker.paper import PaperBroker
from ops.journal import Journal


def _broker(tmp_path, prices: dict[str, str], cash: str = "250"):
    j = Journal(str(tmp_path / "j.sqlite"))
    quotes = {k: Decimal(v) for k, v in prices.items()}
    return PaperBroker(journal=j, quote_source=lambda s: quotes[s], starting_cash=Decimal(cash))


class _MutableQuoteSource:
    """Callable quote source whose prices can be updated between orders."""

    def __init__(self) -> None:
        self._prices: dict[str, Decimal] = {}

    def set(self, symbol: str, price: Decimal) -> None:
        self._prices[symbol] = price

    def __call__(self, symbol: str) -> Decimal:
        return self._prices[symbol]


@pytest.fixture
def quote_source():
    return _MutableQuoteSource()


def test_buy_creates_position_and_debits_cash(tmp_path):
    b = _broker(tmp_path, {"AAPL": "200"})
    o = Order(
        client_order_id="c1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    )
    fill = b.place_order(o)
    assert fill.quantity == Decimal("0.125")
    assert fill.price == Decimal("200")
    assert b.get_cash() == Decimal("225")
    positions = b.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].quantity == Decimal("0.125")
    assert positions[0].stop_loss_price == Decimal("184")


def test_buy_with_insufficient_cash_raises(tmp_path):
    b = _broker(tmp_path, {"AAPL": "200"}, cash="10")
    o = Order(
        client_order_id="c1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    )
    with pytest.raises(InsufficientFunds):
        b.place_order(o)


def test_sell_reduces_position_and_credits_cash(tmp_path):
    b = _broker(tmp_path, {"AAPL": "200"})
    b.place_order(Order(
        client_order_id="c1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    # price moves up — replace the quote_source for the next call
    b._quote = lambda s: Decimal("220")  # type: ignore[attr-defined]
    b.place_order(Order(
        client_order_id="c2", symbol="AAPL", side=Side.SELL,
        notional_dollars=Decimal("22"), order_type=OrderType.MARKET,
    ))
    pos = b.get_positions()[0]
    assert pos.quantity == Decimal("0.15")  # 0.25 bought, 0.1 sold
    assert b.get_cash() == Decimal("222")  # 250 - 50 + 22


def test_sell_zero_notional_closes_position(tmp_path):
    b = _broker(tmp_path, {"AAPL": "200"})
    b.place_order(Order(
        client_order_id="c1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    b.close_position("AAPL")
    assert b.get_positions() == []
    assert b.get_cash() == Decimal("250")


def test_sell_without_position_raises(tmp_path):
    b = _broker(tmp_path, {"AAPL": "200"})
    with pytest.raises(NoSuchPosition):
        b.place_order(Order(
            client_order_id="c1", symbol="AAPL", side=Side.SELL,
            notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        ))


def test_equity_reflects_position_value_and_cash(tmp_path):
    b = _broker(tmp_path, {"AAPL": "200"})
    b.place_order(Order(
        client_order_id="c1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    b._quote = lambda s: Decimal("240")  # type: ignore[attr-defined]
    # cash: 200; position value: 0.25 * 240 = 60; total: 260
    assert b.get_equity() == Decimal("260.000")


def test_fills_are_journaled(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    b = PaperBroker(journal=j, quote_source=lambda s: Decimal("200"), starting_cash=Decimal("250"))
    b.place_order(Order(
        client_order_id="c1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    orders = j.read_orders()
    fills = j.read_fills()
    assert len(orders) == 1 and len(fills) == 1
    assert orders[0]["client_order_id"] == "c1"
    assert fills[0]["quantity"] == Decimal("0.125")


def test_close_position_sells_full_qty(tmp_path):
    b = _broker(tmp_path, {"AAPL": "10"}, cash="100")
    b.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
    ))
    fill = b.close_position("AAPL")
    assert fill.side == Side.SELL
    assert fill.quantity == Decimal("5")
    assert b.get_positions() == []
    assert b.get_cash() == Decimal("100")


def test_close_position_missing_symbol_raises(tmp_path):
    b = _broker(tmp_path, {"AAPL": "10"}, cash="100")
    with pytest.raises(NoSuchPosition):
        b.close_position("NVDA")


def test_close_position_records_fill_to_journal(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    b = PaperBroker(journal=j, quote_source=lambda s: Decimal("10"), starting_cash=Decimal("100"))
    b.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
    ))
    b.close_position("AAPL")
    fills = j.read_fills()
    close_fills = [f for f in fills if f["client_order_id"].startswith("close-AAPL-")]
    assert len(close_fills) == 1
    assert close_fills[0]["side"] == "SELL"


def test_from_journal_empty_journal_yields_starting_state(tmp_path, quote_source):
    journal = Journal(str(tmp_path / "j.sqlite"))
    broker = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("500"),
    )
    assert broker.get_cash() == Decimal("500")
    assert broker.get_positions() == []


def test_from_journal_replays_buy(tmp_path, quote_source):
    journal = Journal(str(tmp_path / "j.sqlite"))
    quote_source.set("AAPL", Decimal("10"))
    seed = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("500"))
    seed.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
    ))
    replayed = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("500"),
    )
    assert replayed.get_cash() == Decimal("400")
    assert len(replayed.get_positions()) == 1
    assert replayed.get_positions()[0].quantity == Decimal("10")


def test_from_journal_replays_buy_then_close(tmp_path, quote_source):
    journal = Journal(str(tmp_path / "j.sqlite"))
    quote_source.set("AAPL", Decimal("10"))
    seed = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("500"))
    seed.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
    ))
    quote_source.set("AAPL", Decimal("11"))
    seed.close_position("AAPL")
    replayed = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("500"),
    )
    assert replayed.get_positions() == []
    assert replayed.get_cash() == Decimal("510")   # 500 - 100 + 110


def test_from_journal_replays_multiple_buys_same_symbol_averages_entry(tmp_path, quote_source):
    journal = Journal(str(tmp_path / "j.sqlite"))
    quote_source.set("AAPL", Decimal("10"))
    seed = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("1000"))
    seed.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
    ))
    quote_source.set("AAPL", Decimal("20"))
    seed.place_order(Order(
        client_order_id="b-2", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("200"), order_type=OrderType.MARKET,
    ))
    replayed = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("1000"),
    )
    pos = replayed.get_positions()[0]
    # 10 shares @ 10 + 10 shares @ 20 = 20 shares avg 15
    assert pos.quantity == Decimal("20")
    assert pos.avg_entry_price == Decimal("15")


def test_from_journal_replay_matches_live_cash_exactly_with_fractional_notional(tmp_path, quote_source):
    """BUY $25 @ $7/share makes qty = 25/7, a repeating decimal. Reconstructing
    cost as qty*price on replay (instead of reading the journaled order's
    notional_dollars) reintroduces the Decimal rounding error from that
    division, so live and replayed cash silently diverge by ~1e-27. Using the
    stored notional_dollars keeps them exactly equal."""
    journal = Journal(str(tmp_path / "j.sqlite"))
    quote_source.set("AAPL", Decimal("7"))
    seed = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("100"))
    seed.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
    ))
    seed.close_position("AAPL")
    live_cash = seed.get_cash()

    replayed = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("100"),
    )
    assert replayed.get_cash() == live_cash


def test_from_journal_emits_positions_recovered_without_stops_event(tmp_path, quote_source):
    journal = Journal(str(tmp_path / "j.sqlite"))
    quote_source.set("AAPL", Decimal("10"))
    seed = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("500"))
    seed.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9"),
    ))
    PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("500"),
    )
    warnings = [e for e in journal.read_events() if e["kind"] == "positions_recovered_without_stops"]
    assert len(warnings) == 1
    assert warnings[0]["payload"]["symbols"] == ["AAPL"]
    assert warnings[0]["payload"]["count"] == 1


def test_from_journal_falls_back_to_qty_times_price_when_order_row_missing(tmp_path, quote_source):
    """Defensive path: a fill with no matching order row (shouldn't happen after
    I1, but journals can predate it) reconstructs cost from qty*price and
    journals a journal_replay_fallback event instead of raising."""
    journal = Journal(str(tmp_path / "j.sqlite"))
    journal.record_fill(
        order_id="orphan-order", client_order_id="orphan-cid", symbol="AAPL",
        side="BUY", quantity=Decimal("5"), price=Decimal("10"),
        filled_at=datetime.now(timezone.utc),
    )
    replayed = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("500"),
    )
    assert replayed.get_cash() == Decimal("450")  # 500 - 5*10
    fallbacks = [e for e in journal.read_events() if e["kind"] == "journal_replay_fallback"]
    assert len(fallbacks) == 1
    assert fallbacks[0]["payload"]["client_order_id"] == "orphan-cid"
