"""Real-broker integration test for the displacement planner's cash math
(finding C1).

The pure-planner unit tests in tests/ops/strategy/test_displacement.py
can't catch this bug: it only manifests once the planned trim and buy are
actually PLACED against the same guardrail stack the orchestrator uses,
because CashReserveRule's post-trade check
(ops/guardrails/sizing_rules.py:73) is exact/unquantized while the
planner's `available` (ops/strategy/displacement.py) is quantized to
cents. A rounding mode that overstates `available` computes a shortfall a
fraction of a cent short; the trim(s) fill for "enough" and then the buy
that follows lands post-trade cash a hair below the floor and
CashReserveRule rejects it — AFTER the trims already sold. This test wires
the real PaperBroker + GuardedBroker (construction pattern per
tests/ops/test_integration.py) so that failure mode is real, not
simulated.
"""
from datetime import date
from decimal import Decimal

from ops import events
from ops.broker.guarded import GuardedBroker
from ops.broker.paper import PaperBroker
from ops.broker.types import Order, OrderType, Side
from ops.config import OpsConfig
from ops.guardrails.drawdown_rules import DailyDrawdownRule, WeeklyDrawdownRule
from ops.guardrails.engine import RuleEngine
from ops.guardrails.sizing_rules import (
    CashReserveRule,
    MaxOpenPositionsRule,
    PerPositionCapRule,
    PerTradeDollarFloorRule,
)
from ops.guardrails.static_rules import (
    DenyListRule,
    FractionalSharesOnlyRule,
    LongOnlyRule,
    NoCryptoRule,
    NoMarginRule,
    NoOptionsRule,
    StopAttachedRule,
)
from ops.journal import Journal
from ops.pipeline_adapter import PipelineDecision, PipelineResult, TIER_HIGH, TIER_STARTER
from ops.strategy.base import StrategyOrder
from ops.strategy.displacement import plan_displacement

ASOF = date(2026, 7, 14)
ENTRY_DATE = date(2026, 6, 1)  # comfortably >= displacement_min_holding_age_days
EARLIER_ENTRY_DATE = date(2026, 5, 1)


def _stack(tmp_path, *, starting_cash, quotes, start_equity):
    j = Journal(str(tmp_path / "j.sqlite"))
    paper = PaperBroker(
        journal=j, quote_source=lambda s: quotes[s], starting_cash=starting_cash,
    )
    cfg = OpsConfig()
    rules = [
        DenyListRule(), NoMarginRule(), NoOptionsRule(), NoCryptoRule(),
        LongOnlyRule(), StopAttachedRule(), FractionalSharesOnlyRule(),
        PerTradeDollarFloorRule(), PerPositionCapRule(),
        MaxOpenPositionsRule(), CashReserveRule(),
        DailyDrawdownRule(start_of_day_equity=lambda: start_equity),
        WeeklyDrawdownRule(start_of_week_equity=lambda: start_equity),
    ]
    guarded = GuardedBroker(inner=paper, engine=RuleEngine(rules), journal=j, config=cfg)
    return j, paper, guarded, cfg


def _seed_aged_starter(journal, paper, *, symbol, notional, entry_date=ENTRY_DATE):
    """Buy a starter-tier position and journal its provenance, mirroring
    what the orchestrator does after a fill
    (ops/scheduler/orchestrator.py::_place_entries).

    Placed directly on the inner PaperBroker, bypassing the guardrail
    stack: this is scene-setting for a position that's already on the
    books (built up over prior, separately-guarded trades), not something
    under test here — a single-shot buy this large would trip
    PerPositionCapRule, which is irrelevant to what this test checks."""
    order = Order(
        client_order_id=f"seed-{symbol}", symbol=symbol, side=Side.BUY,
        notional_dollars=notional, order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    paper.place_order(order)
    journal.record_event(
        events.KIND_POSITION_OPENED,
        events.position_opened_payload(
            symbol=symbol, source="MOMENTUM", entry_date=entry_date,
            client_order_id=order.client_order_id, tier=TIER_STARTER,
        ),
    )


def _high_tier_proposal(symbol, notional):
    order = Order(
        client_order_id=f"pem-{symbol}", symbol=symbol, side=Side.BUY,
        notional_dollars=Decimal(notional), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    pipeline = PipelineResult(
        symbol=symbol, date=ASOF, decision=PipelineDecision.BUY,
        rating="Buy", tier=TIER_HIGH,
    )
    return StrategyOrder(order=order, reason="t", candidate=None, pipeline=pipeline)


def test_trim_funded_buy_fills_and_post_trade_cash_respects_reserve_floor(tmp_path):
    # Fractional equity is the reviewer's exact repro: starting_cash
    # 10000.03, starter positions worth exactly 8400.03 total leave cash at
    # 1600.00 and equity at 10000.03 -> reserve floor = 16% * 10000.03 =
    # 1600.0048, i.e. cash sits a fraction of a cent BELOW the true floor
    # before any trade at all. A rounding mode for `available` that
    # rounds this up to 0.00 (HALF_EVEN, and also plain ROUND_DOWN, which
    # truncates toward zero rather than toward -infinity) computes the
    # shortfall a third of a cent short.
    #
    # Two starters, split so the shortfall (500.01) spans both: SMALS
    # (older, value 300.00) is consumed entirely -> a full_exit trim
    # (finding I1) that must close the position via close_position, not a
    # notional SELL that could leave dust shares behind. OLDS (value
    # 8100.03) absorbs the remaining 200.01 as a partial trim.
    starting_cash = Decimal("10000.03")
    quotes = {"SMALS": Decimal("1"), "OLDS": Decimal("1"), "NEWH": Decimal("50")}
    j, paper, guarded, cfg = _stack(
        tmp_path, starting_cash=starting_cash, quotes=quotes,
        start_equity=starting_cash,
    )
    _seed_aged_starter(
        j, paper, symbol="SMALS", notional=Decimal("300.00"),
        entry_date=EARLIER_ENTRY_DATE,
    )
    _seed_aged_starter(j, paper, symbol="OLDS", notional=Decimal("8100.03"))
    assert paper.get_cash() == Decimal("1600.00")
    assert paper.get_equity() == Decimal("10000.03")

    proposal = _high_tier_proposal("NEWH", "500")
    plan = plan_displacement(
        proposals=[proposal],
        positions=list(paper.get_positions()),
        provenance=j.latest_event_payload_by_symbol(events.KIND_POSITION_OPENED),
        quote=paper.get_quote,
        cash=paper.get_cash(),
        equity=paper.get_equity(),
        trims_used_today=0,
        asof_date=ASOF,
        config=cfg,
    )
    assert plan.trims, "expected the shortfall to trigger a displacement trim"
    assert proposal.order.client_order_id in plan.funded_client_order_ids
    trims_by_symbol = {t.symbol: t for t in plan.trims}
    assert trims_by_symbol["SMALS"].full_exit is True
    assert trims_by_symbol["OLDS"].full_exit is False

    # Execute the plan exactly like the orchestrator: trims first (a full
    # exit closes the position outright, a partial trim is a notional
    # SELL), then the funded buy. All legs must fill -- a rejection on the
    # buy after the trim already sold is precisely the bug (trims are
    # irreversible once placed; a rejected buy strands that cash as an
    # unplanned SELL).
    for trim in plan.trims:
        if trim.full_exit:
            guarded.close_position(trim.symbol)  # must not raise
            continue
        order = Order(
            client_order_id=f"disp-{trim.symbol}", symbol=trim.symbol,
            side=Side.SELL, notional_dollars=trim.notional,
            order_type=OrderType.MARKET,
        )
        guarded.place_order(order)  # must not raise OrderRejected

    guarded.place_order(proposal.order)  # must not raise OrderRejected

    floor = paper.get_equity() * cfg.cash_reserve_pct
    assert paper.get_cash() >= floor
    # I1: a full-exit trim must actually close the position, not leave
    # dust shares behind occupying a max_open_positions slot.
    assert "SMALS" not in {p.symbol for p in paper.get_positions()}
