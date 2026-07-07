"""Per-name daily price history, fetched once and reused.

The screener needs prices twice per name — the last 60 closes for the
selloff trigger and a close near each fiscal year end for the P/E-history
bar. One 6-year yfinance history call serves both, instead of six separate
calls per name.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import yfinance as yf

from ops.universe.earnings import _safe_decimal
from ops.universe.yf_pacing import call_paced


@dataclass(frozen=True)
class PriceContext:
    closes: dict[date, Decimal]            # trading day -> close (split-ADJUSTED, from Yahoo)
    splits: dict[date, Decimal] = field(default_factory=dict)  # split date -> ratio

    def recent_closes(self, *, asof: date, days: int = 60) -> list[Decimal]:
        dates = sorted(d for d in self.closes if d <= asof)[-days:]
        return [self.closes[d] for d in dates]

    def _hit_on_or_before(
        self, when: date, *, max_gap_days: int,
    ) -> tuple[date, Decimal] | None:
        for offset in range(max_gap_days + 1):
            d = when - timedelta(days=offset)
            if d in self.closes:
                return d, self.closes[d]
        return None

    def split_factor_after(self, anchor: date) -> Decimal:
        """Product of split ratios dated after ``anchor`` — converts a Yahoo
        split-adjusted close into the share basis (era) of ``anchor``."""
        factor = Decimal("1")
        for split_date, ratio in self.splits.items():
            if split_date > anchor and ratio > 0:
                factor *= ratio
        return factor

    def close_on_or_before(self, when: date, *, max_gap_days: int = 10) -> Decimal | None:
        hit = self._hit_on_or_before(when, max_gap_days=max_gap_days)
        return hit[1] if hit is not None else None

    def unadjusted_close_on_or_before(
        self, when: date, *, max_gap_days: int = 10, era_end: date | None = None,
    ) -> Decimal | None:
        """As-traded close in the share basis of ``era_end`` (default: the hit
        day). Yahoo back-adjusts splits into Close, but XBRL EPS and universe
        snapshots are as-reported in their era's share count, so comparisons
        must undo every split dated after the era boundary."""
        hit = self._hit_on_or_before(when, max_gap_days=max_gap_days)
        if hit is None:
            return None
        d, close = hit
        return close * self.split_factor_after(era_end if era_end is not None else d)


def fetch_price_context(symbol: str) -> PriceContext | None:
    """6 years of daily closes; None (with a stderr diagnostic) on any fetch failure."""
    try:
        hist = call_paced(
            lambda: yf.Ticker(symbol).history(period="6y", auto_adjust=False, actions=True),
            label="prices",
        )
    except Exception as exc:
        print(
            f"[prices] skipped {symbol}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    if hist is None or hist.empty or "Close" not in hist:
        return None
    closes: dict[date, Decimal] = {}
    for ts, close in hist["Close"].items():
        value = _safe_decimal(close)
        if value > 0:
            closes[ts.date()] = value
    splits: dict[date, Decimal] = {}
    if "Stock Splits" in hist:
        for ts, ratio in hist["Stock Splits"].items():
            value = _safe_decimal(ratio)
            if value > 0:
                splits[ts.date()] = value
    return PriceContext(closes=closes, splits=splits) if closes else None
