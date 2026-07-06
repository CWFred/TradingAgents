"""Unit tests for the null-baseline equal-weight paper portfolio."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ops import events
from ops.broker.paper import PaperBroker
from ops.journal import Journal
from ops.research.baseline import update_baseline_portfolio

pytestmark = pytest.mark.unit

ASOF = date(2026, 7, 1)
NOW = datetime(2026, 7, 1, 21, 0, tzinfo=timezone.utc)


@pytest.fixture
def journal(tmp_path):
    j = Journal(str(tmp_path / "baseline.sqlite"))
    yield j
    j.close()


def _broker(journal, cash="100000"):
    return PaperBroker(
        journal=journal, quote_source=lambda s: Decimal("20"),
        starting_cash=Decimal(cash),
    )


def test_buys_passers_equal_weight(journal):
    broker = _broker(journal)
    summary = update_baseline_portfolio(
        broker=broker, journal=journal, passers=["AAA", "BBB"], asof=ASOF, now=NOW,
    )
    assert summary["buys"] == ["AAA", "BBB"]
    positions = {p.symbol: p for p in broker.get_positions()}
    # 4% of 100k = 4000 for AAA; BBB gets 4% of remaining equity (still 100k mark).
    assert positions["AAA"].quantity == Decimal("4000") / Decimal("20")
    # Events and snapshot recorded.
    kinds = [e["kind"] for e in journal.read_events()]
    assert events.KIND_BASELINE_SCREEN_RUN in kinds
    assert journal.get_latest_equity_snapshot(kind="baseline_run") is not None


def test_held_names_are_not_rebought(journal):
    broker = _broker(journal)
    update_baseline_portfolio(
        broker=broker, journal=journal, passers=["AAA"], asof=ASOF, now=NOW,
    )
    summary = update_baseline_portfolio(
        broker=broker, journal=journal, passers=["AAA"], asof=ASOF, now=NOW,
    )
    assert summary["buys"] == []
    assert len(broker.get_positions()) == 1


def test_positions_exit_after_max_hold(journal):
    # PaperBroker stamps fills with the REAL clock, so "366 days later" must
    # be computed from the real clock too, not from the fake NOW.
    broker = _broker(journal)
    update_baseline_portfolio(
        broker=broker, journal=journal, passers=["AAA"], asof=ASOF, now=NOW,
    )
    later = datetime.now(timezone.utc) + timedelta(days=366)
    summary = update_baseline_portfolio(
        broker=broker, journal=journal, passers=[], asof=date(2027, 7, 2), now=later,
    )
    assert summary["exits"] == ["AAA"]
    assert broker.get_positions() == []
    kinds = [e["kind"] for e in journal.read_events()]
    assert events.KIND_BASELINE_EXIT in kinds


def test_exited_name_can_reenter_on_same_run(journal):
    broker = _broker(journal)
    update_baseline_portfolio(
        broker=broker, journal=journal, passers=["AAA"], asof=ASOF, now=NOW,
    )
    later = datetime.now(timezone.utc) + timedelta(days=366)
    summary = update_baseline_portfolio(
        broker=broker, journal=journal, passers=["AAA"], asof=date(2027, 7, 2), now=later,
    )
    assert summary["exits"] == ["AAA"]
    assert summary["buys"] == ["AAA"]


def test_stops_buying_when_cash_exhausted(journal):
    broker = _broker(journal, cash="5000")
    # Slice = 4% of 5000 = 200; cash runs out after ~25 buys.
    passers = [f"SYM{i:02d}" for i in range(40)]
    summary = update_baseline_portfolio(
        broker=broker, journal=journal, passers=passers, asof=ASOF, now=NOW,
    )
    assert len(summary["buys"]) < 40
    assert broker.get_cash() < Decimal("200")


def test_quote_failure_skips_name_and_continues(journal):
    from ops.broker.base import QuoteUnavailable

    def quotes(symbol):
        if symbol == "BAD":
            raise QuoteUnavailable("no quote")
        return Decimal("20")

    broker = PaperBroker(
        journal=journal, quote_source=quotes, starting_cash=Decimal("100000"),
    )
    summary = update_baseline_portfolio(
        broker=broker, journal=journal, passers=["BAD", "GOOD"], asof=ASOF, now=NOW,
    )
    assert summary["buys"] == ["GOOD"]
    assert summary["skipped"] == ["BAD"]


def test_baseline_survives_unquotable_held_position(journal):
    """A delisted holding must not wedge the control (final-review Fix 1).

    AAA is bought with a working quote, then goes unquotable (delisted).
    A later run for a new passer BBB must not raise, must still buy BBB,
    and must mark AAA at its last journaled fill price when computing
    equity for sizing/reporting instead of calling the (raising) live quote.
    """
    from ops.broker.base import QuoteUnavailable

    aaa_quotable = {"ok": True}

    def quotes(symbol):
        if symbol == "AAA" and not aaa_quotable["ok"]:
            raise QuoteUnavailable("delisted")
        return Decimal("20")

    broker = PaperBroker(
        journal=journal, quote_source=quotes, starting_cash=Decimal("100000"),
    )
    update_baseline_portfolio(
        broker=broker, journal=journal, passers=["AAA"], asof=ASOF, now=NOW,
    )
    aaa_fill = journal.last_buy_fill_for("AAA")
    aaa_quotable["ok"] = False  # AAA is now delisted / unquotable

    summary = update_baseline_portfolio(
        broker=broker, journal=journal, passers=["BBB"], asof=ASOF, now=NOW,
    )

    assert summary["buys"] == ["BBB"]

    positions = {p.symbol: p for p in broker.get_positions()}
    expected_equity = (
        broker.get_cash()
        + positions["AAA"].quantity * aaa_fill["price"]
        + positions["BBB"].quantity * Decimal("20")
    )
    snapshot = journal.get_latest_equity_snapshot(kind="baseline_run")
    assert snapshot is not None
    assert snapshot.equity == expected_equity


def test_same_day_retry_after_quote_failure_does_not_collide(journal):
    from ops.broker.base import QuoteUnavailable

    quotes_ok = {"flag": False}

    def quotes(symbol):
        if symbol == "BAD" and not quotes_ok["flag"]:
            raise QuoteUnavailable("no quote")
        return Decimal("20")

    broker = PaperBroker(
        journal=journal, quote_source=quotes, starting_cash=Decimal("100000"),
    )
    first = update_baseline_portfolio(
        broker=broker, journal=journal, passers=["BAD", "GOOD"], asof=ASOF, now=NOW,
    )
    assert first["skipped"] == ["BAD"]

    quotes_ok["flag"] = True
    second = update_baseline_portfolio(
        broker=broker, journal=journal, passers=["BAD", "GOOD"], asof=ASOF, now=NOW,
    )
    assert second["buys"] == ["BAD"]          # GOOD already held, BAD now fills
    assert second["skipped"] == []


def test_write_off_closes_position_at_given_price(journal):
    from ops.research.baseline import update_baseline_portfolio, write_off_position

    broker = _broker(journal)
    update_baseline_portfolio(
        broker=broker, journal=journal, passers=["DEAD"], asof=ASOF, now=NOW,
    )
    result = write_off_position(
        journal=journal, symbol="DEAD", price=Decimal("2.50"),
        starting_cash=Decimal("100000"), note="acquired at $2.50",
    )
    assert result["symbol"] == "DEAD"
    # Replay must show the position gone and cash credited at the write-off price.
    rebuilt = PaperBroker.from_journal(
        journal=journal, quote_source=lambda s: Decimal("20"),
        starting_cash=Decimal("100000"),
    )
    assert all(p.symbol != "DEAD" for p in rebuilt.get_positions())
    kinds = [e["kind"] for e in journal.read_events()]
    assert "baseline_writeoff" in kinds


def test_write_off_unknown_symbol_raises(journal):
    from ops.broker.base import NoSuchPosition
    from ops.research.baseline import write_off_position

    with pytest.raises(NoSuchPosition):
        write_off_position(
            journal=journal, symbol="GHOST", price=Decimal("1"),
            starting_cash=Decimal("100000"),
        )
