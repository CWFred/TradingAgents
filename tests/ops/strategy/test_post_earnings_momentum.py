from datetime import date
from decimal import Decimal

from ops.broker.types import Side, OrderType
from ops.config import OpsConfig
from ops.pipeline_adapter import PipelineDecision, StubPipelineAdapter
from ops.strategy.post_earnings_momentum import PostEarningsMomentumStrategy
from ops.universe import Candidate, CandidateSource
from ops.universe.earnings import EarningsHit
from ops.universe.momentum import MomentumHit


def _candidate(sym, price="200"):
    hit = EarningsHit(
        symbol=sym, report_date=date(2026, 6, 30),
        eps_actual=Decimal("1"), eps_estimate=Decimal("0.9"),
        revenue_actual=Decimal("100"), revenue_estimate=Decimal("90"),
        eps_beat=True, revenue_beat=True,
    )
    return Candidate(
        symbol=sym, source=CandidateSource.EARNINGS, earnings=hit,
        last_price=Decimal(price), avg_dollar_volume_20d=Decimal("100000000"),
    )


def _mhit(sym):
    return MomentumHit(
        symbol=sym, asof_date=date(2026, 6, 30),
        trailing_return_6m=Decimal("0.4"), close=Decimal("200"),
        sma_200=Decimal("150"), avg_dollar_volume_20d=Decimal("100000000"),
        rank=1,
    )


def _fake_pipeline_buy():
    return StubPipelineAdapter({"NVDA": PipelineDecision.BUY})


def test_emits_buy_order_for_pipeline_buy():
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"AAPL": PipelineDecision.BUY})
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=date(2026, 6, 30),
    )
    assert len(orders) == 1
    so = orders[0]
    assert so.order.symbol == "AAPL"
    assert so.order.side == Side.BUY
    assert so.order.order_type == OrderType.MARKET
    # Per-position cap = 12% of 250 = 30
    assert so.order.notional_dollars == Decimal("30.00")
    # Entry-relative stop_pct, resolved to an absolute price from the actual
    # fill price at fill time — not from cand.last_price (M2).
    assert so.order.stop_pct == cfg.per_position_stop_pct == Decimal("-0.08")
    assert so.order.client_order_id.startswith("pem-")
    assert so.candidate.symbol == "AAPL"
    assert so.pipeline.decision == PipelineDecision.BUY


def test_skips_non_buy_decisions():
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({
        "AAPL": PipelineDecision.HOLD, "MSFT": PipelineDecision.SELL,
    })
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL"), _candidate("MSFT")], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=date(2026, 6, 30),
    )
    assert orders == []


def test_skips_when_notional_below_floor():
    """If 12% of equity is below the per_trade_dollar_floor, skip the candidate."""
    cfg = OpsConfig()  # per_trade_dollar_floor default = $5; per_position_cap = 12%
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"AAPL": PipelineDecision.BUY})
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("40"),     # 12% = $4.80, below $5 floor
        asof_date=date(2026, 6, 30),
    )
    assert orders == []


def test_client_order_id_is_unique_per_candidate(monkeypatch):
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"AAPL": PipelineDecision.BUY, "MSFT": PipelineDecision.BUY})
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL"), _candidate("MSFT")], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=date(2026, 6, 30),
    )
    cids = {o.order.client_order_id for o in orders}
    assert len(cids) == 2


def test_client_order_id_distinct_across_ticks_for_same_symbol_and_date():
    """M3: the same symbol at the same universe index recurs every 30-minute
    tick on the same trading date (e.g. after a CashReserveRule rejection on
    the prior tick). Two separate propose_orders calls for the same
    symbol/date must NOT produce the same client_order_id — a positional
    index alone collides and journal replay keys on client_order_id (last
    write wins), corrupting replayed cash."""
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"AAPL": PipelineDecision.BUY})
    asof = date(2026, 6, 30)

    first_tick = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=asof,
    )
    second_tick = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=asof,
    )
    id1 = first_tick[0].order.client_order_id
    id2 = second_tick[0].order.client_order_id
    assert id1 != id2
    assert id1.startswith("pem-2026-06-30-AAPL-")
    assert id2.startswith("pem-2026-06-30-AAPL-")


def test_live_max_position_cap_clamps_notional():
    """C1: when live_max_position_cap is set, notional is clamped to it."""
    cfg = OpsConfig()  # 12% cap = $30 at $250 equity
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"AAPL": PipelineDecision.BUY})
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=date(2026, 6, 30),
        live_max_position_cap=Decimal("10"),
    )
    assert len(orders) == 1
    assert orders[0].order.notional_dollars == Decimal("10.00")


def test_live_max_position_cap_no_effect_when_higher():
    """C1: when live_max_position_cap is higher than the normal notional,
    the normal notional is used."""
    cfg = OpsConfig()  # 12% cap = $30 at $250 equity
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"AAPL": PipelineDecision.BUY})
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=date(2026, 6, 30),
        live_max_position_cap=Decimal("100"),
    )
    assert len(orders) == 1
    assert orders[0].order.notional_dollars == Decimal("30.00")


def test_live_max_position_cap_none_uses_normal_notional():
    """C1: when live_max_position_cap is None, normal sizing is used."""
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"AAPL": PipelineDecision.BUY})
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=date(2026, 6, 30),
        live_max_position_cap=None,
    )
    assert len(orders) == 1
    assert orders[0].order.notional_dollars == Decimal("30.00")


def test_decision_sink_receives_every_analyzed_candidate():
    """decision_sink, when passed, collects one entry per analyzed candidate
    (BUY or not) while the returned orders still only contain the BUYs."""
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({
        "AAPL": PipelineDecision.BUY, "MSFT": PipelineDecision.HOLD,
    })
    sink: list = []
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL"), _candidate("MSFT")], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=date(2026, 6, 30),
        decision_sink=sink,
    )
    assert len(orders) == 1
    assert orders[0].order.symbol == "AAPL"
    assert len(sink) == 2
    symbols = {d.candidate.symbol for d in sink}
    assert symbols == {"AAPL", "MSFT"}
    decisions_by_symbol = {d.candidate.symbol: d.pipeline.decision for d in sink}
    assert decisions_by_symbol["AAPL"] == PipelineDecision.BUY
    assert decisions_by_symbol["MSFT"] == PipelineDecision.HOLD


def test_decision_sink_none_by_default_is_unaffected():
    """No sink passed -> existing behavior exactly (no side effects, no
    required list, no attribute errors)."""
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"AAPL": PipelineDecision.BUY})
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=date(2026, 6, 30),
    )
    assert len(orders) == 1


def test_momentum_candidate_gets_momentum_reason():
    cand = Candidate(
        symbol="NVDA", source=CandidateSource.MOMENTUM,
        last_price=Decimal("200"), avg_dollar_volume_20d=Decimal("100000000"),
        momentum=_mhit("NVDA"),
    )
    strategy = PostEarningsMomentumStrategy(config=OpsConfig())
    orders = strategy.propose_orders(
        candidates=[cand], pipeline=_fake_pipeline_buy(),
        current_equity=Decimal("1000"), asof_date=date(2026, 7, 2),
    )
    assert len(orders) == 1
    assert "6-mo momentum leader" in orders[0].reason
    assert "0.4" in orders[0].reason
