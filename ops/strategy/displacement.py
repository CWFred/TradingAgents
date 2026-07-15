"""Displacement planner: trim starter positions to fund high-conviction buys.

Pure planning — no broker calls except the injected quote function, no
journal writes. The orchestrator executes the returned plan (spec
2026-07-14, "Displacement engine"). Guards, all from OpsConfig:

- starters only (tier from position_opened provenance; missing tier =
  pre-v2 position = immune),
- oldest entry_date first, partial trims allowed,
- at most displacement_max_trims_per_day trims per trading day (planned
  trims here + trims_used_today already journaled),
- a starter must be >= displacement_min_holding_age_days TRADING days old,
- up-ladder only: trims fund TIER_HIGH proposals exclusively; a starter
  proposal that lacks cash is skipped, never funded by displacement.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ops.broker.base import BrokerError
from ops.broker.types import Position
from ops.config import OpsConfig
from ops.pipeline_adapter import TIER_HIGH, TIER_STARTER
from ops.strategy.base import StrategyOrder
from ops.trading_time import trading_days_between


@dataclass(frozen=True)
class PlannedTrim:
    symbol: str
    tier: str
    notional: Decimal
    funded_symbol: str


@dataclass(frozen=True)
class UnfundedSkip:
    symbol: str
    shortfall: Decimal
    reason: str


@dataclass(frozen=True)
class DisplacementPlan:
    trims: list[PlannedTrim]
    funded_client_order_ids: frozenset[str]
    skips: list[UnfundedSkip]


def _quantize_money(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"))


def _proposal_tier(p: StrategyOrder) -> str:
    return TIER_STARTER if getattr(p.pipeline, "tier", "") == TIER_STARTER else TIER_HIGH


def _trimmable_starters(
    positions: list[Position],
    provenance: dict[str, dict],
    quote: Callable[[str], Decimal],
    asof_date: date,
    min_age_days: int,
) -> tuple[list[str], dict[str, Decimal]]:
    """Starter symbols oldest-first plus their remaining trimmable value."""
    aged: list[tuple[str, Position]] = []
    for pos in positions:
        payload = provenance.get(pos.symbol)
        if not payload or payload.get("tier") != TIER_STARTER:
            continue  # untiered (pre-v2) and high positions are immune
        entry = payload.get("entry_date")
        if not entry:
            continue
        if trading_days_between(date.fromisoformat(entry), asof_date) < min_age_days:
            continue
        aged.append((entry, pos))
    aged.sort(key=lambda t: t[0])
    ordered: list[str] = []
    value: dict[str, Decimal] = {}
    for _, pos in aged:
        try:
            px = quote(pos.symbol)
        except BrokerError:
            continue  # unquotable starter: skip it, never block the plan
        ordered.append(pos.symbol)
        value[pos.symbol] = pos.market_value(px)
    return ordered, value


def plan_displacement(
    *,
    proposals: list[StrategyOrder],
    positions: list[Position],
    provenance: dict[str, dict],
    quote: Callable[[str], Decimal],
    cash: Decimal,
    equity: Decimal,
    trims_used_today: int,
    asof_date: date,
    config: OpsConfig,
) -> DisplacementPlan:
    # Spendable cash above the reserve floor. Trims convert position value
    # to cash without changing equity, so the floor is constant all plan.
    available = cash - equity * config.cash_reserve_pct
    trim_budget = max(0, config.displacement_max_trims_per_day - trims_used_today)

    ordered_starters, remaining_value = _trimmable_starters(
        positions, provenance, quote, asof_date,
        config.displacement_min_holding_age_days,
    )

    trims: list[PlannedTrim] = []
    skips: list[UnfundedSkip] = []
    funded: set[str] = set()

    # High-conviction proposals get first claim on cash AND on trims.
    ordered = sorted(proposals, key=lambda p: 0 if _proposal_tier(p) == TIER_HIGH else 1)
    for p in ordered:
        need = p.order.notional_dollars
        if available >= need:
            available -= need
            funded.add(p.order.client_order_id)
            continue
        if _proposal_tier(p) != TIER_HIGH:
            skips.append(UnfundedSkip(
                symbol=p.order.symbol,
                shortfall=need - available,
                reason="insufficient cash; starter entries never displace",
            ))
            continue
        shortfall = need - available
        planned_here: list[PlannedTrim] = []
        for sym in ordered_starters:
            if len(trims) + len(planned_here) >= trim_budget:
                break
            value = remaining_value.get(sym, Decimal("0"))
            if value <= 0:
                continue
            take = min(value, shortfall)
            planned_here.append(PlannedTrim(
                symbol=sym, tier=TIER_STARTER,
                notional=_quantize_money(take), funded_symbol=p.order.symbol,
            ))
            shortfall -= take
            if shortfall <= 0:
                break
        if shortfall > 0:
            # All-or-nothing per proposal: never trim for a buy that still
            # cannot be placed afterward.
            skips.append(UnfundedSkip(
                symbol=p.order.symbol,
                shortfall=shortfall,
                reason="shortfall remains after displacement guards",
            ))
            continue
        for t in planned_here:
            remaining_value[t.symbol] -= t.notional
        trims.extend(planned_here)
        available = Decimal("0")  # cash was fully consumed; trims covered the rest
        funded.add(p.order.client_order_id)

    return DisplacementPlan(
        trims=trims,
        funded_client_order_ids=frozenset(funded),
        skips=skips,
    )
