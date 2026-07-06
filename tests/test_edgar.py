"""Unit tests for the EDGAR dataflow (all HTTP mocked)."""

from datetime import date

import pytest

from tradingagents.dataflows import edgar
from tradingagents.dataflows.errors import VendorNotConfiguredError

pytestmark = pytest.mark.unit


class FakeResponse:
    def __init__(self, *, json_data=None, text=""):
        self._json = json_data
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        return None


@pytest.fixture(autouse=True)
def edgar_env(monkeypatch):
    """Configure the vendor and neutralize cross-test module state."""
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "Test Suite test@example.com")
    monkeypatch.setattr(edgar, "_MIN_REQUEST_INTERVAL", 0.0)
    monkeypatch.setattr(edgar, "_ticker_map_cache", None)


def _install_routes(monkeypatch, routes):
    """Route GETs by URL substring; record calls for assertions."""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers})
        for fragment, response in routes.items():
            if fragment in url:
                return response
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(edgar.requests, "get", fake_get)
    return calls


TICKER_MAP = FakeResponse(
    json_data={
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
    }
)


def _submissions(**overrides):
    recent = {
        "accessionNumber": ["0000320193-26-000002", "0000320193-26-000001"],
        "form": ["8-K", "10-K"],
        "filingDate": ["2026-06-15", "2026-01-30"],
        "reportDate": ["2026-06-15", "2025-12-31"],
        "primaryDocument": ["ev8k.htm", "annual.htm"],
        "primaryDocDescription": ["8-K", "10-K"],
        "items": ["4.02,9.01", ""],
    }
    recent.update(overrides)
    return FakeResponse(json_data={"filings": {"recent": recent}})


class TestConfig:
    def test_missing_user_agent_raises_vendor_not_configured(self, monkeypatch):
        monkeypatch.delenv("SEC_EDGAR_USER_AGENT")
        with pytest.raises(VendorNotConfiguredError):
            edgar.get_user_agent()

    def test_user_agent_header_sent(self, monkeypatch):
        calls = _install_routes(monkeypatch, {"company_tickers": TICKER_MAP})
        edgar.get_cik("AAPL")
        assert calls[0]["headers"]["User-Agent"] == "Test Suite test@example.com"


class TestCikLookup:
    def test_resolves_case_insensitively_and_caches(self, monkeypatch):
        calls = _install_routes(monkeypatch, {"company_tickers": TICKER_MAP})
        assert edgar.get_cik("aapl") == 320193
        assert edgar.get_cik("TSLA") == 1318605
        assert len(calls) == 1  # second lookup served from cache

    def test_unknown_ticker_raises(self, monkeypatch):
        _install_routes(monkeypatch, {"company_tickers": TICKER_MAP})
        with pytest.raises(KeyError):
            edgar.get_cik("ZZZZZ")


class TestListFilings:
    def test_parses_and_zero_pads_cik(self, monkeypatch):
        calls = _install_routes(
            monkeypatch,
            {"company_tickers": TICKER_MAP, "submissions": _submissions()},
        )
        filings = edgar.list_filings("AAPL")
        assert any("CIK0000320193.json" in c["url"] for c in calls)
        assert [f.form for f in filings] == ["8-K", "10-K"]
        assert filings[0].filing_date == date(2026, 6, 15)
        assert filings[0].items == ("4.02", "9.01")

    def test_form_filter_and_since_cutoff(self, monkeypatch):
        _install_routes(
            monkeypatch,
            {"company_tickers": TICKER_MAP, "submissions": _submissions()},
        )
        only_8k = edgar.list_filings("AAPL", forms={"8-K"})
        assert [f.form for f in only_8k] == ["8-K"]
        recent_only = edgar.list_filings("AAPL", since=date(2026, 3, 1))
        assert [f.form for f in recent_only] == ["8-K"]  # 10-K predates cutoff

    def test_archives_url_strips_accession_dashes(self, monkeypatch):
        _install_routes(
            monkeypatch,
            {"company_tickers": TICKER_MAP, "submissions": _submissions()},
        )
        filing = edgar.list_filings("AAPL", forms={"8-K"})[0]
        assert filing.url == (
            "https://www.sec.gov/Archives/edgar/data/320193/000032019326000002/ev8k.htm"
        )

    def test_trigger_and_8k_item_classification(self, monkeypatch):
        _install_routes(
            monkeypatch,
            {"company_tickers": TICKER_MAP, "submissions": _submissions()},
        )
        eight_k, ten_k = edgar.list_filings("AAPL")
        assert eight_k.trigger_kind() == "material_event"
        assert eight_k.notable_8k_items() == ["non_reliance_on_financials"]
        assert ten_k.trigger_kind() is None


class TestFetchFilingText:
    def test_html_flattened_and_truncated(self, monkeypatch):
        html = "<html><head><style>p{}</style></head><body><p>Item 1A.</p><p>Risk factors here.</p><script>x()</script></body></html>"
        _install_routes(
            monkeypatch,
            {
                "company_tickers": TICKER_MAP,
                "submissions": _submissions(),
                "Archives": FakeResponse(text=html),
            },
        )
        filing = edgar.list_filings("AAPL", forms={"8-K"})[0]
        text = edgar.fetch_filing_text(filing)
        assert "Risk factors here." in text
        assert "x()" not in text and "p{}" not in text
        truncated = edgar.fetch_filing_text(filing, max_chars=10)
        assert truncated.startswith("Item 1A.") and "[truncated at 10 characters]" in truncated


class TestFullTextSearch:
    def test_builds_params_and_limits_hits(self, monkeypatch):
        hits = [{"_id": f"acc:{i}"} for i in range(5)]
        calls = _install_routes(
            monkeypatch,
            {"search-index": FakeResponse(json_data={"hits": {"hits": hits}})},
        )
        out = edgar.full_text_search(
            '"strategic alternatives"',
            forms={"8-K", "10-K"},
            start=date(2026, 1, 1),
            end=date(2026, 6, 30),
            limit=3,
        )
        assert len(out) == 3
        params = calls[0]["params"]
        assert params["q"] == '"strategic alternatives"'
        assert params["forms"] == "10-K,8-K"
        assert params["startdt"] == "2026-01-01"
        assert params["enddt"] == "2026-06-30"
