"""Universe filters: liquidity, deny-list, etc."""
from __future__ import annotations

import sys
from decimal import Decimal
from typing import Callable

import yfinance as yf

from ops.universe.earnings import _safe_decimal
from ops.universe.yf_pacing import call_paced


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

    Boundary policy: yfinance returns float64 pandas Series. Each close/volume
    value goes through _safe_decimal (NaN/None → 0) at the boundary; all
    multiplication, summation, and division then happen in Decimal space.

    Only the yfinance I/O is wrapped in try/except so genuine internal
    regressions surface as real errors rather than being silently reported
    as external fetch failures. A missing/bad column also produces a
    diagnostic-and-skip, not a whole-batch crash.
    """
    try:
        hist = call_paced(
            lambda: yf.Ticker(symbol).history(period="20d", auto_adjust=False),
            label="adv",
        )
    except Exception as exc:
        print(
            f"[filters] skipped {symbol}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    if hist is None or hist.empty:
        return None
    try:
        close_series = hist["Close"].tolist()
        volume_series = hist["Volume"].tolist()
    except (KeyError, AttributeError) as exc:
        print(
            f"[filters] skipped {symbol}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    closes = [_safe_decimal(c) for c in close_series]
    volumes = [_safe_decimal(v) for v in volume_series]
    if not closes:
        return None
    last_price = closes[-1]
    dollar_vols = [c * v for c, v in zip(closes, volumes)]
    avg_dollar_vol = sum(dollar_vols) / Decimal(len(dollar_vols))
    return last_price, avg_dollar_vol
