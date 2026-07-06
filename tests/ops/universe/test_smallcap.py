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
