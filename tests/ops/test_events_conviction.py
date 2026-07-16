"""v2 conviction posture events: rating/tier payload fields + new kinds."""
from decimal import Decimal

from ops import events


def test_analysis_decision_payload_includes_rating_when_given():
    p = events.analysis_decision_payload(
        symbol="AAPL", decision="BUY", source="MOMENTUM", asof="2026-07-14",
        rank=3, rating="Overweight",
    )
    assert p["rating"] == "Overweight"


def test_analysis_decision_payload_omits_empty_rating():
    p = events.analysis_decision_payload(
        symbol="AAPL", decision="HOLD", source="MOMENTUM", asof="2026-07-14",
    )
    assert "rating" not in p


def test_position_opened_payload_includes_tier_when_given():
    from datetime import date
    p = events.position_opened_payload(
        symbol="AAPL", source="MOMENTUM", entry_date=date(2026, 7, 14),
        client_order_id="x", tier="starter",
    )
    assert p["tier"] == "starter"


def test_position_opened_payload_omits_missing_tier():
    from datetime import date
    p = events.position_opened_payload(
        symbol="AAPL", source="MOMENTUM", entry_date=date(2026, 7, 14),
        client_order_id="x",
    )
    assert "tier" not in p


def test_new_kinds_have_payload_builders_and_are_quiet():
    for kind in (
        events.KIND_DISPLACEMENT_TRIM,
        events.KIND_ENTRY_SKIPPED_UNFUNDED,
        events.KIND_UNDERWEIGHT_TRIM,
    ):
        assert kind in events.BUILDERS
        assert kind in events.AUDIT_ONLY


def test_displacement_trim_payload_shape():
    p = events.displacement_trim_payload(
        symbol="OLDN", tier="starter", notional=Decimal("312.50"),
        funded_symbol="NEWB", client_order_id="disp-1",
    )
    assert p == {
        "symbol": "OLDN", "tier": "starter", "notional": "312.50",
        "funded_symbol": "NEWB", "client_order_id": "disp-1",
    }


def test_entry_skipped_unfunded_payload_shape():
    p = events.entry_skipped_unfunded_payload(
        symbol="NEWB", shortfall=Decimal("87.10"), reason="guards exhausted",
    )
    assert p == {"symbol": "NEWB", "shortfall": "87.10", "reason": "guards exhausted"}


def test_underweight_trim_payload_shape():
    p = events.underweight_trim_payload(
        symbol="AAPL", rating="Underweight", notional=Decimal("600.00"),
        client_order_id="uwt-1",
    )
    assert p == {
        "symbol": "AAPL", "rating": "Underweight", "notional": "600.00",
        "client_order_id": "uwt-1",
    }
