"""Change-trigger detection — the reason to look at a name NOW.

A name enters deep research only when it is cheap+quality AND has a change
trigger (design doc: "looking at everything all the time drowns in noise").
Two sources:

- EDGAR filings (via the existing edgar vendor's trigger taxonomy): 13D
  activists, tenders, spinoff registrations, going-private, and 8-Ks whose
  item numbers are in edgar.NOTABLE_8K_ITEMS. Form 4 is excluded from this
  list — see the insider-cluster trigger below.
- Insider clusters: >= INSIDER_CLUSTER_MIN_BUYERS distinct insiders each
  making at least one open-market buy (code P, not a 10b5-1 plan) within
  the lookback window. Routine 10b5-1 sales and equity grants never count —
  raw Form 4 counts are dominated by those and would be noise.
- Price: a guidance-cut-style selloff, defined as the latest close sitting
  >= 25% below the 60-trading-day high.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from tradingagents.dataflows import edgar
from tradingagents.dataflows.form4 import InsiderTransaction

logger = logging.getLogger(__name__)

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


INSIDER_CLUSTER_MIN_BUYERS = 2


def find_insider_cluster_trigger(
    ticker: str,
    *,
    asof: date,
    lookback_days: int = TRIGGER_LOOKBACK_DAYS,
    transactions_fetcher: Callable[..., list] | None = None,
) -> Trigger | None:
    """A cluster of distinct insiders buying on the open market, own cash,
    outside 10b5-1 plans — the strongest single trigger in the taxonomy."""
    from tradingagents.dataflows.form4 import get_insider_transactions

    fetch = transactions_fetcher or get_insider_transactions
    since = asof - timedelta(days=lookback_days)
    txns = fetch(ticker, since=since)
    buys = [
        t for t in txns
        if t.kind == "open_market_buy" and not t.ten_b5_1
        and t.transaction_date is not None and since <= t.transaction_date <= asof
    ]
    buyers = {t.insider_name for t in buys}
    if len(buyers) < INSIDER_CLUSTER_MIN_BUYERS:
        return None
    latest = max(buys, key=lambda t: t.transaction_date)
    return Trigger(
        kind="insider_cluster",
        description=(
            f"{len(buyers)} insiders made open-market buys (non-10b5-1) "
            f"in the last {lookback_days} days"
        ),
        date=latest.transaction_date,
        source=latest.accession,
    )


# --- sources blob: serialize/revive ----------------------------------------
#
# `fetch_trigger_sources` does the once-per-symbol network work and returns a
# JSON-serializable dict (dates -> ISO strings, Decimal -> str) so it can be
# handed to a persistent cache. `triggers_from_sources` is the pure, no-network
# counterpart that revives domain objects from that dict and derives
# date-appropriate triggers locally — it can be called once per asof from a
# single fetch.

# Form 4 filings are rarer to cap tightly than the old per-asof call implied
# (that cap existed to bound XML fetches for ONE 90-day window); the sources
# blob has to answer every asof in a sweep, so it asks for more history,
# amortized once per symbol instead of once per (symbol, asof).
_SOURCES_MAX_FORM4_FILINGS = 250


def _serialize_filing(f: edgar.Filing) -> dict[str, Any]:
    return {
        "ticker": f.ticker,
        "cik": f.cik,
        "accession_number": f.accession_number,
        "form": f.form,
        "filing_date": f.filing_date.isoformat() if f.filing_date else None,
        "report_date": f.report_date.isoformat() if f.report_date else None,
        "primary_document": f.primary_document,
        "primary_doc_description": f.primary_doc_description,
        "items": list(f.items),
    }


def _revive_filing(d: Mapping[str, Any]) -> edgar.Filing:
    return edgar.Filing(
        ticker=d["ticker"],
        cik=d["cik"],
        accession_number=d["accession_number"],
        form=d["form"],
        filing_date=date.fromisoformat(d["filing_date"]) if d["filing_date"] else None,
        report_date=date.fromisoformat(d["report_date"]) if d["report_date"] else None,
        primary_document=d["primary_document"],
        primary_doc_description=d.get("primary_doc_description", ""),
        items=tuple(d.get("items", ())),
    )


def _serialize_txn(t: InsiderTransaction) -> dict[str, Any]:
    return {
        "insider_name": t.insider_name,
        "insider_title": t.insider_title,
        "is_director": t.is_director,
        "is_officer": t.is_officer,
        "is_ten_pct_owner": t.is_ten_pct_owner,
        "transaction_date": t.transaction_date.isoformat() if t.transaction_date else None,
        "code": t.code,
        "shares": str(t.shares) if t.shares is not None else None,
        "price": str(t.price) if t.price is not None else None,
        "acquired": t.acquired,
        "ten_b5_1": t.ten_b5_1,
        "accession": t.accession,
        "filed_date": t.filed_date.isoformat() if t.filed_date else None,
    }


def _revive_txn(d: Mapping[str, Any]) -> InsiderTransaction:
    return InsiderTransaction(
        insider_name=d["insider_name"],
        insider_title=d["insider_title"],
        is_director=d["is_director"],
        is_officer=d["is_officer"],
        is_ten_pct_owner=d["is_ten_pct_owner"],
        transaction_date=(
            date.fromisoformat(d["transaction_date"]) if d["transaction_date"] else None
        ),
        code=d["code"],
        shares=Decimal(d["shares"]) if d["shares"] is not None else None,
        price=Decimal(d["price"]) if d["price"] is not None else None,
        acquired=d["acquired"],
        ten_b5_1=d["ten_b5_1"],
        accession=d["accession"],
        filed_date=date.fromisoformat(d["filed_date"]) if d["filed_date"] else None,
    )


def fetch_trigger_sources(
    symbol: str,
    *,
    list_filings: Callable[..., list[edgar.Filing]] | None = None,
    fetch_raw: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """All network work for one symbol's triggers, done once.

    Returns a JSON-serializable dict holding every trigger-form filing
    (unbounded by asof — the pure filter windows it) and every parsed Form 4
    transaction. This is exactly what a persistent fetch cache should store;
    ``triggers_from_sources`` derives date-appropriate triggers from it with
    no further network access.

    ``insider_transactions_truncated`` is set when the Form 4 filings listing
    hit ``_SOURCES_MAX_FORM4_FILINGS`` — i.e. the cap was reached, so older
    insider activity MAY be missing for asofs near the front of a long sweep.
    (Exactly-cap-many available filings is indistinguishable from more
    existing beyond the cap, so this is deliberately conservative.)
    """
    from tradingagents.dataflows.form4 import get_insider_transactions

    _list_filings = list_filings or edgar.list_filings
    trigger_forms = set(edgar.CHANGE_TRIGGER_FORMS) - {"4"}
    filings = _list_filings(symbol, forms=trigger_forms)

    form4_filings = _list_filings(
        symbol, forms={"4"}, since=date.min, limit=_SOURCES_MAX_FORM4_FILINGS,
    )
    truncated = len(form4_filings) >= _SOURCES_MAX_FORM4_FILINGS

    def _prefetched_form4_filings(_ticker: str, **_kw: Any) -> list[edgar.Filing]:
        return form4_filings

    txns = get_insider_transactions(
        symbol,
        since=date.min,
        max_filings=_SOURCES_MAX_FORM4_FILINGS,
        list_filings=_prefetched_form4_filings,
        fetch_raw=fetch_raw,
    )
    return {
        "symbol": symbol,
        "edgar_filings": [_serialize_filing(f) for f in filings],
        "insider_transactions": [_serialize_txn(t) for t in txns],
        "insider_transactions_truncated": truncated,
    }


def triggers_from_sources(
    sources: Mapping[str, Any],
    *,
    asof: date,
    lookback_days: int = TRIGGER_LOOKBACK_DAYS,
    price_context: Any | None = None,
) -> list[Trigger]:
    """Pure, no-network derivation of triggers from a ``fetch_trigger_sources``
    blob for one ``asof``. Reproduces exactly what ``find_triggers`` returns
    for the same underlying data, plus the selloff trigger when
    ``price_context`` (an object with ``recent_closes(asof=..., days=...)``,
    e.g. ``ops.research.prices.PriceContext``) is supplied.
    """
    if sources.get("insider_transactions_truncated"):
        logger.warning(
            "%s: insider transactions source truncated at %d Form 4 filings "
            "— older insider activity may be missing for asof=%s",
            sources.get("symbol", "?"), _SOURCES_MAX_FORM4_FILINGS, asof,
        )
    since = asof - timedelta(days=lookback_days)
    out: list[Trigger] = []
    for f in (_revive_filing(d) for d in sources.get("edgar_filings", ())):
        if f.filing_date is None or f.filing_date > asof or f.filing_date < since:
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

    txns = [_revive_txn(d) for d in sources.get("insider_transactions", ())]
    buys = [
        t for t in txns
        if t.kind == "open_market_buy" and not t.ten_b5_1
        and t.transaction_date is not None and since <= t.transaction_date <= asof
    ]
    buyers = {t.insider_name for t in buys}
    if len(buyers) >= INSIDER_CLUSTER_MIN_BUYERS:
        latest = max(buys, key=lambda t: t.transaction_date)
        out.append(Trigger(
            kind="insider_cluster",
            description=(
                f"{len(buyers)} insiders made open-market buys (non-10b5-1) "
                f"in the last {lookback_days} days"
            ),
            date=latest.transaction_date,
            source=latest.accession,
        ))

    if price_context is not None:
        closes = price_context.recent_closes(asof=asof, days=SELLOFF_LOOKBACK_DAYS)
        selloff = find_selloff_trigger(sources.get("symbol", ""), closes, asof=asof)
        if selloff is not None:
            out.append(selloff)
    return out


def _legacy_sources_for_find_triggers(
    ticker: str,
    *,
    asof: date,
    lookback_days: int,
    list_filings: Callable[..., list[edgar.Filing]] | None,
    transactions_fetcher: Callable[..., list] | None,
) -> dict[str, Any]:
    """Build a sources blob using find_triggers' original (asof-windowed)
    fetch calls verbatim, so recomposing find_triggers on top of
    triggers_from_sources is behavior-identical to the pre-split
    implementation for every existing caller and test double."""
    from tradingagents.dataflows.form4 import get_insider_transactions

    _list_filings = list_filings or edgar.list_filings
    trigger_forms = set(edgar.CHANGE_TRIGGER_FORMS) - {"4"}
    since = asof - timedelta(days=lookback_days)
    filings = _list_filings(ticker, forms=trigger_forms, since=since)
    fetch = transactions_fetcher or get_insider_transactions
    txns = fetch(ticker, since=since)
    return {
        "symbol": ticker,
        "edgar_filings": [_serialize_filing(f) for f in filings],
        "insider_transactions": [_serialize_txn(t) for t in txns],
        # find_triggers' original per-asof fetch never hit the sweep-sized
        # cap that fetch_trigger_sources applies; nothing to flag.
        "insider_transactions_truncated": False,
    }


def find_triggers(
    ticker: str,
    *,
    asof: date,
    lookback_days: int = TRIGGER_LOOKBACK_DAYS,
    list_filings: Callable[..., list[edgar.Filing]] | None = None,
    transactions_fetcher: Callable[..., list] | None = None,
) -> list[Trigger]:
    """All change triggers for a name: EDGAR filings + insider cluster.

    (The price-selloff trigger stays separate in run.py — it needs the price
    context, which this module deliberately does not fetch.) Recomposed on
    top of the fetch/filter split: builds a sources blob with the exact same
    network calls as before, then derives triggers via the pure filter.
    """
    sources = _legacy_sources_for_find_triggers(
        ticker, asof=asof, lookback_days=lookback_days,
        list_filings=list_filings, transactions_fetcher=transactions_fetcher,
    )
    return triggers_from_sources(sources, asof=asof, lookback_days=lookback_days)
