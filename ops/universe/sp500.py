"""S&P 500 membership list. Cached weekly to JSON; refreshed by scraping
Wikipedia's `List of S&P 500 companies` table."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import urllib.request

_DEFAULT_CACHE = Path(__file__).parent / "_data" / "sp500_members.json"
_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


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
    cache_path = cache_path or _DEFAULT_CACHE
    fetch = fetch or _fetch_from_wikipedia
    if cache_path.exists():
        data = json.loads(cache_path.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if datetime.now(timezone.utc) - fetched_at < timedelta(days=max_age_days):
            members = data["members"]
            return sorted({s.upper() for s in members})
    members = fetch()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "members": sorted({s.upper() for s in members}),
        })
    )
    return sorted({s.upper() for s in members})
