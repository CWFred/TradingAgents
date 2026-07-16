from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from ops.universe import yf_pacing

import pytest
from ops.scheduler.orchestrator import Orchestrator
from ops.broker.types import Order, OrderType, Side
from ops.broker.base import OrderRejected, BrokerError
from ops.strategy.base import StrategyOrder
from ops import events
from ops.pipeline_adapter import PipelineDecision, PipelineResult, TIER_STARTER


def _fake_calendar(is_open: bool):
    cal = MagicMock()
    cal.is_open_now.return_value = is_open
    return cal


def _fake_pipeline():
    return MagicMock()


def _fake_strategy(propose_orders_return):
    strat = MagicMock()
    strat.propose_orders.return_value = propose_orders_return
    return strat


def _fake_universe(symbols, seen=None):
    """`seen`, if given, is updated with every kwarg the builder is called
    with (held_symbols, free_slots, excluded_symbols, momentum_leaders, ...)
    so tests can assert on what the orchestrator handed the builder."""
    def side_effect(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        return [MagicMock(symbol=s) for s in symbols]
    return MagicMock(side_effect=side_effect)


def _fake_broker(positions=None, equity=Decimal("1000"), cash=Decimal("500")):
    b = MagicMock()
    b.get_positions.return_value = positions or []
    b.get_equity.return_value = equity
    b.get_cash.return_value = cash
    return b


def _fake_broker_with_positions(symbols):
    return _fake_broker(positions=[MagicMock(symbol=s) for s in symbols])


def _make_journal():
    from ops.journal import Journal
    return Journal(":memory:")


def _make_orchestrator(**overrides):
    from ops.config import OpsConfig
    defaults = dict(
        broker=_fake_broker(),
        universe_builder=_fake_universe([]),
        strategy=_fake_strategy([]),
        pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True),
        journal=_make_journal(),
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    defaults.update(overrides)
    return Orchestrator(**defaults)


def _order(symbol):
    return Order(
        client_order_id=f"b-{symbol}", symbol=symbol, side=Side.BUY,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )


def _pipeline_result(symbol, decision=PipelineDecision.BUY, tier="high", rating="Buy"):
    return PipelineResult(
        symbol=symbol, date=date(2026, 7, 14), decision=decision,
        rating=rating, tier=tier,
    )


def _strategy_order(symbol):
    return StrategyOrder(
        order=_order(symbol),
        reason="test",
        candidate=MagicMock(symbol=symbol),
        pipeline=_pipeline_result(symbol),
    )


def _strategy_order_tiered(symbol, notional="50", tier="high", rating="Buy"):
    order = Order(
        client_order_id=f"b-{symbol}", symbol=symbol, side=Side.BUY,
        notional_dollars=Decimal(notional), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    return StrategyOrder(
        order=order, reason="test",
        candidate=MagicMock(symbol=symbol, momentum=None, source=MagicMock(value="MOMENTUM")),
        pipeline=_pipeline_result(symbol, tier=tier, rating=rating),
    )


def _fake_strategy_with_sink(proposals, decisions):
    """Strategy fake that also fills the decision_sink like the real one."""
    strat = MagicMock()

    def side_effect(*, decision_sink=None, **kwargs):
        if decision_sink is not None:
            decision_sink.extend(decisions)
        return proposals

    strat.propose_orders.side_effect = side_effect
    return strat


def _decision(symbol, decision, rating, tier=""):
    from ops.strategy.base import AnalyzedDecision
    return AnalyzedDecision(
        candidate=MagicMock(symbol=symbol, momentum=None),
        pipeline=_pipeline_result(symbol, decision=decision, tier=tier, rating=rating),
    )


def _events_of(journal, kind):
    import json
    rows = journal._conn.execute(
        "SELECT payload FROM events WHERE kind = ?", (kind,),
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


def test_tick_market_closed_noop(tmp_path):
    from ops.journal import Journal
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    orch = Orchestrator(
        broker=broker, universe_builder=_fake_universe([]),
        strategy=_fake_strategy([]), pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=False), journal=j,
        config=MagicMock(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    orch.tick()
    assert j.read_events() == []
    broker.get_equity.assert_not_called()


def test_tick_brackets_analysis_in_pipeline_session(tmp_path):
    """A tick that reaches the analysis stage must wrap it in the pipeline's
    session() so a managed local model server is torn down when the batch ends."""
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker(positions=[])
    pipeline = _fake_pipeline()
    orch = Orchestrator(
        broker=broker, universe_builder=_fake_universe(["AAPL"]),
        strategy=_fake_strategy([]), pipeline_adapter=pipeline,
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
    )
    orch.tick()
    pipeline.session.assert_called_once()
    pipeline.session.return_value.__enter__.assert_called_once()
    pipeline.session.return_value.__exit__.assert_called_once()


def test_tick_journals_orchestrator_tick_error_on_unexpected_exception(tmp_path):
    """Any unexpected exception from a collaborator is swallowed and journaled."""
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    universe = _fake_universe(["AAPL"])
    universe.side_effect = RuntimeError("boom")
    orch = Orchestrator(
        broker=broker, universe_builder=universe,
        strategy=_fake_strategy([_strategy_order("AAPL")]),
        pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    orch.tick()  # must NOT raise
    events = j.read_events()
    err_events = [e for e in events if e["kind"] == "orchestrator_tick_error"]
    assert len(err_events) == 1
    assert "boom" in err_events[0]["payload"]["error"]


def test_tick_places_buy_when_strategy_proposes_order(tmp_path):
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    orch = Orchestrator(
        broker=broker,
        universe_builder=_fake_universe(["AAPL"]),
        strategy=_fake_strategy([_strategy_order("AAPL")]),
        pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    orch.tick()
    broker.place_order.assert_called_once()
    placed = broker.place_order.call_args.args[0]
    assert placed.symbol == "AAPL"


def test_tick_skips_when_strategy_proposes_nothing(tmp_path):
    """Real Strategy filters non-BUY decisions internally via pipeline.propagate;
    the orchestrator just sees an empty proposal list."""
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    orch = Orchestrator(
        broker=broker,
        universe_builder=_fake_universe(["AAPL"]),
        strategy=_fake_strategy([]),
        pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
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
        strategy=_fake_strategy([_strategy_order("AAPL"), _strategy_order("MSFT")]),
        pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
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
        strategy=_fake_strategy([_strategy_order("AAPL"), _strategy_order("MSFT")]),
        pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
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
        strategy=_fake_strategy([]), pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
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
        strategy=_fake_strategy([_strategy_order("AAPL")]),
        pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    orch.tick()
    universe.assert_not_called()
    broker.place_order.assert_not_called()


def test_tick_passes_held_and_free_slots_to_builder(tmp_path):
    """Orchestrator computes held symbols + remaining slots and hands them
    to the universe builder (belt-and-suspenders: fresh_candidates filter
    still applies afterward)."""
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker(
        positions=[MagicMock(symbol="AAPL"), MagicMock(symbol="MSFT")]
    )
    universe = _fake_universe([])
    orch = Orchestrator(
        broker=broker, universe_builder=universe,
        strategy=_fake_strategy([]), pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    orch.tick()
    kwargs = universe.call_args.kwargs
    assert kwargs["held_symbols"] == frozenset({"AAPL", "MSFT"})
    assert kwargs["free_slots"] == OpsConfig().max_open_positions - 2


def test_tick_shortcircuits_on_weekly_kill_switch(tmp_path):
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("kill_switch", {"reason": "weekly"})
    broker = _fake_broker()
    universe = _fake_universe(["AAPL"])
    orch = Orchestrator(
        broker=broker, universe_builder=universe,
        strategy=_fake_strategy([_strategy_order("AAPL")]),
        pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    orch.tick()
    universe.assert_not_called()
    broker.place_order.assert_not_called()


def _momentum_candidate(symbol, rank):
    from ops.universe import Candidate, CandidateSource
    from ops.universe.momentum import MomentumHit

    hit = MomentumHit(
        symbol=symbol,
        asof_date=date(2026, 7, 5),
        trailing_return_6m=Decimal("0.30"),
        close=Decimal("120"),
        sma_200=Decimal("100"),
        avg_dollar_volume_20d=Decimal("1000000000"),
        rank=rank,
    )
    return Candidate(
        symbol=symbol,
        source=CandidateSource.MOMENTUM,
        last_price=Decimal("120"),
        avg_dollar_volume_20d=Decimal("1000000000"),
        momentum=hit,
    )


def _strategy_order_for_candidate(candidate):
    return StrategyOrder(
        order=_order(candidate.symbol), reason="test",
        candidate=candidate, pipeline=_pipeline_result(candidate.symbol),
    )


def test_tick_journals_position_opened_on_successful_buy(tmp_path):
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    candidate = _momentum_candidate("NVDA", rank=3)
    orch = Orchestrator(
        broker=broker,
        universe_builder=_fake_universe(["NVDA"]),
        strategy=_fake_strategy([_strategy_order_for_candidate(candidate)]),
        pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    orch.tick()
    evts = [e for e in j.read_events() if e["kind"] == "position_opened"]
    assert len(evts) == 1
    p = evts[0]["payload"]
    assert p["symbol"] == "NVDA"
    assert p["source"] == "MOMENTUM"
    assert p["entry_rank"] == 3
    assert p["entry_date"] == datetime.now(timezone.utc).date().isoformat()
    # Legitimate tier value, never a Mock-repr string: json.dumps(default=str)
    # would happily store "<MagicMock ...>" if the helper's pipeline drifted
    # back to a MagicMock.
    assert p["tier"] == "high"


def test_tick_rejected_order_does_not_journal_position_opened(tmp_path):
    from ops.journal import Journal
    from ops.config import OpsConfig
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    broker.place_order.side_effect = OrderRejected("Some", "reason")
    candidate = _momentum_candidate("NVDA", rank=3)
    orch = Orchestrator(
        broker=broker,
        universe_builder=_fake_universe(["NVDA"]),
        strategy=_fake_strategy([_strategy_order_for_candidate(candidate)]),
        pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    orch.tick()
    assert all(e["kind"] != "position_opened" for e in j.read_events())


def _mhit(sym, rank):
    from ops.universe.momentum import MomentumHit
    return MomentumHit(symbol=sym, asof_date=date(2026, 7, 6),
                       trailing_return_6m=Decimal("0.2"),
                       close=Decimal("110"), sma_200=Decimal("100"),
                       avg_dollar_volume_20d=Decimal("100000000"), rank=rank)


def _uptrend_closes():
    from ops.universe.momentum import SMA_WINDOW
    # 201 rising closes: both last closes comfortably above their MAs.
    return [Decimal(100) + Decimal(i) for i in range(SMA_WINDOW + 1)]


def _broker_holding_momentum(sym):
    """Fake broker holding one position; close_position removes it."""
    from datetime import datetime, timezone
    from ops.broker.types import Fill, Position, Side
    broker = MagicMock()
    state = {"positions": [Position(symbol=sym, quantity=Decimal("1"),
                                    avg_entry_price=Decimal("100"))]}
    broker.get_positions.side_effect = lambda: list(state["positions"])
    broker.get_equity.return_value = Decimal("1000")

    def close(symbol, **kwargs):
        state["positions"] = [p for p in state["positions"] if p.symbol != symbol]
        return Fill(order_id="o", client_order_id=f"exit-{symbol}",
                    symbol=symbol, side=Side.SELL, quantity=Decimal("1"),
                    price=Decimal("100"),
                    filled_at=datetime.now(timezone.utc))
    broker.close_position.side_effect = close
    return broker


def _journal_position_opened(journal, sym, source):
    from datetime import datetime, timezone
    from ops import events
    journal.record_event(events.KIND_POSITION_OPENED, events.position_opened_payload(
        symbol=sym, source=source,
        entry_date=datetime.now(timezone.utc).date(), client_order_id="x",
    ))


def _raises(exc):
    def f(*args, **kwargs):
        raise exc
    return f


def test_exit_decision_sells_and_journals_and_frees_slot():
    # Broker holds NVDA (momentum, rank 30 today -> rank_decay).
    # After the exit, the builder must see NVDA gone and one more free slot.
    from ops.config import OpsConfig
    journal = _make_journal()
    seen = {}
    orch = _make_orchestrator(
        broker=_broker_holding_momentum("NVDA"),
        universe_builder=_fake_universe([], seen),
        journal=journal,
        momentum_finder=lambda members, asof_date: [_mhit("NVDA", 30)],
        closes_fetch=lambda s: (_uptrend_closes(), []),
    )
    _journal_position_opened(journal, "NVDA", "MOMENTUM")
    orch.tick()
    kinds = [e["kind"] for e in journal.read_events()]
    assert "exit_decision" in kinds and "exit_order_placed" in kinds
    assert seen["held_symbols"] == frozenset()
    assert seen["free_slots"] == OpsConfig().max_open_positions
    assert seen["momentum_leaders"][0].symbol == "NVDA"  # computed once, passed through


def test_recent_stop_out_is_excluded_from_builder():
    journal = _make_journal()
    journal.record_event("stop_hit", {"symbol": "BURNED"})
    seen = {}
    orch = _make_orchestrator(
        broker=_fake_broker_with_positions([]),
        universe_builder=_fake_universe([], seen), journal=journal,
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    orch.tick()
    assert "BURNED" in seen["excluded_symbols"]


def test_exit_engine_crash_is_journaled_and_buys_proceed():
    journal = _make_journal()
    seen = {}
    orch = _make_orchestrator(
        broker=_fake_broker_with_positions([]),
        universe_builder=_fake_universe([], seen), journal=journal,
        momentum_finder=_raises(RuntimeError("boom")),
        closes_fetch=lambda s: None,
    )
    orch.tick()
    assert any(e["kind"] == "exit_check_error" for e in journal.read_events())
    assert "held_symbols" in seen  # tick reached the builder anyway


# --- Fix 1: once-per-trading-day gate on the leaderboard/exits/entries cycle ---

_MON = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
_TUE = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)


def _pin_journal_clock(monkeypatch, clock):
    """Journal.record_event always stamps `at` with the real wall clock (no
    override param) — see the `_record_fill_at` pattern in
    tests/ops/notify/test_summary.py. Pin it to the same fabricated `clock`
    dict the orchestrator's now_fn reads from, so a fresh event's stored
    `at` lines up with the has_event_today() query the gate performs."""
    import ops.journal as journal_mod
    monkeypatch.setattr(journal_mod, "_now_iso", lambda: clock["now"].isoformat())


def test_daily_cycle_gate_runs_leaderboard_once_per_day(monkeypatch):
    journal = _make_journal()
    clock = {"now": _MON}
    _pin_journal_clock(monkeypatch, clock)
    universe = _fake_universe([])
    orch = _make_orchestrator(
        universe_builder=universe, journal=journal, now_fn=lambda: clock["now"],
    )
    orch.tick()
    orch.tick()
    assert universe.call_count == 1
    cycle_events = [e for e in journal.read_events() if e["kind"] == "daily_cycle_run"]
    assert len(cycle_events) == 1
    assert cycle_events[0]["payload"]["asof_date"] == "2026-07-06"


def test_daily_cycle_gate_is_restart_safe_over_same_journal_same_day(monkeypatch):
    """A fresh Orchestrator instance (simulating a process restart) reading
    the SAME journal on the SAME trading day must still be gated — the gate
    lives in the journal, not in-memory state."""
    journal = _make_journal()
    clock = {"now": _MON}
    _pin_journal_clock(monkeypatch, clock)
    orch1 = _make_orchestrator(
        universe_builder=_fake_universe([]), journal=journal, now_fn=lambda: clock["now"],
    )
    orch1.tick()

    universe2 = _fake_universe([])
    orch2 = _make_orchestrator(
        universe_builder=universe2, journal=journal, now_fn=lambda: clock["now"],
    )
    orch2.tick()

    universe2.assert_not_called()
    cycle_events = [e for e in journal.read_events() if e["kind"] == "daily_cycle_run"]
    assert len(cycle_events) == 1


def test_daily_cycle_gate_runs_again_on_the_next_trading_day(monkeypatch):
    journal = _make_journal()
    clock = {"now": _MON}
    _pin_journal_clock(monkeypatch, clock)
    universe = _fake_universe([])
    orch = _make_orchestrator(
        universe_builder=universe, journal=journal, now_fn=lambda: clock["now"],
    )
    orch.tick()
    clock["now"] = _TUE
    orch.tick()
    assert universe.call_count == 2
    cycle_events = [e for e in journal.read_events() if e["kind"] == "daily_cycle_run"]
    assert len(cycle_events) == 2


# --- Daily cycle retry-on-failure (up to MAX_DAILY_CYCLE_ATTEMPTS/day) ---

def test_daily_cycle_completed_recorded_on_clean_run_and_gates_second_tick(monkeypatch):
    """A clean cycle records both daily_cycle_run and daily_cycle_completed;
    a SECOND same-day tick returns early — the pipeline/strategy is not
    invoked again."""
    from ops import events

    journal = _make_journal()
    clock = {"now": _MON}
    _pin_journal_clock(monkeypatch, clock)
    universe = _fake_universe([])
    strategy = _fake_strategy([])
    orch = _make_orchestrator(
        universe_builder=universe, strategy=strategy, journal=journal,
        now_fn=lambda: clock["now"],
    )
    orch.tick()
    kinds = [e["kind"] for e in journal.read_events()]
    assert kinds.count(events.KIND_DAILY_CYCLE_RUN) == 1
    assert kinds.count(events.KIND_DAILY_CYCLE_COMPLETED) == 1

    orch.tick()
    assert universe.call_count == 1
    assert strategy.propose_orders.call_count == 1
    kinds = [e["kind"] for e in journal.read_events()]
    assert kinds.count(events.KIND_DAILY_CYCLE_RUN) == 1
    assert kinds.count(events.KIND_DAILY_CYCLE_COMPLETED) == 1


def test_daily_cycle_retries_next_tick_after_failure(monkeypatch):
    """A cycle that raises mid-analysis (the ds4-unreachable case) is
    journaled as orchestrator_tick_error with NO daily_cycle_completed — the
    next same-day tick must retry the cycle (attempts now 2)."""
    from ops import events

    journal = _make_journal()
    clock = {"now": _MON}
    _pin_journal_clock(monkeypatch, clock)
    universe = _fake_universe([])
    strategy = _fake_strategy([])
    strategy.propose_orders.side_effect = RuntimeError("ds4 unreachable")
    orch = _make_orchestrator(
        universe_builder=universe, strategy=strategy, journal=journal,
        now_fn=lambda: clock["now"],
    )
    orch.tick()
    kinds = [e["kind"] for e in journal.read_events()]
    assert kinds.count(events.KIND_DAILY_CYCLE_RUN) == 1
    assert events.KIND_ORCHESTRATOR_TICK_ERROR in kinds
    assert events.KIND_DAILY_CYCLE_COMPLETED not in kinds

    orch.tick()
    assert universe.call_count == 2
    assert strategy.propose_orders.call_count == 2
    kinds = [e["kind"] for e in journal.read_events()]
    assert kinds.count(events.KIND_DAILY_CYCLE_RUN) == 2
    assert events.KIND_DAILY_CYCLE_COMPLETED not in kinds


def test_daily_cycle_attempt_cap_stops_retrying_after_max_attempts(monkeypatch):
    """After MAX_DAILY_CYCLE_ATTEMPTS (3) failed attempts, the 4th same-day
    tick returns early — no 4th attempt, so a persistently-crashing cycle
    can't burn the analysis budget all day."""
    from ops import events
    from ops.scheduler.orchestrator import MAX_DAILY_CYCLE_ATTEMPTS

    journal = _make_journal()
    clock = {"now": _MON}
    _pin_journal_clock(monkeypatch, clock)
    universe = _fake_universe([])
    strategy = _fake_strategy([])
    strategy.propose_orders.side_effect = RuntimeError("boom")
    orch = _make_orchestrator(
        universe_builder=universe, strategy=strategy, journal=journal,
        now_fn=lambda: clock["now"],
    )
    for _ in range(MAX_DAILY_CYCLE_ATTEMPTS):
        orch.tick()
    assert universe.call_count == MAX_DAILY_CYCLE_ATTEMPTS
    assert strategy.propose_orders.call_count == MAX_DAILY_CYCLE_ATTEMPTS
    kinds = [e["kind"] for e in journal.read_events()]
    assert kinds.count(events.KIND_DAILY_CYCLE_RUN) == MAX_DAILY_CYCLE_ATTEMPTS

    orch.tick()  # 4th tick: cap reached, no new attempt
    assert universe.call_count == MAX_DAILY_CYCLE_ATTEMPTS
    assert strategy.propose_orders.call_count == MAX_DAILY_CYCLE_ATTEMPTS
    kinds = [e["kind"] for e in journal.read_events()]
    assert kinds.count(events.KIND_DAILY_CYCLE_RUN) == MAX_DAILY_CYCLE_ATTEMPTS


# --- Fix 2: unknown-provenance positions are journaled for audit ---

def test_exit_engine_journals_unknown_provenance_for_position_with_no_entry_event():
    journal = _make_journal()
    orch = _make_orchestrator(
        broker=_broker_holding_momentum("OLDPOS"),
        universe_builder=_fake_universe([]),
        journal=journal,
        momentum_finder=lambda members, asof_date: [_mhit("OLDPOS", 5)],
        closes_fetch=lambda s: (_uptrend_closes(), []),
    )
    # No position_opened event was ever journaled for OLDPOS.
    orch.tick()
    unknown = [
        e for e in journal.read_events() if e["kind"] == "exit_unknown_provenance"
    ]
    assert len(unknown) == 1
    assert unknown[0]["payload"]["symbol"] == "OLDPOS"


# --- Fix 3: cooldown window boundary is the ET trading-day start ---

def test_cooldown_boundary_uses_et_trading_day_start_not_utc_midnight():
    """since_date for a 1-day cooldown as-of Wed 2026-07-08 is Tue 2026-07-07.
    ET midnight of that Tuesday is 2026-07-07T04:00:00Z (EDT, UTC-4). A
    stop_hit journaled at 2026-07-07T02:00:00Z actually happened at ET
    2026-07-06 22:00 — the PREVIOUS trading day — and must be excluded. The
    old UTC-midnight boundary (2026-07-07T00:00:00Z) would have wrongly
    included it."""
    import json
    from ops.config import OpsConfig

    journal = _make_journal()
    journal._conn.execute(
        "INSERT INTO events (at, kind, payload) VALUES (?, ?, ?)",
        ("2026-07-07T02:00:00+00:00", "stop_hit", json.dumps({"symbol": "OLDBUG"})),
    )
    seen = {}
    orch = _make_orchestrator(
        broker=_fake_broker_with_positions([]),
        universe_builder=_fake_universe([], seen),
        journal=journal,
        config=OpsConfig(stopout_reentry_cooldown_days=1),
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
        now_fn=lambda: datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc),
    )
    orch.tick()
    assert "OLDBUG" not in seen["excluded_symbols"]


# --- Fix 4: guardian/exit race is a breadcrumb, not an emailed error ---

def _broker_holding_with_close_raising(sym, exc):
    from ops.broker.types import Position
    broker = MagicMock()
    broker.get_positions.return_value = [
        Position(symbol=sym, quantity=Decimal("1"), avg_entry_price=Decimal("100"))
    ]
    broker.get_equity.return_value = Decimal("1000")
    broker.close_position.side_effect = exc
    return broker


def test_exit_close_position_no_such_position_journals_skip_not_check_error():
    from ops.broker.base import NoSuchPosition

    journal = _make_journal()
    _journal_position_opened(journal, "NVDA", "MOMENTUM")
    broker = _broker_holding_with_close_raising("NVDA", NoSuchPosition("gone"))
    orch = _make_orchestrator(
        broker=broker,
        universe_builder=_fake_universe([]),
        journal=journal,
        momentum_finder=lambda members, asof_date: [_mhit("NVDA", 30)],
        closes_fetch=lambda s: (_uptrend_closes(), []),
    )
    orch.tick()  # must not raise
    kinds = [e["kind"] for e in journal.read_events()]
    assert "exit_check_error" not in kinds
    skips = [
        e for e in journal.read_events()
        if e["kind"] == "exit_skipped_missing_data" and e["payload"]["symbol"] == "NVDA"
    ]
    assert len(skips) == 1
    assert "guardian race" in skips[0]["payload"]["reason"]


# --- Task 3: Universe diagnostics + blindness alarm ---

def _seed_pacing_failures(n: int) -> None:
    from ops.universe import yf_pacing

    for _ in range(n):
        yf_pacing._count("earnings", ok=False)


def test_daily_cycle_emits_universe_diagnostics(monkeypatch):
    from ops import events

    journal = _make_journal()
    clock = {"now": _MON}
    _pin_journal_clock(monkeypatch, clock)

    def blind_universe_builder(**kwargs):
        _seed_pacing_failures(10)
        return []

    orch = _make_orchestrator(
        universe_builder=MagicMock(side_effect=blind_universe_builder),
        journal=journal, now_fn=lambda: clock["now"],
    )
    orch.tick()
    kinds = [e["kind"] for e in journal.read_events()]
    assert events.KIND_UNIVERSE_DIAGNOSTICS in kinds
    assert events.KIND_UNIVERSE_BLIND in kinds  # 0 candidates, 100% failures


def test_blind_alarm_fires_on_majority_failures_even_with_candidates():
    """A 96%-failed sweep that still scrapes together 2 candidates is a blind
    day: the alarm must not be gated on candidate_count == 0."""
    from ops import events

    journal = _make_journal()
    orch = _make_orchestrator(journal=journal)
    yf_pacing.snapshot_and_reset()  # clear leakage from other tests
    _seed_pacing_failures(48)
    yf_pacing._count("earnings", ok=True)
    yf_pacing._count("earnings", ok=True)
    orch._emit_universe_diagnostics(date(2026, 7, 6), candidate_count=2)
    kinds = [e["kind"] for e in journal.read_events()]
    assert events.KIND_UNIVERSE_BLIND in kinds


# --- Task 1: analysis_decision journaled for every analyzed name ---

def _fake_strategy_recording_decisions(decisions_by_symbol):
    """Mimics the real strategy's decision_sink contract closely enough for
    orchestrator tests: for every candidate handed to propose_orders, looks
    up its decision, appends an AnalyzedDecision to decision_sink (when
    given one), and returns a StrategyOrder only for the BUYs."""
    from ops.pipeline_adapter import PipelineDecision, PipelineResult
    from ops.strategy.base import AnalyzedDecision

    def propose_orders(*, candidates, pipeline, current_equity, asof_date,
                        live_max_position_cap=None, decision_sink=None):
        out = []
        for cand in candidates:
            decision = decisions_by_symbol.get(cand.symbol, PipelineDecision.HOLD)
            result = PipelineResult(symbol=cand.symbol, date=asof_date, decision=decision)
            if decision_sink is not None:
                decision_sink.append(AnalyzedDecision(candidate=cand, pipeline=result))
            if decision == PipelineDecision.BUY:
                out.append(_strategy_order_for_candidate(cand))
        return out

    strat = MagicMock()
    strat.propose_orders.side_effect = propose_orders
    return strat


def test_tick_journals_analysis_decision_for_every_analyzed_name(tmp_path):
    from ops import events
    from ops.journal import Journal
    from ops.config import OpsConfig
    from ops.pipeline_adapter import PipelineDecision

    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    nvda = _momentum_candidate("NVDA", rank=1)
    aapl = _momentum_candidate("AAPL", rank=2)
    strat = _fake_strategy_recording_decisions(
        {"NVDA": PipelineDecision.BUY, "AAPL": PipelineDecision.HOLD},
    )
    universe = MagicMock(side_effect=lambda **kwargs: [nvda, aapl])
    orch = Orchestrator(
        broker=broker,
        universe_builder=universe,
        strategy=strat,
        pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    orch.tick()
    evts = [e for e in j.read_events() if e["kind"] == events.KIND_ANALYSIS_DECISION]
    assert len(evts) == 2
    by_symbol = {e["payload"]["symbol"]: e["payload"] for e in evts}
    assert by_symbol["NVDA"]["decision"] == "BUY"
    assert by_symbol["NVDA"]["source"] == "MOMENTUM"
    assert by_symbol["NVDA"]["rank"] == 1
    assert by_symbol["AAPL"]["decision"] == "HOLD"
    assert by_symbol["AAPL"]["rank"] == 2
    # BUY still resulted in exactly one order placed.
    broker.place_order.assert_called_once()


def test_tick_no_analysis_decision_when_no_candidates(tmp_path):
    """Empty candidate list -> nothing analyzed -> no analysis_decision events."""
    from ops import events
    from ops.journal import Journal
    from ops.config import OpsConfig

    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _fake_broker()
    orch = Orchestrator(
        broker=broker,
        universe_builder=_fake_universe([]),
        strategy=_fake_strategy([]),
        pipeline_adapter=_fake_pipeline(),
        calendar=_fake_calendar(is_open=True), journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    orch.tick()
    assert all(e["kind"] != events.KIND_ANALYSIS_DECISION for e in j.read_events())


def test_no_blind_alarm_when_feed_healthy(monkeypatch):
    from ops import events

    journal = _make_journal()
    clock = {"now": _MON}
    _pin_journal_clock(monkeypatch, clock)

    def healthy_universe_builder(**kwargs):
        _seed_pacing_failures(0)  # no failures
        yf_pacing._count("earnings", ok=True)  # healthy fetches
        return []

    orch = _make_orchestrator(
        universe_builder=MagicMock(side_effect=healthy_universe_builder),
        journal=journal, now_fn=lambda: clock["now"],
    )
    orch.tick()
    kinds = [e["kind"] for e in journal.read_events()]
    assert events.KIND_UNIVERSE_DIAGNOSTICS in kinds
    assert events.KIND_UNIVERSE_BLIND not in kinds


# --- Task 7: orchestrator executes the displacement plan + underweight
# trims, journals rating/tier ---

def test_analysis_decision_journals_native_rating(tmp_path):
    journal = _make_journal()
    strat = _fake_strategy_with_sink(
        [], [_decision("AAPL", PipelineDecision.HOLD, rating="Overweight")],
    )
    orch = _make_orchestrator(journal=journal, strategy=strat)
    orch.tick()
    payloads = _events_of(journal, events.KIND_ANALYSIS_DECISION)
    assert payloads and payloads[0]["rating"] == "Overweight"


def test_position_opened_carries_tier(tmp_path):
    journal = _make_journal()
    so = _strategy_order_tiered("AAPL", tier=TIER_STARTER, rating="Overweight")
    strat = _fake_strategy_with_sink([so], [])
    orch = _make_orchestrator(journal=journal, strategy=strat)
    orch.tick()
    payloads = _events_of(journal, events.KIND_POSITION_OPENED)
    assert payloads and payloads[0]["tier"] == TIER_STARTER


def test_displacement_trim_executed_and_journaled(tmp_path):
    from ops.config import OpsConfig
    journal = _make_journal()
    # Aged starter on the books: provenance via a prior position_opened event.
    journal.record_event(
        events.KIND_POSITION_OPENED,
        events.position_opened_payload(
            symbol="OLDS", source="MOMENTUM", entry_date=date(2026, 7, 1),
            client_order_id="old", tier="starter",
        ),
    )
    starter_pos = MagicMock(symbol="OLDS")
    starter_pos.market_value.return_value = Decimal("600")
    broker = _fake_broker(
        positions=[starter_pos], equity=Decimal("10000"), cash=Decimal("1600"),
    )  # available = 0 -> full shortfall
    broker.get_quote.return_value = Decimal("10")
    so = _strategy_order_tiered("NEWB", notional="500", tier="high")
    strat = _fake_strategy_with_sink([so], [])
    orch = _make_orchestrator(
        journal=journal, strategy=strat, broker=broker, config=OpsConfig(),
    )
    orch.tick()
    trims = _events_of(journal, events.KIND_DISPLACEMENT_TRIM)
    assert len(trims) == 1
    assert trims[0]["symbol"] == "OLDS"
    assert trims[0]["funded_symbol"] == "NEWB"
    # SELL for the trim and BUY for the entry both placed
    sides = [c.args[0].side for c in broker.place_order.call_args_list]
    assert Side.SELL in sides and Side.BUY in sides


def test_unfunded_buy_is_skipped_and_journaled(tmp_path):
    from ops.config import OpsConfig
    journal = _make_journal()
    broker = _fake_broker(equity=Decimal("10000"), cash=Decimal("1600"))
    so = _strategy_order_tiered("NEWB", notional="500", tier="high")
    strat = _fake_strategy_with_sink([so], [])
    orch = _make_orchestrator(
        journal=journal, strategy=strat, broker=broker, config=OpsConfig(),
    )
    orch.tick()
    skips = _events_of(journal, events.KIND_ENTRY_SKIPPED_UNFUNDED)
    assert skips and skips[0]["symbol"] == "NEWB"
    assert broker.place_order.call_count == 0
    assert _events_of(journal, events.KIND_POSITION_OPENED) == []


def test_full_exit_displacement_trim_uses_close_position(tmp_path):
    # I1: when the planned trim consumes the starter's entire remaining
    # value, the orchestrator must close the position outright
    # (broker.close_position) rather than place a notional SELL that can
    # leave dust shares behind and strand the symbol as "held" forever.
    from ops.broker.types import Fill
    from ops.config import OpsConfig
    journal = _make_journal()
    journal.record_event(
        events.KIND_POSITION_OPENED,
        events.position_opened_payload(
            symbol="OLDS", source="MOMENTUM", entry_date=date(2026, 7, 1),
            client_order_id="old", tier="starter",
        ),
    )
    starter_pos = MagicMock(symbol="OLDS")
    starter_pos.market_value.return_value = Decimal("500")
    broker = _fake_broker(
        positions=[starter_pos], equity=Decimal("10000"), cash=Decimal("1600"),
    )  # available = 0 -> shortfall 500 == starter's entire value -> full exit
    broker.get_quote.return_value = Decimal("10")
    close_fill = Fill(
        order_id="o1", client_order_id="close-OLDS-abc", symbol="OLDS",
        side=Side.SELL, quantity=Decimal("50"), price=Decimal("10"),
        filled_at=datetime.now(timezone.utc),
    )
    broker.close_position.return_value = close_fill
    so = _strategy_order_tiered("NEWB", notional="500", tier="high")
    strat = _fake_strategy_with_sink([so], [])
    orch = _make_orchestrator(
        journal=journal, strategy=strat, broker=broker, config=OpsConfig(),
    )
    orch.tick()
    broker.close_position.assert_called_once_with("OLDS")
    # No notional SELL was placed for the trim leg -- only the funded BUY
    # went through place_order.
    placed_symbols = [c.args[0].symbol for c in broker.place_order.call_args_list]
    assert "OLDS" not in placed_symbols
    assert "NEWB" in placed_symbols
    trims = _events_of(journal, events.KIND_DISPLACEMENT_TRIM)
    assert len(trims) == 1
    assert trims[0]["symbol"] == "OLDS"
    assert trims[0]["funded_symbol"] == "NEWB"
    # Journaled notional is the fill's ACTUAL notional (quantity * price),
    # not the planner's pre-fill estimate.
    assert trims[0]["notional"] == "500.00"


def test_trim_broker_error_only_unfunds_that_proposal(tmp_path):
    # I2: a BrokerError placing one trim must not silently cancel every
    # other entry this tick -- only the proposal that trim was funding
    # gets skipped; a cash-funded buy needing no trims must still place.
    from ops.config import OpsConfig
    journal = _make_journal()
    journal.record_event(
        events.KIND_POSITION_OPENED,
        events.position_opened_payload(
            symbol="OLDS", source="MOMENTUM", entry_date=date(2026, 7, 1),
            client_order_id="old", tier="starter",
        ),
    )
    starter_pos = MagicMock(symbol="OLDS")
    starter_pos.market_value.return_value = Decimal("1000")
    broker = _fake_broker(
        positions=[starter_pos], equity=Decimal("10000"), cash=Decimal("1800"),
    )  # available = 200
    broker.get_quote.return_value = Decimal("10")

    def place_order_side_effect(order):
        if order.symbol == "OLDS":
            raise BrokerError("boom")
        return MagicMock()

    broker.place_order.side_effect = place_order_side_effect

    cash_order = _strategy_order_tiered("CASHY", notional="100", tier="high")
    trim_order = _strategy_order_tiered("TRIMY", notional="500", tier="high")
    strat = _fake_strategy_with_sink([cash_order, trim_order], [])
    orch = _make_orchestrator(
        journal=journal, strategy=strat, broker=broker, config=OpsConfig(),
    )
    orch.tick()

    placed_symbols = [c.args[0].symbol for c in broker.place_order.call_args_list]
    assert "CASHY" in placed_symbols  # cash-funded buy still places
    assert "TRIMY" not in placed_symbols  # trim-funded buy does NOT place

    skips = _events_of(journal, events.KIND_ENTRY_SKIPPED_UNFUNDED)
    assert len(skips) == 1
    assert skips[0]["symbol"] == "TRIMY"
    assert skips[0]["reason"] == "displacement trim OLDS failed: BrokerError"


def test_underweight_trim_sells_half_of_held_position(tmp_path):
    journal = _make_journal()
    pos = MagicMock(symbol="AAPL")
    pos.market_value.return_value = Decimal("1200")
    broker = _fake_broker(
        positions=[pos], equity=Decimal("10000"), cash=Decimal("5000"),
    )
    broker.get_quote.return_value = Decimal("120")
    strat = _fake_strategy_with_sink(
        [], [_decision("AAPL", PipelineDecision.TRIM, rating="Underweight")],
    )
    orch = _make_orchestrator(journal=journal, strategy=strat, broker=broker)
    orch.tick()
    trims = _events_of(journal, events.KIND_UNDERWEIGHT_TRIM)
    assert trims and trims[0]["symbol"] == "AAPL"
    assert trims[0]["notional"] == "600.00"
    sell = broker.place_order.call_args_list[0].args[0]
    assert sell.side == Side.SELL and sell.notional_dollars == Decimal("600.00")


def test_underweight_trim_ignored_when_not_held(tmp_path):
    journal = _make_journal()
    broker = _fake_broker(positions=[])
    strat = _fake_strategy_with_sink(
        [], [_decision("GHOST", PipelineDecision.TRIM, rating="Underweight")],
    )
    orch = _make_orchestrator(journal=journal, strategy=strat, broker=broker)
    orch.tick()
    assert _events_of(journal, events.KIND_UNDERWEIGHT_TRIM) == []
    assert broker.place_order.call_count == 0
