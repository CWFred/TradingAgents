"""Small/mid-cap US equity universe.

Source: the Nasdaq stock-screener API — one HTTP call returns every
NYSE/Nasdaq/AMEX listing with symbol, last sale, market cap, sector, and
industry, which replaces thousands of per-name lookups. It is an unofficial
endpoint, so the fetch validates row count and the result is cached to JSON
(same pattern as sp500.py) with a quarterly TTL per the design doc.

Deterministic universe filters (design doc "funnel" stage 1):
  market cap $300M-$10B, price > $5, 20-day ADV > $2M,
  financials excluded (sector == "Finance", which also removes SPAC shells),
  biotech excluded (industry starts with "Biotechnology:" — this includes
  pharmaceutical preparations; deliberately conservative for v1).

ADV comes from the existing yfinance-backed liquidity filter
(ops.universe.filters), reused unchanged.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests

from ops.universe.filters import apply_liquidity_filter, fetch_price_and_adv_from_yfinance

NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"

MIN_MARKET_CAP = Decimal("300000000")
MAX_MARKET_CAP = Decimal("10000000000")
MIN_PRICE = Decimal("5")
MIN_ADV = Decimal("2000000")

_EXCLUDED_SECTORS = frozenset({"Finance"})
_EXCLUDED_INDUSTRY_PREFIXES = ("Biotechnology:",)
# Nasdaq notation for preferred shares / warrants / units — not common equity.
_NON_COMMON_CHARS = ("^", "/", " ")


@dataclass(frozen=True)
class SmallcapMember:
    symbol: str
    name: str
    sector: str
    industry: str
    market_cap: Decimal
    last_price: Decimal


@dataclass(frozen=True)
class UniverseName:
    member: SmallcapMember
    last_price: Decimal   # from the ADV pass (yfinance) — fresher than the snapshot
    adv_20d: Decimal


def _default_cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(os.path.expanduser(base)) / "tradingagents" / "smallcap_universe.json"


def _parse_money(raw: object) -> Decimal | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return Decimal(raw.replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None


def _fetch_from_nasdaq() -> list[dict]:
    resp = requests.get(
        NASDAQ_SCREENER_URL,
        params={"tableonly": "true", "limit": "10000", "download": "true"},
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    rows = ((resp.json().get("data") or {}).get("rows")) or []
    if len(rows) < 1000:
        raise RuntimeError(
            f"nasdaq screener returned only {len(rows)} rows — API format changed?"
        )
    return rows


def load_smallcap_members(*, fetch: Callable[[], list[dict]] | None = None) -> list[SmallcapMember]:
    """Snapshot members passing the deterministic (non-ADV) universe filters."""
    fetch = fetch or _fetch_from_nasdaq
    out: list[SmallcapMember] = []
    for row in fetch():
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol or any(ch in symbol for ch in _NON_COMMON_CHARS):
            continue
        sector = (row.get("sector") or "").strip()
        industry = (row.get("industry") or "").strip()
        if sector in _EXCLUDED_SECTORS:
            continue
        if industry.startswith(_EXCLUDED_INDUSTRY_PREFIXES):
            continue
        market_cap = _parse_money(row.get("marketCap"))
        last_price = _parse_money(row.get("lastsale"))
        if market_cap is None or last_price is None:
            continue
        if not (MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP):
            continue
        if last_price <= MIN_PRICE:
            continue
        out.append(
            SmallcapMember(
                symbol=symbol,
                name=(row.get("name") or "").strip(),
                sector=sector,
                industry=industry,
                market_cap=market_cap,
                last_price=last_price,
            )
        )
    out.sort(key=lambda m: m.symbol)
    return out


def _to_json(names: list[UniverseName]) -> str:
    return json.dumps({
        "built_at": datetime.now(timezone.utc).isoformat(),
        "names": [
            {
                "symbol": n.member.symbol, "name": n.member.name,
                "sector": n.member.sector, "industry": n.member.industry,
                "market_cap": str(n.member.market_cap),
                "snapshot_price": str(n.member.last_price),
                "last_price": str(n.last_price), "adv_20d": str(n.adv_20d),
            }
            for n in names
        ],
    })


def _from_json(data: dict) -> list[UniverseName]:
    return [
        UniverseName(
            member=SmallcapMember(
                symbol=d["symbol"], name=d["name"], sector=d["sector"],
                industry=d["industry"], market_cap=Decimal(d["market_cap"]),
                last_price=Decimal(d["snapshot_price"]),
            ),
            last_price=Decimal(d["last_price"]),
            adv_20d=Decimal(d["adv_20d"]),
        )
        for d in data["names"]
    ]


def build_smallcap_universe(
    *,
    cache_path: Path | None = None,
    max_age_days: int = 90,
    members_loader: Callable[[], list[SmallcapMember]] | None = None,
    metrics_fetcher: Callable[[str], tuple[Decimal, Decimal] | None] | None = None,
) -> list[UniverseName]:
    """Members + ADV liquidity filter, cached quarterly.

    The ADV pass is one yfinance history call per surviving member (~1-2k
    names); with the quarterly cache this cost is paid four times a year.
    """
    cache_path = cache_path or _default_cache_path()
    members_loader = members_loader or load_smallcap_members
    metrics_fetcher = metrics_fetcher or fetch_price_and_adv_from_yfinance
    if cache_path.exists():
        data = json.loads(cache_path.read_text())
        built_at = datetime.fromisoformat(data["built_at"])
        if datetime.now(timezone.utc) - built_at < timedelta(days=max_age_days):
            return _from_json(data)
    members = members_loader()
    by_symbol = {m.symbol: m for m in members}
    print(
        f"[smallcap] ADV-filtering {len(members)} names via yfinance "
        "(slow; result cached quarterly)",
        file=sys.stderr,
    )
    liquid = apply_liquidity_filter(
        sorted(by_symbol), min_adv=MIN_ADV, min_price=MIN_PRICE,
        fetch_metrics=metrics_fetcher,
    )
    names = [
        UniverseName(member=by_symbol[sym], last_price=price, adv_20d=adv)
        for sym, price, adv in liquid
    ]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(_to_json(names))
    return names
