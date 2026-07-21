"""Per-position unrealized P&L math (pure; no I/O).

Money is Decimal end to end (never float). The short sleeve journals
positive-magnitude quantities, so its sign is inverted here: a short
position profits when the price falls.
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from ops.broker.base import QuoteUnavailable


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


def build_sleeve_pnl(
    path: str,
    *,
    is_short: bool,
    quote_source: Callable[[str], Decimal],
    broker_cls=None,
) -> dict[str, Any]:
    """Per-position P&L for one sleeve ledger. Replays positions
    (journal-only), then marks each with a live quote. A quote failure
    degrades that row only (null P&L + error); every other row resolves."""
    from ops.dashboard.snapshot import replay_positions

    rows: list[dict[str, Any]] = []
    for pos in replay_positions(path, broker_cls=broker_cls):
        symbol = pos["symbol"]
        row: dict[str, Any] = {"symbol": symbol}
        try:
            price = quote_source(symbol)
        except QuoteUnavailable as exc:
            row.update(price=None, pnl_dollar=None, pnl_pct=None,
                       error=str(exc))
            rows.append(row)
            continue
        d, p = position_pnl(pos["entry"], pos["quantity"], price,
                            is_short=is_short)
        row.update(
            price=str(price),
            pnl_dollar=None if d is None else str(d),
            pnl_pct=None if p is None else str(p),
        )
        rows.append(row)
    return {"positions": rows}
