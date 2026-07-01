"""Universe filters: liquidity, deny-list, etc."""
from __future__ import annotations

from decimal import Decimal
from typing import Callable

import yfinance as yf


def apply_deny_list(symbols: list[str], deny_list: frozenset[str]) -> list[str]:
    return [s for s in symbols if s not in deny_list]


def apply_liquidity_filter(
    symbols: list[str],
    *,
    min_adv: Decimal,
    min_price: Decimal,
    fetch_metrics: Callable[[str], tuple[Decimal, Decimal] | None],
) -> list[tuple[str, Decimal, Decimal]]:
    out: list[tuple[str, Decimal, Decimal]] = []
    for sym in symbols:
        m = fetch_metrics(sym)
        if m is None:
            continue
        price, adv = m
        if price < min_price or adv < min_adv:
            continue
        out.append((sym, price, adv))
    return out


def fetch_price_and_adv_from_yfinance(symbol: str) -> tuple[Decimal, Decimal] | None:
    """20-day average dollar volume = mean(close * volume) over last 20 trading days.

    Boundary policy: yfinance returns float64 pandas Series. We convert each
    close/volume value to Decimal individually (via str()) and do all
    multiplication, summation, and division in Decimal space."""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="20d", auto_adjust=False)
        if hist.empty:
            return None
        closes = [Decimal(str(c)) for c in hist["Close"].tolist()]
        volumes = [Decimal(str(v)) for v in hist["Volume"].tolist()]
        last_price = closes[-1]
        if not closes:
            return None
        dollar_vols = [c * v for c, v in zip(closes, volumes)]
        avg_dollar_vol = sum(dollar_vols) / Decimal(len(dollar_vols))
        return last_price, avg_dollar_vol
    except Exception:
        return None
