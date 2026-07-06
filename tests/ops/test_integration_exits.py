"""End-to-end momentum lifecycle: buy on day 1; on day 2 the rank-decay exit
sells and the freed slot is refilled the SAME tick. Real composite builder,
exit engine, strategy, guarded paper broker, and journal; yfinance, pipeline,
and calendar are faked (no network, no LLM calls)."""
import functools
from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from ops import build_guarded_paper_broker
from ops.config import OpsConfig
from ops.journal import Journal
from ops.pipeline_adapter import PipelineDecision, PipelineResult
from ops.scheduler.orchestrator import Orchestrator
from ops.strategy.post_earnings_momentum import PostEarningsMomentumStrategy
from ops.universe.composite import build_composite_universe
from ops.universe.momentum import SMA_WINDOW, MomentumHit

_QUOTES = {"NVDA": Decimal("100"), "AMD": Decimal("100")}
_UPTREND = [Decimal(50) + Decimal(i) for i in range(SMA_WINDOW + 1)]


class _AlwaysBuyPipeline:
    def propagate(self, symbol, asof_date):
        return PipelineResult(symbol=symbol, date=asof_date,
                              decision=PipelineDecision.BUY, raw={})

    def session(self):
        return nullcontext()


def test_momentum_lifecycle_buy_then_rank_decay_sell_then_refill(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    cfg = OpsConfig(max_open_positions=1)
    broker = build_guarded_paper_broker(
        config=cfg, journal=j, quote_source=_QUOTES.__getitem__,
        starting_cash=Decimal("1000"),
        start_of_day_equity=lambda: Decimal("1000"),
        start_of_week_equity=lambda: Decimal("1000"),
    )
    calendar = MagicMock()
    calendar.is_open_now.return_value = True

    board = {"ranks": [("NVDA", 1), ("AMD", 2)]}

    def momentum_finder(members, asof_date):
        return [
            MomentumHit(symbol=sym, asof_date=asof_date,
                        trailing_return_6m=Decimal("1") / Decimal(rank),
                        close=Decimal("100"), sma_200=Decimal("80"),
                        avg_dollar_volume_20d=Decimal("100000000"), rank=rank)
            for sym, rank in board["ranks"]
        ]

    universe_builder = functools.partial(
        build_composite_universe,
        members_loader=lambda: ["NVDA", "AMD"],
        earnings_finder=lambda syms, asof_date, lookback_days, fetch=None: [],
        metrics_fetcher=lambda sym: (Decimal("100"), Decimal("100000000")),
    )
    # The leaderboard/exits/entries cycle now runs only once per trading day
    # (daily_cycle_run gate), so the two ticks must land on different
    # trading days — inject a controllable clock.
    clock = {"now": datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)}  # Mon
    orch = Orchestrator(
        broker=broker, universe_builder=universe_builder,
        strategy=PostEarningsMomentumStrategy(config=cfg),
        pipeline_adapter=_AlwaysBuyPipeline(), calendar=calendar,
        journal=j, config=cfg,
        members_loader=lambda: ["NVDA", "AMD"],
        momentum_finder=momentum_finder,
        closes_fetch=lambda s: (_UPTREND, [Decimal("1000000")] * len(_UPTREND)),
        now_fn=lambda: clock["now"],
    )

    orch.tick()  # day 1: NVDA is rank 1, one free slot -> bought
    assert {p.symbol for p in broker.get_positions()} == {"NVDA"}

    # day 2: NVDA decays to rank 30 (> exit rank 25, uptrend intact so no
    # trend_break); AMD is the new leader.
    board["ranks"] = [("AMD", 1), ("NVDA", 30)]
    clock["now"] = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)  # Tue
    orch.tick()
    assert {p.symbol for p in broker.get_positions()} == {"AMD"}

    kinds = [e["kind"] for e in j.read_events()]
    assert kinds.count("position_opened") == 2   # NVDA day 1, AMD day 2
    decisions = [e for e in j.read_events() if e["kind"] == "exit_decision"]
    assert [d["payload"]["symbol"] for d in decisions] == ["NVDA"]
    assert decisions[0]["payload"]["rule"] == "rank_decay"
    assert "exit_order_placed" in kinds
