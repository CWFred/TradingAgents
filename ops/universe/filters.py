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
    """20-day average dollar volume = mean(close * volume) over last 20 trading days."""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="20d", auto_adjust=False)
        if hist.empty:
            return None
        last_price = Decimal(str(hist["Close"].iloc[-1]))
        dollar_vol = (hist["Close"] * hist["Volume"]).mean()
        return last_price, Decimal(str(float(dollar_vol)))
    except Exception:
        return None
