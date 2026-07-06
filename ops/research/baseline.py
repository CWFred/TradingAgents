"""The null-baseline portfolio: equal-weight everything that passes the screen.

This is the control the whole research system is measured against (design
doc, "the mandatory null baseline"): if the LLM deep-research stages cannot
beat this dumb portfolio by more than the token bill, they are not adding
value. It must therefore stay dumb on purpose — no guardrails, no stops, no
conviction sizing, no discretion. Separate journal DB from the trading
journal so the control can never contaminate real state.

Policy:
- exits first: close any position held >= BASELINE_MAX_HOLD_DAYS (matched to
  the value sleeve's 12-month floor horizon);
- then buy every passer not currently held at BASELINE_SLICE_PCT of current
  equity (~equal weight at the target ~25 names), clamped to available cash;
- re-running on the same day is idempotent because held names are skipped.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from ops import events
from ops.broker.base import Broker, InsufficientFunds, QuoteUnavailable
from ops.broker.types import Order, OrderType, Side
from ops.journal import Journal

BASELINE_SLICE_PCT = Decimal("0.04")
BASELINE_MAX_HOLD_DAYS = 365
_MIN_ORDER_DOLLARS = Decimal("100")


def _equity_with_fallback(broker: Broker, journal: Journal) -> Decimal:
    """Equity for sizing/reporting, resilient to unquotable positions.

    A delisted name (tender, acquisition) has no live quote; PaperBroker's
    get_equity would raise and wedge the control forever. Mark such a
    position at its last journaled BUY fill price instead — stale, but the
    baseline keeps accruing, which is the whole point of a control.
    """
    total = broker.get_cash()
    for pos in broker.get_positions():
        try:
            price = broker.get_quote(pos.symbol)
        except QuoteUnavailable:
            last_buy = journal.last_buy_fill_for(pos.symbol)
            price = last_buy["price"] if last_buy is not None else pos.avg_entry_price
        total += pos.market_value(price)
    return total


def update_baseline_portfolio(
    *,
    broker: Broker,
    journal: Journal,
    passers: list[str],
    asof: date,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    exits: list[str] = []
    for pos in list(broker.get_positions()):
        last_buy = journal.last_buy_fill_for(pos.symbol)
        if last_buy is None:
            # No journaled BUY fill for a held position has no hold clock.
            # Replay can't produce this from normal operation — if it shows
            # up, don't guess at a hold age; leave it for manual resolution
            # (the monitoring loop, build-order step 6).
            continue
        held_days = (now - last_buy["filled_at"]).days
        if held_days < BASELINE_MAX_HOLD_DAYS:
            continue
        try:
            broker.close_position(pos.symbol)
        except QuoteUnavailable as exc:
            print(f"[baseline] exit skipped {pos.symbol}: {exc}", file=sys.stderr)
            continue
        journal.record_event(
            events.KIND_BASELINE_EXIT,
            events.baseline_exit_payload(symbol=pos.symbol, held_days=held_days),
        )
        exits.append(pos.symbol)

    held = {p.symbol for p in broker.get_positions()}
    slice_dollars = _equity_with_fallback(broker, journal) * BASELINE_SLICE_PCT
    buys: list[str] = []
    skipped: list[str] = []
    for symbol in sorted(dict.fromkeys(passers)):
        if symbol in held:
            continue
        notional = min(slice_dollars, broker.get_cash())
        if notional < _MIN_ORDER_DOLLARS:
            break
        # uuid4-suffixed: client_order_id must be unique per ORDER, not per
        # (day, symbol) — PaperBroker journals the order row BEFORE quoting,
        # so a QuoteUnavailable skip leaves an orphan order row and a
        # same-day retry would collide with the journal's UNIQUE index
        # (same rationale as ops/strategy/post_earnings_momentum.py).
        order = Order(
            client_order_id=f"baseline-{asof.isoformat()}-{symbol}-{uuid4().hex[:8]}",
            symbol=symbol,
            side=Side.BUY,
            notional_dollars=notional,
            order_type=OrderType.MARKET,
        )
        try:
            broker.place_order(order)
        except QuoteUnavailable as exc:
            print(f"[baseline] buy skipped {symbol}: {exc}", file=sys.stderr)
            skipped.append(symbol)
            continue
        except InsufficientFunds:
            break
        buys.append(symbol)

    equity = _equity_with_fallback(broker, journal)
    journal.record_event(
        events.KIND_BASELINE_SCREEN_RUN,
        events.baseline_screen_run_payload(
            asof=asof.isoformat(), passers=len(passers),
            buys=buys, exits=exits, skipped=skipped, equity=equity,
        ),
    )
    journal.record_equity_snapshot(
        kind="baseline_run", equity=equity, cash=broker.get_cash(), at=now,
    )
    return {"buys": buys, "exits": exits, "skipped": skipped}
