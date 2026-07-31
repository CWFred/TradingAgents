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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from ops.backtest.price_fetch import batch_history_fn, yfinance_bar_fetcher
from ops.backtest.prices import PriceBarLike, PriceCache
from ops.scheduler.market_calendar import MarketCalendar

Fetcher = Callable[[str, date, date], Iterable[PriceBarLike]]
BatchFetcher = Callable[[Sequence[str], date, date], dict]

# Sealed manifests inherit whatever the cache holds before asof; 400d ≈ 13 months of context.
PRE_ASOF_PAD = timedelta(days=400)

# Symbols sharing an identical uncovered window are batched into groups of at
# most this many tickers per ``yf.download`` call.
BATCH_SIZE = 50


@dataclass(frozen=True)
class BackfillSummary:
    symbols: int
    bars: int
    failures: tuple[tuple[str, str], ...]
    skipped: int = 0


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


def _uncovered_windows(
    coverage: tuple[date, date] | None, w0: date, w1: date,
) -> list[tuple[date, date]]:
    """Sub-ranges of ``[w0, w1]`` not already spanned by ``coverage``.

    Empty when ``coverage`` fully spans ``[w0, w1]`` (nothing to fetch). Up
    to two sub-ranges (leading + trailing gap) when coverage only partially
    overlaps; the whole window when there is no coverage at all. Interior
    gaps within an otherwise-covered range are not detected — see
    ``PriceCache.coverage``.
    """
    if coverage is None:
        return [(w0, w1)]
    lo, hi = coverage
    if lo <= w0 and hi >= w1:
        return []
    windows: list[tuple[date, date]] = []
    if w0 < lo:
        windows.append((w0, min(lo - timedelta(days=1), w1)))
    if hi < w1:
        windows.append((max(hi + timedelta(days=1), w0), w1))
    return windows


def _frame_fetcher(frame) -> Fetcher:
    """Adapt an already-fetched batch frame into the ``Fetcher`` shape.

    Lets a batch result flow through ``PriceCache.update`` exactly like a
    live per-symbol call would, so the same mapping/validation code
    (``yfinance_bar_fetcher``) runs either way.
    """

    def _fetch(symbol: str, start: date, end: date):
        return yfinance_bar_fetcher(symbol, start, end, history_fn=lambda *_: frame)

    return _fetch


def backfill_symbol_windows(
    config,
    pairs: Sequence[tuple[str, date]],
    *,
    fetcher: Fetcher | None = None,
    batch_fetcher: BatchFetcher | None = None,
    calendar: MarketCalendar | None = None,
    today: date | None = None,
) -> BackfillSummary:
    """Backfill price bars for explicit ``(symbol, asof)`` pairs.

    One desired window per distinct symbol, spanning
    ``[min_asof - PRE_ASOF_PAD, min(today, horizon_end(max_asof))]`` across
    that symbol's pairs. This is the single window implementation shared by
    :func:`backfill_prices` (post-hoc, derives pairs from stored cases) and
    prepare-time callers that need to backfill a fresh symbol before it is
    ever stored as a case (see ``_reconstruction_prepare_cases``).

    Before fetching, each symbol's cached coverage (see
    ``PriceCache.coverage``) is consulted: a symbol whose desired window is
    already fully cached is skipped entirely (counted in
    ``BackfillSummary.skipped``, not ``symbols``); a partially-covered
    symbol only fetches its uncovered leading/trailing sub-range(s).

    Symbols that end up needing their *entire* desired window (no coverage
    yet) and share an identical window are grouped into batches of at most
    ``BATCH_SIZE`` and fetched with one ``batch_fetcher`` call; a symbol
    missing from the batch result falls back to the per-symbol ``fetcher``
    individually, so one bad ticker in a batch never sinks the rest.

    Returns counts of symbols attempted (including failures), total bars
    persisted, symbols skipped as fully covered, and the per-symbol
    failures; one bad symbol never aborts the rest (see module docstring).
    """
    fetcher = fetcher or yfinance_bar_fetcher
    batch_fetcher = batch_fetcher or batch_history_fn
    today = today or date.today()
    max_horizon = max(config.backtest_horizons)

    if not pairs:
        return BackfillSummary(symbols=0, bars=0, failures=(), skipped=0)

    # Per-symbol as-of extents (distinct symbols, deterministic order).
    extents: dict[str, tuple[date, date]] = {}
    for symbol, asof in pairs:
        normalized = symbol.strip().upper()
        if normalized in extents:
            lo, hi = extents[normalized]
            extents[normalized] = (min(lo, asof), max(hi, asof))
        else:
            extents[normalized] = (asof, asof)

    cache = PriceCache(config.backtest_store_path)
    symbols = 0
    bars = 0
    skipped = 0
    failures: list[tuple[str, str]] = []

    def _fetch(symbol: str, windows: Sequence[tuple[date, date]]) -> None:
        nonlocal symbols, bars
        symbols += 1
        try:
            total = 0
            for w0, w1 in windows:
                total += cache.update(symbol, start=w0, end=w1, fetcher=fetcher)
            bars += total
        except Exception as error:  # noqa: BLE001 - isolate one bad symbol
            failures.append((symbol, str(error)))

    # Determine each symbol's uncovered sub-window(s); fully covered symbols
    # never reach a fetch call at all.
    pending: dict[str, list[tuple[date, date]]] = {}
    for symbol in sorted(extents):
        min_asof, max_asof = extents[symbol]
        w0 = min_asof - PRE_ASOF_PAD
        w1 = min(today, _horizon_end(max_asof, max_horizon, calendar))
        windows = _uncovered_windows(cache.coverage(symbol), w0, w1)
        if not windows:
            skipped += 1
            continue
        pending[symbol] = windows

    # Symbols needing exactly one (whole-window) fetch and sharing an
    # identical window are batchable; partial-coverage symbols (two
    # sub-windows) always go through the per-symbol path.
    single_window: dict[tuple[date, date], list[str]] = {}
    singles_only: dict[str, list[tuple[date, date]]] = {}
    for symbol, windows in pending.items():
        if len(windows) == 1:
            single_window.setdefault(windows[0], []).append(symbol)
        else:
            singles_only[symbol] = windows

    for window, group_symbols in single_window.items():
        w0, w1 = window
        for start_idx in range(0, len(group_symbols), BATCH_SIZE):
            batch = group_symbols[start_idx : start_idx + BATCH_SIZE]
            if len(batch) == 1:
                _fetch(batch[0], [(w0, w1)])
                continue
            try:
                frames = batch_fetcher(batch, w0, w1)
            except Exception:  # noqa: BLE001 - batch call failed; fall back per-symbol
                frames = {}
            for symbol in batch:
                frame = frames.get(symbol)
                updated = False
                if frame is not None:
                    symbols += 1
                    try:
                        bars += cache.update(
                            symbol, start=w0, end=w1, fetcher=_frame_fetcher(frame),
                        )
                        updated = True
                    except Exception:  # noqa: BLE001 - fall back per-symbol below
                        symbols -= 1
                if not updated:
                    _fetch(symbol, [(w0, w1)])

    for symbol, windows in singles_only.items():
        _fetch(symbol, windows)

    return BackfillSummary(
        symbols=symbols, bars=bars, failures=tuple(failures), skipped=skipped,
    )


def backfill_prices(
    config,
    store,
    *,
    sleeve: str,
    start: date,
    end: date,
    fetcher: Fetcher | None = None,
    batch_fetcher: BatchFetcher | None = None,
    calendar: MarketCalendar | None = None,
    today: date | None = None,
) -> BackfillSummary:
    """Backfill price bars for a sleeve's cases plus the benchmark.

    Returns counts of symbols attempted (case symbols + benchmark, including
    failures), total bars persisted, symbols skipped as fully covered, and
    the per-symbol failures.
    """
    cases = [
        case
        for case in store.list_cases(sleeve=sleeve)
        if start <= case.asof <= end
    ]
    if not cases:
        return BackfillSummary(symbols=0, bars=0, failures=(), skipped=0)

    pairs = [(case.symbol, case.asof) for case in cases]
    # Benchmark over the global union window: pin both extremes explicitly
    # since backfill_symbol_windows derives a symbol's window from the min
    # and max asof across all pairs sharing that symbol.
    global_min = min(case.asof for case in cases)
    global_max = max(case.asof for case in cases)
    pairs.append((config.backtest_benchmark, global_min))
    pairs.append((config.backtest_benchmark, global_max))

    return backfill_symbol_windows(
        config, pairs, fetcher=fetcher, batch_fetcher=batch_fetcher,
        calendar=calendar, today=today,
    )
