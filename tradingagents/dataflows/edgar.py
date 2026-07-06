"""SEC EDGAR vendor: filings metadata, documents, and full-text search.

EDGAR is the data foundation of the long-horizon research strategy: it is
free, complete for every US-listed name, and timestamped at filing time —
which gives point-in-time discipline (no restated-data lookahead) for free.
Nearly every "change trigger" the screener needs is derivable from it:

- Form 4 insider transactions (structured XML, 10b5-1 plan flag since 2023)
- SC 13D/13D-A activist stakes (Item 4 purpose, letters in exhibits)
- 8-K item taxonomy (4.02 restatements, 5.02 officer departures, ...)
- Form 10-12B spinoff registrations, SC TO-I/TO-T tender offers

Three free primitives are wrapped here:

1. **Submissions API** (``data.sec.gov``): all filings for a company, newest
   first, as parallel arrays — used to list filings by form type.
2. **Archives** (``www.sec.gov/Archives``): the documents themselves.
3. **Full-text search** (``efts.sec.gov``): corpus-wide search, 2001-present,
   so screening does not require a self-built search index.

SEC fair-access rules require a declared ``User-Agent`` with contact info
(read from ``SEC_EDGAR_USER_AGENT``, e.g. ``"Jane Doe jane@example.com"``)
and at most 10 requests/second; a module-level throttle enforces a safe
margin under that cap.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date

import requests

from .errors import VendorNotConfiguredError

logger = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
FULL_TEXT_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{document}"

REQUEST_TIMEOUT = 30

# SEC caps clients at 10 req/s; throttle to 8/s for headroom.
_MIN_REQUEST_INTERVAL = 1.0 / 8.0

# Form types that constitute a screener "change trigger" — the reason to look
# at a name now instead of never — mapped to the trigger kind they signal.
CHANGE_TRIGGER_FORMS = {
    "4": "insider_transaction",
    "SC 13D": "activist_stake",
    "SC 13D/A": "activist_stake",
    "8-K": "material_event",
    "10-12B": "spinoff_registration",
    "10-12B/A": "spinoff_registration",
    "SC TO-I": "tender_offer",
    "SC TO-T": "tender_offer",
    "SC 13E3": "going_private",
}

# 8-K item numbers worth escalating on. The item taxonomy is machine-parseable
# from submissions metadata, so this classification costs zero LLM calls.
NOTABLE_8K_ITEMS = {
    "4.02": "non_reliance_on_financials",  # restatement — strong avoid/short flag
    "5.02": "officer_departure_or_election",
    "1.01": "material_agreement",
    "2.05": "restructuring_costs",
    "1.03": "bankruptcy_or_receivership",
    "3.01": "delisting_notice",
}


class EdgarNotConfiguredError(VendorNotConfiguredError):
    """SEC_EDGAR_USER_AGENT is not set; EDGAR requires a declared user agent."""


@dataclass(frozen=True)
class Filing:
    """One EDGAR filing, as listed by the submissions API."""

    ticker: str
    cik: int
    accession_number: str  # e.g. "0001234567-24-000123"
    form: str
    filing_date: date
    report_date: date | None
    primary_document: str
    primary_doc_description: str = ""
    items: tuple[str, ...] = field(default=())  # 8-K item numbers, when present

    @property
    def url(self) -> str:
        return ARCHIVES_URL.format(
            cik=self.cik,
            accession_nodash=self.accession_number.replace("-", ""),
            document=self.primary_document,
        )

    def trigger_kind(self) -> str | None:
        """Change-trigger classification for the screener, or None."""
        return CHANGE_TRIGGER_FORMS.get(self.form)

    def notable_8k_items(self) -> list[str]:
        """Human-readable labels for notable 8-K items on this filing."""
        return [NOTABLE_8K_ITEMS[i] for i in self.items if i in NOTABLE_8K_ITEMS]


def get_user_agent() -> str:
    ua = os.environ.get("SEC_EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise EdgarNotConfiguredError(
            "SEC_EDGAR_USER_AGENT is not set. SEC fair-access rules require a "
            'declared user agent with contact info, e.g. "Jane Doe jane@example.com".'
        )
    return ua


_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _throttled_get(url: str, params: dict | None = None) -> requests.Response:
    """GET with the SEC-required User-Agent and a global rate-limit throttle."""
    global _last_request_at
    with _throttle_lock:
        wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
    resp = requests.get(
        url,
        params=params,
        headers={"User-Agent": get_user_agent(), "Accept-Encoding": "gzip, deflate"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp


# Ticker -> CIK mapping is ~1MB and changes rarely; cache per process.
_ticker_map_cache: dict[str, int] | None = None
_ticker_map_lock = threading.Lock()


def get_cik(ticker: str) -> int:
    """Resolve a ticker to its SEC CIK via the official mapping file."""
    global _ticker_map_cache
    with _ticker_map_lock:
        if _ticker_map_cache is None:
            data = _throttled_get(TICKER_MAP_URL).json()
            _ticker_map_cache = {
                entry["ticker"].upper(): int(entry["cik_str"]) for entry in data.values()
            }
    cik = _ticker_map_cache.get(ticker.upper())
    if cik is None:
        raise KeyError(f"ticker {ticker!r} not found in SEC company_tickers.json")
    return cik


def _parse_date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


def list_filings(
    ticker: str,
    *,
    forms: set[str] | None = None,
    since: date | None = None,
    limit: int = 100,
) -> list[Filing]:
    """List a company's filings newest-first from the submissions API.

    Only the "recent" window (~1000 most recent filings) is consulted; for a
    small/mid-cap that is typically several years of history. Older archive
    pages can be added when a use case needs them.
    """
    cik = get_cik(ticker)
    payload = _throttled_get(SUBMISSIONS_URL.format(cik=cik)).json()
    recent = payload.get("filings", {}).get("recent", {})
    out: list[Filing] = []
    n = len(recent.get("accessionNumber", []))
    for i in range(n):
        form = recent["form"][i]
        if forms is not None and form not in forms:
            continue
        filing_date = _parse_date(recent["filingDate"][i])
        if since is not None and filing_date is not None and filing_date < since:
            # Arrays are newest-first, so everything after this is older too.
            break
        items_raw = recent.get("items", [""] * n)[i]
        out.append(
            Filing(
                ticker=ticker.upper(),
                cik=cik,
                accession_number=recent["accessionNumber"][i],
                form=form,
                filing_date=filing_date,
                report_date=_parse_date(recent.get("reportDate", [""] * n)[i]),
                primary_document=recent["primaryDocument"][i],
                primary_doc_description=recent.get("primaryDocDescription", [""] * n)[i],
                items=tuple(s.strip() for s in items_raw.split(",") if s.strip()),
            )
        )
        if len(out) >= limit:
            break
    return out


def fetch_filing_text(filing: Filing, *, max_chars: int | None = None) -> str:
    """Fetch a filing's primary document and reduce it to readable plain text.

    HTML is flattened with parsel (already a project dependency); non-HTML
    documents (Form 4 XML, plain text) are returned as-is. ``max_chars``
    truncates from the end, since filings front-load the substantive sections.
    """
    raw = _throttled_get(filing.url).text
    text = raw
    if filing.primary_document.lower().endswith((".htm", ".html")):
        from parsel import Selector

        selector = Selector(text=raw)
        selector.css("script, style").drop()
        chunks = [t.strip() for t in selector.css("::text").getall()]
        text = "\n".join(c for c in chunks if c)
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[truncated at {max_chars} characters]"
    return text


def full_text_search(
    query: str,
    *,
    forms: set[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 50,
) -> list[dict]:
    """Corpus-wide full-text search over EDGAR (2001-present).

    Returns raw hit dicts: ``_id`` is ``"{accession}:{document}"`` and
    ``_source`` carries ciks, form type, and file date. Free replacement for a
    self-built search index when screening for language across many filers.
    """
    params: dict[str, str] = {"q": query}
    if forms:
        params["forms"] = ",".join(sorted(forms))
    if start:
        params["startdt"] = start.isoformat()
    if end:
        params["enddt"] = end.isoformat()
    payload = _throttled_get(FULL_TEXT_SEARCH_URL, params=params).json()
    hits = payload.get("hits", {}).get("hits", [])
    return hits[:limit]
