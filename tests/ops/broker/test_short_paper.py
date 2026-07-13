from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ops.broker.base import NoSuchPosition
from ops.broker.short_paper import ShortPaperBroker
from ops.broker.types import Order, OrderType, Side
from ops.journal import Journal


@pytest.fixture
def journal(tmp_path):
    with Journal(str(tmp_path / "short.sqlite")) as j:
        yield j


def _broker(journal, prices: dict[str, Decimal], cash="10000"):
    return ShortPaperBroker(
        journal=journal, quote_source=lambda s: prices[s],
        starting_cash=Decimal(cash),
    )


def _short(symbol="XYZ", notional="400"):
    from uuid import uuid4

    return Order(client_order_id=f"s-{symbol}-{uuid4().hex[:8]}", symbol=symbol,
                 side=Side.SHORT, notional_dollars=Decimal(notional),
                 order_type=OrderType.MARKET)


def test_short_credits_proceeds_and_opens_position(journal):
    b = _broker(journal, {"XYZ": Decimal("10")})
    fill = b.place_order(_short())
    assert fill.side is Side.SHORT and fill.quantity == Decimal("40")
    assert b.get_cash() == Decimal("10400")
    (pos,) = b.get_positions()
    assert pos.quantity == Decimal("40") and pos.avg_entry_price == Decimal("10")


def test_equity_is_cash_minus_liability(journal):
    prices = {"XYZ": Decimal("10")}
    b = _broker(journal, prices)
    b.place_order(_short())
    assert b.get_equity() == Decimal("10000")   # unchanged at entry
    prices["XYZ"] = Decimal("9")                # -10%: short profits
    assert b.get_equity() == Decimal("10040")
    prices["XYZ"] = Decimal("12")               # +20%: short loses
    assert b.get_equity() == Decimal("9920")


def test_close_position_covers_all_at_market(journal):
    prices = {"XYZ": Decimal("10")}
    b = _broker(journal, prices)
    b.place_order(_short())
    prices["XYZ"] = Decimal("8")
    fill = b.close_position("XYZ")
    assert fill.side is Side.COVER
    assert b.get_positions() == []
    assert b.get_cash() == Decimal("10400") - Decimal("320")  # 40 * 8


def test_close_position_allows_negative_cash(journal):
    # A blown-up short must still be coverable — forced cover, damage shows in equity.
    prices = {"XYZ": Decimal("10")}
    b = _broker(journal, prices, cash="150")
    b.place_order(_short(notional="400"))
    prices["XYZ"] = Decimal("20")
    b.close_position("XYZ")
    assert b.get_cash() == Decimal("550") - Decimal("800")


def test_rejects_buy_and_sell_sides(journal):
    b = _broker(journal, {"XYZ": Decimal("10")})
    for side in (Side.BUY, Side.SELL):
        with pytest.raises(ValueError, match="SHORT/COVER"):
            b.place_order(Order(client_order_id="x", symbol="XYZ", side=side,
                                notional_dollars=Decimal("100"),
                                order_type=OrderType.MARKET))


def test_cover_unknown_symbol_raises(journal):
    b = _broker(journal, {"XYZ": Decimal("10")})
    with pytest.raises(NoSuchPosition):
        b.close_position("XYZ")


def test_short_adds_average_entry(journal):
    prices = {"XYZ": Decimal("10")}
    b = _broker(journal, prices)
    b.place_order(_short(notional="400"))
    prices["XYZ"] = Decimal("20")
    b.place_order(_short(notional="400"))   # 20 more shares at 20
    (pos,) = b.get_positions()
    assert pos.quantity == Decimal("60")
    assert pos.avg_entry_price == Decimal("40") * 10 / 60 + Decimal("20") * 20 / 60


def test_partial_cover_reduces_position(journal):
    prices = {"XYZ": Decimal("10")}
    b = _broker(journal, prices)
    b.place_order(_short(notional="400"))
    cover = Order(client_order_id="c-1", symbol="XYZ", side=Side.COVER,
                  notional_dollars=Decimal("100"), order_type=OrderType.MARKET)
    fill = b.place_order(cover)
    assert fill.side is Side.COVER and fill.quantity == Decimal("10")
    (pos,) = b.get_positions()
    assert pos.quantity == Decimal("30")
    assert b.get_cash() == Decimal("10300")


def test_cover_exceeding_position_raises(journal):
    prices = {"XYZ": Decimal("10")}
    b = _broker(journal, prices)
    b.place_order(_short(notional="400"))
    cover = Order(client_order_id="c-2", symbol="XYZ", side=Side.COVER,
                  notional_dollars=Decimal("500"), order_type=OrderType.MARKET)
    with pytest.raises(NoSuchPosition):
        b.place_order(cover)


def test_from_journal_rebuilds_shorts(journal):
    prices = {"XYZ": Decimal("10"), "ABC": Decimal("5")}
    b = _broker(journal, prices)
    b.place_order(_short("XYZ", "400"))
    b.place_order(_short("ABC", "200"))
    prices["ABC"] = Decimal("4")
    b.close_position("ABC")
    replayed = ShortPaperBroker.from_journal(
        journal=journal, quote_source=lambda s: prices[s],
        starting_cash=Decimal("10000"),
    )
    assert replayed.get_cash() == b.get_cash()
    assert {p.symbol for p in replayed.get_positions()} == {"XYZ"}
    (pos,) = replayed.get_positions()
    assert pos.quantity == Decimal("40") and pos.avg_entry_price == Decimal("10")


def test_replay_orphan_cover_is_journaled_and_skipped(journal):
    journal.record_order(client_order_id="c1", symbol="GHO", side="COVER",
                         notional_dollars=Decimal("100"), stop_loss_price=None)
    journal.record_fill(order_id="f1", client_order_id="c1", symbol="GHO",
                        side="COVER", quantity=Decimal("10"), price=Decimal("10"),
                        filled_at=datetime.now(timezone.utc))
    replayed = ShortPaperBroker.from_journal(
        journal=journal, quote_source=lambda s: Decimal("10"),
        starting_cash=Decimal("10000"),
    )
    assert replayed.get_cash() == Decimal("10000")
    assert replayed.get_positions() == []
    from ops import events
    assert journal.count_events(events.KIND_JOURNAL_REPLAY_ORPHAN_COVER) == 1
