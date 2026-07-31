"""Fetch yfinance daily bars and map them to the ``PriceBarLike`` contract.

The mapping is the seam that fills the backtest price cache.  Fetching is
injectable: ``yfinance_bar_fetcher`` takes an optional ``history_fn`` so tests
supply a fabricated frame and never touch the network, while production binds
the default that actually calls yfinance.  Money is carried as ``Decimal`` from
``str`` throughout so no binary-float noise ever reaches the cache.
"""
from __future__ import annotations

import concurrent.futures
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import TypeVar

import pandas as pd

HistoryFn = Callable[[str, date, date], "pd.DataFrame"]

_PROVIDER = "yfinance"

# Hard wall-clock deadline on a single yfinance history call. One hung TCP
# connection must not stall an entire sweep forever; per-symbol isolation
# (backfill_symbol_windows, PriceCache.update) is powerless against a call
# that never raises and never returns.
HISTORY_DEADLINE_SECONDS = 60.0

_T = TypeVar("_T")


def _with_deadline(fn: Callable[[], _T], seconds: float) -> _T:
    """Run ``fn()`` with a hard wall-clock deadline; raise ``TimeoutError`` past it.

    Uses a single-worker ``ThreadPoolExecutor`` rather than ``signal.alarm``,
    which only fires in the main thread and would silently no-op (or raise)
    when called from a worker thread. Any exception ``fn`` raises propagates
    unchanged; on a timeout, ``future.result()`` raises
    ``concurrent.futures.TimeoutError`` (a ``TimeoutError`` subclass).

    Caution: on timeout the worker thread computing ``fn()`` is NOT
    cancelled — the executor is shut down with ``wait=False`` so this
    function returns immediately, but the underlying call keeps running to
    completion (or hanging) in the background. This is a deliberate,
    accepted leak: yfinance/requests offer no cooperative cancellation, and
    the alternative (blocking here until the leaked call finishes) would
    defeat the whole point of a deadline.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=seconds)
    finally:
        executor.shutdown(wait=False)


@dataclass(frozen=True)
class YfBar:
    """A single daily bar implementing ``ops.backtest.prices.PriceBarLike``."""

    symbol: str
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjusted_open: Decimal
    adjusted_high: Decimal
    adjusted_low: Decimal
    adjusted_close: Decimal
    volume: Decimal
    dividend: Decimal
    split_ratio: Decimal
    provider: str = _PROVIDER


def _money(value: object) -> Decimal:
    """Decimalize a numeric cell without going through binary float."""
    return Decimal(str(value))


def _default_history_fn(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Real yfinance call.  Mirrors ``get_YFin_data_online`` conventions."""
    import yfinance as yf

    from tradingagents.dataflows.stockstats_utils import yf_retry
    from tradingagents.dataflows.symbol_utils import normalize_symbol

    canonical = normalize_symbol(symbol)
    ticker = yf.Ticker(canonical)
    # yfinance treats ``end`` as EXCLUSIVE; add a day so the requested end
    # session is actually included.  ``auto_adjust=False`` keeps raw OHLC +
    # Adj Close; ``actions=True`` yields Dividends and Stock Splits columns.
    end_inclusive = end + timedelta(days=1)
    frame = _with_deadline(
        lambda: yf_retry(
            lambda: ticker.history(
                start=start.isoformat(),
                end=end_inclusive.isoformat(),
                auto_adjust=False,
                actions=True,
            )
        ),
        HISTORY_DEADLINE_SECONDS,
    )
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_localize(None)
    return frame


def yfinance_bar_fetcher(
    symbol: str,
    start: date,
    end: date,
    *,
    history_fn: HistoryFn | None = None,
) -> tuple[YfBar, ...]:
    """Fetch daily bars for ``symbol`` in ``[start, end]`` and map to ``YfBar``.

    ``history_fn(symbol, start, end) -> DataFrame`` is the injectable seam
    (default: real yfinance).  The frame is expected to carry ``Open, High,
    Low, Close, Adj Close, Volume`` and, when present, ``Dividends`` and
    ``Stock Splits`` columns, indexed by a ``DatetimeIndex``.
    """
    fetch = history_fn or _default_history_fn
    frame = fetch(symbol, start, end)
    normalized = symbol.strip().upper()

    bars: list[YfBar] = []
    for index_value, row in frame.iterrows():
        close_raw = row["Close"]
        # yfinance occasionally emits placeholder rows with a NaN or zero
        # close; they cannot be priced or split-adjusted, so drop them.
        if pd.isna(close_raw) or float(close_raw) == 0.0:
            continue

        close = _money(close_raw)
        adj_close = _money(row["Adj Close"])
        # Adjusted OHLC are not provided; derive them from the same
        # split/dividend adjustment ratio that yfinance applied to the close.
        ratio = adj_close / close

        open_ = _money(row["Open"])
        high = _money(row["High"])
        low = _money(row["Low"])

        dividend_raw = row.get("Dividends", 0.0)
        dividend = (
            Decimal("0") if pd.isna(dividend_raw) else _money(dividend_raw)
        )

        split_raw = row.get("Stock Splits", 0.0)
        # yfinance emits 0 on non-split days; the cache requires split_ratio > 0.
        if pd.isna(split_raw) or float(split_raw) <= 0.0:
            split_ratio = Decimal("1")
        else:
            split_ratio = _money(split_raw)

        session = index_value.date() if hasattr(index_value, "date") else index_value

        bars.append(
            YfBar(
                symbol=normalized,
                session=session,
                open=open_,
                high=high,
                low=low,
                close=close,
                adjusted_open=open_ * ratio,
                adjusted_high=high * ratio,
                adjusted_low=low * ratio,
                adjusted_close=adj_close,
                volume=_money(int(row["Volume"])),
                dividend=dividend,
                split_ratio=split_ratio,
                provider=_PROVIDER,
            )
        )

    return tuple(bars)
