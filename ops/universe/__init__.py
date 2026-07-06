"""Top-level universe builder.

Composes: S&P 500 members → deny-list → recent earnings beats → liquidity →
sorted list of Candidates."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Callable

from ops.config import OpsConfig
from ops.universe.earnings import EarningsHit, find_recent_earnings_beats
from ops.universe.filters import (
    apply_deny_list,
    apply_liquidity_filter,
    fetch_price_and_adv_from_yfinance,
)
from ops.universe.momentum import MomentumHit
from ops.universe.sp500 import load_sp500_members

# Hard-coded for v1 — promoted to OpsConfig only if we need to tune
_MIN_ADV = Decimal("50000000")
_MIN_PRICE = Decimal("5")
_LOOKBACK_TRADING_DAYS = 2


class CandidateSource(str, Enum):
    EARNINGS = "EARNINGS"
    MOMENTUM = "MOMENTUM"


@dataclass(frozen=True)
class Candidate:
    symbol: str
    source: CandidateSource
    last_price: Decimal
    avg_dollar_volume_20d: Decimal
    # Optional means genuinely absent, never fabricated. A name that is both
    # a fresh earnings beat and a momentum leader carries BOTH payloads with
    # source == EARNINGS (the primary thesis).
    earnings: EarningsHit | None = None
    momentum: MomentumHit | None = None

    def __post_init__(self) -> None:
        if self.earnings is None and self.momentum is None:
            raise ValueError("Candidate requires at least one sleeve payload")
        if self.source is CandidateSource.EARNINGS and self.earnings is None:
            raise ValueError("EARNINGS candidate requires an earnings payload")
        if self.source is CandidateSource.MOMENTUM and self.momentum is None:
            raise ValueError("MOMENTUM candidate requires a momentum payload")


def build_universe(
    *,
    asof_date: date,
    config: OpsConfig,
    members_loader: Callable[[], list[str]] | None = None,
    earnings_finder: Callable[..., list[EarningsHit]] | None = None,
    metrics_fetcher: Callable[[str], tuple[Decimal, Decimal] | None] | None = None,
) -> list[Candidate]:
    members_loader = members_loader or load_sp500_members
    earnings_finder = earnings_finder or find_recent_earnings_beats
    metrics_fetcher = metrics_fetcher or fetch_price_and_adv_from_yfinance

    members = members_loader()
    eligible = apply_deny_list(members, config.deny_list)
    hits = earnings_finder(
        eligible, asof_date=asof_date, lookback_days=_LOOKBACK_TRADING_DAYS,
    )
    hits_by_sym = {h.symbol: h for h in hits}
    liquid = apply_liquidity_filter(
        list(hits_by_sym.keys()),
        min_adv=_MIN_ADV,
        min_price=_MIN_PRICE,
        fetch_metrics=metrics_fetcher,
    )
    candidates = [
        Candidate(
            symbol=sym,
            source=CandidateSource.EARNINGS,
            earnings=hits_by_sym[sym],
            last_price=price,
            avg_dollar_volume_20d=adv,
        )
        for sym, price, adv in liquid
    ]
    candidates.sort(key=lambda c: c.symbol)
    return candidates


__all__ = ["Candidate", "CandidateSource", "build_universe"]
