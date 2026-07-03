"""Static guardrail rules.

Most of these rules depend only on the order + config — never on broker
state (cash, positions, market data) — and are cheap, deterministic, and
safe to run first in the guardrail pipeline. LongOnlyRule is the one
exception: it reads ctx.broker.get_positions()/get_quote() to enforce a
real long-only invariant, not just a naming convention.
"""
from __future__ import annotations

import re
from decimal import Decimal

from ops.broker.types import Side
from ops.guardrails.base import Rule, RuleContext, RuleResult

_CRYPTO_SYMBOLS = frozenset({
    "BTC", "ETH", "DOGE", "SHIB", "LTC", "BCH", "ETC", "BSV",
    "BTC-USD", "ETH-USD", "DOGE-USD", "SHIB-USD",
})

# Plain equity ticker, optionally with a single share-class suffix
# (BRK.B, BF-B). Rejects OCC option symbols (spaces + strike/expiry
# digits) and crypto pairs (BTC-USD fails: two letters before the dash).
_EQUITY_SYMBOL_RE = re.compile(r"[A-Z]{1,5}([.-][A-Z])?")

# A sell-side over-sell check needs a little headroom for Decimal division
# rounding between notional_dollars/quote and the broker's own qty math
# (PaperBroker uses the same order of magnitude epsilon internally).
_SELL_QTY_EPSILON = Decimal("0.0000001")


class DenyListRule(Rule):
    """BUY of any denied symbol is always rejected. SELL is allowed for the
    leveraged-ETF portion of the deny list (selling reduces risk — a
    manually-acquired TQQQ position must still be stop-sellable/kill-switch
    closable) but SPOT (config.full_blackout_symbols) is a full contractual
    blackout: SELL of SPOT is rejected exactly like BUY. This is a second,
    independent gate on top of RobinhoodBroker._enforce_spot_hard_check —
    do not weaken it to make the leveraged-ETF sell-allow apply to SPOT."""

    def check(self, ctx: RuleContext) -> RuleResult:
        symbol = ctx.order.symbol.upper()
        if symbol not in ctx.config.deny_list:
            return RuleResult.allow()
        if symbol in ctx.config.full_blackout_symbols:
            return RuleResult.reject(f"{symbol} is a full blackout symbol (buy and sell denied)")
        if ctx.order.side == Side.BUY:
            return RuleResult.reject(f"{symbol} is on the deny list")
        return RuleResult.allow()


class NoMarginRule(Rule):
    """v1 only allows cash trades. Rejects any symbol prefixed MARGIN:."""

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.symbol.startswith("MARGIN:"):
            return RuleResult.reject("margin orders are not allowed in v1")
        return RuleResult.allow()


class NoOptionsRule(Rule):
    """v1 is equity-only. Whitelists plain equity ticker shapes instead of
    blacklisting OCC-style option symbols by length/spacing — a whitelist
    also rejects anything else non-equity, including crypto pairs like
    BTC-USD (see NoCryptoRule, now largely redundant but left in place)."""

    def check(self, ctx: RuleContext) -> RuleResult:
        s = ctx.order.symbol
        if not _EQUITY_SYMBOL_RE.fullmatch(s):
            return RuleResult.reject(f"{s} is not a recognized equity symbol; options/crypto/etc are not allowed in v1")
        return RuleResult.allow()


class NoCryptoRule(Rule):
    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.symbol in _CRYPTO_SYMBOLS:
            return RuleResult.reject(f"{ctx.order.symbol} is crypto; not allowed in v1")
        return RuleResult.allow()


class LongOnlyRule(Rule):
    """v1 does not support short selling. BUYs always pass (nothing to
    short). On a SELL, converts notional_dollars to a share quantity via
    the current quote and rejects if that exceeds the held quantity (plus
    a small epsilon for Decimal division rounding) — i.e. you cannot sell
    more than you own. PaperBroker already enforces this internally; this
    rule makes the invariant hold at the guarded boundary for every broker,
    including RobinhoodBroker where nothing else would catch it."""

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.side != Side.SELL:
            return RuleResult.allow()
        # Normalize both sides of the comparison — a case mismatch between
        # order.symbol and the broker's Position.symbol must never cause a
        # false over-sell rejection.
        symbol = ctx.order.symbol.upper()
        quote = ctx.broker.get_quote(symbol)
        sell_qty = ctx.order.notional_dollars / quote
        held = next(
            (p.quantity for p in ctx.broker.get_positions() if p.symbol.upper() == symbol),
            Decimal("0"),
        )
        if sell_qty > held + _SELL_QTY_EPSILON:
            return RuleResult.reject(
                f"SELL of {sell_qty} {symbol} exceeds held quantity {held}"
            )
        return RuleResult.allow()


class StopAttachedRule(Rule):
    """Every BUY must carry a negative, entry-relative stop_pct. SELLs do
    not require one. The absolute stop price is resolved from the actual
    fill price at fill time (see PaperBroker/RobinhoodBroker) — never from
    a pre-trade reference — so this rule only validates the pct shape."""

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.side != Side.BUY:
            return RuleResult.allow()
        stop_pct = ctx.order.stop_pct
        if stop_pct is None or stop_pct >= 0:
            return RuleResult.reject("BUY orders require a negative stop_pct")
        return RuleResult.allow()


class FractionalSharesOnlyRule(Rule):
    """v1 BUYs use dollar-notional routing (fractional shares). This rule is
    a future-regression guard: it confirms BUY orders specify positive
    notional_dollars (no whole-share-quantity field on the Order)."""

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.side == Side.BUY and ctx.order.notional_dollars <= 0:
            return RuleResult.reject("BUY orders must use dollar-notional routing")
        return RuleResult.allow()
