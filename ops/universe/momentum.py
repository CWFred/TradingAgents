"""Cross-sectional momentum sleeve: 6-month leaders above their 200-day MA.

Structured like earnings.py — a pure finder with an injectable fetcher, so it
is unit-testable with fakes and has no import-time I/O. Returns the FULL
ranked list, not a top-N slice: the composite builder takes the head for
entries and the exit engine (Part 2) looks up ranks of held names — one
computation per tick, two consumers."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable

import yfinance as yf

from ops.universe.earnings import _safe_decimal
from ops.universe.yf_pacing import call_paced

# ~6 months of trading days for the ranking signal — deliberately between
# 3mo (noisier, higher turnover) and 12mo (sluggish). Named so it is easy
# to tune.
RETURN_LOOKBACK_TRADING_DAYS = 126
SMA_WINDOW = 200
# A 200-day SMA needs ~200 trading days ≈ 9.5 calendar months of bars.
_HISTORY_PERIOD = "10mo"


@dataclass(frozen=True)
class MomentumHit:
    symbol: str
    asof_date: date
    trailing_return_6m: Decimal
    close: Decimal
    sma_200: Decimal
    avg_dollar_volume_20d: Decimal
    rank: int  # 1-based position on the day's leaderboard


def fetch_closes_and_volumes_from_yfinance(
    symbol: str,
) -> tuple[list[Decimal], list[Decimal]] | None:
    """Chronological (closes, volumes) for ~10 months of daily bars.

    NaN rows are DROPPED, not zero-filled — a fabricated zero close would
    poison both the return and the SMA. Only the yfinance I/O is wrapped
    in try/except (same policy as filters.py)."""
    try:
        hist = call_paced(
            lambda: yf.Ticker(symbol).history(period=_HISTORY_PERIOD, auto_adjust=False),
            label="momentum",
        )
    except Exception as exc:
        print(
            f"[momentum] skipped {symbol}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    if hist is None or hist.empty:
        return None
    try:
        frame = hist.dropna(subset=["Close", "Volume"])
        closes = [_safe_decimal(c) for c in frame["Close"].tolist()]
        volumes = [_safe_decimal(v) for v in frame["Volume"].tolist()]
    except (KeyError, AttributeError) as exc:
        print(
            f"[momentum] skipped {symbol}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    return closes, volumes


def find_momentum_leaders(
    members: list[str],
    asof_date: date,
    *,
    fetch: Callable[[str], tuple[list[Decimal], list[Decimal]] | None] | None = None,
) -> list[MomentumHit]:
    fetch = fetch or fetch_closes_and_volumes_from_yfinance
    scored: list[tuple[Decimal, str, Decimal, Decimal, Decimal]] = []
    for sym in members:
        data = fetch(sym)
        if data is None:
            continue
        closes, volumes = data
        # Insufficient history: skip, never zero-fill.
        if len(closes) < max(SMA_WINDOW, RETURN_LOOKBACK_TRADING_DAYS + 1):
            continue
        last = closes[-1]
        base = closes[-(RETURN_LOOKBACK_TRADING_DAYS + 1)]
        if base == 0:
            continue
        ret = (last - base) / base
        sma = sum(closes[-SMA_WINDOW:]) / Decimal(SMA_WINDOW)
        if last <= sma:
            continue  # uptrend gate: buy strength, never catch a falling knife
        tail_c = closes[-20:]
        tail_v = volumes[-20:]
        adv = sum(c * v for c, v in zip(tail_c, tail_v)) / Decimal(len(tail_c))
        scored.append((ret, sym, last, sma, adv))
    # Descending return; symbol tie-break keeps ordering deterministic.
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [
        MomentumHit(
            symbol=sym, asof_date=asof_date, trailing_return_6m=ret,
            close=last, sma_200=sma, avg_dollar_volume_20d=adv, rank=i + 1,
        )
        for i, (ret, sym, last, sma, adv) in enumerate(scored)
    ]
