"""yfinance-backed quote source with a small per-symbol TTL cache."""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Callable

import yfinance as yf

from ops.broker.base import QuoteUnavailable


def _now() -> float:
    return time.monotonic()


def make_yfinance_quote_source(*, ttl_seconds: int = 60) -> Callable[[str], Decimal]:
    cache: dict[str, tuple[float, Decimal]] = {}

    def get(symbol: str) -> Decimal:
        now = _now()
        cached = cache.get(symbol)
        if cached is not None and now - cached[0] < ttl_seconds:
            return cached[1]
        try:
            ticker = yf.Ticker(symbol)
            raw = ticker.fast_info.last_price
        except Exception as exc:
            raise QuoteUnavailable(
                f"yfinance quote fetch for {symbol} failed: {type(exc).__name__}: {exc}"
            ) from exc
        if raw is None:
            raise QuoteUnavailable(f"no last_price available for {symbol}")
        price = Decimal(str(raw))
        cache[symbol] = (now, price)
        return price

    return get
