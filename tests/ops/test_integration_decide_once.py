# tests/ops/test_integration_decide_once.py
"""End-to-end: stub universe + stub pipeline + paper broker + guardian.
Verifies the whole Plan 2 chain wires together correctly."""
from datetime import date
from decimal import Decimal

from ops import build_guarded_paper_broker
from ops.config import OpsConfig
from ops.journal import Journal
from ops.pipeline_adapter import PipelineDecision, StubPipelineAdapter
from ops.position_guardian import PositionGuardian
from ops.strategy.post_earnings_momentum import PostEarningsMomentumStrategy
from ops.universe import Candidate
from ops.universe.earnings import EarningsHit


def _candidate(sym, price="200"):
    return Candidate(
        symbol=sym,
        earnings=EarningsHit(
            symbol=sym, report_date=date(2026, 6, 30),
            eps_actual=Decimal("1"), eps_estimate=Decimal("0.9"),
            revenue_actual=Decimal("100"), revenue_estimate=Decimal("90"),
            eps_beat=True, revenue_beat=True,
        ),
        last_price=Decimal(price),
        avg_dollar_volume_20d=Decimal("100000000"),
    )


def test_full_pass_fill_then_stop(tmp_path):
    cfg = OpsConfig()
    j = Journal(str(tmp_path / "j.sqlite"))
    # mutable quote source so we can move price between fill and guardian pass
    quotes = {"AAPL": Decimal("200"), "MSFT": Decimal("200")}
    guarded = build_guarded_paper_broker(
        config=cfg, journal=j,
        quote_source=lambda s: quotes[s],
        starting_cash=Decimal("250"),
        start_of_day_equity=lambda: Decimal("250"),
        start_of_week_equity=lambda: Decimal("250"),
    )

    # Pipeline: BUY for AAPL, HOLD for MSFT
    pipeline = StubPipelineAdapter({
        "AAPL": PipelineDecision.BUY, "MSFT": PipelineDecision.HOLD,
    })
    strategy = PostEarningsMomentumStrategy(config=cfg)
    proposals = strategy.propose_orders(
        candidates=[_candidate("AAPL"), _candidate("MSFT")],
        pipeline=pipeline,
        current_equity=guarded.get_equity(),
        asof_date=date(2026, 6, 30),
    )
    assert {p.order.symbol for p in proposals} == {"AAPL"}

    # Place orders
    for p in proposals:
        guarded.place_order(p.order)

    assert {pos.symbol for pos in guarded.get_positions()} == {"AAPL"}

    # Move AAPL price down to trip the stop
    quotes["AAPL"] = Decimal("180")     # -10%
    guardian = PositionGuardian(
        broker=guarded, quote_source=lambda s: quotes[s], config=cfg,
    )
    actions = guardian.check_stops_once()
    assert any(a.sold and a.symbol == "AAPL" for a in actions)
    assert guarded.get_positions() == []

    # Journal reflects the full sequence
    fills = j.read_fills()
    sides = [f["side"] for f in fills]
    assert sides == ["BUY", "SELL"]
    stop_events = [e for e in j.read_events() if e["kind"] == "stop_hit"]
    assert len(stop_events) == 1
