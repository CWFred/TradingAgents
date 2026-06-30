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
    # Stop = 200 * (1 + -0.08) = 184
    assert so.order.stop_loss_price == Decimal("184.00")
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
