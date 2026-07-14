"""Daily-index Form 4 scan: universe intersection, idempotent watermark,
404-as-empty-day, per-document failure isolation. Zero network."""

from datetime import date

import pytest

from ops.insider.scan import run_insider_scan, scan_daily_index
from ops.insider.store import SignalStore

pytestmark = pytest.mark.unit

DAY = date(2026, 7, 10)  # a Friday

OWNERSHIP_XML = """\
<ownershipDocument>
    <aff10b5One>0</aff10b5One>
    <issuer><issuerTradingSymbol>AAA</issuerTradingSymbol></issuer>
    <reportingOwner>
        <reportingOwnerId><rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>1</isDirector>
            <isOfficer>1</isOfficer>
            <officerTitle>CEO</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionDate><value>2026-07-09</value></transactionDate>
            <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>10000</value></transactionShares>
                <transactionPricePerShare><value>4.25</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>"""

SUBMISSION_TXT = f"""<SEC-DOCUMENT>
<DOCUMENT><TYPE>4
<XML>
{OWNERSHIP_XML}
</XML>
</DOCUMENT>
</SEC-DOCUMENT>"""

IDX_TEXT = "\n".join([
    "CIK|Company Name|Form Type|Date Filed|Filename",
    "---------------------------------------------",
    "111|Aaa Corp|4|2026-07-10|edgar/data/111/0001-26-000001.txt",
    "222|Out Of Universe Inc|4|2026-07-10|edgar/data/222/0002-26-000002.txt",
    "111|Aaa Corp|10-K|2026-07-10|edgar/data/111/0003-26-000003.txt",
])

CIKS = {"AAA": 111, "BBB": 333}


def _cik(symbol):
    return CIKS[symbol]


@pytest.fixture
def store(tmp_path):
    return SignalStore(tmp_path / "signals.sqlite")


def test_scans_only_in_universe_form4s(store):
    fetched = []

    def fetch_raw(url):
        fetched.append(url)
        if "master." in url:
            return IDX_TEXT
        return SUBMISSION_TXT

    summary = scan_daily_index(
        store=store, day=DAY, universe_symbols=["AAA", "BBB"],
        fetch_raw=fetch_raw, cik_resolver=_cik,
    )
    assert summary.form4_seen == 2
    assert summary.universe_matches == 1
    assert summary.transactions_recorded == 1  # one P row parsed for AAA
    # exactly two fetches: the index + the one in-universe submission
    assert len(fetched) == 2
    assert "0001-26-000001" in fetched[1]
    buys = store.buys_in_window("AAA", since=date(2026, 7, 1), until=DAY)
    assert buys[0]["insider_name"] == "DOE JANE"
    assert buys[0]["accession"] == "0001-26-000001"


def test_404_index_is_an_empty_day(store):
    class _NotFound(RuntimeError):
        def __init__(self):
            super().__init__("404 Client Error: Not Found")
            self.response = type("R", (), {"status_code": 404})()

    def fetch_raw(url):
        raise _NotFound()

    summary = scan_daily_index(
        store=store, day=DAY, universe_symbols=["AAA"],
        fetch_raw=fetch_raw, cik_resolver=_cik,
    )
    assert summary.form4_seen == 0 and summary.errors == []


def test_error_text_mentioning_404_is_still_an_error(store):
    # Strict status-code check: a message containing "404" without an HTTP
    # 404 response must be recorded, not silently treated as a holiday.
    def fetch_raw(url):
        raise RuntimeError("connection reset fetching /archives/404-page")

    summary = scan_daily_index(
        store=store, day=DAY, universe_symbols=["AAA"],
        fetch_raw=fetch_raw, cik_resolver=_cik,
    )
    assert len(summary.errors) == 1


def test_document_failure_is_isolated(store):
    def fetch_raw(url):
        if "master." in url:
            return IDX_TEXT
        raise RuntimeError("connection reset")

    summary = scan_daily_index(
        store=store, day=DAY, universe_symbols=["AAA"],
        fetch_raw=fetch_raw, cik_resolver=_cik,
    )
    assert summary.transactions_recorded == 0
    assert len(summary.errors) == 1


def test_run_insider_scan_advances_watermark_and_skips_weekends(store):
    days_scanned = []

    def fetch_raw(url):
        if "master." in url:
            days_scanned.append(url)
            return IDX_TEXT
        return SUBMISSION_TXT

    today = date(2026, 7, 13)  # Monday; fresh store -> 7-day lookback
    run_insider_scan(
        store=store, universe_loader=lambda: ["AAA"], today=today,
        fetch_raw=fetch_raw, cik_resolver=_cik,
    )
    # 2026-07-07..07-12 window: business days are Tue 7..Fri 10 (4 fetches).
    assert len(days_scanned) == 4
    assert store.scan_watermark() == date(2026, 7, 12)
    # Re-run: nothing new to scan, watermark unchanged.
    days_scanned.clear()
    run_insider_scan(
        store=store, universe_loader=lambda: ["AAA"], today=today,
        fetch_raw=fetch_raw, cik_resolver=_cik,
    )
    assert days_scanned == []
