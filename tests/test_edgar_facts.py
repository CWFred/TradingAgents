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
