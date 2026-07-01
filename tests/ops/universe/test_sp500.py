import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ops.universe.sp500 import load_sp500_members


def _write_cache(path: Path, members: list[str], age_days: int) -> None:
    fetched = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": fetched, "members": members}))


def test_uses_cache_when_fresh(tmp_path):
    cache = tmp_path / "sp500.json"
    _write_cache(cache, ["AAPL", "MSFT", "NVDA"], age_days=1)

    def fetch():
        raise AssertionError("should not fetch when cache is fresh")

    members = load_sp500_members(cache_path=cache, fetch=fetch)
    assert members == ["AAPL", "MSFT", "NVDA"]


def test_refetches_when_cache_is_stale(tmp_path):
    cache = tmp_path / "sp500.json"
    _write_cache(cache, ["OLD"], age_days=30)

    def fetch():
        return ["AAPL", "MSFT"]

    members = load_sp500_members(cache_path=cache, max_age_days=7, fetch=fetch)
    assert members == ["AAPL", "MSFT"]
    # Cache should now be updated
    written = json.loads(cache.read_text())
    assert written["members"] == ["AAPL", "MSFT"]


def test_fetches_when_cache_missing(tmp_path):
    cache = tmp_path / "missing.json"
    members = load_sp500_members(cache_path=cache, fetch=lambda: ["AAPL"])
    assert members == ["AAPL"]
    assert cache.exists()


def test_returns_only_unique_uppercase_symbols(tmp_path):
    cache = tmp_path / "sp500.json"

    def fetch():
        return ["aapl", "AAPL", "msft", "BRK.B"]

    members = load_sp500_members(cache_path=cache, fetch=fetch)
    assert members == ["AAPL", "BRK.B", "MSFT"]
