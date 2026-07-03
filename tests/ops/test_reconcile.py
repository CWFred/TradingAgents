from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ops.reconcile import PositionDiff, ReconcileResult, reconcile, emit_reconcile_events
from ops.journal import Journal
from ops.broker.types import Order, OrderType, Side
from ops import build_guarded_paper_broker
from ops.config import OpsConfig


def _quote_source(prices):
    def q(sym):
        return prices[sym]
    return q


def test_reconcile_paper_empty_journal_no_diffs(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = build_guarded_paper_broker(
        config=OpsConfig(), journal=j,
        quote_source=_quote_source({"AAPL": Decimal("10")}),
        starting_cash=Decimal("500"),
        start_of_day_equity=lambda: Decimal("500"),
        start_of_week_equity=lambda: Decimal("500"),
    )
    result = reconcile(journal=j, broker=broker, broker_mode="paper")
    assert result.diffs == []


def test_reconcile_paper_after_buy_no_diffs(tmp_path):
    """No PositionDiffs after a buy, and cash_diff is unchanged by the trade.

    reconcile() always replays the journal with starting_cash=Decimal("0")
    (it compares cash *deltas*, not absolute balances — see
    ops/reconcile.py), so cash_diff carries a constant offset equal to
    whatever starting_cash the live broker itself was built with (here,
    500). A routine BUY moves both the live broker's cash and the
    replay's cash by the identical amount, so that offset — and hence
    cash_diff — must be unchanged by the trade. If it drifts, something
    is inconsistent between the journal and the broker.
    """
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = build_guarded_paper_broker(
        config=OpsConfig(), journal=j,
        quote_source=_quote_source({"AAPL": Decimal("10")}),
        starting_cash=Decimal("500"),
        start_of_day_equity=lambda: Decimal("500"),
        start_of_week_equity=lambda: Decimal("500"),
    )
    result_before = reconcile(journal=j, broker=broker, broker_mode="paper")
    broker.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("40"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.1"),
    ))
    result_after = reconcile(journal=j, broker=broker, broker_mode="paper")
    assert result_after.diffs == []
    assert result_after.cash_diff == result_before.cash_diff


def test_reconcile_live_diff_when_rh_has_extra_symbol(tmp_path):
    """Live broker reports an unjournaled position → PositionDiff kind extra_in_broker."""
    from tests.ops.broker.fakes import FakeMCPClient
    from ops import build_guarded_robinhood_broker
    j = Journal(str(tmp_path / "j.sqlite"))
    client = FakeMCPClient(cash=Decimal("500"))
    client.seed_position("NVDA", Decimal("1"), Decimal("500"))
    broker = build_guarded_robinhood_broker(
        config=OpsConfig(broker_mode="robinhood"), journal=j,
        mcp_client=client,
        start_of_day_equity=lambda: Decimal("500"),
        start_of_week_equity=lambda: Decimal("500"),
    )
    result = reconcile(journal=j, broker=broker, broker_mode="robinhood")
    position_diffs = [d for d in result.diffs if d.kind != "cash_drift"]
    assert len(position_diffs) == 1
    assert position_diffs[0].symbol == "NVDA"
    assert position_diffs[0].kind == "extra_in_broker"


def test_reconcile_live_diff_when_journal_has_extra_symbol(tmp_path):
    """Journal says a position exists that RH doesn't → extra_in_journal."""
    from tests.ops.broker.fakes import FakeMCPClient
    from ops import build_guarded_robinhood_broker
    j = Journal(str(tmp_path / "j.sqlite"))
    ts = datetime(2026, 7, 2, tzinfo=timezone.utc)
    j.record_order(client_order_id="b-1", symbol="AAPL", side="BUY",
                   notional_dollars=Decimal("50"), stop_loss_price=Decimal("9"))
    j.record_fill(order_id="o-1", client_order_id="b-1", symbol="AAPL",
                  side="BUY", quantity=Decimal("5"), price=Decimal("10"),
                  filled_at=ts, stop_loss_price=Decimal("9"))
    client = FakeMCPClient(cash=Decimal("500"))
    broker = build_guarded_robinhood_broker(
        config=OpsConfig(broker_mode="robinhood"), journal=j,
        mcp_client=client,
        start_of_day_equity=lambda: Decimal("500"),
        start_of_week_equity=lambda: Decimal("500"),
    )
    result = reconcile(journal=j, broker=broker, broker_mode="robinhood")
    assert any(d.symbol == "AAPL" and d.kind == "extra_in_journal" for d in result.diffs)


def test_emit_reconcile_events_writes_inconsistency_when_diffs(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    result = ReconcileResult(
        diffs=[PositionDiff(symbol="AAPL", journal_qty=Decimal("5"),
                            broker_qty=Decimal("3"), kind="qty_mismatch")],
        cash_journal=Decimal("100"), cash_broker=Decimal("100"),
        cash_diff=Decimal("0"),
    )
    emit_reconcile_events(j, result)
    events = j.read_events()
    kinds = [e["kind"] for e in events]
    assert "inconsistency" in kinds


def test_reconcile_populates_positions_recovered_without_stops(tmp_path):
    """A live position with no matching journal BUY comes back with
    stop_loss_price=None from RobinhoodBroker.get_positions(); reconcile()
    must surface that symbol in positions_recovered_without_stops."""
    from tests.ops.broker.fakes import FakeMCPClient
    from ops import build_guarded_robinhood_broker
    j = Journal(str(tmp_path / "j.sqlite"))
    client = FakeMCPClient(cash=Decimal("500"))
    client.seed_position("NVDA", Decimal("1"), Decimal("500"))
    broker = build_guarded_robinhood_broker(
        config=OpsConfig(broker_mode="robinhood"), journal=j,
        mcp_client=client,
        start_of_day_equity=lambda: Decimal("500"),
        start_of_week_equity=lambda: Decimal("500"),
    )
    result = reconcile(journal=j, broker=broker, broker_mode="robinhood")
    assert result.positions_recovered_without_stops == ["NVDA"]


def test_emit_reconcile_events_writes_positions_recovered_without_stops(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    result = ReconcileResult(
        diffs=[],
        cash_journal=Decimal("100"), cash_broker=Decimal("100"),
        cash_diff=Decimal("0"),
        positions_recovered_without_stops=["NVDA"],
    )
    emit_reconcile_events(j, result)
    events = j.read_events()
    matching = [e for e in events if e["kind"] == "positions_recovered_without_stops"]
    assert len(matching) == 1
    assert matching[0]["payload"]["symbols"] == ["NVDA"]


def test_reconcile_live_cash_drift_produces_diff(tmp_path):
    """Live mode: material cash drift (Robinhood withdrawal between sessions,
    say) surfaces as a __CASH__ PositionDiff so main.run's halt gate catches it."""
    from tests.ops.broker.fakes import FakeMCPClient
    from ops import build_guarded_robinhood_broker

    j = Journal(str(tmp_path / "j.sqlite"))
    # Journal is empty → replay cash = 0. FakeMCPClient reports live cash of $500.
    # cash_diff = 500 - 0 = 500, well above the 0.01 epsilon.
    client = FakeMCPClient(cash=Decimal("500"))
    broker = build_guarded_robinhood_broker(
        config=OpsConfig(broker_mode="robinhood"), journal=j,
        mcp_client=client,
        start_of_day_equity=lambda: Decimal("500"),
        start_of_week_equity=lambda: Decimal("500"),
    )
    result = reconcile(journal=j, broker=broker, broker_mode="robinhood")
    cash_diffs = [d for d in result.diffs if d.kind == "cash_drift"]
    assert len(cash_diffs) == 1
    assert cash_diffs[0].symbol == "__CASH__"
    assert cash_diffs[0].broker_qty == Decimal("500")


def test_reconcile_paper_mode_ignores_cash_drift(tmp_path):
    """Paper mode has a structural cash offset (live starting_cash vs
    replay starting_cash=0), so cash_diff is NOT surfaced as a diff."""
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = build_guarded_paper_broker(
        config=OpsConfig(), journal=j,
        quote_source=_quote_source({"AAPL": Decimal("10")}),
        starting_cash=Decimal("500"),
        start_of_day_equity=lambda: Decimal("500"),
        start_of_week_equity=lambda: Decimal("500"),
    )
    result = reconcile(journal=j, broker=broker, broker_mode="paper")
    cash_diffs = [d for d in result.diffs if d.kind == "cash_drift"]
    assert cash_diffs == []
