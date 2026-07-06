"""Position exit engine (spec Component 6): daily, rule-based sell decisions.

Pure function over injected data — no I/O, no broker, no journal. The
orchestrator supplies positions, provenance (position_opened payloads),
today's leaderboard, and a closes fetcher, then acts on the report.

Never sells on missing data: an unfetchable symbol is a skip, not a close.
A data outage must not liquidate the book."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from ops.broker.types import Position
from ops.config import OpsConfig
from ops.trading_time import trading_days_between
from ops.universe.momentum import SMA_WINDOW, MomentumHit


@dataclass(frozen=True)
class ExitDecision:
    symbol: str
    rule: str  # "rank_decay" | "trend_break" | "max_hold"
    evidence: str


@dataclass(frozen=True)
class ExitSkip:
    symbol: str
    reason: str


@dataclass(frozen=True)
class ExitReport:
    decisions: list[ExitDecision]
    skips: list[ExitSkip]
    unknown_provenance: list[str]


def _sma(closes: list[Decimal], end: int) -> Decimal:
    window = closes[end - SMA_WINDOW:end]
    return sum(window) / Decimal(SMA_WINDOW)


def evaluate_exits(
    *,
    positions: list[Position],
    provenance: dict[str, dict[str, Any]],
    leaderboard: list[MomentumHit],
    closes_fetch: Callable[[str], tuple[list[Decimal], list[Decimal]] | None],
    config: OpsConfig,
    asof_date: date,
) -> ExitReport:
    rank_by_symbol = {h.symbol: h.rank for h in leaderboard}
    decisions: list[ExitDecision] = []
    skips: list[ExitSkip] = []
    unknown: list[str] = []

    for pos in positions:
        payload = provenance.get(pos.symbol)
        source = (payload or {}).get("source")

        if source == "EARNINGS":
            entry = date.fromisoformat(payload["entry_date"])
            held = trading_days_between(entry, asof_date)
            if held >= config.earnings_max_hold_days:
                decisions.append(ExitDecision(
                    symbol=pos.symbol, rule="max_hold",
                    evidence=(f"held {held} trading days >= "
                              f"{config.earnings_max_hold_days} (PEAD window)"),
                ))
            continue

        # MOMENTUM — or unknown provenance, which gets the general
        # "does this name still deserve a slot" test, flagged for audit.
        if source != "MOMENTUM":
            unknown.append(pos.symbol)

        data = closes_fetch(pos.symbol)
        if data is None or len(data[0]) < SMA_WINDOW + 1:
            skips.append(ExitSkip(
                symbol=pos.symbol,
                reason="insufficient close history for exit evaluation",
            ))
            continue
        closes = data[0]
        n = len(closes)
        sma_today = _sma(closes, n)
        sma_prev = _sma(closes, n - 1)
        if closes[-1] < sma_today and closes[-2] < sma_prev:
            decisions.append(ExitDecision(
                symbol=pos.symbol, rule="trend_break",
                evidence=(f"two closes below 200d MA "
                          f"({closes[-2]}, {closes[-1]} < {sma_today})"),
            ))
            continue
        rank = rank_by_symbol.get(pos.symbol)
        # Absence from the leaderboard alone never fires rank_decay: an
        # absent-but-fetchable name failed the MA gate on ONE close, and a
        # single close must not trigger an exit (hysteresis / whipsaw).
        if rank is not None and rank > config.momentum_exit_rank:
            decisions.append(ExitDecision(
                symbol=pos.symbol, rule="rank_decay",
                evidence=f"rank {rank} > {config.momentum_exit_rank}",
            ))

    return ExitReport(decisions=decisions, skips=skips,
                      unknown_provenance=unknown)
