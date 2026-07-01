# ops/broker/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from ops.broker.types import Fill, Order, Position


class BrokerError(Exception):
    pass


class OrderRejected(BrokerError):
    """Raised when a guardrail rule rejects an order before it reaches the broker."""

    def __init__(self, rule_name: str, reason: str):
        super().__init__(f"{rule_name}: {reason}")
        self.rule_name = rule_name
        self.reason = reason


class InsufficientFunds(BrokerError):
    pass


class NoSuchPosition(BrokerError):
    pass


class QuoteUnavailable(BrokerError):
    """Raised when a quote source cannot return a price for a symbol
    (e.g. yfinance is flaky, the ticker is delisted, network hiccup).
    Because it inherits BrokerError, GuardedBroker's place_order catches it
    and journals as order_rejected."""
    pass


class Broker(ABC):
    @abstractmethod
    def get_cash(self) -> Decimal: ...

    @abstractmethod
    def get_equity(self) -> Decimal: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_quote(self, symbol: str) -> Decimal: ...

    @abstractmethod
    def place_order(self, order: Order) -> Fill: ...

    @abstractmethod
    def close_position(self, symbol: str, *, client_order_id: str | None = None) -> Fill: ...
