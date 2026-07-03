from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ops.broker.base import InsufficientFunds, NoSuchPosition
from ops.broker.paper import PaperBroker
from ops.broker.types import Order, OrderType, Side
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
        stop_pct=Decimal("-0.08"),
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
        stop_pct=Decimal("-0.08"),
    )
    with pytest.raises(InsufficientFunds):
        b.place_order(o)


def test_sell_reduces_position_and_credits_cash(tmp_path):
    b = _broker(tmp_path, {"AAPL": "200"})
    b.place_order(Order(
        client_order_id="c1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
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
        stop_pct=Decimal("-0.08"),
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
        stop_pct=Decimal("-0.08"),
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
        stop_pct=Decimal("-0.08"),
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


def test_from_journal_leaves_stop_none_for_legacy_fills(tmp_path, quote_source):
    """A position whose BUY fill carries no journaled stop (legacy data,
    predating Task 2's stop_loss_price persistence) is rehydrated with
    stop_loss_price=None. The reconciler is now the sole emitter of the
    positions_recovered_without_stops event, so from_journal itself no
    longer writes to the journal from this path."""
    journal = Journal(str(tmp_path / "j.sqlite"))
    quote_source.set("AAPL", Decimal("10"))
    journal.record_order(client_order_id="b-1", symbol="AAPL", side="BUY",
                         notional_dollars=Decimal("100"), stop_loss_price=None)
    journal.record_fill(order_id="o-1", client_order_id="b-1", symbol="AAPL",
                        side="BUY", quantity=Decimal("10"), price=Decimal("10"),
                        filled_at=datetime.now(timezone.utc), stop_loss_price=None)
    broker = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("500"),
    )
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].stop_loss_price is None
    # No positions_recovered_without_stops event emitted from from_journal.
    warnings = [e for e in journal.read_events() if e["kind"] == "positions_recovered_without_stops"]
    assert warnings == []


def test_fill_buy_journals_stop_loss_price(tmp_path):
    b = _broker(tmp_path, prices={"AAPL": Decimal("10")}, cash=Decimal("500"))
    b.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    ))
    fills = b._journal.read_fills()
    assert fills[0]["side"] == "BUY"
    assert fills[0]["stop_loss_price"] == Decimal("9.2")


def test_from_journal_rehydrates_stop_loss_price(tmp_path, quote_source):
    journal = Journal(str(tmp_path / "j.sqlite"))
    quote_source.set("AAPL", Decimal("10"))
    seed = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("500"))
    seed.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    ))
    replayed = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("500"),
    )
    positions = replayed.get_positions()
    assert positions[0].stop_loss_price == Decimal("9.2")


def test_from_journal_carries_stop_forward_through_addon_buy(tmp_path, quote_source):
    """A stop set on the initial BUY must survive an add-on BUY that omits its own stop,
    AND survive replay via from_journal."""
    journal = Journal(str(tmp_path / "j.sqlite"))
    quote_source.set("AAPL", Decimal("10"))
    seed = PaperBroker(journal=journal, quote_source=quote_source, starting_cash=Decimal("500"))
    # First BUY sets a stop.
    seed.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.05"),
    ))
    # Add-on BUY without a stop.
    quote_source.set("AAPL", Decimal("11"))
    seed.place_order(Order(
        client_order_id="b-2", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("22"), order_type=OrderType.MARKET,
    ))
    # Live position kept the carried-forward stop.
    assert seed.get_positions()[0].stop_loss_price == Decimal("9.5")
    # Replay preserves it.
    replayed = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("500"),
    )
    assert replayed.get_positions()[0].stop_loss_price == Decimal("9.5")


def test_from_journal_stop_none_when_no_journaled_stop(tmp_path, quote_source):
    """Positions opened via a BUY that has no stop (legacy) still get None."""
    journal = Journal(str(tmp_path / "j.sqlite"))
    quote_source.set("AAPL", Decimal("10"))
    # Directly journal a BUY fill with stop_loss_price=None to simulate legacy data.
    ts = datetime(2026, 7, 2, tzinfo=timezone.utc)
    journal.record_order(client_order_id="b-1", symbol="AAPL", side="BUY",
                         notional_dollars=Decimal("50"), stop_loss_price=None)
    journal.record_fill(order_id="o-1", client_order_id="b-1", symbol="AAPL",
                        side="BUY", quantity=Decimal("5"), price=Decimal("10"),
                        filled_at=ts, stop_loss_price=None)
    replayed = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("500"),
    )
    assert replayed.get_positions()[0].stop_loss_price is None


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


# --- M2: stop is entry-relative, resolved from the actual fill price -------


def test_stop_computed_from_actual_fill_price_not_stale_reference(tmp_path):
    """A gap-down fill (e.g. reference $100, actual fill $91) must still
    produce a stop BELOW the fill price — never above it. Before this fix,
    an absolute stop computed from a stale reference price could sit above
    a gapped-down fill, causing an instant stop-out on the next guardian
    pass."""
    b = _broker(tmp_path, {"AAPL": "91"})  # gapped down from a $100 reference
    o = Order(
        client_order_id="c1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    fill = b.place_order(o)
    assert fill.price == Decimal("91")
    pos = b.get_positions()[0]
    assert pos.stop_loss_price < fill.price


def test_stop_equals_fill_price_times_one_plus_pct_exactly(tmp_path):
    b = _broker(tmp_path, {"AAPL": "91"})
    o = Order(
        client_order_id="c1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    fill = b.place_order(o)
    expected = fill.price * (Decimal("1") + Decimal("-0.08"))
    pos = b.get_positions()[0]
    assert pos.stop_loss_price == expected
    fills = b._journal.read_fills()
    assert fills[0]["stop_loss_price"] == expected


def test_from_journal_journals_orphan_sell_and_skips_it(tmp_path, quote_source):
    """A SELL fill with no matching prior BUY (journal inconsistency) must not
    silently vanish during replay — it should journal a
    journal_replay_orphan_sell event (mirroring journal_replay_fallback) so
    an ops engineer can see the journal is missing the position, while
    replay itself continues rather than raising."""
    journal = Journal(str(tmp_path / "j.sqlite"))
    journal.record_order(client_order_id="s-1", symbol="AAPL", side="SELL",
                         notional_dollars=Decimal("50"), stop_loss_price=None)
    journal.record_fill(order_id="o-1", client_order_id="s-1", symbol="AAPL",
                        side="SELL", quantity=Decimal("5"), price=Decimal("10"),
                        filled_at=datetime.now(timezone.utc))
    replayed = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("500"),
    )
    # No position existed to sell from — cash is untouched and no phantom
    # position is created.
    assert replayed.get_cash() == Decimal("500")
    assert replayed.get_positions() == []
    orphans = [e for e in journal.read_events() if e["kind"] == "journal_replay_orphan_sell"]
    assert len(orphans) == 1
    assert orphans[0]["payload"]["client_order_id"] == "s-1"
    assert orphans[0]["payload"]["symbol"] == "AAPL"


def test_from_journal_applies_cash_adjustments(tmp_path, quote_source):
    """Replayed cash = starting_cash + adjustments + fills. Deposits and the
    startup seed are journaled as cash adjustments so restarts and live-mode
    reconciliation see the same cash the account actually has."""
    from ops.journal import Journal
    journal = Journal(str(tmp_path / "j.sqlite"))
    journal.record_cash_adjustment(kind="seed", amount=Decimal("250"))
    journal.record_cash_adjustment(kind="deposit", amount=Decimal("100"))
    broker = PaperBroker.from_journal(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("0"),
    )
    assert broker.get_cash() == Decimal("350")
