"""Unit tests for research-sleeve sizing fences (pure, no I/O)."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from ops.research.sizing import (
    TIER_SIZING,
    cost_basis,
    size_entry,
)

pytestmark = pytest.mark.unit

EQUITY = Decimal("100000")


def _size(**overrides):
    kwargs = {
        "tier": "medium", "equity": EQUITY, "cash": Decimal("50000"),
        "cost_by_symbol": {}, "symbol": "WIDG", "sector": "Industrials",
        "cost_by_sector": {}, "adv_20d": Decimal("5000000"),
    }
    kwargs.update(overrides)
    return size_entry(**kwargs)


def test_tier_sizing_within_spec_bands():
    assert Decimal("0.01") <= TIER_SIZING["starter"] <= Decimal("0.02")
    assert Decimal("0.03") <= TIER_SIZING["medium"] <= Decimal("0.05")
    assert Decimal("0.05") <= TIER_SIZING["high"] <= Decimal("0.08")


def test_base_sizing_by_tier():
    assert _size(tier="starter").notional == Decimal("2000.00")
    assert _size(tier="medium").notional == Decimal("4000.00")
    assert _size(tier="high").notional == Decimal("6000.00")
    assert _size().rejected is None


def test_cash_clamps():
    d = _size(tier="high", cash=Decimal("2500"))
    assert d.notional == Decimal("2500.00") and d.rejected is None


def test_cash_clamp_quantizes_to_cents():
    """Cash clamp should quantize to cents, not leak sub-cent precision."""
    d = _size(tier="high", cash=Decimal("2500.005"))
    assert d.notional == Decimal("2500.00")


def test_name_cap_at_cost_clamps_then_rejects():
    # Existing WIDG cost 9k of a 10k cap: headroom 1k >= floor -> clamp.
    d = _size(cost_by_symbol={"WIDG": Decimal("9000")})
    assert d.notional == Decimal("1000.00") and d.rejected is None
    # 9.95k of 10k: headroom 50 < MIN_ORDER_DOLLARS -> reject.
    d = _size(cost_by_symbol={"WIDG": Decimal("9950")})
    assert d.rejected is not None and "name" in d.rejected


def test_sector_cap():
    d = _size(cost_by_sector={"Industrials": Decimal("24000")})
    assert d.notional == Decimal("1000.00")
    d = _size(cost_by_sector={"Industrials": Decimal("25000")})
    assert d.rejected is not None and "sector" in d.rejected
    # Different sector unaffected.
    assert _size(cost_by_sector={"Tech": Decimal("25000")}).rejected is None


def test_adv_cap_and_unavailable():
    # 5% of 60k ADV = 3k < the 4k medium base -> clamp.
    d = _size(adv_20d=Decimal("60000"))
    assert d.notional == Decimal("3000.00")
    d = _size(adv_20d=Decimal("1000"))  # 5% = 50 < floor
    assert d.rejected is not None and "adv" in d.rejected
    d = _size(adv_20d=None)
    assert d.rejected is not None and "unavailable" in d.rejected


def test_unknown_sector_is_a_real_bucket():
    d = _size(sector="UNKNOWN", cost_by_sector={"UNKNOWN": Decimal("25000")})
    assert d.rejected is not None


def test_unknown_tier_rejected():
    d = _size(tier="yolo")
    assert d.rejected is not None and "tier" in d.rejected


def test_cost_basis_from_positions():
    """cost_basis() maps symbols to quantity*avg_entry_price and totals them."""
    pos1 = SimpleNamespace(
        symbol="AAPL",
        quantity=Decimal("100"),
        avg_entry_price=Decimal("150.50"),
    )
    pos2 = SimpleNamespace(
        symbol="MSFT",
        quantity=Decimal("50"),
        avg_entry_price=Decimal("380.00"),
    )
    by_symbol, total = cost_basis([pos1, pos2])
    assert by_symbol == {
        "AAPL": Decimal("15050"),
        "MSFT": Decimal("19000"),
    }
    assert total == Decimal("34050")

    # Empty positions list.
    by_symbol, total = cost_basis([])
    assert by_symbol == {}
    assert total == Decimal("0")
