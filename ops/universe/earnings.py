"""Recent-earnings filter. Returns tickers that reported in the last N
trading days with both an EPS beat and a revenue beat."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable

import yfinance as yf


@dataclass(frozen=True)
class EarningsHit:
    symbol: str
    report_date: date
    eps_actual: Decimal
    eps_estimate: Decimal
    revenue_actual: Decimal
    revenue_estimate: Decimal
    eps_beat: bool
    revenue_beat: bool


def _is_trading_day(d: date) -> bool:
    # Mon=0..Fri=4. Holidays are not handled here — a holiday inside the
    # lookback window simply shortens the effective range by one calendar day.
    return d.weekday() < 5


def _trading_days_back(asof: date, n: int) -> date:
    d = asof
    counted = 0
    while counted < n:
        d -= timedelta(days=1)
        if _is_trading_day(d):
            counted += 1
    return d


def _fetch_from_yfinance(symbol: str) -> EarningsHit | None:
    t = yf.Ticker(symbol)
    df = getattr(t, "earnings_dates", None)
    if df is None or df.empty:
        return None
    df = df.dropna(subset=["EPS Estimate", "Reported EPS"])
    if df.empty:
        return None
    # most recent reported row
    row = df.iloc[0]
    eps_actual = Decimal(str(row["Reported EPS"]))
    eps_est = Decimal(str(row["EPS Estimate"]))
    # Revenue columns may not be present; treat absence as a beat False
    rev_actual = Decimal(str(row.get("Reported Revenue", 0) or 0))
    rev_est = Decimal(str(row.get("Revenue Estimate", 0) or 0))
    return EarningsHit(
        symbol=symbol,
        report_date=row.name.date() if hasattr(row.name, "date") else row.name,
        eps_actual=eps_actual,
        eps_estimate=eps_est,
        revenue_actual=rev_actual,
        revenue_estimate=rev_est,
        eps_beat=eps_actual > eps_est,
        revenue_beat=rev_actual > rev_est,
    )


def find_recent_earnings_beats(
    tickers: list[str],
    asof_date: date,
    *,
    lookback_days: int = 2,
    fetch: Callable[[str], EarningsHit | None] | None = None,
) -> list[EarningsHit]:
    fetch = fetch or _fetch_from_yfinance
    earliest = _trading_days_back(asof_date, lookback_days)
    hits: list[EarningsHit] = []
    for sym in tickers:
        hit = fetch(sym)
        if hit is None:
            continue
        if hit.report_date < earliest or hit.report_date > asof_date:
            continue
        if not (hit.eps_beat and hit.revenue_beat):
            continue
        hits.append(hit)
    return hits
