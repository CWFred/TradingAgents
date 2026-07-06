"""Derived annual fundamentals from SEC XBRL company facts.

Everything is point-in-time via ``edgar_facts.annual_series`` (as-reported at
filing date). Concept fallback chains absorb the most common tagging
variation across filers; within one metric the first chain member with any
data wins the whole series (mixing concepts across years would splice
incompatible definitions).

Missing-data policy: a metric that cannot be computed is None / empty — the
screener treats missing as a failed bar, never as a pass. Composites (EBITDA,
FCF) require both components to end on the same fiscal year; debt and cash
are anchored to the latest equity year, so a stale year's figure is never
reported as current. The one deliberate exception: a company with a balance
sheet (equity filed) but no debt concepts is treated as debt = 0, because
debt-free small caps simply omit the tags and returning None would fail the
leverage bar for exactly the best names.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tradingagents.dataflows.edgar_facts import FactPoint, annual_series

EBIT_CONCEPTS = ("OperatingIncomeLoss",)
DA_CONCEPTS = (
    "DepreciationDepletionAndAmortization",
    "DepreciationAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
)
CFO_CONCEPTS = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
CAPEX_CONCEPTS = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
)
REVENUE_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
)
GROSS_PROFIT_CONCEPTS = ("GrossProfit",)
COST_OF_REVENUE_CONCEPTS = (
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSold",
)
EPS_CONCEPTS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
EQUITY_CONCEPTS = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)
DEBT_NONCURRENT_CONCEPTS = ("LongTermDebtNoncurrent",)
DEBT_CURRENT_CONCEPTS = ("LongTermDebtCurrent",)
DEBT_TOTAL_CONCEPTS = ("LongTermDebt",)
CASH_CONCEPTS = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
)
PRETAX_CONCEPTS = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
)
TAX_CONCEPTS = ("IncomeTaxExpenseBenefit",)

# When the effective tax rate cannot be computed (loss year, missing tags),
# fall back to the US statutory corporate rate; clamp implausible rates.
_DEFAULT_TAX_RATE = Decimal("0.21")
_MAX_TAX_RATE = Decimal("0.35")
_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True)
class YearValue:
    fiscal_year_end: date
    value: Decimal


@dataclass(frozen=True)
class Fundamentals:
    ticker: str
    asof: date
    ebit: Decimal | None
    ebitda: Decimal | None
    total_debt: Decimal | None
    cash: Decimal | None
    fcf: Decimal | None
    eps_history: tuple[YearValue, ...]
    roic_history: tuple[YearValue, ...]
    gross_margin_history: tuple[YearValue, ...]


def _by_year(points: list[FactPoint]) -> dict[date, Decimal]:
    return {p.end: p.value for p in points}


def _latest(points: list[FactPoint]) -> Decimal | None:
    return points[-1].value if points else None


def _latest_aligned(
    a: dict[date, Decimal], b: dict[date, Decimal]
) -> tuple[Decimal, Decimal] | None:
    """Latest values of two series, only when both series end on the SAME
    fiscal year — composites must never splice two reporting periods."""
    if not a or not b:
        return None
    year = max(a)
    if year != max(b):
        return None
    return a[year], b[year]


def _debt_by_year(
    facts: dict, *, asof: date, has_balance_sheet: bool
) -> dict[date, Decimal]:
    noncurrent = _by_year(annual_series(facts, DEBT_NONCURRENT_CONCEPTS, asof=asof))
    current = _by_year(annual_series(facts, DEBT_CURRENT_CONCEPTS, asof=asof))
    if noncurrent or current:
        years = set(noncurrent) | set(current)
        return {y: noncurrent.get(y, _ZERO) + current.get(y, _ZERO) for y in years}
    total = _by_year(annual_series(facts, DEBT_TOTAL_CONCEPTS, asof=asof))
    if total:
        return total
    if has_balance_sheet:
        equity_years = _by_year(annual_series(facts, EQUITY_CONCEPTS, asof=asof))
        return dict.fromkeys(equity_years, _ZERO)
    return {}


def _gross_margins(facts: dict, *, asof: date) -> tuple[YearValue, ...]:
    revenue = _by_year(annual_series(facts, REVENUE_CONCEPTS, asof=asof))
    gross = _by_year(annual_series(facts, GROSS_PROFIT_CONCEPTS, asof=asof))
    if not gross:
        cost = _by_year(annual_series(facts, COST_OF_REVENUE_CONCEPTS, asof=asof))
        gross = {
            y: revenue[y] - cost[y] for y in sorted(set(revenue) & set(cost))
        }
    margins = [
        YearValue(y, gross[y] / revenue[y])
        for y in sorted(set(gross) & set(revenue))
        if revenue[y] > _ZERO
    ]
    return tuple(margins[-5:])


def _roic_history(
    facts: dict,
    *,
    asof: date,
    ebit_by_year: dict[date, Decimal],
    equity_by_year: dict[date, Decimal],
    debt_by_year: dict[date, Decimal],
    cash_by_year: dict[date, Decimal],
) -> tuple[YearValue, ...]:
    pretax = _by_year(annual_series(facts, PRETAX_CONCEPTS, asof=asof))
    tax = _by_year(annual_series(facts, TAX_CONCEPTS, asof=asof))
    out: list[YearValue] = []
    for y in sorted(ebit_by_year):
        equity = equity_by_year.get(y)
        if equity is None:
            continue
        invested = equity + debt_by_year.get(y, _ZERO) - cash_by_year.get(y, _ZERO)
        if invested <= _ZERO:
            continue
        rate = _DEFAULT_TAX_RATE
        pre, tx = pretax.get(y), tax.get(y)
        if pre is not None and tx is not None and pre > _ZERO:
            rate = min(max(tx / pre, _ZERO), _MAX_TAX_RATE)
        nopat = ebit_by_year[y] * (_ONE - rate)
        out.append(YearValue(y, nopat / invested))
    return tuple(out[-5:])


def compute_fundamentals(ticker: str, facts: dict, *, asof: date) -> Fundamentals:
    ebit_pts = annual_series(facts, EBIT_CONCEPTS, asof=asof)
    ebit_by_year = _by_year(ebit_pts)
    equity_by_year = _by_year(annual_series(facts, EQUITY_CONCEPTS, asof=asof))
    cash_by_year = _by_year(annual_series(facts, CASH_CONCEPTS, asof=asof))
    debt_by_year = _debt_by_year(facts, asof=asof, has_balance_sheet=bool(
        equity_by_year
    ))

    ebit = _latest(ebit_pts)
    da_by_year = _by_year(annual_series(facts, DA_CONCEPTS, asof=asof))
    pair = _latest_aligned(ebit_by_year, da_by_year)
    ebitda = pair[0] + pair[1] if pair else None
    cfo_by_year = _by_year(annual_series(facts, CFO_CONCEPTS, asof=asof))
    capex_by_year = _by_year(annual_series(facts, CAPEX_CONCEPTS, asof=asof))
    pair = _latest_aligned(cfo_by_year, capex_by_year)
    fcf = pair[0] - pair[1] if pair else None

    anchor = max(equity_by_year) if equity_by_year else None

    eps = tuple(
        YearValue(p.end, p.value)
        for p in annual_series(facts, EPS_CONCEPTS, asof=asof, unit="USD/shares")
    )

    return Fundamentals(
        ticker=ticker.upper(),
        asof=asof,
        ebit=ebit,
        ebitda=ebitda,
        total_debt=debt_by_year.get(anchor) if anchor is not None else None,
        cash=cash_by_year.get(anchor) if anchor is not None else None,
        fcf=fcf,
        eps_history=eps,
        roic_history=_roic_history(
            facts,
            asof=asof,
            ebit_by_year=ebit_by_year,
            equity_by_year=equity_by_year,
            debt_by_year=debt_by_year,
            cash_by_year=cash_by_year,
        ),
        gross_margin_history=_gross_margins(facts, asof=asof),
    )
