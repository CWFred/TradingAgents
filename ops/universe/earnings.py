"""Recent-earnings filter. Returns tickers that reported in the last N
trading days with an EPS beat.

v1 note: this only enforces an EPS beat. yfinance's `earnings_dates` frame
does not expose reported/estimated revenue, so `revenue_actual`,
`revenue_estimate`, and `revenue_beat` on `EarningsHit` are `None` when that
data isn't genuinely available — they are populated (and `revenue_beat`
computed) only when both source columns are actually present, and are never
fabricated as zero. Revenue-beat enforcement would require a different data
source (e.g. Finnhub's earnings calendar); see
docs/superpowers/specs/2026-06-30-tradingagents-live-v1-design.md for the
future option."""
from __future__ import annotations

import math
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
    revenue_actual: Decimal | None
    revenue_estimate: Decimal | None
    eps_beat: bool
    revenue_beat: bool | None


def _safe_decimal(v) -> Decimal:
    """Convert a yfinance numeric value to Decimal, treating NaN/None as 0.
    Prevents decimal.InvalidOperation on NaN and normalises absent data."""
    if v is None:
        return Decimal("0")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return Decimal("0")
    if math.isnan(f):
        return Decimal("0")
    return Decimal(str(f))


def _optional_decimal(row, col: str) -> Decimal | None:
    """Return a Decimal for `col` only if the column is actually present on
    `row` and its value parses to a real (non-NaN) number. Returns None on
    absence rather than fabricating a zero — callers must be able to tell
    "no data" apart from "value of zero"."""
    if col not in row.index:
        return None
    v = row[col]
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return Decimal(str(f))


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
    """Fetch the most recent earnings row for `symbol` from yfinance.

    Only yfinance's I/O boundary is wrapped in try/except — subsequent
    conversion and dataclass construction happen outside so genuine
    internal regressions surface as real errors rather than being
    silently reported as external fetch failures."""
    import sys
    try:
        t = yf.Ticker(symbol)
        df = getattr(t, "earnings_dates", None)
    except Exception as exc:
        print(
            f"[earnings] skipped {symbol}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    if df is None or df.empty:
        return None
    try:
        df = df.dropna(subset=["EPS Estimate", "Reported EPS"])
    except (KeyError, ValueError) as exc:
        # DataFrame column layout changed — treat as fetch failure
        print(
            f"[earnings] skipped {symbol}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    if df.empty:
        return None
    row = df.iloc[0]
    eps_actual = _safe_decimal(row["Reported EPS"])
    eps_est = _safe_decimal(row["EPS Estimate"])
    rev_actual = _optional_decimal(row, "Reported Revenue")
    rev_est = _optional_decimal(row, "Revenue Estimate")
    if rev_actual is not None and rev_est is not None:
        revenue_beat = rev_actual > rev_est
    else:
        # Only genuinely available when BOTH columns are present — a
        # one-sided value is still absent data, not honest, so discard it
        # rather than compare against a fabricated zero.
        rev_actual = None
        rev_est = None
        revenue_beat = None
    report_date = row.name.date() if hasattr(row.name, "date") else row.name
    return EarningsHit(
        symbol=symbol,
        report_date=report_date,
        eps_actual=eps_actual,
        eps_estimate=eps_est,
        revenue_actual=rev_actual,
        revenue_estimate=rev_est,
        eps_beat=eps_actual > eps_est,
        revenue_beat=revenue_beat,
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
        if not hit.eps_beat:
            continue
        hits.append(hit)
    return hits
