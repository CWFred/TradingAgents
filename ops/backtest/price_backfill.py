"""Populate the backtest ``price_bars`` cache from stored cases.

The driver walks the sealed cases for a sleeve, opens a per-symbol fetch window
that reaches from a short pre-roll before the earliest as-of to far enough past
the latest as-of to cover the longest scoring horizon, and updates the offline
:class:`~ops.backtest.prices.PriceCache` once per distinct symbol.  The
benchmark (SPY by default) is fetched once over the global union window because
excess-return scoring compares every case against it.

Two invariants make this safe to run against a live corpus:

* **Horizon coverage in calendar days.**  Horizons are measured in *trading
  sessions*, not calendar days.  A ``MarketCalendar`` (when supplied) resolves
  the exact Nth session after an as-of; otherwise a conservative pad of
  ``ceil(max_horizon * 1.6) + 5`` calendar days over-covers the window (roughly
  1.6 calendar days per session plus slack for holidays) so the horizon bar is
  always present.
* **Per-symbol failure isolation.**  One symbol that fails to fetch records a
  ``(symbol, error)`` entry and never aborts the run; the remaining symbols and
  the benchmark still persist.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from ops.backtest.price_fetch import yfinance_bar_fetcher
from ops.backtest.prices import PriceBarLike, PriceCache
from ops.scheduler.market_calendar import MarketCalendar

Fetcher = Callable[[str, date, date], Iterable[PriceBarLike]]

_PREROLL_DAYS = 10


@dataclass(frozen=True)
class BackfillSummary:
    symbols: int
    bars: int
    failures: tuple[tuple[str, str], ...]


def _horizon_end(asof: date, max_horizon: int, calendar: MarketCalendar | None) -> date:
    """Calendar date on/after which ``max_horizon`` sessions have elapsed.

    With a calendar we resolve the exact Nth trading session strictly after
    ``asof``.  Without one (or if the padded search window somehow holds too
    few sessions) we fall back to the safe calendar pad, which over-covers.
    """
    pad = math.ceil(max_horizon * 1.6) + 5
    if calendar is not None:
        sessions = calendar.sessions_between(
            asof + timedelta(days=1), asof + timedelta(days=pad)
        )
        if len(sessions) >= max_horizon:
            return sessions[max_horizon - 1]
    return asof + timedelta(days=pad)


def backfill_prices(
    config,
    store,
    *,
    sleeve: str,
    start: date,
    end: date,
    fetcher: Fetcher | None = None,
    calendar: MarketCalendar | None = None,
    today: date | None = None,
) -> BackfillSummary:
    """Backfill price bars for a sleeve's cases plus the benchmark.

    Returns counts of symbols attempted (case symbols + benchmark, including
    failures), total bars persisted, and the per-symbol failures.
    """
    fetcher = fetcher or yfinance_bar_fetcher
    today = today or date.today()
    max_horizon = max(config.backtest_horizons)

    cases = [
        case
        for case in store.list_cases(sleeve=sleeve)
        if start <= case.asof <= end
    ]
    if not cases:
        return BackfillSummary(symbols=0, bars=0, failures=())

    # Per-symbol as-of extents (distinct symbols, deterministic order).
    extents: dict[str, tuple[date, date]] = {}
    for case in cases:
        symbol = case.symbol.strip().upper()
        if symbol in extents:
            lo, hi = extents[symbol]
            extents[symbol] = (min(lo, case.asof), max(hi, case.asof))
        else:
            extents[symbol] = (case.asof, case.asof)

    cache = PriceCache(config.backtest_store_path)
    symbols = 0
    bars = 0
    failures: list[tuple[str, str]] = []

    def _fetch(symbol: str, w0: date, w1: date) -> None:
        nonlocal symbols, bars
        symbols += 1
        try:
            bars += cache.update(symbol, start=w0, end=w1, fetcher=fetcher)
        except Exception as error:  # noqa: BLE001 - isolate one bad symbol
            failures.append((symbol, str(error)))

    for symbol in sorted(extents):
        min_asof, max_asof = extents[symbol]
        w0 = min_asof - timedelta(days=_PREROLL_DAYS)
        w1 = min(today, _horizon_end(max_asof, max_horizon, calendar))
        _fetch(symbol, w0, w1)

    # Benchmark once over the global union window.
    global_min = min(lo for lo, _ in extents.values())
    global_max = max(hi for _, hi in extents.values())
    bench_w0 = global_min - timedelta(days=_PREROLL_DAYS)
    bench_w1 = min(today, _horizon_end(global_max, max_horizon, calendar))
    _fetch(config.backtest_benchmark.strip().upper(), bench_w0, bench_w1)

    return BackfillSummary(symbols=symbols, bars=bars, failures=tuple(failures))
