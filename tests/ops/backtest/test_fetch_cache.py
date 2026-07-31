from datetime import datetime, timedelta, timezone

import pytest

from ops.backtest.fetch_cache import FetchCache, default_fetch_cache_path

pytestmark = pytest.mark.unit


@pytest.fixture
def cache(tmp_path):
    return FetchCache(tmp_path / "fetch_cache.sqlite")


def test_roundtrip_dict_value(cache):
    cache.put("edgar", "AAPL:filings", {"a": 1, "b": [1, 2, 3]})
    assert cache.get("edgar", "AAPL:filings") == {"a": 1, "b": [1, 2, 3]}


def test_roundtrip_list_value(cache):
    cache.put("form4", "0001-xml", [1, "two", {"three": 3}])
    assert cache.get("form4", "0001-xml") == [1, "two", {"three": 3}]


def test_miss_returns_none(cache):
    assert cache.get("edgar", "does-not-exist") is None


def test_max_age_staleness_with_injected_now(cache):
    fetched_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    cache.put("edgar", "AAPL:filings", {"x": 1}, now=fetched_at)

    fresh_now = fetched_at + timedelta(minutes=30)
    assert cache.get(
        "edgar", "AAPL:filings", max_age=timedelta(hours=1), now=fresh_now
    ) == {"x": 1}

    stale_now = fetched_at + timedelta(hours=2)
    assert cache.get(
        "edgar", "AAPL:filings", max_age=timedelta(hours=1), now=stale_now
    ) is None


def test_get_or_fetch_calls_fetch_exactly_once(cache):
    calls = []

    def fetch():
        calls.append(1)
        return {"result": "value"}

    first = cache.get_or_fetch("edgar", "AAPL:facts", fetch)
    second = cache.get_or_fetch("edgar", "AAPL:facts", fetch)
    assert first == {"result": "value"}
    assert second == {"result": "value"}
    assert len(calls) == 1


def test_get_or_fetch_refetches_when_stale(cache):
    calls = []

    def fetch():
        calls.append(1)
        return {"n": len(calls)}

    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    first = cache.get_or_fetch("edgar", "k", fetch, max_age=timedelta(hours=1), now=t0)
    assert first == {"n": 1}
    later = t0 + timedelta(hours=2)
    second = cache.get_or_fetch("edgar", "k", fetch, max_age=timedelta(hours=1), now=later)
    assert second == {"n": 2}
    assert len(calls) == 2


def test_namespace_isolation(cache):
    cache.put("edgar", "shared-key", {"ns": "edgar"})
    cache.put("form4", "shared-key", {"ns": "form4"})
    assert cache.get("edgar", "shared-key") == {"ns": "edgar"}
    assert cache.get("form4", "shared-key") == {"ns": "form4"}


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "fetch_cache.sqlite"
    first = FetchCache(path)
    first.put("edgar", "AAPL:filings", {"a": 1})
    second = FetchCache(path)
    assert second.get("edgar", "AAPL:filings") == {"a": 1}


def test_put_rejects_naive_now(cache):
    with pytest.raises(ValueError):
        cache.put("edgar", "k", {"x": 1}, now=datetime(2026, 7, 1))


def test_get_rejects_naive_now(cache):
    cache.put("edgar", "k", {"x": 1})
    with pytest.raises(ValueError):
        cache.get("edgar", "k", max_age=timedelta(hours=1), now=datetime(2026, 7, 1))


def test_default_fetch_cache_path_uses_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = default_fetch_cache_path()
    assert path == str(tmp_path / "tradingagents" / "fetch_cache.sqlite")
