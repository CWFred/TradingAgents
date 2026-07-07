"""Unit tests for falsifier metric evaluation (pure, no network)."""

from datetime import date
from decimal import Decimal

import pytest

from ops.research.metrics import (
    SUPPORTED_METRICS,
    MetricContext,
    drawdown_pct,
    evaluate_falsifier,
    observations,
)
from ops.research.prices import PriceContext
from tradingagents.dataflows.fundamentals import Fundamentals, YearValue
from tradingagents.memos.schema import Falsifier

pytestmark = pytest.mark.unit

ASOF = date(2026, 7, 7)
ERA = date(2026, 1, 5)


def _ctx(**overrides):
    kwargs = {
        "entry_price_ref": 10.0,
        "asof": ASOF,
        "entry_era": ERA,
        "price_ctx": PriceContext(closes={
            date(2026, 7, 2): Decimal("8"),
            date(2026, 7, 6): Decimal("7"),
            date(2026, 7, 7): Decimal("6.5"),
        }),
    }
    kwargs.update(overrides)
    return MetricContext(**kwargs)


def _fundamentals(**overrides):
    kwargs = {
        "ticker": "WIDG",
        "asof": ASOF,
        "ebit": Decimal("10"),
        "ebitda": Decimal("20"),
        "total_debt": Decimal("50"),
        "cash": Decimal("10"),
        "fcf": Decimal("5"),
        "eps_history": (),
        "roic_history": (),
        "gross_margin_history": (
            YearValue(date(2024, 12, 31), Decimal("0.40")),
            YearValue(date(2025, 12, 31), Decimal("0.28")),
        ),
    }
    kwargs.update(overrides)
    return Fundamentals(**kwargs)


def _falsifier(**overrides):
    kwargs = {
        "description": "drawdown",
        "check_type": "price",
        "metric": "drawdown_from_cost_pct",
        "operator": "<",
        "threshold": -25.0,
        "consecutive_periods": 1,
    }
    kwargs.update(overrides)
    return Falsifier(**kwargs)


def test_drawdown_observations_most_recent_first_in_entry_era():
    obs = observations("drawdown_from_cost_pct", _ctx())
    # closes 6.5, 7, 8 (most recent first) against entry 10 -> -35%, -30%, -20%
    assert obs == pytest.approx([-35.0, -30.0, -20.0])
    assert drawdown_pct(_ctx()) == pytest.approx(-35.0)


def test_drawdown_undoes_splits_after_entry_era():
    # 2-for-1 split after entry: Yahoo-adjusted 6.5 is 13.0 in entry-era shares.
    ctx = _ctx(price_ctx=PriceContext(
        closes={date(2026, 7, 7): Decimal("6.5")},
        splits={date(2026, 6, 1): Decimal("2")},
    ))
    assert drawdown_pct(ctx) == pytest.approx(30.0)  # (13 - 10) / 10


def test_gross_margin_pct_sorted_descending_and_scaled():
    obs = observations("gross_margin_pct", _ctx(fundamentals=_fundamentals()))
    assert obs == pytest.approx([28.0, 40.0])  # FY2025 first


def test_net_debt_to_ebitda_single_observation():
    obs = observations("net_debt_to_ebitda", _ctx(fundamentals=_fundamentals()))
    assert obs == pytest.approx([2.0])  # (50 - 10) / 20


def test_net_debt_to_ebitda_unprofitable_is_unevaluable():
    f = _fundamentals(ebitda=Decimal("0"))
    assert observations("net_debt_to_ebitda", _ctx(fundamentals=f)) is None


def test_revenue_yoy_from_facts(monkeypatch):
    from ops.research import metrics

    def fake_annual_series(facts, concepts, *, asof, unit="USD", max_years=5):
        from tradingagents.dataflows.edgar_facts import FactPoint
        return [
            FactPoint("Revenues", Decimal("110"), "USD", date(2024, 12, 31), None, "10-K", date(2025, 2, 28), "0000000000-25-000001"),
            FactPoint("Revenues", Decimal("88"), "USD", date(2025, 12, 31), None, "10-K", date(2026, 2, 28), "0000000000-26-000001"),
        ]

    monkeypatch.setattr(metrics, "annual_series", fake_annual_series)
    obs = observations("revenue_yoy_pct", _ctx(facts={"facts": {}}))
    assert obs == pytest.approx([-20.0])  # 88 vs 110


def test_unknown_metric_and_missing_inputs_return_none():
    assert observations("free_cash_flow_conversion", _ctx()) is None
    assert observations("gross_margin_pct", _ctx(fundamentals=None)) is None
    assert observations("drawdown_from_cost_pct", _ctx(price_ctx=None)) is None


def test_evaluate_falsifier_trips_on_threshold():
    check = evaluate_falsifier(_falsifier(threshold=-30.0), _ctx())
    assert check.status == "tripped"
    assert check.observed == pytest.approx(-35.0)


def test_evaluate_falsifier_consecutive_periods():
    # -35, -30, -20: two most recent both < -25 -> trips at periods=2 ...
    check = evaluate_falsifier(_falsifier(consecutive_periods=2), _ctx())
    assert check.status == "tripped"
    # ... but not at periods=3 (the -20 observation breaks the streak).
    check = evaluate_falsifier(_falsifier(consecutive_periods=3), _ctx())
    assert check.status == "ok"


def test_evaluate_falsifier_insufficient_history_unevaluable():
    check = evaluate_falsifier(_falsifier(consecutive_periods=9), _ctx())
    assert check.status == "unevaluable"


def test_prose_only_falsifier_unevaluable():
    prose = Falsifier(description="thesis stops working", check_type="fundamental")
    check = evaluate_falsifier(prose, _ctx())
    assert check.status == "unevaluable"
    assert "machine-checkable" in check.detail


def test_supported_metrics_frozen():
    assert {
        "drawdown_from_cost_pct", "gross_margin_pct",
        "revenue_yoy_pct", "net_debt_to_ebitda",
    } == SUPPORTED_METRICS
