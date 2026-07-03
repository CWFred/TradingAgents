"""S&P 500 membership list. Cached weekly to JSON; refreshed by scraping
Wikipedia's `List of S&P 500 companies` table."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import urllib.request

# Old cache location — writing here breaks read-only package installs
# (L5). Kept only as an optional bundled first-run fallback: if a snapshot
# exists here (e.g. shipped with the package) and the new XDG cache is
# empty, it is used to seed the cache instead of hitting the network on
# first run.
_BUNDLED_SNAPSHOT = Path(__file__).parent / "_data" / "sp500_members.json"
_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def _default_cache_path() -> Path:
    """Default cache location: ${XDG_CACHE_HOME:-~/.cache}/tradingagents/sp500_members.json.

    Computed fresh on every call (not a module-level constant) so tests can
    monkeypatch XDG_CACHE_HOME.
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(os.path.expanduser(base)) / "tradingagents" / "sp500_members.json"


def _fetch_from_wikipedia() -> list[str]:
    req = urllib.request.Request(_WIKI_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    # The first table (id="constituents") has columns: Symbol, Security, ...
    # Each row's first <td> is the ticker, sometimes wrapped in <a>.
    # Wikipedia uses "BRK.B"; yfinance uses "BRK-B" — translate dots to dashes.
    pattern = re.compile(
        r'<tr[^>]*>\s*<td[^>]*>\s*<a[^>]*>([A-Z][A-Z0-9.]*)</a>', re.MULTILINE
    )
    matches = pattern.findall(html)
    if len(matches) < 400:
        raise RuntimeError(f"sp500 scrape returned only {len(matches)} symbols — page format changed?")
    return [m.replace(".", "-") for m in matches]


def load_sp500_members(
    *,
    cache_path: Path | None = None,
    max_age_days: int = 7,
    fetch: Callable[[], list[str]] | None = None,
) -> list[str]:
    # Only consult the bundled-snapshot fallback when the caller didn't pass
    # an explicit cache_path — it exists purely to seed a genuinely fresh
    # default XDG cache on a real first run, not to shadow a caller-chosen
    # (e.g. test) cache location.
    using_default_cache = cache_path is None
    cache_path = cache_path or _default_cache_path()
    fetch = fetch or _fetch_from_wikipedia
    if cache_path.exists():
        data = json.loads(cache_path.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if datetime.now(timezone.utc) - fetched_at < timedelta(days=max_age_days):
            members = data["members"]
            return sorted({s.upper() for s in members})
    elif using_default_cache and _BUNDLED_SNAPSHOT.exists():
        # First run against a fresh XDG cache: seed from the bundled
        # snapshot (if one ships with the package and is still fresh)
        # instead of hitting the network, then migrate it forward into the
        # proper cache location.
        data = json.loads(_BUNDLED_SNAPSHOT.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if datetime.now(timezone.utc) - fetched_at < timedelta(days=max_age_days):
            members = sorted({s.upper() for s in data["members"]})
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"fetched_at": data["fetched_at"], "members": members})
            )
            return members
    members = fetch()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "members": sorted({s.upper() for s in members}),
        })
    )
    return sorted({s.upper() for s in members})
