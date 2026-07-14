"""The short-sleeve post-close trade step.

Mechanical, no LLM — the mirror of ops/research/trading.py with inverted
semantics. Exits run before entries; one memo = one position lifecycle (a
memo whose memo_id carries a short_position_closed event never re-enters).
Exit reasons, first match wins:

    1. memo missing            4. hard stop  (quote >= entry * 1.25)
    2. memo resolved           5. target hit (quote <= price_target_low)
    3. falsifier tripped       6. time stop  (held > min(expected, 9) months)

Price-target semantics INVERT for shorts (ShortThesis docstring):
price_target_low is the cover target — profit is the price falling to it.

Sleeve isolation: this module opens/replays only the SHORT journal via
ShortPaperBroker.from_journal. main_journal is touched for exactly two
things — reading falsifier-trip events and writing the single
short_trade_run summary event (the daemon's once-per-day gate and the
user's push). Sizing fences read LIVE exposure (qty x current price), not
cost, because a short grows as it goes wrong.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from ops import events
from ops.broker.base import QuoteUnavailable
from ops.broker.short_paper import ShortPaperBroker
from ops.broker.types import Order, OrderType, Side
from ops.research.short_sizing import size_short_entry
from ops.research.trading import (
    TradeOutcome,
    _default_adv_fetcher,
    _default_sector_lookup,
)

HARD_STOP_PCT = Decimal("0.25")   # cover when quote >= entry * (1 + 25%)
MAX_HOLD_MONTHS = 9               # shorts are never buy-and-hold
_DAYS_PER_MONTH = 30


def _last_short_fill_price(journal, symbol: str) -> Decimal | None:
    """Most recent SHORT fill price for ``symbol`` (equity-fallback mark).
    The journal's last_buy_fill_for only reads BUY fills, which this
    journal never records."""
    price = None
    for f in journal.read_fills():
        if f["symbol"] == symbol and f["side"] == Side.SHORT.value:
            price = f["price"]
    return price


def _equity_with_fallback(broker, journal, outcome: TradeOutcome) -> Decimal:
    """Equity resilient to unquotable positions: an unquotable short is
    marked at its last SHORT fill price (stale, but keeps the sleeve
    accruing instead of wedging — same doctrine as the research sleeve)."""
    total = broker.get_cash()
    for pos in broker.get_positions():
        try:
            price = broker.get_quote(pos.symbol)
        except QuoteUnavailable as exc:
            last = _last_short_fill_price(journal, pos.symbol)
            price = last if last is not None else pos.avg_entry_price
            outcome.errors.append(
                f"{pos.symbol}: equity fallback to last fill price ({exc})"
            )
        total -= pos.quantity * price
    return total


def _held_too_long(entry_date: str | None, memo, today: date) -> bool:
    if not entry_date:
        return False
    held_days = (today - date.fromisoformat(entry_date)).days
    cap_months = min(memo.expected_holding_months, MAX_HOLD_MONTHS)
    return held_days > cap_months * _DAYS_PER_MONTH


def _exit_reason(
    *, symbol: str, memo_id, entry_date, memo_store, main_journal, broker, today,
) -> str | None:
    """First-match-wins exit reason, or None to hold. May raise
    QuoteUnavailable from the price checks — the caller handles it."""
    memo = memo_store.get(memo_id) if memo_id else None
    if memo is None:
        return "memo missing"
    if memo.status == "resolved":
        return "resolved"
    if main_journal.count_events(
        events.KIND_FALSIFIER_TRIPPED, payload_equals={"memo_id": memo_id},
    ) > 0:
        return "falsifier tripped"
    pos = next(p for p in broker.get_positions() if p.symbol == symbol)
    quote = broker.get_quote(symbol)
    if quote >= pos.avg_entry_price * (Decimal("1") + HARD_STOP_PCT):
        return "hard stop"
    if quote <= Decimal(str(memo.price_target_low)):
        return "target hit"
    if _held_too_long(entry_date, memo, today):
        return "time stop"
    return None


def _exit_pass(*, broker, memo_store, short_journal, main_journal, prov, now, outcome):
    """Cover every position whose exit reason fires. Returns the set of
    symbols closed this run, so the entry pass never re-shorts them from
    the same-run-stale open memo."""
    exited: set[str] = set()
    for pos in list(broker.get_positions()):
        symbol = pos.symbol
        pos_prov = prov.get(symbol, {})
        memo_id = pos_prov.get("memo_id")
        try:
            reason = _exit_reason(
                symbol=symbol, memo_id=memo_id,
                entry_date=pos_prov.get("entry_date"), memo_store=memo_store,
                main_journal=main_journal, broker=broker, today=now.date(),
            )
            if reason is None:
                continue
            fill = broker.close_position(symbol)
        except QuoteUnavailable as exc:
            outcome.errors.append(f"{symbol}: quote unavailable on exit check ({exc})")
            continue
        except Exception as exc:  # noqa: BLE001 - one bad position must not wedge the run
            outcome.errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            continue
        short_journal.record_event(
            events.KIND_SHORT_POSITION_CLOSED,
            events.short_position_closed_payload(
                symbol=symbol, memo_id=memo_id or "", reason=reason,
                exit_date=now.date().isoformat(), price=str(fill.price),
            ),
            at=now,
        )
        outcome.exited.append(symbol)
        exited.add(symbol)
    return exited


def _live_exposures(broker, outcome) -> dict[str, Decimal]:
    """{symbol: qty x current price}, falling back per-symbol to entry price
    on QuoteUnavailable (a missing quote must not zero a cap input)."""
    out: dict[str, Decimal] = {}
    for pos in broker.get_positions():
        try:
            price = broker.get_quote(pos.symbol)
        except QuoteUnavailable:
            price = pos.avg_entry_price
            outcome.errors.append(f"{pos.symbol}: exposure marked at entry (no quote)")
        out[pos.symbol] = pos.quantity * price
    return out


def _entry_pass(
    *, broker, memo_store, short_journal, deny_list, asof, now, exited,
    sector_lookup, adv_fetcher, outcome,
):
    held = {p.symbol for p in broker.get_positions()}
    memos = sorted(memo_store.open_memos(), key=lambda m: m.created_at)
    sector_cache: dict[str, str] = {}

    def sector_for(symbol: str) -> str:
        if symbol not in sector_cache:
            sector_cache[symbol] = sector_lookup(symbol)
        return sector_cache[symbol]

    # Exposure maps are built ONCE and updated incrementally after each
    # fill — rebuilding them per candidate memo issued a quote call per
    # held position per memo (review finding P3). New entries are marked
    # at their fill notional, which IS their live exposure at that instant.
    exposure_by_symbol = _live_exposures(broker, outcome)
    exposure_by_sector: dict[str, Decimal] = {}
    for symbol, exposure in exposure_by_symbol.items():
        sector = sector_for(symbol)
        exposure_by_sector[sector] = (
            exposure_by_sector.get(sector, Decimal("0")) + exposure
        )
    gross = sum(exposure_by_symbol.values(), Decimal("0"))

    for memo in memos:
        ticker = memo.ticker
        if ticker in held or ticker in exited:
            continue
        if ticker in deny_list:
            outcome.skipped.append(f"{ticker}: deny-listed")
            continue
        if short_journal.count_events(
            events.KIND_SHORT_POSITION_CLOSED,
            payload_equals={"memo_id": memo.memo_id},
        ) > 0:
            outcome.skipped.append(f"{ticker}: memo already had a position (closed)")
            continue

        try:
            decision = size_short_entry(
                tier=memo.conviction_tier, equity=broker.get_equity(),
                exposure_by_symbol=exposure_by_symbol, symbol=ticker,
                sector=sector_for(ticker), exposure_by_sector=exposure_by_sector,
                gross_short_exposure=gross, adv_20d=adv_fetcher(ticker),
            )
        except QuoteUnavailable as exc:
            outcome.skipped.append(f"{ticker}: quote unavailable ({exc})")
            continue

        if decision.rejected is not None:
            outcome.skipped.append(f"{ticker}: {decision.rejected}")
            continue

        order = Order(
            client_order_id=f"short-{asof.isoformat()}-{ticker}-{uuid4().hex[:8]}",
            symbol=ticker, side=Side.SHORT, notional_dollars=decision.notional,
            order_type=OrderType.MARKET,
        )
        try:
            broker.place_order(order)
        except QuoteUnavailable as exc:
            outcome.skipped.append(f"{ticker}: quote unavailable ({exc})")
            continue

        short_journal.record_event(
            events.KIND_SHORT_POSITION_OPENED,
            events.short_position_opened_payload(
                symbol=ticker, memo_id=memo.memo_id,
                conviction_tier=memo.conviction_tier, entry_date=asof.isoformat(),
                client_order_id=order.client_order_id,
                notional=str(decision.notional),
            ),
            at=now,
        )
        outcome.entered.append(ticker)
        held.add(ticker)
        sector = sector_for(ticker)
        exposure_by_symbol[ticker] = (
            exposure_by_symbol.get(ticker, Decimal("0")) + decision.notional
        )
        exposure_by_sector[sector] = (
            exposure_by_sector.get(sector, Decimal("0")) + decision.notional
        )
        gross += decision.notional


def trade_short_sleeve(
    *,
    memo_store,
    short_journal,
    main_journal,
    quote_source,
    starting_cash: Decimal,
    deny_list: frozenset[str],
    asof: date,
    now: datetime | None = None,
    sector_lookup=None,
    adv_fetcher=None,
) -> TradeOutcome:
    now = now or datetime.now(timezone.utc)
    outcome = TradeOutcome(asof=asof.isoformat())

    broker = ShortPaperBroker.from_journal(
        journal=short_journal, quote_source=quote_source, starting_cash=starting_cash,
    )
    prov = short_journal.latest_event_payload_by_symbol(events.KIND_SHORT_POSITION_OPENED)

    exited = _exit_pass(
        broker=broker, memo_store=memo_store, short_journal=short_journal,
        main_journal=main_journal, prov=prov, now=now, outcome=outcome,
    )

    if sector_lookup is None:
        sector_lookup = _default_sector_lookup(outcome)
    if adv_fetcher is None:
        adv_fetcher = _default_adv_fetcher

    _entry_pass(
        broker=broker, memo_store=memo_store, short_journal=short_journal,
        deny_list=deny_list, asof=asof, now=now, exited=exited,
        sector_lookup=sector_lookup, adv_fetcher=adv_fetcher, outcome=outcome,
    )

    equity = _equity_with_fallback(broker, short_journal, outcome)
    cash = broker.get_cash()
    outcome.equity = equity
    outcome.cash = cash

    short_journal.record_equity_snapshot(
        kind="short_run", equity=equity, cash=cash, at=now,
    )
    main_journal.record_event(
        events.KIND_SHORT_TRADE_RUN,
        events.short_trade_run_payload(
            asof=outcome.asof, entered=outcome.entered, exited=outcome.exited,
            skipped=outcome.skipped, equity=str(equity), cash=str(cash),
        ),
        at=now,
    )
    return outcome
