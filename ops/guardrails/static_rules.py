"""Static guardrail rules.

These rules depend only on the order + config — never on broker state (cash,
positions, market data). They are cheap, deterministic, and safe to run
first in the guardrail pipeline.
"""
from __future__ import annotations

from ops.broker.types import Side
from ops.guardrails.base import Rule, RuleContext, RuleResult

_CRYPTO_SYMBOLS = frozenset({
    "BTC", "ETH", "DOGE", "SHIB", "LTC", "BCH", "ETC", "BSV",
    "BTC-USD", "ETH-USD", "DOGE-USD", "SHIB-USD",
})


class DenyListRule(Rule):
    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.symbol in ctx.config.deny_list:
            return RuleResult.reject(f"{ctx.order.symbol} is on the deny list")
        return RuleResult.allow()


class NoMarginRule(Rule):
    """v1 only allows cash trades. Rejects any symbol prefixed MARGIN:."""

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.symbol.startswith("MARGIN:"):
            return RuleResult.reject("margin orders are not allowed in v1")
        return RuleResult.allow()


class NoOptionsRule(Rule):
    """Rejects OCC-style option symbols. v1 is equity-only."""

    def check(self, ctx: RuleContext) -> RuleResult:
        s = ctx.order.symbol
        if " " in s and len(s) >= 16:
            return RuleResult.reject("options orders are not allowed in v1")
        return RuleResult.allow()


class NoCryptoRule(Rule):
    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.symbol in _CRYPTO_SYMBOLS:
            return RuleResult.reject(f"{ctx.order.symbol} is crypto; not allowed in v1")
        return RuleResult.allow()


class LongOnlyRule(Rule):
    """Rejects any order whose client_order_id is prefixed SHORT-, which is
    the convention strategies use to mark short attempts. v1 does not support
    short selling."""

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.client_order_id.startswith("SHORT-"):
            return RuleResult.reject("short selling is not allowed in v1")
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
