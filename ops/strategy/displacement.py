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
from decimal import ROUND_DOWN, ROUND_FLOOR, Decimal

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
    full_exit: bool = False
    """True when this trim consumes the starter's entire remaining value
    (i.e. `take == value` before the shortfall min — see plan_displacement).
    A full exit must be executed via broker.close_position, never a
    notional SELL: a value-rounded-DOWN-to-cents SELL can leave up to ~1
    cent of dust shares behind (above the paper broker's 1e-7 epsilon),
    which keeps the position alive — occupying a max_open_positions slot
    and blocking re-entry via `held` (finding I1)."""


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
    """Raw tier string off the proposal, defaulting to "" (fail CLOSED).

    D4: only an *exact* TIER_HIGH match is displacement-fundable. An empty
    or unrecognized tier must never be treated as TIER_HIGH — that would
    grant displacement-funding rights to any future tier-less proposal
    (e.g. a new upstream rating this code doesn't know about yet). Callers
    checking fundability compare `== TIER_HIGH` directly; this function
    intentionally does NOT collapse unknown tiers to TIER_HIGH or
    TIER_STARTER so the skip-reason logic below can tell the two apart.
    """
    return getattr(p.pipeline, "tier", "") or ""


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
        # Quantize once at the anchor: fractional-share positions (the norm,
        # see ops/broker/paper.py) produce sub-cent market values, and all
        # downstream shortfall/take arithmetic must stay cent-aligned.
        # ROUND_DOWN, never half-even: rounding a 99.995 position up to
        # 100.00 plans a sell above its quoted value, which the broker
        # rejects (qty_to_sell > held) — stranding the buy it funded.
        value[pos.symbol] = pos.market_value(px).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN,
        )
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
    # Quantized once here: with this and starter values cent-anchored, every
    # derived shortfall/take (and UnfundedSkip.shortfall) is exact cents.
    #
    # ROUND_FLOOR, never HALF_EVEN and never ROUND_DOWN: this value's sign
    # is not guaranteed. With fractional equity (the norm — paper fills are
    # fractional-share) cash can sit fractionally BELOW the exact floor,
    # e.g. equity 10000.03 / cash 1600.00 puts the true floor at 1600.0048
    # and the true available at -0.0048. HALF_EVEN rounds that to 0.00, and
    # so does ROUND_DOWN (which truncates *toward* zero, not toward
    # -infinity — for a negative input that means UP, i.e. more generous).
    # Both overstate how much is actually spendable, so a trim-funded buy's
    # shortfall gets computed a fraction of a cent short: the planner trims
    # exactly "enough", the trims fill, and the buy that follows lands
    # post-trade cash a hair below the floor — CashReserveRule's unquantized
    # compare rejects it AFTER the trims already sold (see
    # tests/ops/strategy/test_displacement_integration.py for the real-
    # broker repro). ROUND_FLOOR is the only mode that keeps
    # available <= the true value on both sides of zero, so the computed
    # shortfall is never short.
    available = (cash - equity * config.cash_reserve_pct).quantize(
        Decimal("0.01"), rounding=ROUND_FLOOR,
    )
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
        tier = _proposal_tier(p)
        if tier != TIER_HIGH:
            # D4: fail CLOSED. TIER_STARTER gets its own reason (working as
            # designed — starters never displace); anything else (empty or
            # an unrecognized tier string) is called out separately so the
            # journal makes clear this wasn't a starter-tier proposal, it's
            # a proposal displacement doesn't understand and refuses to
            # fund via trims.
            reason = (
                "insufficient cash; starter entries never displace"
                if tier == TIER_STARTER
                else "insufficient cash; unknown tier never displaces"
            )
            skips.append(UnfundedSkip(
                symbol=p.order.symbol,
                shortfall=need - available,
                reason=reason,
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
                full_exit=take == value,
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
