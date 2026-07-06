"""End-to-end: BUY through orchestrator → simulated restart → simulated price drop
→ stop fires via guardian → close_position.

Verifies stop_loss_price persists across a simulated restart (from_journal rehydrate)
and that a real guardian stop-check against a real (guarded) paper broker closes the
position and journals a stop_hit event. Universe/strategy/pipeline are all mocked so
this test costs no LLM calls and has no market-open dependency; the broker, guardrail
engine, journal, and guardian are the real production objects."""
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

from ops import build_guarded_paper_broker
from ops.broker.paper import PaperBroker
from ops.broker.types import Order, OrderType, Side
from ops.config import OpsConfig
from ops.journal import Journal
from ops.pipeline_adapter import PipelineDecision, PipelineResult
from ops.position_guardian import PositionGuardian
from ops.scheduler.orchestrator import Orchestrator
from ops.strategy.base import StrategyOrder


class _Q:
    def __init__(self): self._m = {}
    def set(self, s, p): self._m[s] = p
    def get(self, s): return self._m[s]


def test_end_to_end_orchestrator_buy_then_guardian_stop_survives_restart(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    quotes = _Q()
    quotes.set("AAPL", Decimal("10"))
    broker = build_guarded_paper_broker(
        config=OpsConfig(), journal=j,
        quote_source=quotes.get, starting_cash=Decimal("250"),
        start_of_day_equity=lambda: Decimal("250"),
        start_of_week_equity=lambda: Decimal("250"),
    )
    calendar = MagicMock()
    calendar.is_open_now.return_value = True

    # universe_builder is a callable(asof_date=, config=) -> list[Candidate-like].
    candidate = MagicMock(symbol="AAPL", last_price=Decimal("10"))
    universe_builder = MagicMock(return_value=[candidate])

    # strategy.propose_orders(candidates=, pipeline=, current_equity=, asof_date=)
    # -> list[StrategyOrder]; the orchestrator just places each proposal's order.
    order = Order(
        client_order_id="b-AAPL", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("20"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.05"),
    )
    pipeline_result = PipelineResult(
        symbol="AAPL", date=date(2026, 6, 30), decision=PipelineDecision.BUY, raw={},
    )
    strategy = MagicMock()
    strategy.propose_orders.return_value = [
        StrategyOrder(order=order, reason="test", candidate=candidate, pipeline=pipeline_result),
    ]
    pipeline = MagicMock()

    orch = Orchestrator(
        broker=broker, universe_builder=universe_builder, strategy=strategy,
        pipeline_adapter=pipeline, calendar=calendar, journal=j,
        config=OpsConfig(),
        members_loader=lambda: [],
        momentum_finder=lambda members, asof_date: [],
        closes_fetch=lambda s: None,
    )
    orch.tick()
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].stop_loss_price == Decimal("9.5")

    # Simulate restart — rebuild the inner broker from the journal alone, proving
    # stop_loss_price survives a process restart (Task 2 persistence).
    replayed_inner = PaperBroker.from_journal(
        journal=j, quote_source=quotes.get, starting_cash=Decimal("250"),
    )
    assert replayed_inner.get_positions()[0].stop_loss_price == Decimal("9.5")

    # Drop the quote below the persisted absolute stop and run the guardian against
    # the still-live (never restarted) guarded broker.
    quotes.set("AAPL", Decimal("9.4"))
    guardian = PositionGuardian(
        broker=broker, quote_source=quotes.get, config=OpsConfig(),
        journal=j, broker_mode="paper",
    )
    guardian.check_stops_once()

    events = j.read_events()
    kinds = [e["kind"] for e in events]
    assert "stop_hit" in kinds
    stop_hit = [e for e in events if e["kind"] == "stop_hit"][0]
    assert stop_hit["payload"]["mode"] == "absolute"
    assert broker.get_positions() == []
