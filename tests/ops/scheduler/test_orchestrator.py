from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from ops.scheduler.orchestrator import Orchestrator
from ops.broker.types import Order, OrderType, Side
from ops.broker.base import OrderRejected, BrokerError


def _fake_calendar(is_open: bool):
    cal = MagicMock()
    cal.is_open_now.return_value = is_open
    return cal


def _fake_pipeline(decision_by_symbol: dict[str, str]):
    pa = MagicMock()
    def _propagate(symbol, d):
        result = MagicMock()
        result.action = decision_by_symbol.get(symbol, "HOLD")
        return result
    pa.propagate.side_effect = _propagate
    return pa


def _fake_strategy(candidates, buy_order_by_symbol):
    strat = MagicMock()
    strat.rank.return_value = candidates
    def _build(candidate, decision):
        return buy_order_by_symbol[candidate.symbol]
    strat.build_order.side_effect = _build
    return strat


def _fake_universe(symbols):
    ub = MagicMock()
    ub.build.return_value = set(symbols)
    return ub


def _fake_broker(positions=None, equity=Decimal("1000"), cash=Decimal("500")):
    b = MagicMock()
    b.get_positions.return_value = positions or []
    b.get_equity.return_value = equity
    b.get_cash.return_value = cash
    return b


def _order(symbol):
    return Order(
        client_order_id=f"b-{symbol}", symbol=symbol, side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("9.2"),
    )


def test_tick_market_closed_noop(tmp_path):
    from ops.journal import Journal
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    orch = Orchestrator(
        broker=broker, universe_builder=_fake_universe([]),
        strategy=_fake_strategy([], {}), pipeline_adapter=_fake_pipeline({}),
        calendar=_fake_calendar(is_open=False), journal=j,
        config=MagicMock(),
    )
    orch.tick()
    assert j.read_events() == []
    broker.get_equity.assert_not_called()


def test_tick_journals_orchestrator_tick_error_on_unexpected_exception(tmp_path):
    """Any unexpected exception from a collaborator is swallowed and journaled."""
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    universe = _fake_universe(["AAPL"])
    universe.build.side_effect = RuntimeError("boom")
    orch = Orchestrator(
        broker=broker, universe_builder=universe,
        strategy=_fake_strategy([MagicMock(symbol="AAPL")], {"AAPL": _order("AAPL")}),
        pipeline_adapter=_fake_pipeline({"AAPL": "BUY"}),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
    )
    orch.tick()  # must NOT raise
    events = j.read_events()
    err_events = [e for e in events if e["kind"] == "orchestrator_tick_error"]
    assert len(err_events) == 1
    assert "boom" in err_events[0]["payload"]["error"]


def test_tick_places_buy_when_pipeline_says_buy(tmp_path):
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    orch = Orchestrator(
        broker=broker,
        universe_builder=_fake_universe(["AAPL"]),
        strategy=_fake_strategy([MagicMock(symbol="AAPL")], {"AAPL": _order("AAPL")}),
        pipeline_adapter=_fake_pipeline({"AAPL": "BUY"}),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
    )
    orch.tick()
    broker.place_order.assert_called_once()
    placed = broker.place_order.call_args.args[0]
    assert placed.symbol == "AAPL"


def test_tick_skips_hold(tmp_path):
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    orch = Orchestrator(
        broker=broker,
        universe_builder=_fake_universe(["AAPL"]),
        strategy=_fake_strategy([MagicMock(symbol="AAPL")], {"AAPL": _order("AAPL")}),
        pipeline_adapter=_fake_pipeline({"AAPL": "HOLD"}),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
    )
    orch.tick()
    broker.place_order.assert_not_called()


def test_tick_continues_after_rule_reject(tmp_path):
    """OrderRejected on one candidate → next candidate still tried."""
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    broker.place_order.side_effect = [OrderRejected("Some", "reason"), MagicMock()]
    orch = Orchestrator(
        broker=broker,
        universe_builder=_fake_universe(["AAPL", "MSFT"]),
        strategy=_fake_strategy(
            [MagicMock(symbol="AAPL"), MagicMock(symbol="MSFT")],
            {"AAPL": _order("AAPL"), "MSFT": _order("MSFT")},
        ),
        pipeline_adapter=_fake_pipeline({"AAPL": "BUY", "MSFT": "BUY"}),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
    )
    orch.tick()
    assert broker.place_order.call_count == 2


def test_tick_breaks_on_broker_error(tmp_path):
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    broker.place_order.side_effect = BrokerError("mcp died")
    orch = Orchestrator(
        broker=broker,
        universe_builder=_fake_universe(["AAPL", "MSFT"]),
        strategy=_fake_strategy(
            [MagicMock(symbol="AAPL"), MagicMock(symbol="MSFT")],
            {"AAPL": _order("AAPL"), "MSFT": _order("MSFT")},
        ),
        pipeline_adapter=_fake_pipeline({"AAPL": "BUY", "MSFT": "BUY"}),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
    )
    orch.tick()
    assert broker.place_order.call_count == 1


def test_maybe_snapshot_equity_writes_open_day_once(tmp_path):
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker(equity=Decimal("1000"), cash=Decimal("500"))
    orch = Orchestrator(
        broker=broker, universe_builder=_fake_universe([]),
        strategy=_fake_strategy([], {}), pipeline_adapter=_fake_pipeline({}),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
    )
    orch.tick()
    orch.tick()
    open_day_snaps = [
        s for s in j._conn.execute(
            "SELECT kind FROM equity_snapshots"
        ).fetchall() if s[0] == "open_day"
    ]
    assert len(open_day_snaps) == 1


def test_tick_shortcircuits_on_daily_halt(tmp_path):
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("daily_halt", {"reason": "drawdown"})
    broker = _fake_broker()
    universe = _fake_universe(["AAPL"])
    orch = Orchestrator(
        broker=broker, universe_builder=universe,
        strategy=_fake_strategy([MagicMock(symbol="AAPL")], {"AAPL": _order("AAPL")}),
        pipeline_adapter=_fake_pipeline({"AAPL": "BUY"}),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
    )
    orch.tick()
    universe.build.assert_not_called()
    broker.place_order.assert_not_called()


def test_tick_shortcircuits_on_weekly_kill_switch(tmp_path):
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("kill_switch", {"reason": "weekly"})
    broker = _fake_broker()
    universe = _fake_universe(["AAPL"])
    orch = Orchestrator(
        broker=broker, universe_builder=universe,
        strategy=_fake_strategy([MagicMock(symbol="AAPL")], {"AAPL": _order("AAPL")}),
        pipeline_adapter=_fake_pipeline({"AAPL": "BUY"}),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
    )
    orch.tick()
    universe.build.assert_not_called()
    broker.place_order.assert_not_called()
