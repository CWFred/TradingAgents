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

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

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
