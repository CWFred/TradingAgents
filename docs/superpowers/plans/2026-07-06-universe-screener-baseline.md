# Small/Mid-Cap Universe + Point-in-Time Screener + Null-Baseline Portfolio — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build build-order step 3 of the long-horizon research system (`docs/long_horizon_research.md`): a $300M–$10B US-equity universe, a point-in-time fundamental screener gated on change triggers, a persistent deep-research queue, and the mandatory screen-only equal-weight paper portfolio (the "null baseline") that starts accruing track record the day this lands.

**Architecture:** A funnel of pure, injectable stages: Nasdaq-screener universe snapshot (one HTTP call, quarterly cache) → ADV liquidity filter (reuses `ops.universe.filters`) → per-name fundamentals from SEC XBRL company-facts (as-reported at filing date, never restated) → deterministic screen bars (2-of-3 valuation AND 2-of-3 quality AND ≥1 EDGAR/price change trigger) → SQLite screen store (the deep-research queue) → baseline paper portfolio driven through the existing `PaperBroker`/`Journal` with its own journal DB. The momentum sleeve and ops chassis are untouched.

**Tech Stack:** Python 3.10+, stdlib `sqlite3`, `requests`, `yfinance`, `click`, pytest (all network mocked). No new dependencies.

## Global Constraints

- Branch: `claude/smallcap-research-coverage-dervpt`. Commit and push there. Never commit to `main`.
- Lint: `ruff check` must pass (line-length 100, py310+, config in `pyproject.toml`).
- Tests: pytest, every test module sets `pytestmark = pytest.mark.unit`, ALL network/HTTP mocked (see `tests/test_edgar.py` for the house style).
- **Point-in-time discipline:** screener fundamentals must be as-reported at filing time. Only facts with `filed <= asof` are visible; when a fiscal year appears in multiple filings, the EARLIEST filing wins (restatements ignored).
- **LLM probabilities are never sizing inputs** (not relevant to this step — the baseline is equal-weight by construction — but do not add any probability-weighted sizing).
- **Do not modify:** `ops/main.py`, `ops/scheduler/orchestrator.py`, `ops/strategy/post_earnings_momentum.py`, `ops/universe/__init__.py`, `ops/universe/sp500.py`, `ops/universe/earnings.py`, `ops/broker/*`, `ops/journal.py`, `tradingagents/dataflows/edgar.py`. Everything is additive.
- New event kinds MUST be added to `ops/events.py` `BUILDERS` and `AUDIT_ONLY` — `tests/ops/notify/test_policy.py` enforces this.
- SEC fair access: all EDGAR-family HTTP goes through `edgar._throttled_get` so the process-wide throttle covers the combined request rate. Requires env `SEC_EDGAR_USER_AGENT="Your Name you@email.com"` at runtime (tests monkeypatch it).
- Money math in `Decimal`, never float. Convert at I/O boundaries with `Decimal(str(x))`.
- Vendor errors follow `tradingagents/dataflows/errors.py`; per-name failures in batch sweeps are logged to stderr and skipped (style of `ops/universe/filters.py`), never allowed to kill the sweep.

## Domain glossary (for implementers new to finance)

| Term | Meaning |
|---|---|
| Market cap | Share price × shares outstanding — what the whole company trades for. |
| ADV | Average daily dollar volume — how much of the stock trades per day; a liquidity measure. |
| EV (enterprise value) | Market cap + total debt − cash: the price to buy the whole business including its debts. |
| EBIT / EBITDA | Operating profit before interest+taxes / same before depreciation+amortization too. |
| EV/EBIT | A valuation multiple: lower = cheaper per dollar of operating profit. |
| FCF yield | Free cash flow (operating cash flow − capital expenditures) ÷ market cap. Higher = cheaper. |
| P/E | Price ÷ earnings per share. Compared here to the company's own 5-year history. |
| ROIC | Return on invested capital: after-tax operating profit ÷ (equity + debt − cash). Quality measure. |
| Gross margin | (Revenue − cost of goods) ÷ revenue. Stability over years signals a durable business. |
| 10-K / 10-Q / 8-K / 13D | SEC filings: annual report / quarterly report / material event / activist stake disclosure. |
| XBRL company facts | SEC's machine-readable database of every number a company ever filed, with filing dates. |

## File structure

| File | Responsibility |
|---|---|
| `tradingagents/dataflows/edgar_facts.py` (new) | XBRL company-facts fetch + point-in-time annual series extraction |
| `tradingagents/dataflows/fundamentals.py` (new) | Derive EBIT/EBITDA/FCF/debt/ROIC/margins from facts, concept fallback chains |
| `ops/universe/smallcap.py` (new) | Universe snapshot (Nasdaq screener API), sector/biotech exclusion, cap/price/ADV filters, quarterly JSON cache |
| `ops/research/__init__.py` (new) | Empty package init |
| `ops/research/triggers.py` (new) | Change-trigger detection: EDGAR filings + price selloff |
| `ops/research/prices.py` (new) | One-call-per-name daily price history context (selloff closes + fiscal-year-end closes) |
| `ops/research/screener.py` (new) | Pure screen logic: bars, sector medians, pass rule |
| `ops/research/store.py` (new) | SQLite screen-run/hit store = the deep-research queue |
| `ops/research/baseline.py` (new) | Null-baseline portfolio engine on PaperBroker |
| `ops/research/run.py` (new) | Composition root: universe → screen → store → baseline |
| `ops/config.py` (modify) | Add baseline journal path, baseline starting cash, screen store path |
| `ops/events.py` (modify) | Add `baseline_screen_run` / `baseline_exit` kinds + payloads |
| `ops/cli.py` (modify) | Add `ops screen` command |
| `docs/long_horizon_research.md` (modify) | Check off build-order step 3 |
| `docs/research_screener.md` (new) | Runbook: how to run, cadence, env vars |

Tests: `tests/test_edgar_facts.py`, `tests/test_fundamentals.py`, `tests/ops/universe/test_smallcap.py`, `tests/ops/research/{__init__.py,test_triggers.py,test_prices.py,test_screener.py,test_store.py,test_baseline.py,test_run.py}`.

---

### Task 1: EDGAR company-facts dataflow (`edgar_facts.py`)

**Files:**
- Create: `tradingagents/dataflows/edgar_facts.py`
- Test: `tests/test_edgar_facts.py`

**Interfaces:**
- Consumes: `edgar.get_cik(ticker) -> int`, `edgar._throttled_get(url) -> requests.Response` (private import is deliberate: the SEC throttle must be process-global across both modules).
- Produces (used by Tasks 2, 8):
  - `FactPoint` frozen dataclass: `concept: str, value: Decimal, unit: str, end: date, start: date | None, form: str, filed: date, accession: str`
  - `get_company_facts(ticker: str) -> dict` — raw companyfacts JSON payload
  - `annual_points(facts: dict, concept: str, *, asof: date, unit: str = "USD", taxonomy: str = "us-gaap") -> list[FactPoint]` — oldest-first
  - `annual_series(facts: dict, concepts: Sequence[str], *, asof: date, unit: str = "USD", max_years: int = 5) -> list[FactPoint]` — fallback chain, first concept with data wins
  - `latest_annual(facts: dict, concepts: Sequence[str], *, asof: date, unit: str = "USD") -> FactPoint | None`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for point-in-time XBRL company-facts extraction (no HTTP)."""

from datetime import date
from decimal import Decimal

import pytest

from tradingagents.dataflows import edgar_facts

pytestmark = pytest.mark.unit


def _row(val, *, end, filed, start=None, form="10-K", fp="FY", accn="acc-1"):
    row = {"val": val, "end": end, "filed": filed, "form": form, "fp": fp, "accn": accn}
    if start is not None:
        row["start"] = start
    return row


def _facts(concept_rows: dict, taxonomy="us-gaap", unit="USD"):
    return {
        "facts": {
            taxonomy: {
                concept: {"units": {unit: rows}} for concept, rows in concept_rows.items()
            }
        }
    }


def test_annual_points_point_in_time_excludes_future_filings():
    facts = _facts({"Revenues": [
        _row(100, start="2023-01-01", end="2023-12-31", filed="2024-02-15"),
        _row(120, start="2024-01-01", end="2024-12-31", filed="2025-02-15"),
    ]})
    pts = edgar_facts.annual_points(facts, "Revenues", asof=date(2024, 6, 1))
    assert [p.value for p in pts] == [Decimal("100")]


def test_annual_points_as_reported_earliest_filing_wins():
    # FY2023 appears in the original 10-K and restated in the FY2024 10-K.
    facts = _facts({"Revenues": [
        _row(100, start="2023-01-01", end="2023-12-31", filed="2024-02-15", accn="orig"),
        _row(95, start="2023-01-01", end="2023-12-31", filed="2025-02-15", accn="restated"),
    ]})
    pts = edgar_facts.annual_points(facts, "Revenues", asof=date(2025, 6, 1))
    assert len(pts) == 1
    assert pts[0].value == Decimal("100")
    assert pts[0].accession == "orig"


def test_annual_points_skips_short_duration_fy_rows_and_non_10k():
    facts = _facts({"Revenues": [
        _row(30, start="2023-10-01", end="2023-12-31", filed="2024-02-15"),  # Q4 slice
        _row(100, start="2023-01-01", end="2023-12-31", filed="2024-02-15"),
        _row(50, start="2023-01-01", end="2023-12-31", filed="2023-08-01", form="10-Q", fp="Q2"),
    ]})
    pts = edgar_facts.annual_points(facts, "Revenues", asof=date(2024, 6, 1))
    assert [p.value for p in pts] == [Decimal("100")]


def test_annual_points_accepts_instant_concepts_without_start():
    facts = _facts({"StockholdersEquity": [
        _row(500, end="2023-12-31", filed="2024-02-15"),
    ]})
    pts = edgar_facts.annual_points(facts, "StockholdersEquity", asof=date(2024, 6, 1))
    assert [p.value for p in pts] == [Decimal("500")]


def test_annual_series_fallback_chain_first_with_data_wins():
    facts = _facts({"RevenueFromContractWithCustomerExcludingAssessedTax": [
        _row(100, start="2023-01-01", end="2023-12-31", filed="2024-02-15"),
    ]})
    pts = edgar_facts.annual_series(
        facts,
        ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        asof=date(2024, 6, 1),
    )
    assert [p.value for p in pts] == [Decimal("100")]
    assert edgar_facts.annual_series(facts, ("NoSuch",), asof=date(2024, 6, 1)) == []


def test_annual_series_caps_at_max_years_keeping_newest():
    rows = [
        _row(i, start=f"{2018 + i}-01-01", end=f"{2018 + i}-12-31", filed=f"{2019 + i}-02-15")
        for i in range(7)
    ]
    facts = _facts({"Revenues": rows})
    pts = edgar_facts.annual_series(facts, ("Revenues",), asof=date(2026, 6, 1), max_years=5)
    assert len(pts) == 5
    assert pts[-1].end == date(2024, 12, 31)
    assert pts[0].end == date(2020, 12, 31)


def test_latest_annual_returns_newest_or_none():
    facts = _facts({"Revenues": [
        _row(100, start="2022-01-01", end="2022-12-31", filed="2023-02-15"),
        _row(120, start="2023-01-01", end="2023-12-31", filed="2024-02-15"),
    ]})
    pt = edgar_facts.latest_annual(facts, ("Revenues",), asof=date(2024, 6, 1))
    assert pt is not None and pt.value == Decimal("120")
    assert edgar_facts.latest_annual(facts, ("NoSuch",), asof=date(2024, 6, 1)) is None


def test_get_company_facts_resolves_cik_and_hits_facts_url(monkeypatch):
    calls = []

    class FakeResponse:
        def json(self):
            return {"facts": {}}

    monkeypatch.setattr(edgar_facts.edgar, "get_cik", lambda t: 320193)

    def fake_get(url, params=None):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(edgar_facts.edgar, "_throttled_get", fake_get)
    payload = edgar_facts.get_company_facts("AAPL")
    assert payload == {"facts": {}}
    assert calls == ["https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_edgar_facts.py -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'tradingagents.dataflows.edgar_facts'`

- [ ] **Step 3: Write the implementation**

```python
"""SEC XBRL company-facts: point-in-time annual fundamentals.

The companyfacts API returns every value a company ever filed for every XBRL
concept, each row tagged with the form, fiscal period, and — critically —
the ``filed`` date. That makes point-in-time discipline (design doc,
"non-negotiable constraints") free: a screener running as of date D sees only
rows filed on or before D, and when a fiscal year was later restated, the
EARLIEST filing wins — the value as the market first saw it.

HTTP goes through ``edgar._throttled_get`` on purpose: the SEC fair-access
cap applies per client, so this module must share the same process-global
throttle (and the same SEC_EDGAR_USER_AGENT requirement) as the rest of the
EDGAR vendor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

from tradingagents.dataflows import edgar

COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# fp="FY" rows include both true annual durations and Q4-only slices; a
# duration shorter than this is not an annual value.
_MIN_ANNUAL_SPAN_DAYS = 300


@dataclass(frozen=True)
class FactPoint:
    """One as-reported XBRL value."""

    concept: str
    value: Decimal
    unit: str
    end: date            # period end (fiscal year end for annual points)
    start: date | None   # None for instant (balance-sheet) concepts
    form: str
    filed: date
    accession: str


def get_company_facts(ticker: str) -> dict:
    """Fetch the raw companyfacts payload for a ticker (throttled, ~100KB-2MB)."""
    cik = edgar.get_cik(ticker)
    return edgar._throttled_get(COMPANY_FACTS_URL.format(cik=cik)).json()


def annual_points(
    facts: dict,
    concept: str,
    *,
    asof: date,
    unit: str = "USD",
    taxonomy: str = "us-gaap",
) -> list[FactPoint]:
    """As-reported annual values for one concept, oldest-first.

    Point-in-time rules: only 10-K family rows with fp="FY" filed on or
    before ``asof``; per fiscal year (keyed by period end) the earliest
    filing wins, so later restatements never leak backwards in time.
    """
    rows = (
        facts.get("facts", {}).get(taxonomy, {}).get(concept, {}).get("units", {}).get(unit, [])
    )
    by_end: dict[date, FactPoint] = {}
    for row in rows:
        if not row.get("form", "").startswith("10-K"):
            continue
        if row.get("fp") != "FY":
            continue
        filed = date.fromisoformat(row["filed"])
        if filed > asof:
            continue
        end = date.fromisoformat(row["end"])
        start = date.fromisoformat(row["start"]) if row.get("start") else None
        if start is not None and (end - start).days < _MIN_ANNUAL_SPAN_DAYS:
            continue
        point = FactPoint(
            concept=concept,
            value=Decimal(str(row["val"])),
            unit=unit,
            end=end,
            start=start,
            form=row["form"],
            filed=filed,
            accession=row.get("accn", ""),
        )
        existing = by_end.get(end)
        if existing is None or filed < existing.filed:
            by_end[end] = point
    return sorted(by_end.values(), key=lambda p: p.end)


def annual_series(
    facts: dict,
    concepts: Sequence[str],
    *,
    asof: date,
    unit: str = "USD",
    max_years: int = 5,
) -> list[FactPoint]:
    """Annual points via a concept fallback chain, newest ``max_years`` only.

    The first concept with any data wins the whole series — mixing concepts
    across years would splice incompatible definitions.
    """
    for concept in concepts:
        points = annual_points(facts, concept, asof=asof, unit=unit)
        if points:
            return points[-max_years:]
    return []


def latest_annual(
    facts: dict,
    concepts: Sequence[str],
    *,
    asof: date,
    unit: str = "USD",
) -> FactPoint | None:
    series = annual_series(facts, concepts, asof=asof, unit=unit, max_years=1)
    return series[-1] if series else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_edgar_facts.py -v`
Expected: 8 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check tradingagents/dataflows/edgar_facts.py tests/test_edgar_facts.py
git add tradingagents/dataflows/edgar_facts.py tests/test_edgar_facts.py
git commit -m "feat(dataflows): point-in-time XBRL company-facts extraction"
```

---

### Task 2: Derived fundamentals (`fundamentals.py`)

**Files:**
- Create: `tradingagents/dataflows/fundamentals.py`
- Test: `tests/test_fundamentals.py`

**Interfaces:**
- Consumes: `edgar_facts.annual_series`, `edgar_facts.FactPoint` (Task 1 signatures above).
- Produces (used by Tasks 5, 8):
  - `YearValue` frozen dataclass: `fiscal_year_end: date, value: Decimal`
  - `Fundamentals` frozen dataclass: `ticker: str, asof: date, ebit: Decimal | None, ebitda: Decimal | None, total_debt: Decimal | None, cash: Decimal | None, fcf: Decimal | None, eps_history: tuple[YearValue, ...], roic_history: tuple[YearValue, ...], gross_margin_history: tuple[YearValue, ...]` (histories oldest-first, ≤5 entries)
  - `compute_fundamentals(ticker: str, facts: dict, *, asof: date) -> Fundamentals`

- [ ] **Step 1: Write the failing tests**

```python
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


def test_eps_history_oldest_first():
    facts = _facts({
        "EarningsPerShareDiluted": [_row("2.5", 2024), _row("3.0", 2025)],
    })
    f = compute_fundamentals("TEST", facts, asof=ASOF)
    assert [m.fiscal_year_end for m in f.eps_history] == [
        date(2024, 12, 31), date(2025, 12, 31),
    ]
    assert [m.value for m in f.eps_history] == [Decimal("2.5"), Decimal("3.0")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fundamentals.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradingagents.dataflows.fundamentals'`

- [ ] **Step 3: Write the implementation**

```python
"""Derived annual fundamentals from SEC XBRL company facts.

Everything is point-in-time via ``edgar_facts.annual_series`` (as-reported at
filing date). Concept fallback chains absorb the most common tagging
variation across filers; within one metric the first chain member with any
data wins the whole series (mixing concepts across years would splice
incompatible definitions).

Missing-data policy: a metric that cannot be computed is None / empty — the
screener treats missing as a failed bar, never as a pass. The one deliberate
exception: a company with a balance sheet (equity filed) but no debt concepts
is treated as debt = 0, because debt-free small caps simply omit the tags and
returning None would fail the leverage bar for exactly the best names.
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


def _debt_by_year(facts: dict, *, asof: date, has_balance_sheet: bool) -> dict[date, Decimal]:
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
        return {y: _ZERO for y in equity_years}
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
    debt_by_year = _debt_by_year(facts, asof=asof, has_balance_sheet=bool(equity_by_year))

    ebit = _latest(ebit_pts)
    da = _latest(annual_series(facts, DA_CONCEPTS, asof=asof))
    ebitda = ebit + da if ebit is not None and da is not None else None
    cfo = _latest(annual_series(facts, CFO_CONCEPTS, asof=asof))
    capex = _latest(annual_series(facts, CAPEX_CONCEPTS, asof=asof))
    fcf = cfo - capex if cfo is not None and capex is not None else None

    eps = tuple(
        YearValue(p.end, p.value)
        for p in annual_series(facts, EPS_CONCEPTS, asof=asof, unit="USD/shares")
    )

    return Fundamentals(
        ticker=ticker.upper(),
        asof=asof,
        ebit=ebit,
        ebitda=ebitda,
        total_debt=max(debt_by_year.items())[1] if debt_by_year else None,
        cash=max(cash_by_year.items())[1] if cash_by_year else None,
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_fundamentals.py tests/test_edgar_facts.py -v`
Expected: all passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check tradingagents/dataflows/fundamentals.py tests/test_fundamentals.py
git add tradingagents/dataflows/fundamentals.py tests/test_fundamentals.py
git commit -m "feat(dataflows): derived point-in-time fundamentals with concept fallbacks"
```

---

### Task 3: Small/mid-cap universe (`ops/universe/smallcap.py`)

**Files:**
- Create: `ops/universe/smallcap.py`
- Test: `tests/ops/universe/test_smallcap.py`

**Interfaces:**
- Consumes: `ops.universe.filters.apply_liquidity_filter(symbols, *, min_adv, min_price, fetch_metrics) -> list[tuple[str, Decimal, Decimal]]` and `filters.fetch_price_and_adv_from_yfinance` (existing).
- Produces (used by Task 8):
  - `SmallcapMember` frozen dataclass: `symbol: str, name: str, sector: str, industry: str, market_cap: Decimal, last_price: Decimal`
  - `UniverseName` frozen dataclass: `member: SmallcapMember, last_price: Decimal, adv_20d: Decimal` (`last_price` here is the fresher yfinance price from the ADV pass)
  - `load_smallcap_members(*, fetch: Callable[[], list[dict]] | None = None) -> list[SmallcapMember]`
  - `build_smallcap_universe(*, cache_path: Path | None = None, max_age_days: int = 90, members_loader=None, metrics_fetcher=None) -> list[UniverseName]`
  - Constants: `MIN_MARKET_CAP = Decimal("300000000")`, `MAX_MARKET_CAP = Decimal("10000000000")`, `MIN_PRICE = Decimal("5")`, `MIN_ADV = Decimal("2000000")`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the small/mid-cap universe (no HTTP, no yfinance)."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ops.universe import smallcap

pytestmark = pytest.mark.unit


def _rows():
    return [
        {"symbol": "GOOD", "name": "Good Co", "lastsale": "$25.00",
         "marketCap": "1,500,000,000.00", "sector": "Industrials", "industry": "Machinery"},
        {"symbol": "BIGG", "name": "Too Big", "lastsale": "$100.00",
         "marketCap": "50,000,000,000.00", "sector": "Technology", "industry": "Software"},
        {"symbol": "TINY", "name": "Too Small", "lastsale": "$8.00",
         "marketCap": "100,000,000.00", "sector": "Industrials", "industry": "Machinery"},
        {"symbol": "CHEP", "name": "Penny", "lastsale": "$2.00",
         "marketCap": "900,000,000.00", "sector": "Industrials", "industry": "Machinery"},
        {"symbol": "BANK", "name": "A Bank", "lastsale": "$30.00",
         "marketCap": "2,000,000,000.00", "sector": "Finance", "industry": "Major Banks"},
        {"symbol": "GENE", "name": "Bio Co", "lastsale": "$30.00",
         "marketCap": "2,000,000,000.00", "sector": "Health Care",
         "industry": "Biotechnology: Biological Products (No Diagnostic Substances)"},
        {"symbol": "PFD^A", "name": "Preferred", "lastsale": "$25.00",
         "marketCap": "1,000,000,000.00", "sector": "Industrials", "industry": "Machinery"},
        {"symbol": "NOCAP", "name": "No Cap", "lastsale": "$25.00",
         "marketCap": "", "sector": "Industrials", "industry": "Machinery"},
    ]


def test_load_members_applies_cap_price_sector_and_symbol_filters():
    members = smallcap.load_smallcap_members(fetch=_rows)
    assert [m.symbol for m in members] == ["GOOD"]
    m = members[0]
    assert m.market_cap == Decimal("1500000000.00")
    assert m.last_price == Decimal("25.00")
    assert m.sector == "Industrials"


def test_build_universe_applies_adv_filter_and_caches(tmp_path):
    cache = tmp_path / "universe.json"

    def metrics(symbol):
        assert symbol == "GOOD"
        return (Decimal("25.50"), Decimal("3000000"))

    names = smallcap.build_smallcap_universe(
        cache_path=cache,
        members_loader=lambda: smallcap.load_smallcap_members(fetch=_rows),
        metrics_fetcher=metrics,
    )
    assert len(names) == 1
    assert names[0].member.symbol == "GOOD"
    assert names[0].adv_20d == Decimal("3000000")
    assert names[0].last_price == Decimal("25.50")
    assert cache.exists()

    # Second call must come from cache: loaders that explode prove it.
    names2 = smallcap.build_smallcap_universe(
        cache_path=cache,
        members_loader=lambda: (_ for _ in ()).throw(AssertionError("hit network")),
        metrics_fetcher=lambda s: (_ for _ in ()).throw(AssertionError("hit yfinance")),
    )
    assert names2 == names


def test_build_universe_refreshes_stale_cache(tmp_path):
    cache = tmp_path / "universe.json"
    stale = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    cache.write_text(json.dumps({"built_at": stale, "names": []}))

    names = smallcap.build_smallcap_universe(
        cache_path=cache,
        members_loader=lambda: smallcap.load_smallcap_members(fetch=_rows),
        metrics_fetcher=lambda s: (Decimal("25.50"), Decimal("3000000")),
    )
    assert [n.member.symbol for n in names] == ["GOOD"]


def test_adv_below_floor_is_dropped(tmp_path):
    names = smallcap.build_smallcap_universe(
        cache_path=tmp_path / "u.json",
        members_loader=lambda: smallcap.load_smallcap_members(fetch=_rows),
        metrics_fetcher=lambda s: (Decimal("25.50"), Decimal("500000")),
    )
    assert names == []


def test_fetch_rejects_suspiciously_small_row_count(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"rows": [{"symbol": "X"}]}}

    monkeypatch.setattr(
        smallcap.requests, "get", lambda *a, **k: FakeResponse()
    )
    with pytest.raises(RuntimeError, match="only 1 rows"):
        smallcap._fetch_from_nasdaq()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ops/universe/test_smallcap.py -v`
Expected: FAIL with `ModuleNotFoundError` / `ImportError` on `ops.universe.smallcap`

- [ ] **Step 3: Write the implementation**

```python
"""Small/mid-cap US equity universe.

Source: the Nasdaq stock-screener API — one HTTP call returns every
NYSE/Nasdaq/AMEX listing with symbol, last sale, market cap, sector, and
industry, which replaces thousands of per-name lookups. It is an unofficial
endpoint, so the fetch validates row count and the result is cached to JSON
(same pattern as sp500.py) with a quarterly TTL per the design doc.

Deterministic universe filters (design doc "funnel" stage 1):
  market cap $300M-$10B, price > $5, 20-day ADV > $2M,
  financials excluded (sector == "Finance", which also removes SPAC shells),
  biotech excluded (industry starts with "Biotechnology:" — this includes
  pharmaceutical preparations; deliberately conservative for v1).

ADV comes from the existing yfinance-backed liquidity filter
(ops.universe.filters), reused unchanged.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

import requests

from ops.universe.filters import apply_liquidity_filter, fetch_price_and_adv_from_yfinance

NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"

MIN_MARKET_CAP = Decimal("300000000")
MAX_MARKET_CAP = Decimal("10000000000")
MIN_PRICE = Decimal("5")
MIN_ADV = Decimal("2000000")

_EXCLUDED_SECTORS = frozenset({"Finance"})
_EXCLUDED_INDUSTRY_PREFIXES = ("Biotechnology:",)
# Nasdaq notation for preferred shares / warrants / units — not common equity.
_NON_COMMON_CHARS = ("^", "/", " ")


@dataclass(frozen=True)
class SmallcapMember:
    symbol: str
    name: str
    sector: str
    industry: str
    market_cap: Decimal
    last_price: Decimal


@dataclass(frozen=True)
class UniverseName:
    member: SmallcapMember
    last_price: Decimal   # from the ADV pass (yfinance) — fresher than the snapshot
    adv_20d: Decimal


def _default_cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(os.path.expanduser(base)) / "tradingagents" / "smallcap_universe.json"


def _parse_money(raw: object) -> Decimal | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return Decimal(raw.replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None


def _fetch_from_nasdaq() -> list[dict]:
    resp = requests.get(
        NASDAQ_SCREENER_URL,
        params={"tableonly": "true", "limit": "10000", "download": "true"},
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    rows = ((resp.json().get("data") or {}).get("rows")) or []
    if len(rows) < 1000:
        raise RuntimeError(
            f"nasdaq screener returned only {len(rows)} rows — API format changed?"
        )
    return rows


def load_smallcap_members(*, fetch: Callable[[], list[dict]] | None = None) -> list[SmallcapMember]:
    """Snapshot members passing the deterministic (non-ADV) universe filters."""
    fetch = fetch or _fetch_from_nasdaq
    out: list[SmallcapMember] = []
    for row in fetch():
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol or any(ch in symbol for ch in _NON_COMMON_CHARS):
            continue
        sector = (row.get("sector") or "").strip()
        industry = (row.get("industry") or "").strip()
        if sector in _EXCLUDED_SECTORS:
            continue
        if industry.startswith(_EXCLUDED_INDUSTRY_PREFIXES):
            continue
        market_cap = _parse_money(row.get("marketCap"))
        last_price = _parse_money(row.get("lastsale"))
        if market_cap is None or last_price is None:
            continue
        if not (MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP):
            continue
        if last_price <= MIN_PRICE:
            continue
        out.append(
            SmallcapMember(
                symbol=symbol,
                name=(row.get("name") or "").strip(),
                sector=sector,
                industry=industry,
                market_cap=market_cap,
                last_price=last_price,
            )
        )
    out.sort(key=lambda m: m.symbol)
    return out


def _to_json(names: list[UniverseName]) -> str:
    return json.dumps({
        "built_at": datetime.now(timezone.utc).isoformat(),
        "names": [
            {
                "symbol": n.member.symbol, "name": n.member.name,
                "sector": n.member.sector, "industry": n.member.industry,
                "market_cap": str(n.member.market_cap),
                "snapshot_price": str(n.member.last_price),
                "last_price": str(n.last_price), "adv_20d": str(n.adv_20d),
            }
            for n in names
        ],
    })


def _from_json(data: dict) -> list[UniverseName]:
    return [
        UniverseName(
            member=SmallcapMember(
                symbol=d["symbol"], name=d["name"], sector=d["sector"],
                industry=d["industry"], market_cap=Decimal(d["market_cap"]),
                last_price=Decimal(d["snapshot_price"]),
            ),
            last_price=Decimal(d["last_price"]),
            adv_20d=Decimal(d["adv_20d"]),
        )
        for d in data["names"]
    ]


def build_smallcap_universe(
    *,
    cache_path: Path | None = None,
    max_age_days: int = 90,
    members_loader: Callable[[], list[SmallcapMember]] | None = None,
    metrics_fetcher: Callable[[str], tuple[Decimal, Decimal] | None] | None = None,
) -> list[UniverseName]:
    """Members + ADV liquidity filter, cached quarterly.

    The ADV pass is one yfinance history call per surviving member (~1-2k
    names); with the quarterly cache this cost is paid four times a year.
    """
    cache_path = cache_path or _default_cache_path()
    members_loader = members_loader or load_smallcap_members
    metrics_fetcher = metrics_fetcher or fetch_price_and_adv_from_yfinance
    if cache_path.exists():
        data = json.loads(cache_path.read_text())
        built_at = datetime.fromisoformat(data["built_at"])
        if datetime.now(timezone.utc) - built_at < timedelta(days=max_age_days):
            return _from_json(data)
    members = members_loader()
    by_symbol = {m.symbol: m for m in members}
    print(
        f"[smallcap] ADV-filtering {len(members)} names via yfinance "
        "(slow; result cached quarterly)",
        file=sys.stderr,
    )
    liquid = apply_liquidity_filter(
        sorted(by_symbol), min_adv=MIN_ADV, min_price=MIN_PRICE,
        fetch_metrics=metrics_fetcher,
    )
    names = [
        UniverseName(member=by_symbol[sym], last_price=price, adv_20d=adv)
        for sym, price, adv in liquid
    ]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(_to_json(names))
    return names
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ops/universe/test_smallcap.py -v`
Expected: 5 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check ops/universe/smallcap.py tests/ops/universe/test_smallcap.py
git add ops/universe/smallcap.py tests/ops/universe/test_smallcap.py
git commit -m "feat(universe): small/mid-cap universe with sector exclusions and quarterly cache"
```

---

### Task 4: Change triggers (`ops/research/triggers.py`)

**Files:**
- Create: `ops/research/__init__.py` (empty, one docstring line: `"""Long-horizon research pipeline: screener, triggers, baseline portfolio."""`)
- Create: `ops/research/triggers.py`
- Test: `tests/ops/research/__init__.py` (empty), `tests/ops/research/test_triggers.py`

**Interfaces:**
- Consumes: `edgar.list_filings(ticker, *, forms, since, limit) -> list[Filing]`, `edgar.CHANGE_TRIGGER_FORMS: dict[str, str]`, `Filing.trigger_kind() -> str | None`, `Filing.notable_8k_items() -> list[str]`, `Filing.accession_number`, `Filing.form`, `Filing.filing_date`.
- Produces (used by Tasks 5, 8):
  - `Trigger` frozen dataclass: `kind: str, description: str, date: date, source: str` (`source` = accession number or `"price"`)
  - `find_edgar_triggers(ticker: str, *, asof: date, lookback_days: int = 90, list_filings=None) -> list[Trigger]`
  - `find_selloff_trigger(symbol: str, closes: list[Decimal], *, asof: date) -> Trigger | None` (`closes` oldest-first, ≤60 most recent daily closes)
  - Constants: `TRIGGER_LOOKBACK_DAYS = 90`, `SELLOFF_LOOKBACK_DAYS = 60`, `SELLOFF_DRAWDOWN = Decimal("0.25")`, `_MIN_SELLOFF_HISTORY = 20`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for change-trigger detection (EDGAR mocked, no yfinance)."""

from datetime import date
from decimal import Decimal

import pytest

from ops.research.triggers import Trigger, find_edgar_triggers, find_selloff_trigger
from tradingagents.dataflows.edgar import Filing

pytestmark = pytest.mark.unit

ASOF = date(2026, 7, 1)


def _filing(form, filed, items=(), accn="0001-26-000001"):
    return Filing(
        ticker="TEST", cik=1, accession_number=accn, form=form,
        filing_date=filed, report_date=None, primary_document="doc.htm",
        items=tuple(items),
    )


def test_edgar_triggers_classified_by_form():
    filings = [
        _filing("SC 13D", date(2026, 6, 20), accn="a1"),
        _filing("SC TO-I", date(2026, 6, 10), accn="a2"),
    ]
    triggers = find_edgar_triggers(
        "TEST", asof=ASOF, list_filings=lambda t, **kw: filings,
    )
    assert [(t.kind, t.source) for t in triggers] == [
        ("activist_stake", "a1"), ("tender_offer", "a2"),
    ]


def test_8k_only_triggers_on_notable_items():
    filings = [
        _filing("8-K", date(2026, 6, 20), items=("5.02", "9.01"), accn="a1"),
        _filing("8-K", date(2026, 6, 10), items=("7.01",), accn="a2"),
    ]
    triggers = find_edgar_triggers(
        "TEST", asof=ASOF, list_filings=lambda t, **kw: filings,
    )
    assert len(triggers) == 1
    assert triggers[0].kind == "material_event"
    assert "officer_departure_or_election" in triggers[0].description


def test_form4_is_excluded_and_lookback_forwarded():
    seen = {}

    def fake_list(ticker, *, forms=None, since=None, limit=100):
        seen["forms"] = forms
        seen["since"] = since
        return []

    find_edgar_triggers("TEST", asof=ASOF, list_filings=fake_list)
    assert "4" not in seen["forms"]           # deferred to build-order step 4
    assert "SC 13D" in seen["forms"]
    assert seen["since"] == date(2026, 4, 2)  # asof - 90 days


def test_filings_after_asof_are_ignored():
    filings = [_filing("SC 13D", date(2026, 7, 2))]
    assert find_edgar_triggers("TEST", asof=ASOF, list_filings=lambda t, **kw: filings) == []


def test_selloff_trigger_fires_at_25pct_drawdown():
    closes = [Decimal("100")] * 30 + [Decimal("74")]
    t = find_selloff_trigger("TEST", closes, asof=ASOF)
    assert t is not None
    assert t.kind == "selloff"
    assert t.source == "price"


def test_selloff_no_trigger_on_shallow_drawdown_or_short_history():
    assert find_selloff_trigger("TEST", [Decimal("100")] * 30 + [Decimal("80")], asof=ASOF) is None
    assert find_selloff_trigger("TEST", [Decimal("100"), Decimal("70")], asof=ASOF) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ops/research/test_triggers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.research'`

- [ ] **Step 3: Write the implementation**

```python
"""Change-trigger detection — the reason to look at a name NOW.

A name enters deep research only when it is cheap+quality AND has a change
trigger (design doc: "looking at everything all the time drowns in noise").
Two sources:

- EDGAR filings (via the existing edgar vendor's trigger taxonomy): 13D
  activists, tenders, spinoff registrations, going-private, and 8-Ks whose
  item numbers are in edgar.NOTABLE_8K_ITEMS. Form 4 insider clusters are
  DEFERRED to build-order step 4: raw Form 4 counts are dominated by routine
  sales and grants, and separating open-market buys needs the XML parser
  that step builds.
- Price: a guidance-cut-style selloff, defined as the latest close sitting
  >= 25% below the 60-trading-day high.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable

from tradingagents.dataflows import edgar

TRIGGER_LOOKBACK_DAYS = 90
SELLOFF_LOOKBACK_DAYS = 60
SELLOFF_DRAWDOWN = Decimal("0.25")
# Fewer closes than this and the "60-day high" is meaningless (fresh IPO).
_MIN_SELLOFF_HISTORY = 20


@dataclass(frozen=True)
class Trigger:
    kind: str          # e.g. "activist_stake", "material_event", "selloff"
    description: str
    date: date
    source: str        # accession number, or "price" for the selloff trigger


def find_edgar_triggers(
    ticker: str,
    *,
    asof: date,
    lookback_days: int = TRIGGER_LOOKBACK_DAYS,
    list_filings: Callable[..., list[edgar.Filing]] | None = None,
) -> list[Trigger]:
    list_filings = list_filings or edgar.list_filings
    forms = set(edgar.CHANGE_TRIGGER_FORMS) - {"4"}
    filings = list_filings(ticker, forms=forms, since=asof - timedelta(days=lookback_days))
    out: list[Trigger] = []
    for f in filings:
        if f.filing_date is None or f.filing_date > asof:
            continue
        if f.form == "8-K":
            labels = f.notable_8k_items()
            if not labels:
                continue
            out.append(Trigger(
                kind="material_event", description=", ".join(labels),
                date=f.filing_date, source=f.accession_number,
            ))
            continue
        kind = f.trigger_kind()
        if kind is None:
            continue
        out.append(Trigger(
            kind=kind, description=f.form,
            date=f.filing_date, source=f.accession_number,
        ))
    return out


def find_selloff_trigger(
    symbol: str, closes: list[Decimal], *, asof: date,
) -> Trigger | None:
    """``closes``: up to the last 60 daily closes ending at ``asof``, oldest-first."""
    if len(closes) < _MIN_SELLOFF_HISTORY:
        return None
    peak = max(closes)
    last = closes[-1]
    if peak <= 0:
        return None
    drawdown = (peak - last) / peak
    if drawdown < SELLOFF_DRAWDOWN:
        return None
    return Trigger(
        kind="selloff",
        description=(
            f"{symbol} close {last} is {(drawdown * 100).quantize(Decimal('1'))}% "
            f"below its {SELLOFF_LOOKBACK_DAYS}-day high {peak}"
        ),
        date=asof,
        source="price",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ops/research/test_triggers.py -v`
Expected: 6 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check ops/research/ tests/ops/research/
git add ops/research/ tests/ops/research/
git commit -m "feat(research): change-trigger detection from EDGAR filings and price selloffs"
```

---

### Task 5: Price context (`ops/research/prices.py`)

**Files:**
- Create: `ops/research/prices.py`
- Test: `tests/ops/research/test_prices.py`

**Interfaces:**
- Consumes: `yfinance` (only inside `fetch_price_context`, mirroring the error style of `ops/universe/filters.py`), `ops.universe.earnings._safe_decimal`.
- Produces (used by Task 8):
  - `PriceContext` frozen dataclass with field `closes: dict[date, Decimal]` (trading day → close, ~6 years) and methods:
    - `recent_closes(*, asof: date, days: int = 60) -> list[Decimal]` — closes for the last `days` trading dates ≤ asof, oldest-first
    - `close_on_or_before(when: date, *, max_gap_days: int = 10) -> Decimal | None`
  - `fetch_price_context(symbol: str) -> PriceContext | None` — one yfinance `history(period="6y")` call per name; None + stderr diagnostic on failure

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for PriceContext (no yfinance)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from ops.research.prices import PriceContext

pytestmark = pytest.mark.unit


def _ctx():
    # 100 consecutive weekdays ending Tuesday 2026-06-30. Values encode
    # recency: the NEWEST day was inserted first, so newer date = smaller
    # value (newest = 10.00, next 10.01, ...).
    closes = {}
    d = date(2026, 6, 30)
    while len(closes) < 100:
        if d.weekday() < 5:
            closes[d] = Decimal("10") + Decimal(len(closes)) / 100
        d -= timedelta(days=1)
    return PriceContext(closes=closes)


def test_recent_closes_returns_last_n_trading_days_oldest_first():
    ctx = _ctx()
    closes = ctx.recent_closes(asof=date(2026, 6, 30), days=60)
    assert len(closes) == 60
    # Oldest-first: values strictly descend toward the newest close (10.00).
    assert closes == sorted(closes, reverse=True)
    assert closes[-1] == Decimal("10.00")


def test_recent_closes_excludes_dates_after_asof():
    ctx = _ctx()
    closes = ctx.recent_closes(asof=date(2026, 6, 15), days=60)
    assert len(closes) == 60
    # 2026-06-15 is the 12th-newest trading day in the fixture (index 11),
    # so with everything after asof excluded the newest value is 10.11.
    assert closes[-1] == Decimal("10.11")


def test_close_on_or_before_picks_prior_trading_day():
    ctx = _ctx()
    # 2026-06-28 is a Sunday; the prior trading day is Friday 2026-06-26.
    assert ctx.close_on_or_before(date(2026, 6, 28)) == ctx.closes[date(2026, 6, 26)]


def test_close_on_or_before_respects_max_gap():
    ctx = _ctx()
    oldest = min(ctx.closes)
    assert ctx.close_on_or_before(oldest - timedelta(days=30)) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ops/research/test_prices.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.research.prices'`

- [ ] **Step 3: Write the implementation**

```python
"""Per-name daily price history, fetched once and reused.

The screener needs prices twice per name — the last 60 closes for the
selloff trigger and a close near each fiscal year end for the P/E-history
bar. One 6-year yfinance history call serves both, instead of six separate
calls per name.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import yfinance as yf

from ops.universe.earnings import _safe_decimal


@dataclass(frozen=True)
class PriceContext:
    closes: dict[date, Decimal]  # trading day -> close

    def recent_closes(self, *, asof: date, days: int = 60) -> list[Decimal]:
        dates = sorted(d for d in self.closes if d <= asof)[-days:]
        return [self.closes[d] for d in dates]

    def close_on_or_before(self, when: date, *, max_gap_days: int = 10) -> Decimal | None:
        for offset in range(max_gap_days + 1):
            d = when - timedelta(days=offset)
            if d in self.closes:
                return self.closes[d]
        return None


def fetch_price_context(symbol: str) -> PriceContext | None:
    """6 years of daily closes; None (with a stderr diagnostic) on any fetch failure."""
    try:
        hist = yf.Ticker(symbol).history(period="6y", auto_adjust=False)
    except Exception as exc:
        print(
            f"[prices] skipped {symbol}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    if hist is None or hist.empty or "Close" not in hist:
        return None
    closes: dict[date, Decimal] = {}
    for ts, close in hist["Close"].items():
        value = _safe_decimal(close)
        if value > 0:
            closes[ts.date()] = value
    return PriceContext(closes=closes) if closes else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ops/research/test_prices.py -v`
Expected: 4 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check ops/research/prices.py tests/ops/research/test_prices.py
git add ops/research/prices.py tests/ops/research/test_prices.py
git commit -m "feat(research): shared per-name daily price context"
```

---

### Task 6: The screener (`ops/research/screener.py`)

**Files:**
- Create: `ops/research/screener.py`
- Test: `tests/ops/research/test_screener.py`

**Interfaces:**
- Consumes: `Fundamentals`, `YearValue` (Task 2), `Trigger` (Task 4). Pure module — no I/O, no network.
- Produces (used by Tasks 7, 8):
  - `NameInputs` frozen dataclass: `symbol: str, sector: str, price: Decimal, market_cap: Decimal, fundamentals: Fundamentals, triggers: tuple[Trigger, ...], year_end_prices: dict[date, Decimal]`
  - `Bar` frozen dataclass: `name: str, passed: bool, detail: str`
  - `ScreenResult` frozen dataclass: `symbol: str, asof: date, passed: bool, cheap: bool, quality: bool, valuation_bars: tuple[Bar, ...], quality_bars: tuple[Bar, ...], triggers: tuple[Trigger, ...], market_cap: Decimal, ev_ebit: Decimal | None`
  - `screen_universe(inputs: list[NameInputs], *, asof: date) -> list[ScreenResult]`
  - Constants: `FCF_YIELD_MIN = Decimal("0.06")`, `ROIC_MIN = Decimal("0.12")`, `DEBT_EBITDA_MAX = Decimal("3")`, `GROSS_MARGIN_BAND_MAX = Decimal("0.10")`, `MIN_HISTORY_YEARS = 3`, `MIN_SECTOR_PEERS = 5`

**Screen rules (locked by the design doc — do not soften):**
- Valuation bars (cheap = ≥2 of 3 pass): (V1) EV/EBIT positive and below the sector median (sector = universe peers with valid EV/EBIT; sectors with <5 such peers compare against the whole-universe median); (V2) FCF yield > 6%; (V3) current P/E below the median of its own historical fiscal-year-end P/Es (≥3 usable years, positive EPS only).
- Quality bars (quality = ≥2 of 3 pass): (Q1) mean 5y ROIC > 12% with ≥3 years; (Q2) total debt / EBITDA < 3 with EBITDA > 0; (Q3) gross-margin band (max−min over ≤5y) ≤ 10 percentage points with ≥3 years.
- A bar with missing data FAILS (detail says why) — missing data never passes a screen.
- `passed = cheap AND quality AND len(triggers) >= 1`.

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the pure screener (no I/O)."""

from datetime import date
from decimal import Decimal

import pytest

from ops.research.screener import NameInputs, screen_universe
from ops.research.triggers import Trigger
from tradingagents.dataflows.fundamentals import Fundamentals, YearValue

pytestmark = pytest.mark.unit

ASOF = date(2026, 7, 1)
D = Decimal


def _fund(ticker, **overrides):
    yv = lambda pairs: tuple(YearValue(date(y, 12, 31), D(str(v))) for y, v in pairs)
    defaults = dict(
        ticker=ticker, asof=ASOF,
        ebit=D("100"), ebitda=D("120"), total_debt=D("100"), cash=D("50"),
        fcf=D("80"),
        eps_history=yv([(2021, "2.0"), (2022, "2.2"), (2023, "2.4"), (2024, "2.6"), (2025, "2.0")]),
        roic_history=yv([(2023, "0.15"), (2024, "0.16"), (2025, "0.14")]),
        gross_margin_history=yv([(2023, "0.40"), (2024, "0.42"), (2025, "0.41")]),
    )
    defaults.update(overrides)
    return Fundamentals(**defaults)


def _trigger():
    return Trigger(kind="activist_stake", description="SC 13D", date=ASOF, source="a1")


def _inputs(symbol, *, sector="Industrials", market_cap=D("1000"), price=D("20"),
            triggers=(), fund=None):
    year_end_prices = {date(y, 12, 31): D("40") for y in range(2021, 2026)}
    return NameInputs(
        symbol=symbol, sector=sector, price=price, market_cap=market_cap,
        fundamentals=fund or _fund(symbol), triggers=tuple(triggers),
        year_end_prices=year_end_prices,
    )


def _expensive_peer(symbol):
    # Same sector, EV/EBIT far above the candidate's, to anchor the median.
    return _inputs(symbol, market_cap=D("5000"), fund=_fund(symbol, ebit=D("100")))


def test_cheap_quality_and_trigger_passes():
    # Candidate: EV = 1000 + 100 - 50 = 1050, EV/EBIT = 10.5 vs peers at 50.5.
    # FCF yield = 80/1000 = 8% > 6%. Current P/E = 20/2.0 = 10 vs history 40/eps ~ 15-20.
    universe = [
        _inputs("GOOD", triggers=[_trigger()]),
        *[_expensive_peer(f"PEER{i}") for i in range(5)],
    ]
    results = {r.symbol: r for r in screen_universe(universe, asof=ASOF)}
    good = results["GOOD"]
    assert good.cheap and good.quality and good.passed
    assert [b.passed for b in good.valuation_bars] == [True, True, True]
    assert [b.passed for b in good.quality_bars] == [True, True, True]


def test_no_trigger_means_no_pass_even_when_cheap_and_quality():
    universe = [
        _inputs("GOOD", triggers=[]),
        *[_expensive_peer(f"PEER{i}") for i in range(5)],
    ]
    good = {r.symbol: r for r in screen_universe(universe, asof=ASOF)}["GOOD"]
    assert good.cheap and good.quality and not good.passed


def test_missing_data_fails_bars_not_passes():
    fund = _fund("MISS", ebit=None, ebitda=None, fcf=None,
                 roic_history=(), gross_margin_history=(), eps_history=())
    universe = [
        _inputs("MISS", triggers=[_trigger()], fund=fund),
        *[_expensive_peer(f"PEER{i}") for i in range(5)],
    ]
    miss = {r.symbol: r for r in screen_universe(universe, asof=ASOF)}["MISS"]
    assert not miss.passed
    assert all(not b.passed for b in miss.valuation_bars)
    # Q2 (debt/EBITDA) fails because EBITDA is missing.
    assert all(not b.passed for b in miss.quality_bars)
    assert any("missing" in b.detail for b in miss.valuation_bars)


def test_small_sector_falls_back_to_universe_median():
    # Only 2 names in "Rare" sector (< MIN_SECTOR_PEERS): candidate must be
    # compared against the whole-universe median instead.
    universe = [
        _inputs("RARE", sector="Rare", triggers=[_trigger()]),
        _inputs("RARE2", sector="Rare", market_cap=D("5000")),
        *[_expensive_peer(f"PEER{i}") for i in range(5)],
    ]
    rare = {r.symbol: r for r in screen_universe(universe, asof=ASOF)}["RARE"]
    v1 = rare.valuation_bars[0]
    assert v1.passed


def test_high_leverage_fails_quality_bar():
    fund = _fund("LEVD", total_debt=D("500"), ebitda=D("100"))
    universe = [
        _inputs("LEVD", triggers=[_trigger()], fund=fund),
        *[_expensive_peer(f"PEER{i}") for i in range(5)],
    ]
    levd = {r.symbol: r for r in screen_universe(universe, asof=ASOF)}["LEVD"]
    q2 = levd.quality_bars[1]
    assert not q2.passed
    # 2-of-3 still holds via Q1 + Q3.
    assert levd.quality


def test_unstable_gross_margins_fail_q3():
    fund = _fund("SWNG", gross_margin_history=tuple(
        YearValue(date(y, 12, 31), v)
        for y, v in [(2023, D("0.40")), (2024, D("0.55")), (2025, D("0.30"))]
    ))
    universe = [
        _inputs("SWNG", triggers=[_trigger()], fund=fund),
        *[_expensive_peer(f"PEER{i}") for i in range(5)],
    ]
    swng = {r.symbol: r for r in screen_universe(universe, asof=ASOF)}["SWNG"]
    assert not swng.quality_bars[2].passed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ops/research/test_screener.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.research.screener'`

- [ ] **Step 3: Write the implementation**

```python
"""Point-in-time fundamental screener: funnel stage 2.

Pure module — callers assemble ``NameInputs`` (fundamentals, triggers,
prices) and this decides. Two phases because the EV/EBIT bar is relative:
phase 1 computes every name's EV/EBIT so sector medians exist, phase 2
evaluates bars per name.

Pass rule (design doc): statistically cheap (>=2 of 3 valuation bars) AND
quality (>=2 of 3 quality bars) AND at least one change trigger. A bar with
missing data fails with a "missing:" detail — absence of evidence is never
treated as cheapness or quality.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from statistics import median

from ops.research.triggers import Trigger
from tradingagents.dataflows.fundamentals import Fundamentals

FCF_YIELD_MIN = Decimal("0.06")
ROIC_MIN = Decimal("0.12")
DEBT_EBITDA_MAX = Decimal("3")
GROSS_MARGIN_BAND_MAX = Decimal("0.10")
MIN_HISTORY_YEARS = 3
MIN_SECTOR_PEERS = 5

_ZERO = Decimal("0")


@dataclass(frozen=True)
class NameInputs:
    symbol: str
    sector: str
    price: Decimal        # close on/before asof
    market_cap: Decimal   # snapshot cap rescaled to `price` by the caller
    fundamentals: Fundamentals
    triggers: tuple[Trigger, ...]
    year_end_prices: dict[date, Decimal]  # fiscal year end -> close


@dataclass(frozen=True)
class Bar:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ScreenResult:
    symbol: str
    asof: date
    passed: bool
    cheap: bool
    quality: bool
    valuation_bars: tuple[Bar, ...]
    quality_bars: tuple[Bar, ...]
    triggers: tuple[Trigger, ...]
    market_cap: Decimal
    ev_ebit: Decimal | None


def _ev_ebit(inputs: NameInputs) -> Decimal | None:
    f = inputs.fundamentals
    if f.ebit is None or f.ebit <= _ZERO or f.total_debt is None:
        return None
    ev = inputs.market_cap + f.total_debt - (f.cash or _ZERO)
    if ev <= _ZERO:
        return None
    return ev / f.ebit


def _ev_ebit_bar(ev_ebit: Decimal | None, benchmark: Decimal | None, label: str) -> Bar:
    name = "ev_ebit_vs_sector"
    if ev_ebit is None:
        return Bar(name, False, "missing: EV/EBIT not computable (no EBIT or balance sheet)")
    if benchmark is None:
        return Bar(name, False, "missing: no peer median available")
    passed = ev_ebit < benchmark
    return Bar(name, passed, f"EV/EBIT {ev_ebit:.1f} vs {label} median {benchmark:.1f}")


def _fcf_yield_bar(inputs: NameInputs) -> Bar:
    name = "fcf_yield"
    f = inputs.fundamentals
    if f.fcf is None or inputs.market_cap <= _ZERO:
        return Bar(name, False, "missing: no FCF (needs both CFO and capex)")
    yld = f.fcf / inputs.market_cap
    return Bar(name, yld > FCF_YIELD_MIN, f"FCF yield {(yld * 100):.1f}% vs {FCF_YIELD_MIN * 100}%")


def _pe_history_bar(inputs: NameInputs) -> Bar:
    name = "pe_vs_own_history"
    eps = inputs.fundamentals.eps_history
    if not eps or eps[-1].value <= _ZERO:
        return Bar(name, False, "missing: no positive current EPS")
    current_pe = inputs.price / eps[-1].value
    historical: list[Decimal] = []
    for yv in eps:
        px = inputs.year_end_prices.get(yv.fiscal_year_end)
        if px is not None and yv.value > _ZERO:
            historical.append(px / yv.value)
    if len(historical) < MIN_HISTORY_YEARS:
        return Bar(name, False, f"missing: only {len(historical)} usable historical P/E years")
    med = median(historical)
    return Bar(name, current_pe < med, f"P/E {current_pe:.1f} vs own 5y median {med:.1f}")


def _roic_bar(inputs: NameInputs) -> Bar:
    name = "roic_5y"
    hist = inputs.fundamentals.roic_history
    if len(hist) < MIN_HISTORY_YEARS:
        return Bar(name, False, f"missing: only {len(hist)} ROIC years")
    avg = sum(yv.value for yv in hist) / Decimal(len(hist))
    return Bar(name, avg > ROIC_MIN, f"mean ROIC {(avg * 100):.1f}% vs {ROIC_MIN * 100}%")


def _debt_ebitda_bar(inputs: NameInputs) -> Bar:
    name = "debt_to_ebitda"
    f = inputs.fundamentals
    if f.ebitda is None or f.ebitda <= _ZERO or f.total_debt is None:
        return Bar(name, False, "missing: no positive EBITDA or no balance sheet")
    ratio = f.total_debt / f.ebitda
    return Bar(name, ratio < DEBT_EBITDA_MAX, f"debt/EBITDA {ratio:.2f} vs {DEBT_EBITDA_MAX}")


def _gross_margin_bar(inputs: NameInputs) -> Bar:
    name = "gross_margin_stability"
    hist = inputs.fundamentals.gross_margin_history
    if len(hist) < MIN_HISTORY_YEARS:
        return Bar(name, False, f"missing: only {len(hist)} gross-margin years")
    values = [yv.value for yv in hist]
    band = max(values) - min(values)
    return Bar(
        name, band <= GROSS_MARGIN_BAND_MAX,
        f"gross-margin band {(band * 100):.1f}pp vs {GROSS_MARGIN_BAND_MAX * 100}pp",
    )


def screen_universe(inputs: list[NameInputs], *, asof: date) -> list[ScreenResult]:
    ev_ebit_by_symbol = {n.symbol: _ev_ebit(n) for n in inputs}
    valid = [(n.sector, v) for n, v in zip(inputs, ev_ebit_by_symbol.values()) if v is not None]
    universe_median = median([v for _, v in valid]) if valid else None
    by_sector: dict[str, list[Decimal]] = {}
    for sector, v in valid:
        by_sector.setdefault(sector, []).append(v)

    results: list[ScreenResult] = []
    for n in inputs:
        peers = by_sector.get(n.sector, [])
        if len(peers) >= MIN_SECTOR_PEERS:
            benchmark, label = median(peers), n.sector
        else:
            benchmark, label = universe_median, "universe"
        valuation = (
            _ev_ebit_bar(ev_ebit_by_symbol[n.symbol], benchmark, label),
            _fcf_yield_bar(n),
            _pe_history_bar(n),
        )
        quality = (_roic_bar(n), _debt_ebitda_bar(n), _gross_margin_bar(n))
        cheap = sum(b.passed for b in valuation) >= 2
        is_quality = sum(b.passed for b in quality) >= 2
        results.append(ScreenResult(
            symbol=n.symbol,
            asof=asof,
            passed=cheap and is_quality and len(n.triggers) >= 1,
            cheap=cheap,
            quality=is_quality,
            valuation_bars=valuation,
            quality_bars=quality,
            triggers=n.triggers,
            market_cap=n.market_cap,
            ev_ebit=ev_ebit_by_symbol[n.symbol],
        ))
    return results
```

Note: `f"{decimal:.1f}"` works for `Decimal`. `statistics.median` over `Decimal` is fine (sorting + `(a+b)/2`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ops/research/test_screener.py -v`
Expected: 6 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check ops/research/screener.py tests/ops/research/test_screener.py
git add ops/research/screener.py tests/ops/research/test_screener.py
git commit -m "feat(research): deterministic point-in-time screener with trigger gate"
```

---

### Task 7: Screen store — the deep-research queue (`ops/research/store.py`)

**Files:**
- Create: `ops/research/store.py`
- Test: `tests/ops/research/test_store.py`

**Interfaces:**
- Consumes: `ScreenResult` (Task 6) — persisted as JSON via `dataclasses.asdict` + `json.dumps(..., default=str)`.
- Produces (used by Task 8; build-order step 5 will consume `pending_hits`):
  - `ScreenStore(db_path: str | Path)` — stdlib sqlite3, process lock, ISO-8601 UTC TEXT timestamps (conventions of `tradingagents/memos/store.py`)
  - `record_run(*, asof: date, universe_size: int, results: list[ScreenResult]) -> str` — returns generated `run_id`; stores every `passed` result as a hit with `status='pending'`; a symbol that already has a pending hit is NOT duplicated
  - `pending_hits() -> list[dict]` — the deep-research queue, oldest-first; each dict has keys `id, run_id, symbol, asof, status, payload` (payload JSON-decoded)
  - `mark_researched(hit_id: int) -> None`, `mark_expired(hit_id: int) -> None`
  - `last_run() -> dict | None` — keys `run_id, asof, created_at, universe_size, passed_count`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the screen store / deep-research queue."""

from datetime import date
from decimal import Decimal

import pytest

from ops.research.screener import Bar, ScreenResult
from ops.research.store import ScreenStore

pytestmark = pytest.mark.unit

ASOF = date(2026, 7, 1)


def _result(symbol, passed=True):
    bar = Bar(name="fcf_yield", passed=True, detail="FCF yield 8.0% vs 6%")
    return ScreenResult(
        symbol=symbol, asof=ASOF, passed=passed, cheap=passed, quality=passed,
        valuation_bars=(bar,), quality_bars=(bar,), triggers=(),
        market_cap=Decimal("1000"), ev_ebit=Decimal("10.5"),
    )


@pytest.fixture
def store(tmp_path):
    return ScreenStore(tmp_path / "screen.sqlite")


def test_record_run_stores_only_passed_results_as_hits(store):
    run_id = store.record_run(
        asof=ASOF, universe_size=100,
        results=[_result("AAA"), _result("BBB", passed=False)],
    )
    hits = store.pending_hits()
    assert [h["symbol"] for h in hits] == ["AAA"]
    assert hits[0]["run_id"] == run_id
    assert hits[0]["payload"]["market_cap"] == "1000"


def test_pending_symbol_not_duplicated_across_runs(store):
    store.record_run(asof=ASOF, universe_size=100, results=[_result("AAA")])
    store.record_run(asof=date(2026, 7, 8), universe_size=100, results=[_result("AAA")])
    assert len(store.pending_hits()) == 1


def test_mark_researched_removes_from_queue_and_allows_requeue(store):
    store.record_run(asof=ASOF, universe_size=100, results=[_result("AAA")])
    hit = store.pending_hits()[0]
    store.mark_researched(hit["id"])
    assert store.pending_hits() == []
    # Once researched, a later screen pass may queue the name again.
    store.record_run(asof=date(2026, 10, 1), universe_size=100, results=[_result("AAA")])
    assert len(store.pending_hits()) == 1


def test_mark_expired(store):
    store.record_run(asof=ASOF, universe_size=100, results=[_result("AAA")])
    store.mark_expired(store.pending_hits()[0]["id"])
    assert store.pending_hits() == []


def test_last_run_summary(store):
    assert store.last_run() is None
    run_id = store.record_run(
        asof=ASOF, universe_size=100,
        results=[_result("AAA"), _result("BBB", passed=False)],
    )
    run = store.last_run()
    assert run["run_id"] == run_id
    assert run["universe_size"] == 100
    assert run["passed_count"] == 1
    assert run["asof"] == "2026-07-01"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ops/research/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.research.store'`

- [ ] **Step 3: Write the implementation**

```python
"""SQLite store for screen runs and hits — the deep-research queue.

Follows the conventions of ``tradingagents/memos/store.py``: stdlib sqlite3,
a process-wide lock, ISO-8601 UTC TEXT timestamps, full payload as JSON with
columns as query indexes only.

Hit lifecycle: ``pending`` (awaiting deep research — build-order step 5
consumes these) -> ``researched`` (a memo exists) or ``expired`` (went stale
before research). A pending symbol is never duplicated by later runs; once
researched/expired it may be queued again by a fresh screen pass.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from ops.research.screener import ScreenResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screen_runs (
    run_id TEXT PRIMARY KEY,
    asof TEXT NOT NULL,
    created_at TEXT NOT NULL,
    universe_size INTEGER NOT NULL,
    passed_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS screen_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asof TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_hits_status ON screen_hits(status);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScreenStore:
    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def record_run(
        self, *, asof: date, universe_size: int, results: list[ScreenResult],
    ) -> str:
        run_id = f"screen-{asof.isoformat()}-{uuid4().hex[:8]}"
        passed = [r for r in results if r.passed]
        now = _now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO screen_runs (run_id, asof, created_at, universe_size, passed_count)"
                " VALUES (?, ?, ?, ?, ?)",
                (run_id, asof.isoformat(), now, universe_size, len(passed)),
            )
            for result in passed:
                already_pending = conn.execute(
                    "SELECT 1 FROM screen_hits WHERE symbol = ? AND status = 'pending' LIMIT 1",
                    (result.symbol,),
                ).fetchone()
                if already_pending:
                    continue
                conn.execute(
                    "INSERT INTO screen_hits (run_id, symbol, asof, status, payload, created_at)"
                    " VALUES (?, ?, ?, 'pending', ?, ?)",
                    (
                        run_id, result.symbol, asof.isoformat(),
                        json.dumps(asdict(result), default=str), now,
                    ),
                )
        return run_id

    def pending_hits(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, run_id, symbol, asof, status, payload FROM screen_hits"
                " WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        return [
            {
                "id": r["id"], "run_id": r["run_id"], "symbol": r["symbol"],
                "asof": r["asof"], "status": r["status"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    def _set_status(self, hit_id: int, status: str) -> None:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE screen_hits SET status = ? WHERE id = ?", (status, hit_id)
            )
            if cur.rowcount == 0:
                raise KeyError(f"no screen hit with id {hit_id!r}")

    def mark_researched(self, hit_id: int) -> None:
        self._set_status(hit_id, "researched")

    def mark_expired(self, hit_id: int) -> None:
        self._set_status(hit_id, "expired")

    def last_run(self) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id, asof, created_at, universe_size, passed_count"
                " FROM screen_runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ops/research/test_store.py -v`
Expected: 5 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check ops/research/store.py tests/ops/research/test_store.py
git add ops/research/store.py tests/ops/research/test_store.py
git commit -m "feat(research): screen store with pending-hit deep-research queue"
```

---

### Task 8: Config + events + null-baseline portfolio (`ops/research/baseline.py`)

**Files:**
- Modify: `ops/config.py` (add three fields + env overrides + validation)
- Modify: `ops/events.py` (two new kinds, payload builders, `BUILDERS` + `AUDIT_ONLY` registration)
- Create: `ops/research/baseline.py`
- Test: `tests/ops/research/test_baseline.py` (plus extend existing `tests/ops/test_config.py` with one test)

**Interfaces:**
- Consumes: `PaperBroker` (`get_positions/get_equity/get_cash/place_order/close_position`), `Journal` (`last_buy_fill_for`, `record_event`, `record_equity_snapshot`), `Order/Side/OrderType` from `ops.broker.types`, `InsufficientFunds` from `ops.broker.base`, `QuoteUnavailable` from `ops.broker.base`.
- Produces (used by Task 9):
  - `OpsConfig` gains: `baseline_journal_path: str` (default `${XDG_STATE_HOME:-~/.local/state}/tradingagents/baseline_journal.sqlite`, env `OPS_BASELINE_JOURNAL_PATH`), `baseline_starting_cash: Decimal = Decimal("100000")` (env `OPS_BASELINE_STARTING_CASH`, must be > 0), `screen_store_path: str` (default `${XDG_STATE_HOME:-~/.local/state}/tradingagents/research_screen.sqlite`, env `OPS_SCREEN_STORE_PATH`)
  - `events.KIND_BASELINE_SCREEN_RUN = "baseline_screen_run"`, `events.KIND_BASELINE_EXIT = "baseline_exit"`, `events.baseline_screen_run_payload(*, asof: str, passers: int, buys: list[str], exits: list[str], skipped: list[str], equity: Decimal) -> dict`, `events.baseline_exit_payload(*, symbol: str, held_days: int) -> dict`
  - `baseline.update_baseline_portfolio(*, broker, journal, passers: list[str], asof: date, now: datetime | None = None) -> dict` — returns `{"buys": [...], "exits": [...], "skipped": [...]}`
  - Constants: `BASELINE_SLICE_PCT = Decimal("0.04")`, `BASELINE_MAX_HOLD_DAYS = 365`, `_MIN_ORDER_DOLLARS = Decimal("100")`

**Baseline policy (this IS the null hypothesis — keep it dumb and deterministic):**
- Separate journal DB from the trading journal; separate `PaperBroker` rebuilt each run via `PaperBroker.from_journal`.
- Exits first: any position whose last BUY fill is ≥ 365 days old is closed (equal-weight portfolio matched to the value sleeve's 12-month floor horizon).
- Buys second: every screen passer not currently held gets `4% of current equity` (≈ equal weight at the target ~25 names), clamped to available cash; skip a name on `QuoteUnavailable`; stop buying when cash < max($100, slice). Re-running the same day is idempotent (held names are skipped).
- One `baseline_screen_run` event + one `equity_snapshot(kind="baseline_run")` per run. No guardrails, no stops — the baseline must not inherit the full system's risk logic or it stops being a control.

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the null-baseline equal-weight paper portfolio."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ops import events
from ops.broker.paper import PaperBroker
from ops.journal import Journal
from ops.research.baseline import update_baseline_portfolio

pytestmark = pytest.mark.unit

ASOF = date(2026, 7, 1)
NOW = datetime(2026, 7, 1, 21, 0, tzinfo=timezone.utc)


@pytest.fixture
def journal(tmp_path):
    j = Journal(str(tmp_path / "baseline.sqlite"))
    yield j
    j.close()


def _broker(journal, cash="100000"):
    return PaperBroker(
        journal=journal, quote_source=lambda s: Decimal("20"),
        starting_cash=Decimal(cash),
    )


def test_buys_passers_equal_weight(journal):
    broker = _broker(journal)
    summary = update_baseline_portfolio(
        broker=broker, journal=journal, passers=["AAA", "BBB"], asof=ASOF, now=NOW,
    )
    assert summary["buys"] == ["AAA", "BBB"]
    positions = {p.symbol: p for p in broker.get_positions()}
    # 4% of 100k = 4000 for AAA; BBB gets 4% of remaining equity (still 100k mark).
    assert positions["AAA"].quantity == Decimal("4000") / Decimal("20")
    # Events and snapshot recorded.
    kinds = [e["kind"] for e in journal.read_events()]
    assert events.KIND_BASELINE_SCREEN_RUN in kinds
    assert journal.get_latest_equity_snapshot(kind="baseline_run") is not None


def test_held_names_are_not_rebought(journal):
    broker = _broker(journal)
    update_baseline_portfolio(
        broker=broker, journal=journal, passers=["AAA"], asof=ASOF, now=NOW,
    )
    summary = update_baseline_portfolio(
        broker=broker, journal=journal, passers=["AAA"], asof=ASOF, now=NOW,
    )
    assert summary["buys"] == []
    assert len(broker.get_positions()) == 1


def test_positions_exit_after_max_hold(journal):
    # PaperBroker stamps fills with the REAL clock, so "366 days later" must
    # be computed from the real clock too, not from the fake NOW.
    broker = _broker(journal)
    update_baseline_portfolio(
        broker=broker, journal=journal, passers=["AAA"], asof=ASOF, now=NOW,
    )
    later = datetime.now(timezone.utc) + timedelta(days=366)
    summary = update_baseline_portfolio(
        broker=broker, journal=journal, passers=[], asof=date(2027, 7, 2), now=later,
    )
    assert summary["exits"] == ["AAA"]
    assert broker.get_positions() == []
    kinds = [e["kind"] for e in journal.read_events()]
    assert events.KIND_BASELINE_EXIT in kinds


def test_exited_name_can_reenter_on_same_run(journal):
    broker = _broker(journal)
    update_baseline_portfolio(
        broker=broker, journal=journal, passers=["AAA"], asof=ASOF, now=NOW,
    )
    later = datetime.now(timezone.utc) + timedelta(days=366)
    summary = update_baseline_portfolio(
        broker=broker, journal=journal, passers=["AAA"], asof=date(2027, 7, 2), now=later,
    )
    assert summary["exits"] == ["AAA"]
    assert summary["buys"] == ["AAA"]


def test_stops_buying_when_cash_exhausted(journal):
    broker = _broker(journal, cash="5000")
    # Slice = 4% of 5000 = 200; cash runs out after ~25 buys.
    passers = [f"SYM{i:02d}" for i in range(40)]
    summary = update_baseline_portfolio(
        broker=broker, journal=journal, passers=passers, asof=ASOF, now=NOW,
    )
    assert len(summary["buys"]) < 40
    assert broker.get_cash() < Decimal("200")


def test_quote_failure_skips_name_and_continues(journal):
    from ops.broker.base import QuoteUnavailable

    def quotes(symbol):
        if symbol == "BAD":
            raise QuoteUnavailable("no quote")
        return Decimal("20")

    broker = PaperBroker(
        journal=journal, quote_source=quotes, starting_cash=Decimal("100000"),
    )
    summary = update_baseline_portfolio(
        broker=broker, journal=journal, passers=["BAD", "GOOD"], asof=ASOF, now=NOW,
    )
    assert summary["buys"] == ["GOOD"]
    assert summary["skipped"] == ["BAD"]
```

And add to `tests/ops/test_config.py` (append; follow the file's existing style):

```python
def test_baseline_config_fields_and_env_overrides(monkeypatch):
    from decimal import Decimal

    from ops.config import OpsConfig, load_config

    cfg = OpsConfig()
    assert cfg.baseline_starting_cash == Decimal("100000")
    assert cfg.baseline_journal_path.endswith("baseline_journal.sqlite")
    assert cfg.screen_store_path.endswith("research_screen.sqlite")

    monkeypatch.setenv("OPS_BASELINE_JOURNAL_PATH", "/tmp/x.sqlite")
    monkeypatch.setenv("OPS_BASELINE_STARTING_CASH", "50000")
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", "/tmp/y.sqlite")
    cfg = load_config()
    assert cfg.baseline_journal_path == "/tmp/x.sqlite"
    assert cfg.baseline_starting_cash == Decimal("50000")
    assert cfg.screen_store_path == "/tmp/y.sqlite"

    import pytest as _pytest
    with _pytest.raises(ValueError):
        OpsConfig(baseline_starting_cash=Decimal("0"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ops/research/test_baseline.py tests/ops/test_config.py -v`
Expected: baseline tests ERROR with `ModuleNotFoundError: No module named 'ops.research.baseline'`; config test FAILS with `AttributeError` / `TypeError` on the new fields

- [ ] **Step 3: Config changes (`ops/config.py`)**

Add after `_default_journal_path()`:

```python
def _default_baseline_journal_path() -> str:
    """Baseline (null-hypothesis) paper portfolio journal — separate DB from
    the trading journal so the control can never contaminate real state."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(os.path.expanduser(base), "tradingagents", "baseline_journal.sqlite")


def _default_screen_store_path() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(os.path.expanduser(base), "tradingagents", "research_screen.sqlite")
```

Add fields to `OpsConfig` (after `live_fill_gate_count`):

```python
    baseline_journal_path: str = field(default_factory=_default_baseline_journal_path)
    baseline_starting_cash: Decimal = Decimal("100000")
    screen_store_path: str = field(default_factory=_default_screen_store_path)
```

Add to `__post_init__` (after the `live_fill_gate_count` check):

```python
        if self.baseline_starting_cash <= 0:
            raise ValueError(
                f"baseline_starting_cash must be > 0, got {self.baseline_starting_cash}"
            )
```

Add to `load_config()` (before the return, following the existing pattern):

```python
    baseline_journal_path = os.environ.get("OPS_BASELINE_JOURNAL_PATH")
    if baseline_journal_path is not None:
        kwargs["baseline_journal_path"] = baseline_journal_path

    baseline_starting_cash = _env_decimal("OPS_BASELINE_STARTING_CASH")
    if baseline_starting_cash is not None:
        kwargs["baseline_starting_cash"] = baseline_starting_cash

    screen_store_path = os.environ.get("OPS_SCREEN_STORE_PATH")
    if screen_store_path is not None:
        kwargs["screen_store_path"] = screen_store_path
```

- [ ] **Step 4: Event changes (`ops/events.py`)**

Add kind constants in the events module near the other domain groups:

```python
# Baseline (null-hypothesis) screen portfolio
KIND_BASELINE_SCREEN_RUN = "baseline_screen_run"
KIND_BASELINE_EXIT = "baseline_exit"
```

Add both kinds to the `AUDIT_ONLY` frozenset (the baseline is a control portfolio in its own journal; nothing to notify). Add payload builders next to the other builders:

```python
def baseline_screen_run_payload(
    *, asof: str, passers: int, buys: list[str], exits: list[str],
    skipped: list[str], equity: Decimal,
) -> dict[str, Any]:
    return {
        "asof": asof, "passers": passers, "buys": buys,
        "exits": exits, "skipped": skipped, "equity": str(equity),
    }


def baseline_exit_payload(*, symbol: str, held_days: int) -> dict[str, Any]:
    return {"symbol": symbol, "held_days": held_days}
```

Register both in `BUILDERS`:

```python
    KIND_BASELINE_SCREEN_RUN: baseline_screen_run_payload,
    KIND_BASELINE_EXIT: baseline_exit_payload,
```

Run: `pytest tests/ops/notify/test_policy.py -v` — Expected: PASS (proves AUDIT_ONLY registration is correct).

- [ ] **Step 5: Write `ops/research/baseline.py`**

```python
"""The null-baseline portfolio: equal-weight everything that passes the screen.

This is the control the whole research system is measured against (design
doc, "the mandatory null baseline"): if the LLM deep-research stages cannot
beat this dumb portfolio by more than the token bill, they are not adding
value. It must therefore stay dumb on purpose — no guardrails, no stops, no
conviction sizing, no discretion. Separate journal DB from the trading
journal so the control can never contaminate real state.

Policy:
- exits first: close any position held >= BASELINE_MAX_HOLD_DAYS (matched to
  the value sleeve's 12-month floor horizon);
- then buy every passer not currently held at BASELINE_SLICE_PCT of current
  equity (~equal weight at the target ~25 names), clamped to available cash;
- re-running on the same day is idempotent because held names are skipped.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal

from ops import events
from ops.broker.base import Broker, InsufficientFunds, QuoteUnavailable
from ops.broker.types import Order, OrderType, Side
from ops.journal import Journal

BASELINE_SLICE_PCT = Decimal("0.04")
BASELINE_MAX_HOLD_DAYS = 365
_MIN_ORDER_DOLLARS = Decimal("100")


def update_baseline_portfolio(
    *,
    broker: Broker,
    journal: Journal,
    passers: list[str],
    asof: date,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    exits: list[str] = []
    for pos in list(broker.get_positions()):
        last_buy = journal.last_buy_fill_for(pos.symbol)
        if last_buy is None:
            continue
        held_days = (now - last_buy["filled_at"]).days
        if held_days < BASELINE_MAX_HOLD_DAYS:
            continue
        try:
            broker.close_position(pos.symbol)
        except QuoteUnavailable as exc:
            print(f"[baseline] exit skipped {pos.symbol}: {exc}", file=sys.stderr)
            continue
        journal.record_event(
            events.KIND_BASELINE_EXIT,
            events.baseline_exit_payload(symbol=pos.symbol, held_days=held_days),
        )
        exits.append(pos.symbol)

    held = {p.symbol for p in broker.get_positions()}
    slice_dollars = broker.get_equity() * BASELINE_SLICE_PCT
    buys: list[str] = []
    skipped: list[str] = []
    for symbol in sorted(dict.fromkeys(passers)):
        if symbol in held:
            continue
        notional = min(slice_dollars, broker.get_cash())
        if notional < _MIN_ORDER_DOLLARS:
            break
        order = Order(
            client_order_id=f"baseline-{asof.isoformat()}-{symbol}",
            symbol=symbol,
            side=Side.BUY,
            notional_dollars=notional,
            order_type=OrderType.MARKET,
        )
        try:
            broker.place_order(order)
        except QuoteUnavailable as exc:
            print(f"[baseline] buy skipped {symbol}: {exc}", file=sys.stderr)
            skipped.append(symbol)
            continue
        except InsufficientFunds:
            break
        buys.append(symbol)

    journal.record_event(
        events.KIND_BASELINE_SCREEN_RUN,
        events.baseline_screen_run_payload(
            asof=asof.isoformat(), passers=len(passers),
            buys=buys, exits=exits, skipped=skipped, equity=broker.get_equity(),
        ),
    )
    journal.record_equity_snapshot(
        kind="baseline_run", equity=broker.get_equity(), cash=broker.get_cash(), at=now,
    )
    return {"buys": buys, "exits": exits, "skipped": skipped}
```

Note: check `ops/broker/base.py` for the exact names `Broker`, `InsufficientFunds`, `QuoteUnavailable` — all three exist there (PaperBroker raises the first two; `ops/quotes.py` raises the third through the quote source).

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/ops/research/test_baseline.py tests/ops/test_config.py tests/ops/notify/test_policy.py -v`
Expected: all passed

- [ ] **Step 7: Full suite, lint, commit**

```bash
pytest tests/ -q
ruff check ops/
git add ops/config.py ops/events.py ops/research/baseline.py tests/ops/research/test_baseline.py tests/ops/test_config.py
git commit -m "feat(research): null-baseline equal-weight paper portfolio on its own journal"
```

---

### Task 9: Composition root + CLI + docs (`ops/research/run.py`, `ops screen`)

**Files:**
- Create: `ops/research/run.py`
- Modify: `ops/cli.py` (add `screen` command)
- Modify: `docs/long_horizon_research.md` (build-order step 3 → ✅)
- Create: `docs/research_screener.md`
- Test: `tests/ops/research/test_run.py`

**Interfaces:**
- Consumes (everything above): `build_smallcap_universe`, `UniverseName`, `get_company_facts`, `compute_fundamentals`, `find_edgar_triggers`, `find_selloff_trigger`, `fetch_price_context`, `PriceContext`, `NameInputs`, `screen_universe`, `ScreenStore`, `update_baseline_portfolio`, `PaperBroker.from_journal`, `make_yfinance_quote_source`, `load_config`.
- Produces:
  - `ScreenRunSummary` frozen dataclass: `run_id: str | None, asof: date, universe_size: int, screened: int, passed: tuple[str, ...], errors: tuple[str, ...], baseline: dict | None`
  - `run_screen(*, config, asof: date, dry_run: bool = False, limit: int | None = None, universe_builder=None, facts_fetcher=None, triggers_finder=None, price_context_fetcher=None, quote_source=None) -> ScreenRunSummary`
  - CLI: `ops screen [--asof YYYY-MM-DD] [--dry-run] [--limit N]`

- [ ] **Step 1: Write the failing tests**

```python
"""Integration-style unit tests for the screen composition root (all I/O injected)."""

from datetime import date
from decimal import Decimal

import pytest

from ops.config import OpsConfig
from ops.research import run as run_mod
from ops.research.prices import PriceContext
from ops.research.store import ScreenStore
from ops.research.triggers import Trigger
from ops.universe.smallcap import SmallcapMember, UniverseName

pytestmark = pytest.mark.unit

ASOF = date(2026, 7, 1)
D = Decimal


def _name(symbol, sector="Industrials"):
    member = SmallcapMember(
        symbol=symbol, name=f"{symbol} Co", sector=sector, industry="Machinery",
        market_cap=D("1000000000"), last_price=D("20"),
    )
    return UniverseName(member=member, last_price=D("20"), adv_20d=D("5000000"))


def _facts_for_passer():
    """Facts making a name cheap + quality AGAINST A $1B MARKET CAP.

    Dollar concepts must be scaled to the market cap or the FCF-yield and
    EV/EBIT bars can never pass: FCF 100M / cap 1B = 10% yield; EBIT 150M
    gives EV/EBIT ~6.7; equity 800M keeps ROIC ~17%.
    """
    def _row(val, year, instant=False):
        row = {"val": val, "end": f"{year}-12-31", "filed": f"{year + 1}-02-15",
               "form": "10-K", "fp": "FY", "accn": f"a{year}"}
        if not instant:
            row["start"] = f"{year}-01-01"
        return row

    def series(vals, instant=False):
        return [_row(v, 2021 + i, instant) for i, v in enumerate(vals)]

    m = 1_000_000
    concepts = {
        "OperatingIncomeLoss": series([130 * m, 135 * m, 140 * m, 145 * m, 150 * m]),
        "DepreciationDepletionAndAmortization": series([30 * m] * 5),
        "NetCashProvidedByUsedInOperatingActivities": series([120 * m] * 5),
        "PaymentsToAcquirePropertyPlantAndEquipment": series([20 * m] * 5),
        "StockholdersEquity": series([800 * m] * 5, instant=True),
        "CashAndCashEquivalentsAtCarryingValue": series([100 * m] * 5, instant=True),
        "Revenues": series([1000 * m, 1020 * m, 1040 * m, 1060 * m, 1080 * m]),
        "CostOfRevenue": series([600 * m, 612 * m, 624 * m, 636 * m, 648 * m]),
    }
    payload = {}
    for concept, rows in concepts.items():
        payload[concept] = {"units": {"USD": rows}}
    payload["EarningsPerShareDiluted"] = {"units": {"USD/shares": series(
        ["2.0", "2.2", "2.4", "2.6", "2.8"],
    )}}
    return {"facts": {"us-gaap": payload}}


def _price_ctx():
    from datetime import timedelta
    closes = {}
    d = ASOF
    while len(closes) < 1500:
        if d.weekday() < 5:
            closes[d] = D("20")
        d -= timedelta(days=1)
    return PriceContext(closes=closes)


@pytest.fixture
def config(tmp_path):
    return OpsConfig(
        journal_path=str(tmp_path / "j.sqlite"),
        baseline_journal_path=str(tmp_path / "b.sqlite"),
        screen_store_path=str(tmp_path / "s.sqlite"),
        baseline_starting_cash=D("100000"),
    )


def _run(config, *, dry_run=False, facts=None, triggers=None):
    universe = [_name("GOOD")] + [_name(f"PEER{i}") for i in range(5)]
    trigger = Trigger(kind="activist_stake", description="SC 13D", date=ASOF, source="a1")

    def fake_triggers(ticker, *, asof, lookback_days=90, list_filings=None):
        return [trigger] if ticker == "GOOD" else []

    return run_mod.run_screen(
        config=config, asof=ASOF, dry_run=dry_run,
        universe_builder=lambda: universe,
        facts_fetcher=facts or (lambda t: _facts_for_passer()),
        triggers_finder=triggers or fake_triggers,
        price_context_fetcher=lambda s: _price_ctx(),
        quote_source=lambda s: D("20"),
    )


def test_full_run_screens_stores_and_buys_baseline(config):
    summary = _run(config)
    assert summary.universe_size == 6
    assert summary.screened == 6
    assert "GOOD" in summary.passed
    store = ScreenStore(config.screen_store_path)
    assert [h["symbol"] for h in store.pending_hits()] == list(summary.passed)
    assert summary.baseline is not None
    assert summary.baseline["buys"] == list(summary.passed)


def test_dry_run_touches_nothing(config, tmp_path):
    summary = _run(config, dry_run=True)
    assert "GOOD" in summary.passed
    assert summary.baseline is None
    assert ScreenStore(config.screen_store_path).last_run() is None


def test_per_name_errors_are_skipped_not_fatal(config):
    def exploding_facts(ticker):
        if ticker == "GOOD":
            raise KeyError("ticker not in SEC map")
        return _facts_for_passer()

    summary = _run(config, facts=exploding_facts)
    assert summary.screened == 5
    assert any("GOOD" in e for e in summary.errors)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ops/research/test_run.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.research.run'`

- [ ] **Step 3: Write `ops/research/run.py`**

```python
"""Composition root for a screen run: universe -> screen -> store -> baseline.

Per-name failures (SEC map misses, vendor errors, missing prices) are logged
to stderr and skipped — a sweep over ~1500 names must never die on name #937.
Every stage is injectable so tests run with zero network.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ops.broker.paper import PaperBroker
from ops.config import OpsConfig
from ops.journal import Journal
from ops.quotes import make_yfinance_quote_source
from ops.research.baseline import update_baseline_portfolio
from ops.research.prices import PriceContext, fetch_price_context
from ops.research.screener import NameInputs, screen_universe
from ops.research.store import ScreenStore
from ops.research.triggers import (
    SELLOFF_LOOKBACK_DAYS,
    find_edgar_triggers,
    find_selloff_trigger,
)
from ops.universe.smallcap import UniverseName, build_smallcap_universe
from tradingagents.dataflows.edgar_facts import get_company_facts
from tradingagents.dataflows.fundamentals import compute_fundamentals


@dataclass(frozen=True)
class ScreenRunSummary:
    run_id: str | None
    asof: date
    universe_size: int
    screened: int
    passed: tuple[str, ...]
    errors: tuple[str, ...]
    baseline: dict | None


def _name_inputs(
    name: UniverseName,
    *,
    asof: date,
    facts_fetcher,
    triggers_finder,
    price_context_fetcher,
) -> NameInputs | None:
    symbol = name.member.symbol
    ctx: PriceContext | None = price_context_fetcher(symbol)
    if ctx is None:
        return None
    price = ctx.close_on_or_before(asof)
    if price is None or name.member.last_price <= 0:
        return None
    # Rescale the snapshot market cap to the as-of price: shares from the
    # snapshot, price from now — keeps cheapness bars honest between the
    # quarterly universe refresh and a weekly screen run.
    market_cap = name.member.market_cap * price / name.member.last_price
    facts = facts_fetcher(symbol)
    fundamentals = compute_fundamentals(symbol, facts, asof=asof)
    triggers = list(triggers_finder(symbol, asof=asof))
    selloff = find_selloff_trigger(
        symbol, ctx.recent_closes(asof=asof, days=SELLOFF_LOOKBACK_DAYS), asof=asof,
    )
    if selloff is not None:
        triggers.append(selloff)
    year_end_prices = {
        yv.fiscal_year_end: px
        for yv in fundamentals.eps_history
        if (px := ctx.close_on_or_before(yv.fiscal_year_end)) is not None
    }
    return NameInputs(
        symbol=symbol,
        sector=name.member.sector,
        price=price,
        market_cap=market_cap,
        fundamentals=fundamentals,
        triggers=tuple(triggers),
        year_end_prices=year_end_prices,
    )


def run_screen(
    *,
    config: OpsConfig,
    asof: date,
    dry_run: bool = False,
    limit: int | None = None,
    universe_builder=None,
    facts_fetcher=None,
    triggers_finder=None,
    price_context_fetcher=None,
    quote_source=None,
) -> ScreenRunSummary:
    universe_builder = universe_builder or build_smallcap_universe
    facts_fetcher = facts_fetcher or get_company_facts
    triggers_finder = triggers_finder or find_edgar_triggers
    price_context_fetcher = price_context_fetcher or fetch_price_context

    universe = universe_builder()
    if limit is not None:
        universe = universe[:limit]

    inputs: list[NameInputs] = []
    errors: list[str] = []
    for name in universe:
        symbol = name.member.symbol
        try:
            ni = _name_inputs(
                name, asof=asof, facts_fetcher=facts_fetcher,
                triggers_finder=triggers_finder,
                price_context_fetcher=price_context_fetcher,
            )
        except Exception as exc:  # a sweep must survive any single name
            msg = f"{symbol}: {type(exc).__name__}: {exc}"
            print(f"[screen] skipped {msg}", file=sys.stderr)
            errors.append(msg)
            continue
        if ni is not None:
            inputs.append(ni)

    results = screen_universe(inputs, asof=asof)
    passed = tuple(r.symbol for r in results if r.passed)

    run_id = None
    baseline_summary = None
    if not dry_run:
        store = ScreenStore(config.screen_store_path)
        run_id = store.record_run(
            asof=asof, universe_size=len(universe), results=results,
        )
        with Journal(config.baseline_journal_path) as baseline_journal:
            broker = PaperBroker.from_journal(
                journal=baseline_journal,
                quote_source=quote_source or make_yfinance_quote_source(),
                starting_cash=config.baseline_starting_cash,
            )
            baseline_summary = update_baseline_portfolio(
                broker=broker, journal=baseline_journal,
                passers=list(passed), asof=asof,
            )

    return ScreenRunSummary(
        run_id=run_id,
        asof=asof,
        universe_size=len(universe),
        screened=len(inputs),
        passed=passed,
        errors=tuple(errors),
        baseline=baseline_summary,
    )
```

- [ ] **Step 4: Add the CLI command to `ops/cli.py`**

Add after the `status` command (imports go inside the function, matching how `run`/`install-service` lazily import):

```python
@cli.command()
@click.option("--asof", default=None, help="Screen as of this date (YYYY-MM-DD); default today.")
@click.option("--dry-run", is_flag=True,
              help="Screen and print only — no store writes, no baseline trades.")
@click.option("--limit", default=None, type=int,
              help="Screen only the first N universe names (smoke runs).")
def screen(asof: str | None, dry_run: bool, limit: int | None) -> None:
    """Run the small/mid-cap fundamental screen + null-baseline portfolio."""
    from ops.research.run import run_screen

    config = load_config()
    asof_date = date_cls.fromisoformat(asof) if asof else datetime.now().date()
    summary = run_screen(config=config, asof=asof_date, dry_run=dry_run, limit=limit)
    click.echo(f"screen run {summary.run_id or '(dry-run)'} asof {summary.asof}")
    click.echo(
        f"universe {summary.universe_size}, screened {summary.screened}, "
        f"passed {len(summary.passed)}, errors {len(summary.errors)}"
    )
    for symbol in summary.passed:
        click.echo(f"  PASS {symbol}")
    if summary.baseline is not None:
        click.echo(
            f"baseline: {len(summary.baseline['buys'])} buys, "
            f"{len(summary.baseline['exits'])} exits, "
            f"{len(summary.baseline['skipped'])} skipped"
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/ops/research/test_run.py -v`
Expected: 3 passed

- [ ] **Step 6: Write the runbook `docs/research_screener.md`**

```markdown
# Research screener + null-baseline portfolio

Build-order step 3 of docs/long_horizon_research.md.

## What it does

`ops screen` runs the funnel's cheap stages: quarterly-cached small/mid-cap
universe ($300M-$10B, price > $5, 20-day ADV > $2M, no financials/biotech) →
point-in-time fundamental screen (2-of-3 valuation bars AND 2-of-3 quality
bars AND ≥1 change trigger) → writes passers to the deep-research queue
(`research_screen.sqlite`) → updates the null-baseline paper portfolio
(equal-weight every passer, 12-month holds, its own journal).

The baseline is the control for the whole system: LLM stages must beat it by
more than the token bill (design doc, "the mandatory null baseline").

## Running

    SEC_EDGAR_USER_AGENT="Your Name you@email.com" ops screen

First run of a quarter is slow (one yfinance history call per universe name
for ADV, then per-name company-facts + price history). Subsequent runs reuse
the quarterly universe cache. Smoke-test with `--limit 25 --dry-run`.

Cadence: weekly, outside market hours. Example launchd/cron: Saturday 09:00
local. There is deliberately no always-on service for this yet — the
monitoring loop is build-order step 6.

## Env vars

| Var | Default | Meaning |
|---|---|---|
| `SEC_EDGAR_USER_AGENT` | (required) | SEC fair-access contact string |
| `OPS_SCREEN_STORE_PATH` | `~/.local/state/tradingagents/research_screen.sqlite` | screen runs + deep-research queue |
| `OPS_BASELINE_JOURNAL_PATH` | `~/.local/state/tradingagents/baseline_journal.sqlite` | baseline portfolio journal |
| `OPS_BASELINE_STARTING_CASH` | `100000` | baseline paper cash |

## Form 4 note

Insider-cluster triggers are deferred to build-order step 4 (needs the Form 4
XML parser to separate open-market buys from routine sales/grants). EDGAR
triggers today: 13D/13D-A, notable 8-K items, 10-12B spinoffs, tenders,
going-private. Plus the price trigger: close ≥25% below the 60-day high.
```

- [ ] **Step 7: Check off step 3 in `docs/long_horizon_research.md`**

Change the build-order line:

```
3. Small/mid-cap universe module + point-in-time screener; **screen-only
   paper portfolio starts accruing track record the day this lands**
```

to:

```
3. ✅ Small/mid-cap universe + point-in-time screener + null-baseline
   portfolio (`ops/universe/smallcap.py`, `ops/research/`, `ops screen` —
   see docs/research_screener.md)
```

- [ ] **Step 8: Full suite, lint, commit, push**

```bash
pytest tests/ -q
ruff check .
git add ops/research/run.py ops/cli.py docs/research_screener.md docs/long_horizon_research.md tests/ops/research/test_run.py
git commit -m "feat(research): ops screen command — full screen run + null-baseline wiring"
git push origin claude/smallcap-research-coverage-dervpt
```

---

## Verification checklist (after all tasks)

1. `pytest tests/ -q` — entire suite green (momentum sleeve untouched).
2. `ruff check .` — clean.
3. Smoke (requires network + `SEC_EDGAR_USER_AGENT`; NOT part of CI): `ops screen --limit 15 --dry-run` prints a summary without writing state. This is the only step where real EDGAR/Nasdaq/yfinance behavior is observed — expect some `[screen] skipped ...` stderr lines; that is the designed per-name failure path.
4. `ops screen --limit 15` then `sqlite3 ~/.local/state/tradingagents/research_screen.sqlite 'select run_id, universe_size, passed_count from screen_runs'` shows the run; re-running is idempotent for held baseline names.
