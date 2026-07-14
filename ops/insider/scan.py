"""EDGAR daily-index Form 4 scan for the insider sleeve.

One index fetch per day + one document fetch per in-universe Form 4 —
NEVER per-ticker polling (a ~1500-name universe would hammer SEC rate
limits; the throttle in edgar._throttled_get is process-wide and required).
A 404 index (weekend/holiday) is an empty day, not an error; any per-
document failure is recorded and skipped — the scan must survive any
single filing.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

from ops.insider.store import SignalStore
from tradingagents.dataflows.form4 import parse_form4_xml

DAILY_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{q}/master.{ymd}.idx"
)
ARCHIVE_URL = "https://www.sec.gov/Archives/{path}"
FIRST_RUN_LOOKBACK_DAYS = 7
_XML_BLOCK = re.compile(r"<XML>(.*?)</XML>", re.DOTALL | re.IGNORECASE)


@dataclass
class ScanSummary:
    day: date
    form4_seen: int = 0
    universe_matches: int = 0
    transactions_recorded: int = 0
    errors: list[str] = field(default_factory=list)


def _default_fetch_raw(url: str) -> str:
    from tradingagents.dataflows.edgar import _throttled_get

    return _throttled_get(url).text


def _quarter(d: date) -> int:
    return (d.month - 1) // 3 + 1


def _accession_from_path(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".txt")


def _ownership_xml(submission_text: str) -> str | None:
    """First <XML> block carrying the ownershipDocument — the full daily
    submission .txt embeds several XML documents."""
    for m in _XML_BLOCK.finditer(submission_text):
        if "<ownershipDocument" in m.group(1):
            return m.group(1).strip()
    return None


def _is_404(exc: Exception) -> bool:
    """Strictly the HTTP status — string matching on the message would let
    any error text containing '404' silently skip a real scan day
    (review finding P3)."""
    resp = getattr(exc, "response", None)
    return resp is not None and getattr(resp, "status_code", None) == 404


def scan_daily_index(
    *,
    store: SignalStore,
    day: date,
    universe_symbols: list[str],
    fetch_raw=None,
    cik_resolver=None,
) -> ScanSummary:
    fetch_raw = fetch_raw or _default_fetch_raw
    if cik_resolver is None:
        from tradingagents.dataflows.edgar import get_cik

        cik_resolver = get_cik
    summary = ScanSummary(day=day)

    symbol_by_cik: dict[int, str] = {}
    for sym in universe_symbols:
        try:
            symbol_by_cik[cik_resolver(sym)] = sym
        except Exception:  # not every listed symbol is in company_tickers.json
            continue

    url = DAILY_INDEX_URL.format(year=day.year, q=_quarter(day),
                                 ymd=day.strftime("%Y%m%d"))
    try:
        idx_text = fetch_raw(url)
    except Exception as exc:
        if _is_404(exc):
            return summary  # holiday/weekend: an empty day, not an error
        summary.errors.append(f"daily index fetch failed: {exc}")
        return summary

    for line in idx_text.splitlines():
        parts = line.split("|")
        if len(parts) != 5 or parts[2].strip() != "4":
            continue
        summary.form4_seen += 1
        try:
            cik = int(parts[0])
        except ValueError:
            continue
        symbol = symbol_by_cik.get(cik)
        if symbol is None:
            continue
        summary.universe_matches += 1
        path = parts[4].strip()
        try:
            xml = _ownership_xml(fetch_raw(ARCHIVE_URL.format(path=path)))
            if xml is None:
                raise ValueError("no ownershipDocument XML block")
            txns = parse_form4_xml(
                xml, accession=_accession_from_path(path), filed_date=day,
            )
            summary.transactions_recorded += store.record_transactions(symbol, txns)
        except Exception as exc:  # one bad document must not kill the scan
            summary.errors.append(f"{symbol} {path}: {exc}")
            print(f"[insider-scan] {symbol}: {exc}", file=sys.stderr)
    return summary


def run_insider_scan(
    *,
    store: SignalStore,
    universe_loader=None,
    today: date | None = None,
    fetch_raw=None,
    cik_resolver=None,
) -> list[ScanSummary]:
    """Scan every business day after the watermark up to yesterday (bounded
    to FIRST_RUN_LOOKBACK_DAYS on a fresh store), advancing the watermark
    per day completed so a crash mid-run never re-scans finished days."""
    if universe_loader is None:
        from ops.universe.smallcap import load_smallcap_members

        def universe_loader():
            return [m.symbol for m in load_smallcap_members()]

    today = today or date.today()
    start = store.scan_watermark()
    if start is None:
        start = today - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
    symbols = universe_loader()
    out: list[ScanSummary] = []
    day = start + timedelta(days=1)
    while day < today:
        if day.weekday() < 5:  # the daily index only exists for business days
            out.append(scan_daily_index(
                store=store, day=day, universe_symbols=symbols,
                fetch_raw=fetch_raw, cik_resolver=cik_resolver,
            ))
        store.set_scan_watermark(day)
        day += timedelta(days=1)
    return out
