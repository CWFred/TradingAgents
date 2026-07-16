"""Displacement planner: trims starters, oldest first, to fund high-conviction buys.

Pure-function tests — no broker, no journal. `provenance` is the
position_opened payload per symbol (entry_date + tier), exactly what
Journal.latest_event_payload_by_symbol returns.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

from ops.broker.base import BrokerError
from ops.broker.types import Order, OrderType, Position, Side
from ops.config import OpsConfig
from ops.pipeline_adapter import TIER_HIGH, TIER_STARTER
from ops.strategy.base import StrategyOrder
from ops.strategy.displacement import plan_displacement

ASOF = date(2026, 7, 14)


def _proposal(symbol, notional, tier=TIER_HIGH):
    order = Order(
        client_order_id=f"pem-{symbol}", symbol=symbol, side=Side.BUY,
        notional_dollars=Decimal(notional), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    pipeline = MagicMock()
    pipeline.tier = tier
    return StrategyOrder(order=order, reason="t", candidate=MagicMock(), pipeline=pipeline)


def _position(symbol, qty="10", price="10"):
    return Position(symbol=symbol, quantity=Decimal(qty), avg_entry_price=Decimal(price))


def _prov(entry_date, tier="starter"):
    return {"entry_date": entry_date, "tier": tier}


def _plan(**overrides):
    defaults = dict(
        proposals=[], positions=[], provenance={},
        quote=lambda s: Decimal("10"),
        cash=Decimal("0"), equity=Decimal("10000"), trims_used_today=0,
        asof_date=ASOF, config=OpsConfig(),
    )
    defaults.update(overrides)
    return plan_displacement(**defaults)


def test_funds_from_free_cash_without_trimming():
    # reserve floor = 16% of 10_000 = 1600; cash 3000 leaves 1400 available
    plan = _plan(proposals=[_proposal("NEWB", "1200")], cash=Decimal("3000"))
    assert plan.trims == []
    assert plan.funded_client_order_ids == frozenset({"pem-NEWB"})
    assert plan.skips == []


def test_high_tier_shortfall_trims_oldest_starter_first():
    plan = _plan(
        proposals=[_proposal("NEWB", "1200")],
        cash=Decimal("2400"),  # available = 800, shortfall = 400
        positions=[_position("OLD1", qty="30"), _position("OLD2", qty="30")],
        provenance={"OLD1": _prov("2026-07-06"), "OLD2": _prov("2026-07-01")},
    )
    # OLD2 is older -> trimmed first; 400 shortfall < 300 value? no: value 300
    # each -> takes OLD2 fully (300) then 100 from OLD1.
    assert [(t.symbol, t.notional) for t in plan.trims] == [
        ("OLD2", Decimal("300.00")), ("OLD1", Decimal("100.00")),
    ]
    assert all(t.funded_symbol == "NEWB" for t in plan.trims)
    assert plan.funded_client_order_ids == frozenset({"pem-NEWB"})


def test_starter_proposal_never_triggers_trims():
    plan = _plan(
        proposals=[_proposal("NEWB", "500", tier=TIER_STARTER)],
        cash=Decimal("1600"),  # available = 0
        positions=[_position("OLD1", qty="100")],
        provenance={"OLD1": _prov("2026-07-01")},
    )
    assert plan.trims == []
    assert plan.funded_client_order_ids == frozenset()
    assert len(plan.skips) == 1 and plan.skips[0].symbol == "NEWB"


def test_min_holding_age_gate():
    plan = _plan(
        proposals=[_proposal("NEWB", "100")],
        cash=Decimal("1600"),
        # entered Friday 07-10; Tue 07-14 is only 2 trading days later -> immune
        positions=[_position("OLD1", qty="100")],
        provenance={"OLD1": _prov("2026-07-10")},
    )
    assert plan.trims == []
    assert plan.skips[0].symbol == "NEWB"


def test_max_trims_per_day_budget_respected():
    cfg = OpsConfig()  # displacement_max_trims_per_day = 2
    plan = _plan(
        proposals=[_proposal("NEWB", "1000")],
        cash=Decimal("1600"),  # available 0, shortfall 1000
        positions=[
            _position("OLD1", qty="30"), _position("OLD2", qty="30"),
            _position("OLD3", qty="30"),
        ],
        provenance={
            "OLD1": _prov("2026-07-01"), "OLD2": _prov("2026-07-02"),
            "OLD3": _prov("2026-07-03"),
        },
        config=cfg,
    )
    # needs 1000 but 2 trims x 300 = 600 max -> nothing trimmed, buy skipped
    assert plan.trims == []
    assert plan.funded_client_order_ids == frozenset()
    assert "displacement guards" in plan.skips[0].reason


def test_trims_used_today_counts_against_budget():
    plan = _plan(
        proposals=[_proposal("NEWB", "400")],
        cash=Decimal("1600"),
        positions=[_position("OLD1", qty="30"), _position("OLD2", qty="30")],
        provenance={"OLD1": _prov("2026-07-01"), "OLD2": _prov("2026-07-02")},
        trims_used_today=1,  # only 1 trim left; 400 needs two 300-value starters
    )
    assert plan.trims == []
    assert plan.funded_client_order_ids == frozenset()


def test_untiered_legacy_position_is_immune():
    plan = _plan(
        proposals=[_proposal("NEWB", "100")],
        cash=Decimal("1600"),
        positions=[_position("DAL", qty="100")],
        provenance={"DAL": {"entry_date": "2026-07-01"}},  # no tier key (pre-v2)
    )
    assert plan.trims == []


def test_high_tier_position_is_never_trimmed():
    plan = _plan(
        proposals=[_proposal("NEWB", "100")],
        cash=Decimal("1600"),
        positions=[_position("BIGW", qty="100")],
        provenance={"BIGW": _prov("2026-07-01", tier="high")},
    )
    assert plan.trims == []


def test_quote_failure_skips_that_starter_and_continues():
    def quote(sym):
        if sym == "OLD1":
            raise BrokerError("no quote")
        return Decimal("10")
    plan = _plan(
        proposals=[_proposal("NEWB", "200")],
        cash=Decimal("1600"),
        positions=[_position("OLD1", qty="100"), _position("OLD2", qty="30")],
        provenance={"OLD1": _prov("2026-07-01"), "OLD2": _prov("2026-07-02")},
        quote=quote,
    )
    assert [t.symbol for t in plan.trims] == ["OLD2"]
    assert plan.funded_client_order_ids == frozenset({"pem-NEWB"})


def test_unknown_or_empty_tier_never_displaces():
    # D4: only an exact TIER_HIGH tier is displacement-fundable. Empty/
    # unknown tiers must fail CLOSED (behave like starters for funding
    # purposes) — never trigger trims, even with an aged starter sitting
    # right there ready to fund them.
    plan = _plan(
        proposals=[
            _proposal("EMPTY", "100", tier=""),
            _proposal("BANANA", "100", tier="banana"),
        ],
        cash=Decimal("1600"),  # available = 0
        positions=[_position("OLD1", qty="100")],
        provenance={"OLD1": _prov("2026-07-01")},
    )
    assert plan.trims == []
    assert plan.funded_client_order_ids == frozenset()
    assert len(plan.skips) == 2
    reasons = {s.symbol: s.reason for s in plan.skips}
    assert reasons["EMPTY"] == "insufficient cash; unknown tier never displaces"
    assert reasons["BANANA"] == "insufficient cash; unknown tier never displaces"


def test_full_exit_flag_set_when_trim_consumes_entire_starter_value():
    # I1: OLD2 (value 300) is entirely consumed by the shortfall -> a full
    # exit; OLD1 only gives up part of its value -> a partial trim. Mirrors
    # test_high_tier_shortfall_trims_oldest_starter_first's numbers.
    plan = _plan(
        proposals=[_proposal("NEWB", "1200")],
        cash=Decimal("2400"),  # available = 800, shortfall = 400
        positions=[_position("OLD1", qty="30"), _position("OLD2", qty="30")],
        provenance={"OLD1": _prov("2026-07-06"), "OLD2": _prov("2026-07-01")},
    )
    trims_by_symbol = {t.symbol: t for t in plan.trims}
    assert trims_by_symbol["OLD2"].notional == Decimal("300.00")
    assert trims_by_symbol["OLD2"].full_exit is True
    assert trims_by_symbol["OLD1"].notional == Decimal("100.00")
    assert trims_by_symbol["OLD1"].full_exit is False


def test_full_exit_flag_false_when_shortfall_exactly_equals_multiple_starters():
    # Two starters, each entirely consumed to cover the shortfall exactly:
    # both must be full_exit, not just the last one.
    plan = _plan(
        proposals=[_proposal("NEWB", "1900")],
        cash=Decimal("1600"),  # available = 0, shortfall = 1900
        positions=[_position("OLD1", qty="100"), _position("OLD2", qty="90")],
        provenance={"OLD1": _prov("2026-07-01"), "OLD2": _prov("2026-07-02")},
    )
    # OLD1 value 1000 (fully consumed), OLD2 value 900 -> only 900 needed
    # of the remaining 900 shortfall, so OLD2 is also fully consumed.
    trims_by_symbol = {t.symbol: t for t in plan.trims}
    assert trims_by_symbol["OLD1"].full_exit is True
    assert trims_by_symbol["OLD2"].full_exit is True


def test_high_before_starter_ordering_and_partial_funding():
    # available 1400: high (1200) funds first, starter (500) then falls short.
    plan = _plan(
        proposals=[
            _proposal("STRT", "500", tier=TIER_STARTER),
            _proposal("HIGH", "1200"),
        ],
        cash=Decimal("3000"),
    )
    assert plan.funded_client_order_ids == frozenset({"pem-HIGH"})
    assert plan.skips[0].symbol == "STRT"


def test_two_high_proposals_share_starters_without_double_spending():
    plan = _plan(
        proposals=[_proposal("NEW1", "300"), _proposal("NEW2", "300")],
        cash=Decimal("1600"),  # available 0
        positions=[_position("OLD1", qty="60")],  # value 600, one position
        provenance={"OLD1": _prov("2026-07-01")},
    )
    # One starter can fund both via two partial trims of the same symbol —
    # but that is TWO trims against the daily budget of 2.
    assert [(t.symbol, t.notional, t.funded_symbol) for t in plan.trims] == [
        ("OLD1", Decimal("300.00"), "NEW1"),
        ("OLD1", Decimal("300.00"), "NEW2"),
    ]
    assert plan.funded_client_order_ids == frozenset({"pem-NEW1", "pem-NEW2"})


def test_fractional_share_values_are_cent_quantized_with_no_phantom_residue():
    # Fractional shares are the norm (paper broker fills fractionally):
    # qty 10.0005 @ 10 -> raw market value 100.005, which the planner must
    # anchor at cents (100.00 under banker's rounding). NEW1 consumes the
    # whole starter; NEW2 then finds no phantom 0.005 left to trim.
    cents = Decimal("0.01")
    plan = _plan(
        proposals=[_proposal("NEW1", "100"), _proposal("NEW2", "5")],
        cash=Decimal("1600"),  # available 0
        positions=[_position("OLD1", qty="10.0005")],  # raw value 100.005
        provenance={"OLD1": _prov("2026-07-01")},
    )
    # Full trim of the starter is exact cents, not the raw sub-cent value.
    assert [(t.symbol, t.notional, t.funded_symbol) for t in plan.trims] == [
        ("OLD1", Decimal("100.00"), "NEW1"),
    ]
    assert plan.funded_client_order_ids == frozenset({"pem-NEW1"})
    # Bookkeeping zeroed cleanly: NEW2 cannot extract the 0.005 residue
    # (unfixed code plans a 0.00-notional trim here and reports a raw
    # 4.995 shortfall).
    assert len(plan.skips) == 1
    skip = plan.skips[0]
    assert skip.symbol == "NEW2"
    assert skip.shortfall == Decimal("5.00")
    assert skip.shortfall == skip.shortfall.quantize(cents)


def test_starter_value_anchor_rounds_down_never_plans_oversell():
    # qty 9.9995 @ 10 -> true quoted value 99.995. Half-even would anchor
    # that UP to 100.00 and plan a sell 0.005 above what the position is
    # worth — the broker rejects that (qty_to_sell > held) and strands the
    # funded buy. The anchor must round DOWN: trim at most 99.99.
    plan = _plan(
        proposals=[_proposal("NEWB", "150")],
        cash=Decimal("1600"),  # available 0, shortfall 150
        positions=[_position("OLD1", qty="9.9995"), _position("OLD2", qty="30")],
        provenance={"OLD1": _prov("2026-07-01"), "OLD2": _prov("2026-07-02")},
    )
    assert [(t.symbol, t.notional) for t in plan.trims] == [
        ("OLD1", Decimal("99.99")), ("OLD2", Decimal("50.01")),
    ]
    assert plan.funded_client_order_ids == frozenset({"pem-NEWB"})
