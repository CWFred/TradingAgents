"""Unit tests for change-trigger detection (EDGAR mocked, no yfinance)."""

import json
from datetime import date
from decimal import Decimal

import pytest

from ops.research.triggers import (
    fetch_trigger_sources,
    find_edgar_triggers,
    find_insider_cluster_trigger,
    find_selloff_trigger,
    find_triggers,
    triggers_from_sources,
)
from tradingagents.dataflows.edgar import Filing
from tradingagents.dataflows.form4 import InsiderTransaction

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


def _buy(name, day, *, ten_b5_1=False, code="P"):
    return InsiderTransaction(
        insider_name=name, insider_title="", is_director=True, is_officer=False,
        is_ten_pct_owner=False, transaction_date=day, code=code,
        shares=Decimal("1000"), price=Decimal("5"), acquired=(code == "P"),
        ten_b5_1=ten_b5_1, accession=f"acc-{name}-{day.isoformat()}",
        filed_date=day,
    )


def test_two_distinct_open_market_buyers_trigger():
    asof = date(2026, 7, 1)
    txns = [_buy("DOE JANE", date(2026, 6, 20)), _buy("ROE RICHARD", date(2026, 6, 25))]
    trig = find_insider_cluster_trigger(
        "WIDG", asof=asof, transactions_fetcher=lambda t, *, since, **kw: txns,
    )
    assert trig is not None
    assert trig.kind == "insider_cluster"
    assert trig.source == "acc-ROE RICHARD-2026-06-25"


def test_single_buyer_sales_grants_and_10b51_do_not_trigger():
    asof = date(2026, 7, 1)
    cases = [
        [_buy("DOE JANE", date(2026, 6, 20))],                                # one buyer
        [_buy("DOE JANE", date(2026, 6, 20)), _buy("DOE JANE", date(2026, 6, 25))],  # same buyer twice
        [_buy("A", date(2026, 6, 20), code="S"), _buy("B", date(2026, 6, 25), code="S")],
        [_buy("A", date(2026, 6, 20), code="A"), _buy("B", date(2026, 6, 25), code="A")],
        [_buy("A", date(2026, 6, 20), ten_b5_1=True), _buy("B", date(2026, 6, 25), ten_b5_1=True)],
        [_buy("A", date(2026, 2, 1)), _buy("B", date(2026, 2, 2))],           # outside lookback
    ]
    for txns in cases:
        trig = find_insider_cluster_trigger(
            "WIDG", asof=asof,
            transactions_fetcher=lambda t, *, since, txns=txns, **kw: txns,
        )
        assert trig is None, txns


def test_find_triggers_combines_edgar_and_cluster():
    asof = date(2026, 7, 1)
    txns = [_buy("DOE JANE", date(2026, 6, 20)), _buy("ROE RICHARD", date(2026, 6, 25))]
    out = find_triggers(
        "WIDG", asof=asof,
        list_filings=lambda ticker, **kw: [],
        transactions_fetcher=lambda t, *, since, **kw: txns,
    )
    assert [t.kind for t in out] == ["insider_cluster"]


# --- Task 2: per-symbol fetch + pure per-asof filter split -----------------


def _form4_xml(name: str, txn_date: date, *, code: str = "P") -> str:
    return f"""<?xml version="1.0"?>
<ownershipDocument>
    <aff10b5One>0</aff10b5One>
    <reportingOwner>
        <reportingOwnerId><rptOwnerName>{name}</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionDate><value>{txn_date.isoformat()}</value></transactionDate>
            <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>1000</value></transactionShares>
                <transactionPricePerShare><value>5</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>"""


def test_fetch_trigger_sources_returns_json_serializable_dict():
    filings = [
        _filing("SC 13D", date(2026, 6, 20), accn="a1"),
        Filing(
            ticker="WIDG", cik=1, accession_number="acc-jane",
            form="4", filing_date=date(2026, 6, 18), report_date=None,
            primary_document="jane.xml",
        ),
    ]

    def fake_list_filings(ticker, *, forms=None, since=None, limit=100):
        if forms is None:
            return filings
        return [f for f in filings if f.form in forms]

    def fake_fetch_raw(url):
        assert url.endswith("jane.xml")
        return _form4_xml("DOE JANE", date(2026, 6, 18))

    sources = fetch_trigger_sources(
        "WIDG", list_filings=fake_list_filings, fetch_raw=fake_fetch_raw,
    )

    json.dumps(sources)  # must not raise — this is what FetchCache stores
    assert sources["symbol"] == "WIDG"
    assert [f["accession_number"] for f in sources["edgar_filings"]] == ["a1"]
    assert [t["accession"] for t in sources["insider_transactions"]] == ["acc-jane"]


def test_triggers_from_sources_windows_by_asof_from_one_fetch():
    filings = [
        _filing("SC 13D", date(2026, 3, 1), accn="a1"),
        _filing("8-K", date(2026, 6, 20), items=("5.02",), accn="a2"),
    ]
    call_count = {"n": 0}

    def fake_list_filings(ticker, *, forms=None, since=None, limit=100):
        call_count["n"] += 1
        if forms is None:
            return filings
        return [f for f in filings if f.form in forms]

    sources = fetch_trigger_sources(
        "WIDG", list_filings=fake_list_filings, fetch_raw=lambda url: "<not-xml",
    )
    fetch_calls_after_one_sweep = call_count["n"]

    early = triggers_from_sources(sources, asof=date(2026, 3, 15))
    late = triggers_from_sources(sources, asof=date(2026, 7, 1))

    # No additional fetches happened deriving triggers for either asof.
    assert call_count["n"] == fetch_calls_after_one_sweep
    assert [t.source for t in early] == ["a1"]
    assert [t.source for t in late] == ["a2"]


def test_find_triggers_equals_compose_from_sources_with_fakes():
    asof = date(2026, 7, 1)
    edgar_filing = _filing("SC 13D", date(2026, 6, 20), accn="a1")
    jane_filing = Filing(
        ticker="WIDG", cik=1, accession_number="acc-DOE JANE-2026-06-20",
        form="4", filing_date=date(2026, 6, 20), report_date=None,
        primary_document="jane.xml",
    )
    roe_filing = Filing(
        ticker="WIDG", cik=1, accession_number="acc-ROE RICHARD-2026-06-25",
        form="4", filing_date=date(2026, 6, 25), report_date=None,
        primary_document="roe.xml",
    )
    all_filings = [edgar_filing, jane_filing, roe_filing]

    def fake_list_filings(ticker, *, forms=None, since=None, limit=100):
        if forms is None:
            return all_filings
        return [f for f in all_filings if f.form in forms]

    xml_by_doc = {
        "jane.xml": _form4_xml("DOE JANE", date(2026, 6, 20)),
        "roe.xml": _form4_xml("ROE RICHARD", date(2026, 6, 25)),
    }

    def fake_fetch_raw(url):
        for doc, xml_text in xml_by_doc.items():
            if url.endswith(doc):
                return xml_text
        raise AssertionError(url)

    sources = fetch_trigger_sources(
        "WIDG", list_filings=fake_list_filings, fetch_raw=fake_fetch_raw,
    )
    composed = triggers_from_sources(sources, asof=asof)

    legacy_txns = [_buy("DOE JANE", date(2026, 6, 20)), _buy("ROE RICHARD", date(2026, 6, 25))]
    direct = find_triggers(
        "WIDG", asof=asof, list_filings=fake_list_filings,
        transactions_fetcher=lambda t, *, since, **kw: legacy_txns,
    )

    assert composed == direct
    assert [t.kind for t in composed] == ["activist_stake", "insider_cluster"]
