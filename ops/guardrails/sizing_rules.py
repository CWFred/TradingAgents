"""Sizing/exposure guardrail rules.

These rules check a candidate order against live broker state (equity, cash,
open positions) plus configured limits in OpsConfig. They never constrain
SELL orders — sizing limits only apply to opening/adding exposure via BUYs.
"""
from __future__ import annotations

from typing import Callable

from ops.broker.types import Side
from ops.guardrails.base import Rule, RuleContext, RuleResult


class PerPositionCapRule(Rule):
    """BUY notional must be <= per_position_cap_pct * current equity."""

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.side != Side.BUY:
            return RuleResult.allow()
        equity = ctx.broker.get_equity()
        cap = equity * ctx.config.per_position_cap_pct
        if ctx.order.notional_dollars > cap:
            return RuleResult.reject(
                f"order ${ctx.order.notional_dollars} exceeds per-position cap ${cap}"
            )
        return RuleResult.allow()


class PerTradeDollarFloorRule(Rule):
    """BUY notional must meet a minimum to avoid noise trades."""

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.side != Side.BUY:
            return RuleResult.allow()
        if ctx.order.notional_dollars < ctx.config.per_trade_dollar_floor:
            return RuleResult.reject(
                f"order ${ctx.order.notional_dollars} below floor "
                f"${ctx.config.per_trade_dollar_floor}"
            )
        return RuleResult.allow()


class MaxOpenPositionsRule(Rule):
    """BUYs that would open a NEW position are blocked when at the cap.
    Adding to an existing position is always allowed by this rule."""

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.side != Side.BUY:
            return RuleResult.allow()
        positions = ctx.broker.get_positions()
        held_symbols = {p.symbol for p in positions}
        if ctx.order.symbol in held_symbols:
            return RuleResult.allow()
        if len(held_symbols) >= ctx.config.max_open_positions:
            return RuleResult.reject(
                f"at max open positions ({ctx.config.max_open_positions}) "
                f"and {ctx.order.symbol} is new"
            )
        return RuleResult.allow()


class CashReserveRule(Rule):
    """After a BUY, cash must remain >= cash_reserve_pct * equity."""

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.side != Side.BUY:
            return RuleResult.allow()
        equity = ctx.broker.get_equity()
        cash = ctx.broker.get_cash()
        floor = equity * ctx.config.cash_reserve_pct
        post_cash = cash - ctx.order.notional_dollars
        if post_cash < floor:
            return RuleResult.reject(
                f"post-trade cash ${post_cash} below reserve floor ${floor}"
            )
        return RuleResult.allow()


class LiveMaxPositionRule(Rule):
    """During the first `live_fill_gate_count` live BUY fills after the
    paper->robinhood flip, cap each BUY notional at `live_max_position`.
    Inert in paper mode and after the gate lifts. Independent of
    PerPositionCapRule (the stricter of the two applies while active)."""

    def __init__(self, live_fill_count: Callable[[], int]):
        self._live_fill_count = live_fill_count

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.side != Side.BUY:
            return RuleResult.allow()
        if ctx.config.broker_mode != "robinhood":
            return RuleResult.allow()
        if self._live_fill_count() >= ctx.config.live_fill_gate_count:
            return RuleResult.allow()
        if ctx.order.notional_dollars > ctx.config.live_max_position:
            return RuleResult.reject(
                f"live-gate: first {ctx.config.live_fill_gate_count} live fills "
                f"capped at ${ctx.config.live_max_position}, "
                f"order ${ctx.order.notional_dollars}"
            )
        return RuleResult.allow()
