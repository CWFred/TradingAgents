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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

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
