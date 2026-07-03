import dataclasses
from decimal import Decimal

import pytest

from ops import build_guarded_paper_broker
from ops.broker.types import Order, OrderType, Side
from ops.config import OpsConfig
from ops.journal import Journal
from ops.position_guardian import PositionGuardian
from ops.trading_time import trading_week_start


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
        stop_pct=Decimal("-0.08"),
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
def _broker_with_positions(tmp_path):
    from ops import build_guarded_paper_broker
    from ops.config import OpsConfig
    from ops.journal import Journal

    class _Q:
        def __init__(self):
            self._m = {}
        def set(self, s, p): self._m[s] = p
        def get(self, s): return self._m[s]

    def _make(positions, weekly_open_equity):
        j = Journal(str(tmp_path / "j.sqlite"))
        quotes = _Q()
        for symbol, entry, _stop, _notional in positions:
            quotes.set(symbol, entry)
        broker = build_guarded_paper_broker(
            config=OpsConfig(), journal=j, quote_source=quotes.get,
            starting_cash=Decimal("500"),
            start_of_day_equity=lambda: weekly_open_equity,
            start_of_week_equity=lambda: weekly_open_equity,
        )
        for symbol, entry, stop, notional in positions:
            # positions tuples carry an absolute target stop for readability;
            # convert to entry-relative stop_pct since that's what Order
            # carries now (the absolute stop is resolved from the fill price,
            # which equals `entry` here since quotes are pinned before the buy).
            stop_pct = (stop / entry) - Decimal("1")
            broker.place_order(Order(
                client_order_id=f"b-{symbol}", symbol=symbol, side=Side.BUY,
                notional_dollars=notional, order_type=OrderType.MARKET,
                stop_pct=stop_pct,
            ))
        # Baseline must be in the CURRENT week — the guardian ignores stale
        # open_week snapshots, and a hardcoded date would rot next Monday.
        from datetime import datetime, timezone
        j.record_equity_snapshot(
            kind="open_week", equity=weekly_open_equity, cash=Decimal("500"),
            at=datetime.now(timezone.utc),
        )
        return broker, quotes, OpsConfig(), j
    return _make


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
        stop_pct=Decimal("-0.08"),
    ))
    guarded.place_order(Order(
        client_order_id="m", symbol="MSFT", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
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
        stop_pct=Decimal("-0.08"),
    ))
    guarded.place_order(Order(
        client_order_id="m", symbol="MSFT", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
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
        stop_pct=Decimal("-0.08"),
    ))
    guarded.place_order(Order(
        client_order_id="m", symbol="MSFT", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
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


def test_guardian_survives_brokererror_at_quote_step(tmp_path):
    """A live BrokerError (RobinhoodBroker.get_quote's real failure mode, not
    just QuoteUnavailable) for one position must not abort the pass; the
    remaining position is still evaluated and no guardian_check_error fires."""
    from ops.broker.base import BrokerError
    quotes = {"AAPL": Decimal("200"), "MSFT": Decimal("200")}
    j, guarded, cfg, _ = _stack(tmp_path, starting_cash="10000", quotes=quotes)
    guarded.place_order(Order(
        client_order_id="a", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    ))
    guarded.place_order(Order(
        client_order_id="m", symbol="MSFT", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    ))

    def flaky_quote(symbol):
        if symbol == "AAPL":
            raise BrokerError(f"live quote transport error on {symbol}")
        return Decimal("180")   # MSFT will trip stop

    g = PositionGuardian(broker=guarded, quote_source=flaky_quote, config=cfg)
    actions = g.check_stops_once()

    # Both positions were evaluated despite AAPL's raw BrokerError.
    assert {a.symbol for a in actions} == {"AAPL", "MSFT"}
    aapl = next(a for a in actions if a.symbol == "AAPL")
    msft = next(a for a in actions if a.symbol == "MSFT")
    assert aapl.sold is False
    assert "quote unavailable" in aapl.reason.lower()
    assert msft.sold is True
    events = j.read_events()
    assert any(e["kind"] == "quote_unavailable" for e in events)
    assert not any(e["kind"] == "guardian_check_error" for e in events)


# --- blind-guardian escalation: consecutive fully-failed passes -------------


def test_guardian_blind_after_five_consecutive_fully_failed_passes(tmp_path):
    """5 consecutive passes where the only open position's quote fails
    journal exactly one guardian_blind event (not every pass thereafter)."""
    from ops.broker.base import BrokerError
    quotes = {"AAPL": Decimal("200")}
    j, guarded, cfg, _ = _stack(tmp_path, starting_cash="10000", quotes=quotes)
    guarded.place_order(Order(
        client_order_id="a", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    ))

    def always_fails(symbol):
        raise BrokerError("broker unreachable")

    g = PositionGuardian(broker=guarded, quote_source=always_fails, config=cfg)
    for _ in range(4):
        g.check_stops_once()
    events = j.read_events()
    assert not any(e["kind"] == "guardian_blind" for e in events)

    g.check_stops_once()   # 5th consecutive fully-failed pass
    events = j.read_events()
    blind_events = [e for e in events if e["kind"] == "guardian_blind"]
    assert len(blind_events) == 1

    g.check_stops_once()   # 6th consecutive — must NOT duplicate
    events = j.read_events()
    blind_events = [e for e in events if e["kind"] == "guardian_blind"]
    assert len(blind_events) == 1


def test_guardian_blind_streak_resets_after_successful_pass(tmp_path):
    """A pass that gets a usable quote resets the consecutive-failure
    counter, so an intermittent outage doesn't cross the threshold on
    stale count."""
    from ops.broker.base import BrokerError
    quotes = {"AAPL": Decimal("200")}
    j, guarded, cfg, _ = _stack(tmp_path, starting_cash="10000", quotes=quotes)
    guarded.place_order(Order(
        client_order_id="a", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    ))

    state = {"fail": True}

    def toggling_quote(symbol):
        if state["fail"]:
            raise BrokerError("broker unreachable")
        return Decimal("200")   # well above stop; holds, doesn't sell

    g = PositionGuardian(broker=guarded, quote_source=toggling_quote, config=cfg)
    for _ in range(4):
        g.check_stops_once()
    state["fail"] = False
    g.check_stops_once()       # success resets the streak
    state["fail"] = True
    for _ in range(4):
        g.check_stops_once()   # only 4 consecutive since the reset
    events = j.read_events()
    assert not any(e["kind"] == "guardian_blind" for e in events)

    g.check_stops_once()       # 5th consecutive since the reset
    events = j.read_events()
    blind_events = [e for e in events if e["kind"] == "guardian_blind"]
    assert len(blind_events) == 1


def test_guardian_empty_book_does_not_trip_blind_alarm(tmp_path):
    """A pass with zero open positions has nothing to check and must never
    count toward the blind-guardian streak, no matter how many times it
    repeats."""
    j, guarded, cfg, _ = _stack(tmp_path, starting_cash="10000")
    g = PositionGuardian(broker=guarded, quote_source=guarded.get_quote, config=cfg)
    for _ in range(10):
        g.check_stops_once()
    events = j.read_events()
    assert not any(e["kind"] == "guardian_blind" for e in events)


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
        stop_pct=Decimal("-0.08"),
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
        stop_pct=Decimal("-0.08"),
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
        stop_pct=Decimal("-0.05"),
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
        stop_pct=Decimal("-0.2"),
    ))
    _clear_position_stop(broker, "MSFT")
    guardian = PositionGuardian(broker=broker, quote_source=quotes.get, config=cfg)
    quotes.set("MSFT", Decimal("92.5"))   # -7.5%, above -8% threshold
    assert guardian.check_stops_once()[0].sold is False
    quotes.set("MSFT", Decimal("91.5"))   # -8.5%, past threshold
    assert guardian.check_stops_once()[0].sold is True


def test_kill_switch_paper_mode_closes_all_positions(_broker_with_positions):
    """Paper mode: guardian trips kill_switch and closes every open position.

    Notional is capped at 10% of equity ($50 on $500) by PerPositionCapRule,
    so two $50 positions are used (not the brief's $50/$100 split, which
    would violate the cap). Stops are set loose (well below the target
    quotes) so neither position closes via the ordinary per-position stop
    pass — the closes below are exercised via the kill-switch path only.
    AAPL 10->2 (-$40) and MSFT 100->20 (-$40) drop equity from $500 to
    $420, a -16% weekly move, past the -15% default threshold.
    """
    broker, quotes, cfg, journal = _broker_with_positions(
        [("AAPL", Decimal("10"), Decimal("0.50"), Decimal("50")),
         ("MSFT", Decimal("100"), Decimal("5"), Decimal("50"))],
        weekly_open_equity=Decimal("500"),
    )
    quotes.set("AAPL", Decimal("2"))
    quotes.set("MSFT", Decimal("20"))
    guardian = PositionGuardian(
        broker=broker, quote_source=quotes.get, config=cfg,
        journal=journal, broker_mode="paper",
    )
    guardian.check_stops_once()
    assert broker.get_positions() == []
    events = journal.read_events()
    assert any(e["kind"] == "kill_switch" for e in events)


def test_kill_switch_live_mode_halts_only(_broker_with_positions):
    """Live mode: guardian trips kill_switch but does NOT close positions.

    Same two-position, -16% setup as the paper-mode test above (a single
    $50 position out of $500 equity can only ever lose 10% given the
    per-position cap, so a lone AAPL position can never breach -15%).
    """
    broker, quotes, cfg, journal = _broker_with_positions(
        [("AAPL", Decimal("10"), Decimal("0.50"), Decimal("50")),
         ("MSFT", Decimal("100"), Decimal("5"), Decimal("50"))],
        weekly_open_equity=Decimal("500"),
    )
    quotes.set("AAPL", Decimal("2"))
    quotes.set("MSFT", Decimal("20"))
    guardian = PositionGuardian(
        broker=broker, quote_source=quotes.get, config=cfg,
        journal=journal, broker_mode="robinhood",
    )
    guardian.check_stops_once()
    # Positions remain; kill_switch event was still journaled.
    assert len(broker.get_positions()) == 2
    events = journal.read_events()
    assert any(e["kind"] == "kill_switch" for e in events)


def test_kill_switch_not_tripped_when_within_threshold(_broker_with_positions):
    broker, quotes, cfg, journal = _broker_with_positions(
        [("AAPL", Decimal("10"), Decimal("9"), Decimal("50"))],
        weekly_open_equity=Decimal("500"),
    )
    # Small drop only (-0.5% portfolio-level, -5% per-share): does NOT trip.
    quotes.set("AAPL", Decimal("9.5"))
    guardian = PositionGuardian(
        broker=broker, quote_source=quotes.get, config=cfg,
        journal=journal, broker_mode="paper",
    )
    guardian.check_stops_once()
    events = journal.read_events()
    assert not any(e["kind"] == "kill_switch" for e in events)


def test_guardian_records_stop_hit_with_mode_and_threshold(guardian_fixtures):
    """stop_hit event distinguishes absolute vs pct triggers."""
    broker, quotes, cfg = guardian_fixtures
    quotes.set("AAPL", Decimal("10"))
    broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.05"),
    ))
    guardian = PositionGuardian(broker=broker, quote_source=quotes.get, config=cfg)
    quotes.set("AAPL", Decimal("9.45"))
    guardian.check_stops_once()
    events = broker.journal.read_events()
    stop_events = [e for e in events if e["kind"] == "stop_hit"]
    assert stop_events[-1]["payload"]["mode"] == "absolute"
    assert stop_events[-1]["payload"]["threshold_repr"].startswith("abs ")


def test_kill_switch_idempotent_within_week(_broker_with_positions):
    """Second guardian pass in the same week does NOT record a duplicate kill_switch event."""
    broker, quotes, cfg, journal = _broker_with_positions(
        [("AAPL", Decimal("10"), Decimal("0.50"), Decimal("50")),
         ("MSFT", Decimal("100"), Decimal("5"), Decimal("50"))],
        weekly_open_equity=Decimal("500"),
    )
    # Trigger >15% weekly loss: 10->2 loses $40, 100->20 loses $40, total -16%
    quotes.set("AAPL", Decimal("2"))
    quotes.set("MSFT", Decimal("20"))
    guardian = PositionGuardian(
        broker=broker, quote_source=quotes.get, config=cfg,
        journal=journal, broker_mode="paper",
    )
    guardian.check_stops_once()   # trips kill_switch, closes positions
    guardian.check_stops_once()   # runs again — should NOT record another kill_switch
    events = journal.read_events()
    kill_events = [e for e in events if e["kind"] == "kill_switch"]
    assert len(kill_events) == 1


def test_kill_switch_paper_mode_records_close_failure_and_continues(_broker_with_positions):
    """Close failure during kill-switch sweep is journaled; sweep continues to next symbol."""
    import unittest.mock as _mock

    from ops.broker.base import BrokerError
    broker, quotes, cfg, journal = _broker_with_positions(
        [("AAPL", Decimal("10"), Decimal("0.50"), Decimal("50")),
         ("MSFT", Decimal("100"), Decimal("5"), Decimal("50"))],
        weekly_open_equity=Decimal("500"),
    )
    # Trigger >15% weekly loss: 10->2 loses $40, 100->20 loses $40, total -16%
    quotes.set("AAPL", Decimal("2"))
    quotes.set("MSFT", Decimal("20"))

    original_close = broker.close_position
    def _selective_close(symbol, **kw):
        if symbol == "AAPL":
            raise BrokerError("simulated failure on AAPL close")
        return original_close(symbol, **kw)

    with _mock.patch.object(broker, "close_position", side_effect=_selective_close):
        guardian = PositionGuardian(
            broker=broker, quote_source=quotes.get, config=cfg,
            journal=journal, broker_mode="paper",
        )
        guardian.check_stops_once()

    events = journal.read_events()
    kinds = [e["kind"] for e in events]
    assert "kill_switch" in kinds
    fail_events = [e for e in events if e["kind"] == "kill_switch_close_failed"]
    assert len(fail_events) == 1
    assert fail_events[0]["payload"]["symbol"] == "AAPL"
    # MSFT was still closed despite AAPL failing.
    remaining = {p.symbol for p in broker.get_positions()}
    assert "MSFT" not in remaining


def test_guardian_journals_check_error_on_unexpected_exception(tmp_path):
    """The scheduler-safety invariant: any exception from broker/journal
    is journaled as guardian_check_error and swallowed. The APScheduler
    job body must never propagate an exception up."""
    from unittest.mock import MagicMock

    from ops.broker.base import BrokerError

    j = Journal(str(tmp_path / "j.sqlite"))
    broker = MagicMock()
    broker.get_positions.side_effect = BrokerError("mcp down")

    guardian = PositionGuardian(
        broker=broker,
        quote_source=lambda s: Decimal("10"),
        config=OpsConfig(),
        journal=j,
        broker_mode="robinhood",
    )

    result = guardian.check_stops_once()
    assert result == []

    events = j.read_events()
    err_events = [e for e in events if e["kind"] == "guardian_check_error"]
    assert len(err_events) == 1
    assert "mcp down" in err_events[0]["payload"]["error"]


# --- market-hours gate: the guardian must not trade outside RTH -------------


def test_guardian_skips_entirely_when_market_closed(tmp_path):
    """Spec: 'Guardian tracks AH quotes but does not trade. Any stop breached
    AH fires at next open.' A breached stop with the market closed must not
    sell, must not journal stop_hit, and must not trip the kill switch."""
    j, guarded, cfg, quotes = _stack(tmp_path)
    _open_position(guarded)
    quotes["AAPL"] = Decimal("100")   # far below the $184 stop
    g = PositionGuardian(
        broker=guarded, quote_source=guarded.get_quote, config=cfg,
        market_open_fn=lambda: False,
    )
    assert g.check_stops_once() == []
    assert len(guarded.get_positions()) == 1
    kinds = [e["kind"] for e in j.read_events()]
    assert "stop_hit" not in kinds
    assert "kill_switch" not in kinds


def test_guardian_trades_when_market_open_fn_true(tmp_path):
    j, guarded, cfg, quotes = _stack(tmp_path)
    _open_position(guarded)
    quotes["AAPL"] = Decimal("100")
    g = PositionGuardian(
        broker=guarded, quote_source=guarded.get_quote, config=cfg,
        market_open_fn=lambda: True,
    )
    actions = g.check_stops_once()
    assert actions[0].sold is True
    assert guarded.get_positions() == []


# --- kill switch: fresh weekly baseline only, and resumable auto-close ------


def test_kill_switch_ignores_stale_week_snapshot_and_rebaselines(tmp_path):
    """An open_week snapshot from a PREVIOUS week must not be used as the
    drawdown baseline (a guardian-only or long-idle restart would otherwise
    compare against weeks-old equity and can falsely liquidate everything).
    Instead the guardian records a fresh baseline for this week."""
    from datetime import datetime, timedelta, timezone
    j, guarded, cfg, quotes = _stack(tmp_path)
    _open_position(guarded)
    now = datetime.now(timezone.utc)
    j.record_equity_snapshot(
        kind="open_week", equity=Decimal("1000"), cash=Decimal("1000"),
        at=now - timedelta(days=14),
    )
    g = PositionGuardian(
        broker=guarded, quote_source=guarded.get_quote, config=cfg,
        journal=j, broker_mode="paper",
    )
    g.check_stops_once()
    kinds = [e["kind"] for e in j.read_events()]
    assert "kill_switch" not in kinds       # equity 250 vs stale 1000 must NOT trip
    assert len(guarded.get_positions()) == 1
    # Use the real ET-calendar boundary (matches what PositionGuardian
    # itself uses, per M7) rather than re-implementing the old
    # UTC-Monday-midnight formula inline.
    monday = trading_week_start(now)
    fresh = [s for s in j.read_equity_snapshots()
             if s["kind"] == "open_week" and s["at"] >= monday]
    assert fresh, "guardian should have recorded a fresh weekly baseline"


def test_kill_switch_paper_close_resumes_after_restart(tmp_path):
    """If the kill_switch event was journaled but the process died before
    the auto-close sweep finished, a later guardian pass (same week) must
    close the remaining positions — without duplicating the event."""
    from datetime import datetime, timezone
    j, guarded, cfg, quotes = _stack(tmp_path)
    _open_position(guarded)
    quotes["AAPL"] = Decimal("190")   # above the $184 stop: ordinary stop must not fire
    j.record_equity_snapshot(
        kind="open_week", equity=Decimal("250"), cash=Decimal("225"),
        at=datetime.now(timezone.utc),
    )
    j.record_event("kill_switch", {"mode": "paper", "pct": "-0.2"})
    g = PositionGuardian(
        broker=guarded, quote_source=guarded.get_quote, config=cfg,
        journal=j, broker_mode="paper",
    )
    g.check_stops_once()
    assert guarded.get_positions() == [], "remaining positions must be swept"
    kill_events = [e for e in j.read_events() if e["kind"] == "kill_switch"]
    assert len(kill_events) == 1, "no duplicate kill_switch event"


# --- daily halt: computed in the same pass as the weekly kill switch --------


def _daily_halt_stack(tmp_path, *, drop_price):
    """500 starting cash, one AAPL position bought at $10 (5 shares, notional
    $50, cash 450). The stop is set absurdly loose (-99%, absolute stop
    $0.10) so the ordinary per-position stop never interferes — only the
    portfolio-level daily-halt check is exercised. An open_day baseline of
    $500 is recorded 'now' (today), matching the freshness convention
    _maybe_trip_kill_switch uses for open_week. `drop_price` sets AAPL's
    quote so total equity = 450 + 5*drop_price."""
    from datetime import datetime, timezone

    j = Journal(str(tmp_path / "j.sqlite"))
    cfg = OpsConfig()  # daily_drawdown_pct = -0.07
    quotes = _MutableQuotes()
    quotes.set("AAPL", Decimal("10"))
    broker = build_guarded_paper_broker(
        config=cfg, journal=j, quote_source=quotes.get,
        starting_cash=Decimal("500"),
        start_of_day_equity=lambda: Decimal("500"),
        start_of_week_equity=lambda: Decimal("500"),
    )
    broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.99"),
    ))
    j.record_equity_snapshot(
        kind="open_day", equity=Decimal("500"), cash=Decimal("500"),
        at=datetime.now(timezone.utc),
    )
    quotes.set("AAPL", drop_price)
    guardian = PositionGuardian(
        broker=broker, quote_source=quotes.get, config=cfg,
        journal=j, broker_mode="paper",
    )
    return j, broker, guardian


def test_daily_halt_trips_at_exactly_threshold(tmp_path):
    """equity 450 + 5*3 = 465, pct = (465-500)/500 = -0.07 exactly ==
    daily_drawdown_pct. Must record daily_halt."""
    j, broker, guardian = _daily_halt_stack(tmp_path, drop_price=Decimal("3"))
    guardian.check_stops_once()
    events = j.read_events()
    halts = [e for e in events if e["kind"] == "daily_halt"]
    assert len(halts) == 1
    assert halts[0]["payload"]["pct"] == "-0.07"
    # No position closing — daily halt only blocks new BUYs elsewhere.
    assert len(broker.get_positions()) == 1


def test_daily_halt_not_tripped_just_above_threshold(tmp_path):
    """equity 450 + 5*3.1 = 465.5, pct = -0.069, ABOVE (less negative than)
    the -0.07 threshold. Must NOT record daily_halt."""
    j, broker, guardian = _daily_halt_stack(tmp_path, drop_price=Decimal("3.1"))
    guardian.check_stops_once()
    events = j.read_events()
    assert not any(e["kind"] == "daily_halt" for e in events)


def test_daily_halt_idempotent_within_day(tmp_path):
    """A second guardian pass the same day, still past threshold, must not
    duplicate the daily_halt event."""
    j, broker, guardian = _daily_halt_stack(tmp_path, drop_price=Decimal("3"))
    guardian.check_stops_once()
    guardian.check_stops_once()
    events = j.read_events()
    halts = [e for e in events if e["kind"] == "daily_halt"]
    assert len(halts) == 1
