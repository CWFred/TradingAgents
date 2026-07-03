from datetime import date
from decimal import Decimal

from ops.broker.types import Side, OrderType
from ops.config import OpsConfig
from ops.pipeline_adapter import PipelineDecision, StubPipelineAdapter
from ops.strategy.post_earnings_momentum import PostEarningsMomentumStrategy
from ops.universe import Candidate
from ops.universe.earnings import EarningsHit


def _candidate(sym, price="200"):
    hit = EarningsHit(
        symbol=sym, report_date=date(2026, 6, 30),
        eps_actual=Decimal("1"), eps_estimate=Decimal("0.9"),
        revenue_actual=Decimal("100"), revenue_estimate=Decimal("90"),
        eps_beat=True, revenue_beat=True,
    )
    return Candidate(
        symbol=sym, earnings=hit,
        last_price=Decimal(price), avg_dollar_volume_20d=Decimal("100000000"),
    )


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
    # Per-position cap = 10% of 250 = 25
    assert so.order.notional_dollars == Decimal("25.00")
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
    """If 10% of equity is below the per_trade_dollar_floor, skip the candidate."""
    cfg = OpsConfig()  # per_trade_dollar_floor default = $5; per_position_cap = 10%
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"AAPL": PipelineDecision.BUY})
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("40"),     # 10% = $4, below $5 floor
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
