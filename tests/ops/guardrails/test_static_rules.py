from decimal import Decimal
import pytest
from ops.broker.base import Broker
from ops.broker.types import Order, Position, Side, OrderType
from ops.config import OpsConfig
from ops.guardrails.base import RuleContext
from ops.guardrails.static_rules import (
    DenyListRule, NoMarginRule, NoOptionsRule, NoCryptoRule,
    LongOnlyRule, StopAttachedRule, FractionalSharesOnlyRule,
)


class _FakeBroker(Broker):
    """Minimal Broker test double: only get_positions/get_quote are wired,
    which is all LongOnlyRule needs. Other methods raise if a rule ever
    reaches for them, so an accidental broader dependency fails loudly."""

    def __init__(self, positions: list[Position] | None = None,
                 quotes: dict[str, Decimal] | None = None):
        self._positions = positions or []
        self._quotes = quotes or {}

    def get_cash(self) -> Decimal:
        raise NotImplementedError

    def get_equity(self) -> Decimal:
        raise NotImplementedError

    def get_positions(self) -> list[Position]:
        return self._positions

    def get_quote(self, symbol: str) -> Decimal:
        return self._quotes[symbol]

    def place_order(self, order: Order):
        raise NotImplementedError

    def close_position(self, symbol: str, *, client_order_id: str | None = None):
        raise NotImplementedError


def _ctx(order: Order, cfg: OpsConfig | None = None, broker: Broker | None = None) -> RuleContext:
    return RuleContext(order=order, broker=broker, config=cfg or OpsConfig())  # type: ignore[arg-type]


def _sell(symbol: str = "AAPL", **kwargs) -> Order:
    defaults = dict(
        client_order_id="c", symbol=symbol, side=Side.SELL,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
    )
    defaults.update(kwargs)
    return Order(**defaults)


def _buy(symbol: str = "AAPL", **kwargs) -> Order:
    defaults = dict(
        client_order_id="c", symbol=symbol, side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    defaults.update(kwargs)
    return Order(**defaults)


def test_deny_list_blocks_buy_spot():
    assert DenyListRule().check(_ctx(_buy("SPOT"))).allowed is False

def test_deny_list_blocks_buy_leveraged_etf():
    assert DenyListRule().check(_ctx(_buy("TQQQ"))).allowed is False

def test_deny_list_allows_normal_ticker():
    assert DenyListRule().check(_ctx(_buy("AAPL"))).allowed is True

def test_deny_list_blocks_sell_spot():
    """SPOT is a full contractual blackout: buy AND sell are rejected. A
    manually-acquired SPOT position must be untouchable by design."""
    assert DenyListRule().check(_ctx(_sell("SPOT"))).allowed is False

def test_deny_list_allows_sell_leveraged_etf():
    """Selling a denied leveraged ETF reduces risk, so SELL is allowed even
    though BUY of the same symbol is rejected — otherwise a manually
    acquired TQQQ position could never be stop-sold or kill-switch-closed."""
    assert DenyListRule().check(_ctx(_sell("TQQQ"))).allowed is True

def test_deny_list_is_case_insensitive():
    assert DenyListRule().check(_ctx(_buy("spot"))).allowed is False
    assert DenyListRule().check(_ctx(_sell("spot"))).allowed is False
    assert DenyListRule().check(_ctx(_buy("tqqq"))).allowed is False
    assert DenyListRule().check(_ctx(_sell("tqqq"))).allowed is True

def test_no_margin_blocks_explicit_margin_symbol_format():
    o = _buy("MARGIN:AAPL")
    assert NoMarginRule().check(_ctx(o)).allowed is False

def test_no_margin_allows_regular_symbol():
    assert NoMarginRule().check(_ctx(_buy("AAPL"))).allowed is True

def test_no_options_blocks_occ_symbol():
    o = _buy("AAPL  260117C00200000")
    assert NoOptionsRule().check(_ctx(o)).allowed is False

def test_no_options_allows_equity():
    assert NoOptionsRule().check(_ctx(_buy("AAPL"))).allowed is True

def test_no_options_allows_dotted_class_share_symbol():
    assert NoOptionsRule().check(_ctx(_buy("BRK.B"))).allowed is True

def test_no_options_blocks_crypto_pair_symbol():
    assert NoOptionsRule().check(_ctx(_buy("BTC-USD"))).allowed is False

def test_no_crypto_blocks_known_crypto_symbols():
    for sym in ("BTC", "ETH", "DOGE", "SHIB", "BTC-USD"):
        assert NoCryptoRule().check(_ctx(_buy(sym))).allowed is False, sym

def test_no_crypto_allows_equity():
    assert NoCryptoRule().check(_ctx(_buy("AAPL"))).allowed is True

def test_long_only_allows_buy_without_touching_broker():
    # BUY never consults the broker; broker=None must not raise.
    assert LongOnlyRule().check(_ctx(_buy())).allowed is True

def test_long_only_rejects_oversell():
    broker = _FakeBroker(
        positions=[Position(symbol="AAPL", quantity=Decimal("1"), avg_entry_price=Decimal("100"))],
        quotes={"AAPL": Decimal("100")},
    )
    # notional 150 / quote 100 = 1.5 shares > 1 held.
    order = _sell("AAPL", notional_dollars=Decimal("150"))
    assert LongOnlyRule().check(_ctx(order, broker=broker)).allowed is False

def test_long_only_allows_exact_quantity_sell():
    broker = _FakeBroker(
        positions=[Position(symbol="AAPL", quantity=Decimal("1"), avg_entry_price=Decimal("100"))],
        quotes={"AAPL": Decimal("100")},
    )
    # notional 100 / quote 100 = 1.0 shares == 1 held exactly.
    order = _sell("AAPL", notional_dollars=Decimal("100"))
    assert LongOnlyRule().check(_ctx(order, broker=broker)).allowed is True

def test_long_only_rejects_sell_with_no_position():
    broker = _FakeBroker(positions=[], quotes={"AAPL": Decimal("100")})
    order = _sell("AAPL", notional_dollars=Decimal("50"))
    assert LongOnlyRule().check(_ctx(order, broker=broker)).allowed is False

def test_long_only_matches_held_position_case_insensitively():
    """A lowercase-symbol SELL order must still match an uppercase-held
    position (and vice versa) — a case mismatch between order.symbol and
    the broker's Position.symbol must never cause a false over-sell
    rejection."""
    broker = _FakeBroker(
        positions=[Position(symbol="AAPL", quantity=Decimal("1"), avg_entry_price=Decimal("100"))],
        quotes={"AAPL": Decimal("100")},
    )
    order = _sell("aapl", notional_dollars=Decimal("100"))
    assert LongOnlyRule().check(_ctx(order, broker=broker)).allowed is True

def test_stop_attached_requires_stop_on_buy():
    o = Order(
        client_order_id="c", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_pct=None,
    )
    assert StopAttachedRule().check(_ctx(o)).allowed is False

def test_stop_attached_rejects_positive_stop_pct_on_buy():
    """Order.__post_init__ already rejects stop_pct >= 0, so the only way to
    get a positive stop_pct into the rule is a Rule-level check that doesn't
    trust the dataclass invariant — build via object.__new__ to bypass
    __post_init__ and exercise the rule's own guard directly."""
    o = object.__new__(Order)
    object.__setattr__(o, "client_order_id", "c")
    object.__setattr__(o, "symbol", "AAPL")
    object.__setattr__(o, "side", Side.BUY)
    object.__setattr__(o, "notional_dollars", Decimal("25"))
    object.__setattr__(o, "order_type", OrderType.MARKET)
    object.__setattr__(o, "limit_price", None)
    object.__setattr__(o, "stop_pct", Decimal("0.08"))
    assert StopAttachedRule().check(_ctx(o)).allowed is False

def test_stop_attached_allows_sell_without_stop():
    sell = Order(
        client_order_id="c", symbol="AAPL", side=Side.SELL,
        notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
    )
    assert StopAttachedRule().check(_ctx(sell)).allowed is True

def test_fractional_only_blocks_whole_share_order_marker():
    assert FractionalSharesOnlyRule().check(_ctx(_buy())).allowed is True
