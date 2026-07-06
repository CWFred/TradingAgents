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

    def close_on_or_before(self, when: date, *, max_gap_days: int = 10) -> Decimal | None:
        for offset in range(max_gap_days + 1):
            d = when - timedelta(days=offset)
            if d in self.closes:
                return self.closes[d]
        return None

    def unadjusted_close_on_or_before(
        self, when: date, *, max_gap_days: int = 10,
    ) -> Decimal | None:
        """As-traded close: Yahoo back-adjusts splits into Close, but XBRL EPS
        is as-reported in the era's share count, so P/E history must undo the
        adjustment — multiply by every split ratio dated after the hit day."""
        for offset in range(max_gap_days + 1):
            d = when - timedelta(days=offset)
            if d in self.closes:
                factor = Decimal("1")
                for split_date, ratio in self.splits.items():
                    if split_date > d and ratio > 0:
                        factor *= ratio
                return self.closes[d] * factor
        return None


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
