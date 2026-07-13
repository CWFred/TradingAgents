from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    SHORT = "SHORT"    # sell-to-open; ShortPaperBroker only
    COVER = "COVER"    # buy-to-close; ShortPaperBroker only


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass(frozen=True)
class Order:
    client_order_id: str
    symbol: str
    side: Side
    notional_dollars: Decimal
    order_type: OrderType
    limit_price: Decimal | None = None
    stop_pct: Decimal | None = None
    """Entry-relative stop, e.g. Decimal("-0.08") for an 8% trailing-from-entry
    stop. Resolved to an absolute price at fill time from the ACTUAL fill
    price (see PaperBroker/RobinhoodBroker), never from a pre-trade reference
    price — a stale reference can gap past the fill and put an absolute stop
    on the wrong side of it. Must be strictly negative when set."""

    def __post_init__(self) -> None:
        # Every order (BUY or SELL) requires strictly positive notional —
        # the BUY and SELL branches used to be identical checks; this single
        # check subsumes both (and the separate `< 0` check, since `<= 0`
        # already covers negative values).
        if self.notional_dollars <= 0:
            raise ValueError("notional_dollars must be positive")
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT order requires limit_price")
        if self.stop_pct is not None:
            if self.side in (Side.SHORT, Side.COVER):
                raise ValueError(
                    "stop_pct is not supported on SHORT/COVER orders; the short "
                    "trade step enforces stops from average entry price"
                )
            if self.stop_pct >= 0:
                raise ValueError("stop_pct must be negative (entry-relative, e.g. -0.08)")


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal
    avg_entry_price: Decimal
    stop_loss_price: Decimal | None = None
    shares_available_for_sells: Decimal | None = None
    """Currently-sellable share count (live only). None means "no
    distinction" — paper never sets this, so sellable == quantity there.
    A concrete value (RobinhoodBroker.get_positions) may be less than
    quantity when shares are held/unsettled. Use sellable_quantity rather
    than reading this field directly."""

    def market_value(self, current_price: Decimal) -> Decimal:
        return self.quantity * current_price

    def unrealized_pct(self, current_price: Decimal) -> Decimal:
        return (current_price - self.avg_entry_price) / self.avg_entry_price

    @property
    def sellable_quantity(self) -> Decimal:
        return (
            self.shares_available_for_sells
            if self.shares_available_for_sells is not None
            else self.quantity
        )


@dataclass(frozen=True)
class Fill:
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    filled_at: datetime
