"""Per-position unrealized P&L math (pure; no I/O).

Money is Decimal end to end (never float). The short sleeve journals
positive-magnitude quantities, so its sign is inverted here: a short
position profits when the price falls.
"""
from __future__ import annotations

from decimal import Decimal


def position_pnl(
    entry: Decimal | None,
    quantity: Decimal,
    price: Decimal | None,
    *,
    is_short: bool,
) -> tuple[Decimal | None, Decimal | None]:
    """Return (pnl_dollar, pnl_pct) for one open position.

    - pnl_dollar is None only when price is unavailable.
    - pnl_pct is None when price is unavailable, or entry is None/0 (no
      basis / divide-by-zero guard); pnl_dollar can still be computed
      from entry in the entry==0 case.
    """
    if price is None:
        return None, None
    if entry is None:
        return None, None
    move = (entry - price) if is_short else (price - entry)
    pnl_dollar = move * quantity
    pnl_pct = (move / entry) if entry != 0 else None
    return pnl_dollar, pnl_pct
