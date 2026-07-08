"""The research-sleeve post-close trade step (Phase D, build-order step 5/6).

Mechanical, no LLM: entries are conviction-tier sized under the hard fences
in ``ops/research/sizing.py`` (name/sector/ADV caps — the module docstring
there locks LLM-stated probabilities out as inputs; the only research signal
used here is ``memo.conviction_tier``). Exits are driven purely by Phase C
signals — a resolved/missing memo, a falsifier trip journaled on the MAIN
journal, or the price crossing the memo's stated target — never by
discretion computed here.

Exits run before entries so a name whose memo just resolved (or whose
falsifier just tripped) frees its sizing room in the very same run, and a
symbol closed this run is never immediately re-bought from the same open
memo (that would just be a same-run wash). One memo = one position
lifecycle: a memo whose ``memo_id`` already carries a
``research_position_closed`` event never re-enters, even in a later run —
a falsifier trip or target-hit exit does not resolve the memo, so without
this guard the very next run would re-buy it and immediately re-trip the
same historical falsifier or target, thrashing enter/exit until a human
resolves the memo.

Third-ledger isolation: this module opens/replays only the RESEARCH journal
(``research_journal``, via ``PaperBroker.from_journal``). ``main_journal`` is
touched for exactly two things — reading falsifier-trip events (Phase C
writes those there) and writing the single ``research_trade_run`` summary
event, which doubles as the daemon's once-per-day gate and the basis for the
user's push notification. It is never used to open a broker or read/write
research positions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from ops import events
from ops.broker.base import InsufficientFunds, QuoteUnavailable
from ops.broker.paper import PaperBroker
from ops.broker.types import Order, OrderType, Side
from ops.research.sizing import cost_basis, size_entry


@dataclass
class TradeOutcome:
    asof: str
    entered: list[str] = field(default_factory=list)
    exited: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    equity: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")


def _default_sector_lookup(outcome: TradeOutcome):
    """Built once per run from ``load_smallcap_members()`` (lazy import — no
    network cost when a caller injects its own lookup, e.g. every test).
    Any failure degrades every ticker to "UNKNOWN" plus a single note rather
    than raising and wedging the whole trade step."""
    try:
        from ops.universe.smallcap import load_smallcap_members

        by_symbol = {m.symbol: m.sector for m in load_smallcap_members()}
    except Exception as exc:  # noqa: BLE001 - degrade, never crash the run
        outcome.errors.append(
            f"sector lookup unavailable ({type(exc).__name__}: {exc}); "
            "treating every ticker as UNKNOWN"
        )
        by_symbol = {}
    return lambda ticker: by_symbol.get(ticker, "UNKNOWN")


def _default_adv_fetcher(ticker: str) -> Decimal | None:
    from ops.universe.filters import fetch_price_and_adv_from_yfinance

    return (fetch_price_and_adv_from_yfinance(ticker) or (None, None))[1]


def _equity_with_fallback(broker, journal, outcome: TradeOutcome) -> Decimal:
    """Equity for the snapshot/summary, resilient to unquotable positions.

    Mirrors the spirit of ``ops.research.baseline._equity_with_fallback``:
    a name that can't be quoted right now (feed hiccup, temporary halt) is
    marked at its last journaled BUY fill price instead of raising and
    wedging the sleeve forever.
    """
    total = broker.get_cash()
    for pos in broker.get_positions():
        try:
            price = broker.get_quote(pos.symbol)
        except QuoteUnavailable as exc:
            last_buy = journal.last_buy_fill_for(pos.symbol)
            price = last_buy["price"] if last_buy is not None else pos.avg_entry_price
            outcome.errors.append(
                f"{pos.symbol}: equity fallback to last fill price ({exc})"
            )
        total += pos.market_value(price)
    return total


def _exit_reason(*, symbol: str, memo_id, memo_store, main_journal, broker) -> str | None:
    """First-match-wins exit reason, or None to hold. May raise
    QuoteUnavailable from the target-hit check — the caller handles it."""
    memo = memo_store.get(memo_id) if memo_id else None
    if memo is None:
        return "memo missing"
    if memo.status == "resolved":
        return "resolved"
    if main_journal.count_events(
        events.KIND_FALSIFIER_TRIPPED, payload_equals={"memo_id": memo_id},
    ) > 0:
        return "falsifier tripped"
    quote = broker.get_quote(symbol)
    if quote >= Decimal(str(memo.price_target_high)):
        return "target hit"
    return None


def _exit_pass(*, broker, memo_store, research_journal, main_journal, prov, now, outcome) -> set[str]:
    """Close every position whose exit reason fires. Returns the set of
    symbols closed this run, so the entry pass never re-buys them from the
    same-run-stale open memo."""
    exited: set[str] = set()
    for pos in list(broker.get_positions()):
        symbol = pos.symbol
        memo_id = prov.get(symbol, {}).get("memo_id")
        try:
            reason = _exit_reason(
                symbol=symbol, memo_id=memo_id, memo_store=memo_store,
                main_journal=main_journal, broker=broker,
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
        research_journal.record_event(
            events.KIND_RESEARCH_POSITION_CLOSED,
            events.research_position_closed_payload(
                symbol=symbol, memo_id=memo_id or "", reason=reason,
                exit_date=now.date().isoformat(), price=str(fill.price),
            ),
            at=now,
        )
        outcome.exited.append(symbol)
        exited.add(symbol)
    return exited


def _entry_pass(
    *, broker, memo_store, research_journal, asof, now, exited,
    sector_lookup, adv_fetcher, outcome,
) -> None:
    held = {p.symbol for p in broker.get_positions()}
    memos = sorted(memo_store.open_memos(), key=lambda m: m.created_at)
    sector_cache: dict[str, str] = {}

    def sector_for(symbol: str) -> str:
        if symbol not in sector_cache:
            sector_cache[symbol] = sector_lookup(symbol)
        return sector_cache[symbol]

    for memo in memos:
        ticker = memo.ticker
        if ticker in held or ticker in exited:
            continue

        if research_journal.count_events(
            events.KIND_RESEARCH_POSITION_CLOSED,
            payload_equals={"memo_id": memo.memo_id},
        ) > 0:
            outcome.skipped.append(
                f"{ticker}: memo already had a position (closed)"
            )
            continue

        cost_by_symbol, _total_cost = cost_basis(broker.get_positions())
        cost_by_sector: dict[str, Decimal] = {}
        for symbol, cost in cost_by_symbol.items():
            sector = sector_for(symbol)
            cost_by_sector[sector] = cost_by_sector.get(sector, Decimal("0")) + cost

        try:
            decision = size_entry(
                tier=memo.conviction_tier, equity=broker.get_equity(), cash=broker.get_cash(),
                cost_by_symbol=cost_by_symbol, symbol=ticker, sector=sector_for(ticker),
                cost_by_sector=cost_by_sector, adv_20d=adv_fetcher(ticker),
            )
        except QuoteUnavailable as exc:
            outcome.skipped.append(f"{ticker}: quote unavailable ({exc})")
            continue

        if decision.rejected is not None:
            outcome.skipped.append(f"{ticker}: {decision.rejected}")
            continue

        order = Order(
            client_order_id=f"research-{asof.isoformat()}-{ticker}-{uuid4().hex[:8]}",
            symbol=ticker, side=Side.BUY, notional_dollars=decision.notional,
            order_type=OrderType.MARKET,
        )
        try:
            broker.place_order(order)
        except QuoteUnavailable as exc:
            outcome.skipped.append(f"{ticker}: quote unavailable ({exc})")
            continue
        except InsufficientFunds:
            break

        research_journal.record_event(
            events.KIND_RESEARCH_POSITION_OPENED,
            events.research_position_opened_payload(
                symbol=ticker, memo_id=memo.memo_id, conviction_tier=memo.conviction_tier,
                entry_date=asof.isoformat(), client_order_id=order.client_order_id,
                notional=str(decision.notional),
            ),
            at=now,
        )
        outcome.entered.append(ticker)
        held.add(ticker)


def trade_research_sleeve(
    *,
    memo_store,
    research_journal,
    main_journal,
    quote_source,
    starting_cash: Decimal,
    asof: date,
    now: datetime | None = None,
    sector_lookup=None,
    adv_fetcher=None,
) -> TradeOutcome:
    now = now or datetime.now(timezone.utc)
    outcome = TradeOutcome(asof=asof.isoformat())

    broker = PaperBroker.from_journal(
        journal=research_journal, quote_source=quote_source, starting_cash=starting_cash,
    )
    prov = research_journal.latest_event_payload_by_symbol(events.KIND_RESEARCH_POSITION_OPENED)

    exited = _exit_pass(
        broker=broker, memo_store=memo_store, research_journal=research_journal,
        main_journal=main_journal, prov=prov, now=now, outcome=outcome,
    )

    if sector_lookup is None:
        sector_lookup = _default_sector_lookup(outcome)
    if adv_fetcher is None:
        adv_fetcher = _default_adv_fetcher

    _entry_pass(
        broker=broker, memo_store=memo_store, research_journal=research_journal,
        asof=asof, now=now, exited=exited, sector_lookup=sector_lookup,
        adv_fetcher=adv_fetcher, outcome=outcome,
    )

    equity = _equity_with_fallback(broker, research_journal, outcome)
    cash = broker.get_cash()
    outcome.equity = equity
    outcome.cash = cash

    research_journal.record_equity_snapshot(
        kind="research_run", equity=equity, cash=cash, at=now,
    )
    main_journal.record_event(
        events.KIND_RESEARCH_TRADE_RUN,
        events.research_trade_run_payload(
            asof=outcome.asof, entered=outcome.entered, exited=outcome.exited,
            skipped=outcome.skipped, equity=str(equity), cash=str(cash),
        ),
        at=now,
    )
    return outcome
