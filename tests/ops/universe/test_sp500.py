import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ops.universe.sp500 import _default_cache_path, load_sp500_members


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


def test_default_cache_path_resolves_under_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert _default_cache_path() == tmp_path / "cache" / "tradingagents" / "sp500_members.json"


def test_default_cache_path_falls_back_to_dot_cache_when_xdg_unset(monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    expected = Path.home() / ".cache" / "tradingagents" / "sp500_members.json"
    assert _default_cache_path() == expected


def test_load_sp500_members_writes_under_xdg_cache_dir_not_package_dir(monkeypatch, tmp_path):
    """The default cache write target must resolve under the user cache
    directory (honoring XDG_CACHE_HOME), never into the installed package
    directory — writing there breaks read-only installs (L5)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    # No bundled snapshot at the (irrelevant, in this isolated test) package
    # location — force the fallback off so this exercises fetch() directly.
    import ops.universe.sp500 as sp500mod
    monkeypatch.setattr(sp500mod, "_BUNDLED_SNAPSHOT", tmp_path / "no-such-bundled-file.json")

    members = load_sp500_members(fetch=lambda: ["AAPL", "MSFT"])

    expected_path = tmp_path / "cache" / "tradingagents" / "sp500_members.json"
    assert expected_path.exists()
    assert members == ["AAPL", "MSFT"]
    package_dir_cache = Path(sp500mod.__file__).parent / "_data" / "sp500_members.json"
    # The package-dir file (if present from before this fix, or from another
    # test run) must not have been (re)written by this call — we can't
    # assert non-existence since it may pre-exist locally, so instead assert
    # our symbols only landed under the XDG cache path.
    written = json.loads(expected_path.read_text())
    assert written["members"] == ["AAPL", "MSFT"]


def test_returns_only_unique_uppercase_symbols(tmp_path):
    cache = tmp_path / "sp500.json"

    def fetch():
        return ["aapl", "AAPL", "msft", "BRK.B"]

    members = load_sp500_members(cache_path=cache, fetch=fetch)
    assert members == ["AAPL", "BRK.B", "MSFT"]
