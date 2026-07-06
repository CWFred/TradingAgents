"""Unit tests for derived fundamentals (synthetic facts payloads, no HTTP)."""

from datetime import date
from decimal import Decimal

import pytest

from tradingagents.dataflows.fundamentals import compute_fundamentals

pytestmark = pytest.mark.unit

ASOF = date(2026, 6, 1)


def _row(val, year, *, instant=False, form="10-K", fp="FY"):
    row = {
        "val": val,
        "end": f"{year}-12-31",
        "filed": f"{year + 1}-02-15",
        "form": form,
        "fp": fp,
        "accn": f"acc-{year}",
    }
    if not instant:
        row["start"] = f"{year}-01-01"
    return row


def _facts(concepts: dict, unit="USD"):
    payload = {}
    for concept, rows in concepts.items():
        u = "USD/shares" if concept.startswith("EarningsPerShare") else unit
        payload[concept] = {"units": {u: rows}}
    return {"facts": {"us-gaap": payload}}


def test_headline_values_and_fcf():
    facts = _facts({
        "OperatingIncomeLoss": [_row(100, 2025)],
        "DepreciationDepletionAndAmortization": [_row(20, 2025)],
        "NetCashProvidedByUsedInOperatingActivities": [_row(110, 2025)],
        "PaymentsToAcquirePropertyPlantAndEquipment": [_row(30, 2025)],
        "LongTermDebtNoncurrent": [_row(200, 2025, instant=True)],
        "LongTermDebtCurrent": [_row(50, 2025, instant=True)],
        "CashAndCashEquivalentsAtCarryingValue": [_row(80, 2025, instant=True)],
        "StockholdersEquity": [_row(400, 2025, instant=True)],
    })
    f = compute_fundamentals("TEST", facts, asof=ASOF)
    assert f.ebit == Decimal("100")
    assert f.ebitda == Decimal("120")
    assert f.fcf == Decimal("80")
    assert f.total_debt == Decimal("250")
    assert f.cash == Decimal("80")


def test_missing_capex_means_fcf_none():
    facts = _facts({
        "NetCashProvidedByUsedInOperatingActivities": [_row(110, 2025)],
    })
    f = compute_fundamentals("TEST", facts, asof=ASOF)
    assert f.fcf is None


def test_debt_free_with_balance_sheet_is_zero_not_none():
    # No debt concepts filed at all, but equity proves a balance sheet exists.
    facts = _facts({"StockholdersEquity": [_row(400, 2025, instant=True)]})
    f = compute_fundamentals("TEST", facts, asof=ASOF)
    assert f.total_debt == Decimal("0")


def test_no_balance_sheet_at_all_means_debt_none():
    facts = _facts({"OperatingIncomeLoss": [_row(100, 2025)]})
    f = compute_fundamentals("TEST", facts, asof=ASOF)
    assert f.total_debt is None


def test_roic_uses_effective_tax_rate_and_invested_capital():
    facts = _facts({
        "OperatingIncomeLoss": [_row(100, 2025)],
        "StockholdersEquity": [_row(400, 2025, instant=True)],
        "LongTermDebtNoncurrent": [_row(200, 2025, instant=True)],
        "CashAndCashEquivalentsAtCarryingValue": [_row(100, 2025, instant=True)],
        "IncomeTaxExpenseBenefit": [_row(20, 2025)],
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": [
            _row(100, 2025)
        ],
    })
    f = compute_fundamentals("TEST", facts, asof=ASOF)
    # NOPAT = 100 * (1 - 0.20) = 80; IC = 400 + 200 - 100 = 500; ROIC = 0.16
    assert len(f.roic_history) == 1
    assert f.roic_history[0].value == Decimal("80") / Decimal("500")


def test_roic_skips_years_with_nonpositive_invested_capital():
    facts = _facts({
        "OperatingIncomeLoss": [_row(100, 2025)],
        "StockholdersEquity": [_row(50, 2025, instant=True)],
        "CashAndCashEquivalentsAtCarryingValue": [_row(100, 2025, instant=True)],
    })
    f = compute_fundamentals("TEST", facts, asof=ASOF)
    assert f.roic_history == ()


def test_gross_margin_falls_back_to_revenue_minus_cogs():
    facts = _facts({
        "Revenues": [_row(200, 2024), _row(250, 2025)],
        "CostOfRevenue": [_row(120, 2024), _row(150, 2025)],
    })
    f = compute_fundamentals("TEST", facts, asof=ASOF)
    assert [m.value for m in f.gross_margin_history] == [
        Decimal("80") / Decimal("200"),
        Decimal("100") / Decimal("250"),
    ]


def test_ebitda_and_fcf_none_when_component_years_misaligned():
    facts = _facts({
        "OperatingIncomeLoss": [_row(90, 2024), _row(100, 2025)],
        "DepreciationDepletionAndAmortization": [_row(20, 2024)],
        "NetCashProvidedByUsedInOperatingActivities": [_row(110, 2024), _row(115, 2025)],
        "PaymentsToAcquirePropertyPlantAndEquipment": [_row(30, 2024)],
    })
    f = compute_fundamentals("TEST", facts, asof=ASOF)
    assert f.ebitda is None
    assert f.fcf is None


def test_debt_and_cash_anchor_to_latest_equity_year_not_stale_years():
    facts = _facts({
        "StockholdersEquity": [_row(380, 2024, instant=True), _row(400, 2025, instant=True)],
        "LongTermDebtNoncurrent": [_row(200, 2024, instant=True)],
        "CashAndCashEquivalentsAtCarryingValue": [_row(80, 2024, instant=True)],
    })
    f = compute_fundamentals("TEST", facts, asof=ASOF)
    assert f.total_debt is None
    assert f.cash is None


def test_eps_history_oldest_first():
    facts = _facts({
        "EarningsPerShareDiluted": [_row("2.5", 2024), _row("3.0", 2025)],
    })
    f = compute_fundamentals("TEST", facts, asof=ASOF)
    assert [m.fiscal_year_end for m in f.eps_history] == [
        date(2024, 12, 31), date(2025, 12, 31),
    ]
    assert [m.value for m in f.eps_history] == [Decimal("2.5"), Decimal("3.0")]
