import dataclasses
from decimal import Decimal

import pytest

from ops import build_guarded_paper_broker
from ops.broker.types import Order, OrderType, Side
from ops.config import OpsConfig
from ops.journal import Journal
from ops.position_guardian import PositionGuardian


def _stack(tmp_path, *, starting_cash="250", quotes=None):
    quotes = quotes or {"AAPL": Decimal("200")}
    j = Journal(str(tmp_path / "j.sqlite"))
    cfg = OpsConfig()
    guarded = build_guarded_paper_broker(
        config=cfg, journal=j,
        quote_source=lambda s: quotes[s],
        starting_cash=Decimal(starting_cash),
        start_of_day_equity=lambda: Decimal(starting_cash),
        start_of_week_equity=lambda: Decimal(starting_cash),
    )
    return j, guarded, cfg, quotes


def _open_position(guarded):
    guarded.place_order(Order(
        client_order_id="open", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))


def _clear_position_stop(broker, symbol):
    """Simulate a legacy/imported position with no per-position stop.
    StopAttachedRule requires every BUY to carry a stop_loss_price, so this
    reaches past the rule chain into the inner PaperBroker's position table
    (name-mangled via GuardedBroker) to null out the stop after the fact."""
    inner = broker._GuardedBroker__inner
    pos = inner._positions[symbol]
    inner._positions[symbol] = dataclasses.replace(pos, stop_loss_price=None)


class _MutableQuotes:
    """A settable quote source: quotes.set(sym, price) / quotes.get(sym)."""

    def __init__(self):
        self._prices: dict[str, Decimal] = {}

    def set(self, symbol: str, price: Decimal) -> None:
        self._prices[symbol] = price

    def get(self, symbol: str) -> Decimal:
        return self._prices[symbol]


@pytest.fixture
def guardian_fixtures(tmp_path):
    """(broker, quotes, cfg) — cfg.per_position_stop_pct defaults to -0.08."""
    j = Journal(str(tmp_path / "j.sqlite"))
    cfg = OpsConfig()
    quotes = _MutableQuotes()
    broker = build_guarded_paper_broker(
        config=cfg, journal=j,
        quote_source=quotes.get,
        starting_cash=Decimal("10000"),
        start_of_day_equity=lambda: Decimal("10000"),
        start_of_week_equity=lambda: Decimal("10000"),
    )
    return broker, quotes, cfg


def test_guardian_does_nothing_when_above_stop(tmp_path):
    j, guarded, cfg, quotes = _stack(tmp_path)
    _open_position(guarded)
    quotes["AAPL"] = Decimal("190")   # -5%, above -8% threshold
    g = PositionGuardian(broker=guarded, quote_source=guarded.get_quote, config=cfg)
    actions = g.check_stops_once()
    assert len(actions) == 1
    assert actions[0].sold is False
    assert len(guarded.get_positions()) == 1


def test_guardian_closes_position_at_stop(tmp_path):
    j, guarded, cfg, quotes = _stack(tmp_path)
    _open_position(guarded)
    quotes["AAPL"] = Decimal("184")   # -8% exactly
    g = PositionGuardian(broker=guarded, quote_source=guarded.get_quote, config=cfg)
    actions = g.check_stops_once()
    assert actions[0].sold is True
    assert actions[0].symbol == "AAPL"
    assert guarded.get_positions() == []
    # Stop event journaled
    events = j.read_events()
    stops = [e for e in events if e["kind"] == "stop_hit"]
    assert len(stops) == 1
    assert stops[0]["payload"]["symbol"] == "AAPL"


def test_guardian_handles_multiple_positions(tmp_path):
    quotes = {"AAPL": Decimal("200"), "MSFT": Decimal("200")}
    j, guarded, cfg, _ = _stack(tmp_path, starting_cash="10000", quotes=quotes)
    guarded.place_order(Order(
        client_order_id="a", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    guarded.place_order(Order(
        client_order_id="m", symbol="MSFT", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    quotes["AAPL"] = Decimal("220")    # +10%, hold
    quotes["MSFT"] = Decimal("180")    # -10%, stop
    g = PositionGuardian(broker=guarded, quote_source=guarded.get_quote, config=cfg)
    actions = g.check_stops_once()
    assert {a.symbol for a in actions} == {"AAPL", "MSFT"}
    sold = {a.symbol for a in actions if a.sold}
    assert sold == {"MSFT"}
    remaining = {p.symbol for p in guarded.get_positions()}
    assert remaining == {"AAPL"}


def test_guardian_continues_after_failed_sell(tmp_path):
    """If one position's stop-sell fails, remaining positions must still be checked."""
    import unittest.mock as _mock
    quotes = {"AAPL": Decimal("200"), "MSFT": Decimal("200")}
    j, guarded, cfg, _ = _stack(tmp_path, starting_cash="10000", quotes=quotes)
    # Open two positions, both will trip the stop simultaneously
    guarded.place_order(Order(
        client_order_id="a", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    guarded.place_order(Order(
        client_order_id="m", symbol="MSFT", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    quotes["AAPL"] = Decimal("180")   # -10%, stop
    quotes["MSFT"] = Decimal("180")   # -10%, stop

    # Rig the broker so the first close_position raises; the second symbol
    # must still be attempted. Guardian iterates get_positions() order,
    # which for a dict-backed PaperBroker follows insertion order (AAPL first).
    original_close = guarded.close_position
    call_count = {"n": 0}

    def flaky_close_position(symbol):
        call_count["n"] += 1
        if call_count["n"] == 1:
            from ops.broker.base import BrokerError
            raise BrokerError("simulated failure on first close")
        return original_close(symbol)

    with _mock.patch.object(guarded, "close_position", side_effect=flaky_close_position):
        g = PositionGuardian(broker=guarded, quote_source=guarded.get_quote, config=cfg)
        actions = g.check_stops_once()

    # Both positions were evaluated (one action per position)
    assert {a.symbol for a in actions} == {"AAPL", "MSFT"}
    # Exactly one succeeded, one failed
    sold_symbols = {a.symbol for a in actions if a.sold}
    failed_symbols = {a.symbol for a in actions if not a.sold}
    assert len(sold_symbols) == 1 and len(failed_symbols) == 1
    # The failed one has an error-flavored reason
    failed_action = next(a for a in actions if not a.sold)
    assert "failed" in failed_action.reason.lower()
    # Journal recorded exactly one stop_failed event
    events = j.read_events()
    stop_failed = [e for e in events if e["kind"] == "stop_failed"]
    assert len(stop_failed) == 1


def test_guardian_survives_quote_unavailable(tmp_path):
    """If the quote source fails for one position, guardian must not halt."""
    from ops.broker.base import QuoteUnavailable
    quotes = {"AAPL": Decimal("200"), "MSFT": Decimal("200")}
    j, guarded, cfg, _ = _stack(tmp_path, starting_cash="10000", quotes=quotes)
    guarded.place_order(Order(
        client_order_id="a", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    guarded.place_order(Order(
        client_order_id="m", symbol="MSFT", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))

    def flaky_quote(symbol):
        if symbol == "AAPL":
            raise QuoteUnavailable(f"boom on {symbol}")
        return Decimal("180")   # MSFT will trip stop

    g = PositionGuardian(broker=guarded, quote_source=flaky_quote, config=cfg)
    actions = g.check_stops_once()

    # Both positions were evaluated
    assert {a.symbol for a in actions} == {"AAPL", "MSFT"}
    aapl = next(a for a in actions if a.symbol == "AAPL")
    msft = next(a for a in actions if a.symbol == "MSFT")
    # AAPL got a quote_unavailable non-sale
    assert aapl.sold is False
    assert "quote unavailable" in aapl.reason.lower()
    # MSFT tripped and sold
    assert msft.sold is True
    events = j.read_events()
    assert any(e["kind"] == "quote_unavailable" for e in events)


def test_guardian_stop_sell_client_order_ids_are_unique_per_attempt(tmp_path):
    """Two check_stops_once() passes that hit the same symbol must emit
    distinct client_order_ids on the resulting close_position SELLs.
    Duplicate IDs break any future replay/idempotency logic keyed on
    client_order_id. Guardian now delegates to broker.close_position, which
    mints its own uuid-suffixed 'close-{symbol}-...' id per call."""
    quotes = {"AAPL": Decimal("200")}
    j, guarded, cfg, _ = _stack(tmp_path, starting_cash="10000", quotes=quotes)
    guarded.place_order(Order(
        client_order_id="open-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    quotes["AAPL"] = Decimal("180")   # trip the stop
    g = PositionGuardian(broker=guarded, quote_source=guarded.get_quote, config=cfg)
    actions_1 = g.check_stops_once()
    assert any(a.sold and a.symbol == "AAPL" for a in actions_1)

    # Re-enter AAPL, then trip stop again
    quotes["AAPL"] = Decimal("200")
    guarded.place_order(Order(
        client_order_id="open-2", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    quotes["AAPL"] = Decimal("180")
    actions_2 = g.check_stops_once()
    assert any(a.sold and a.symbol == "AAPL" for a in actions_2)

    # The two SELLs in the journal should have distinct client_order_ids
    fills = j.read_fills()
    stop_fills = [f for f in fills if f["side"] == "SELL"]
    assert len(stop_fills) == 2
    cids = {f["client_order_id"] for f in stop_fills}
    assert len(cids) == 2
    # Both should still start with the readable close_position prefix
    assert all(c.startswith("close-AAPL-") for c in cids)


def test_guardian_uses_absolute_stop_when_position_has_one(guardian_fixtures):
    """A position with an explicit stop_loss_price fires at that absolute price,
    even if it's above the config default."""
    broker, quotes, cfg = guardian_fixtures  # cfg.per_position_stop_pct = -0.08
    # BUY at $10 with a tighter absolute stop of $9.50 (~ -5%).
    quotes.set("AAPL", Decimal("10"))
    broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9.5"),
    ))
    guardian = PositionGuardian(broker=broker, quote_source=quotes.get, config=cfg)
    # Price at $9.60 — below config default -8% (would be $9.20) but ABOVE explicit stop.
    quotes.set("AAPL", Decimal("9.60"))
    actions = guardian.check_stops_once()
    assert actions[0].sold is False, "explicit stop $9.50 not yet triggered at $9.60"
    # Price at $9.45 — below explicit stop, fires.
    quotes.set("AAPL", Decimal("9.45"))
    actions = guardian.check_stops_once()
    assert actions[0].sold is True
    assert broker.get_positions() == []


def test_guardian_falls_back_to_config_pct_when_no_position_stop(guardian_fixtures):
    """A position with stop_loss_price=None uses the config per_position_stop_pct."""
    broker, quotes, cfg = guardian_fixtures  # -0.08
    quotes.set("MSFT", Decimal("100"))
    # StopAttachedRule requires every BUY to carry a stop_loss_price, so we
    # can't place a stop-less BUY directly. Place one normally, then clear
    # the resulting position's stop to simulate a legacy/imported position
    # that predates per-position stops (e.g. synced from a live account).
    broker.place_order(Order(
        client_order_id="b-1", symbol="MSFT", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("80"),
    ))
    _clear_position_stop(broker, "MSFT")
    guardian = PositionGuardian(broker=broker, quote_source=quotes.get, config=cfg)
    quotes.set("MSFT", Decimal("92.5"))   # -7.5%, above -8% threshold
    assert guardian.check_stops_once()[0].sold is False
    quotes.set("MSFT", Decimal("91.5"))   # -8.5%, past threshold
    assert guardian.check_stops_once()[0].sold is True


def test_guardian_records_stop_hit_with_mode_and_threshold(guardian_fixtures):
    """stop_hit event distinguishes absolute vs pct triggers."""
    broker, quotes, cfg = guardian_fixtures
    quotes.set("AAPL", Decimal("10"))
    broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9.5"),
    ))
    guardian = PositionGuardian(broker=broker, quote_source=quotes.get, config=cfg)
    quotes.set("AAPL", Decimal("9.45"))
    guardian.check_stops_once()
    events = broker.journal.read_events()
    stop_events = [e for e in events if e["kind"] == "stop_hit"]
    assert stop_events[-1]["payload"]["mode"] == "absolute"
    assert stop_events[-1]["payload"]["threshold_repr"].startswith("abs ")
