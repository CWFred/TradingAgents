"""yfinance-backed quote source with a small per-symbol TTL cache."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from decimal import Decimal
from typing import Callable

import yfinance as yf

from ops.broker.base import QuoteUnavailable


def _now() -> float:
    return time.monotonic()


def make_yfinance_quote_source(
    *, ttl_seconds: int = 60, timeout_seconds: float | None = None,
) -> Callable[[str], Decimal]:
    """A cached yfinance quote source.

    ``timeout_seconds`` bounds each network fetch. yfinance's ``fast_info``
    has no timeout of its own and can hang indefinitely on a slow or
    rate-limited network; on a threaded server that strands the request
    thread (the fd/thread-leak pattern). When set, each fetch runs on a
    bounded pool and a fetch exceeding the deadline raises QuoteUnavailable
    (callers such as the dashboard's /api/pnl already degrade a failed quote
    to a null price) instead of blocking. Left ``None`` the behavior is the
    original inline fetch, so the trading daemon is unchanged.
    """
    cache: dict[str, tuple[float, Decimal]] = {}
    # One small pool for the lifetime of this source (the dashboard makes a
    # single source at startup). Only created when a timeout is requested.
    executor = (
        ThreadPoolExecutor(max_workers=8, thread_name_prefix="yfquote")
        if timeout_seconds is not None else None
    )

    def _fetch(symbol: str):
        return yf.Ticker(symbol).fast_info.last_price

    def get(symbol: str) -> Decimal:
        now = _now()
        cached = cache.get(symbol)
        if cached is not None and now - cached[0] < ttl_seconds:
            return cached[1]
        try:
            if executor is not None:
                raw = executor.submit(_fetch, symbol).result(timeout=timeout_seconds)
            else:
                raw = _fetch(symbol)
        except FuturesTimeout as exc:
            raise QuoteUnavailable(
                f"yfinance quote fetch for {symbol} timed out after "
                f"{timeout_seconds}s"
            ) from exc
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
