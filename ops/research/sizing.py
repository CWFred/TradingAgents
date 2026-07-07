"""Conviction-tier sizing + hard fences for the research sleeve (Phase D).

The spec's "guardrails profile": implemented as a pure fence module rather
than ops/guardrails Rule subclasses because the Rule chain's context
(order, broker, config) cannot see sector, ADV, or the memo — and the
sleeve's sell rules live in memos, not broker stops. Every fence is
mechanical; LLM-stated probabilities are NEVER inputs (locked decision) —
the only research-quality signal used is the memo's conviction_tier.

All money in Decimal. Rejections carry the fence name + numbers so the
trade-run summary can say exactly why a memo produced no position.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

TIER_SIZING: dict[str, Decimal] = {
    "starter": Decimal("0.02"),
    "medium": Decimal("0.04"),
    "high": Decimal("0.06"),
}
NAME_CAP_PCT = Decimal("0.10")     # single name <= 10% of research equity at cost
SECTOR_CAP_PCT = Decimal("0.25")   # sector <= 25% at cost
ADV_CAP_PCT = Decimal("0.05")      # position <= 5% of 20-day dollar ADV
MIN_ORDER_DOLLARS = Decimal("100")


def _quantize_money(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"))


@dataclass(frozen=True)
class SizingDecision:
    notional: Decimal
    rejected: str | None = None


def cost_basis(positions) -> tuple[dict[str, Decimal], Decimal]:
    """{symbol: quantity * avg_entry_price} and the total, from live positions."""
    by_symbol = {p.symbol: p.quantity * p.avg_entry_price for p in positions}
    return by_symbol, sum(by_symbol.values(), Decimal("0"))


def size_entry(
    *,
    tier: str,
    equity: Decimal,
    cash: Decimal,
    cost_by_symbol: dict[str, Decimal],
    symbol: str,
    sector: str,
    cost_by_sector: dict[str, Decimal],
    adv_20d: Decimal | None,
) -> SizingDecision:
    pct = TIER_SIZING.get(tier)
    if pct is None:
        return SizingDecision(Decimal("0"), f"unknown tier {tier!r}")
    notional = _quantize_money(equity * pct)
    notional = min(notional, _quantize_money(cash))

    name_room = NAME_CAP_PCT * equity - cost_by_symbol.get(symbol, Decimal("0"))
    if name_room < MIN_ORDER_DOLLARS:
        return SizingDecision(Decimal("0"), (
            f"name cap: {symbol} cost {cost_by_symbol.get(symbol, Decimal('0'))} "
            f"leaves {name_room:.2f} of {NAME_CAP_PCT * equity:.2f}"
        ))
    notional = min(notional, _quantize_money(name_room))

    sector_room = SECTOR_CAP_PCT * equity - cost_by_sector.get(sector, Decimal("0"))
    if sector_room < MIN_ORDER_DOLLARS:
        return SizingDecision(Decimal("0"), (
            f"sector cap: {sector} cost {cost_by_sector.get(sector, Decimal('0'))} "
            f"leaves {sector_room:.2f} of {SECTOR_CAP_PCT * equity:.2f}"
        ))
    notional = min(notional, _quantize_money(sector_room))

    if adv_20d is None:
        return SizingDecision(Decimal("0"), f"adv unavailable for {symbol}")
    adv_room = _quantize_money(ADV_CAP_PCT * adv_20d)
    if adv_room < MIN_ORDER_DOLLARS:
        return SizingDecision(Decimal("0"), (
            f"adv cap: 5% of 20d ADV {adv_20d:.0f} = {adv_room} below floor"
        ))
    notional = min(notional, adv_room)

    if notional < MIN_ORDER_DOLLARS:
        return SizingDecision(Decimal("0"), f"below floor after fences ({notional})")
    return SizingDecision(notional)
