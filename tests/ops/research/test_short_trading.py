"""Short-sleeve trade step: inverted exits, live-exposure sizing, isolation."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ops import events
from ops.broker.short_paper import ShortPaperBroker
from ops.research.short_trading import trade_short_sleeve
from tradingagents.memos.schema import (
    EvidenceItem, Falsifier, Memo, Resolution, ShortThesis,
)
from tradingagents.memos.store import MemoStore
from ops.journal import Journal

pytestmark = pytest.mark.unit

D = Decimal
ASOF = date(2026, 7, 13)
NOW = datetime(2026, 7, 13, 20, 30, tzinfo=timezone.utc)


def _memo(ticker="GHST", *, status="open", target_low=25.0, holding_months=6,
          tier="starter"):
    return Memo(
        ticker=ticker, as_of_date=ASOF, thesis_type="short",
        thesis="priced for growth that is not coming",
        evidence=[EvidenceItem(claim="c", source_type="filing", source_ref="0001:mdna")],
        short_block=ShortThesis(
            overvaluation_mechanism="story multiple", red_flags=["4.02"],
            why_now="restatement", squeeze_risk="low", downside_scenario="-40%",
        ),
        conviction_tier=tier, entry_price_ref=40.0,
        price_target_low=target_low, price_target_high=60.0,
        expected_holding_months=holding_months,
        must_be_true=["restated margins lower"],
        falsifiers=[Falsifier(description="margins recover", check_type="fundamental",
                              metric="gross_margin_pct", operator=">", threshold=45.0)],
        status=status,
    )


@pytest.fixture
def stores(tmp_path):
    memo_store = MemoStore(tmp_path / "short_memos.sqlite")
    with Journal(str(tmp_path / "short.sqlite")) as short_journal, \
            Journal(str(tmp_path / "main.sqlite")) as main_journal:
        yield memo_store, short_journal, main_journal


def _trade(stores, prices, *, deny=frozenset(), now=NOW,
           adv=lambda t: D("1000000")):
    memo_store, short_journal, main_journal = stores
    return trade_short_sleeve(
        memo_store=memo_store, short_journal=short_journal,
        main_journal=main_journal, quote_source=lambda s: prices[s],
        starting_cash=D("10000"), deny_list=deny, asof=ASOF, now=now,
        sector_lookup=lambda s: "Industrials", adv_fetcher=adv,
    )


def _seed_position(stores, prices, *, ticker="GHST", memo=None,
                   entry_date="2026-07-01"):
    """Open a short via the broker + provenance event, as a prior run would."""
    memo_store, short_journal, _ = stores
    memo = memo or _memo(ticker=ticker)
    memo_store.save(memo)
    broker = ShortPaperBroker(
        journal=short_journal, quote_source=lambda s: prices[s],
        starting_cash=D("10000"),
    )
    from ops.broker.types import Order, OrderType, Side

    broker.place_order(Order(
        client_order_id=f"seed-{ticker}", symbol=ticker, side=Side.SHORT,
        notional_dollars=D("400"), order_type=OrderType.MARKET,
    ))
    short_journal.record_event(
        events.KIND_SHORT_POSITION_OPENED,
        events.short_position_opened_payload(
            symbol=ticker, memo_id=memo.memo_id, conviction_tier=memo.conviction_tier,
            entry_date=entry_date, client_order_id=f"seed-{ticker}", notional="400",
        ),
    )
    return memo


# --- entries ---------------------------------------------------------------

def test_open_short_memo_enters_short_position(stores):
    memo_store, short_journal, main_journal = stores
    memo_store.save(_memo())
    prices = {"GHST": D("40")}
    outcome = _trade(stores, prices)
    assert outcome.entered == ["GHST"]
    assert outcome.equity == D("10000")
    assert outcome.cash == D("10100")  # starter 1% of 10k shorted -> proceeds in
    assert main_journal.count_events(events.KIND_SHORT_TRADE_RUN) == 1
    assert short_journal.count_events(events.KIND_SHORT_POSITION_OPENED) == 1


def test_deny_listed_ticker_is_skipped(stores):
    memo_store, _, _ = stores
    memo_store.save(_memo(ticker="SPOT"))
    outcome = _trade(stores, {"SPOT": D("40")}, deny=frozenset({"SPOT"}))
    assert outcome.entered == []
    assert any("deny-listed" in s for s in outcome.skipped)


def test_closed_memo_never_reenters(stores):
    memo_store, short_journal, _ = stores
    memo = _memo()
    memo_store.save(memo)
    short_journal.record_event(
        events.KIND_SHORT_POSITION_CLOSED,
        events.short_position_closed_payload(
            symbol="GHST", memo_id=memo.memo_id, reason="hard stop",
            exit_date="2026-07-10", price="50",
        ),
    )
    outcome = _trade(stores, {"GHST": D("40")})
    assert outcome.entered == []
    assert any("already had a position" in s for s in outcome.skipped)


# --- exits (first-match-wins) ----------------------------------------------

def test_memo_missing_exits(tmp_path):
    prices = {"GHST": D("40")}
    memo_store = MemoStore(tmp_path / "memos.sqlite")
    other_store = MemoStore(tmp_path / "other.sqlite")
    with Journal(str(tmp_path / "s.sqlite")) as short_journal, \
            Journal(str(tmp_path / "m.sqlite")) as main_journal:
        stores = (memo_store, short_journal, main_journal)
        _seed_position(stores, prices)
        outcome = trade_short_sleeve(
            memo_store=other_store, short_journal=short_journal,
            main_journal=main_journal, quote_source=lambda s: prices[s],
            starting_cash=D("10000"), deny_list=frozenset(), asof=ASOF, now=NOW,
            sector_lookup=lambda s: "Industrials",
            adv_fetcher=lambda t: D("1000000"),
        )
        assert outcome.exited == ["GHST"]
        closed = short_journal.count_events(events.KIND_SHORT_POSITION_CLOSED)
        assert closed == 1


def test_resolved_memo_exits(stores):
    memo_store, short_journal, _ = stores
    prices = {"GHST": D("40")}
    memo = _seed_position(stores, prices)
    memo_store.resolve(memo.memo_id, Resolution(
        resolved_at=NOW, exit_price=30.0, realized_return_pct=0.25,
        benchmark_return_pct=0.0, holding_days=12,
        outcome_label="thesis_right_made_money", narrative="worked",
    ))
    outcome = _trade(stores, prices)
    assert outcome.exited == ["GHST"]


def test_falsifier_trip_exits(stores):
    memo_store, _, main_journal = stores
    prices = {"GHST": D("40")}
    memo = _seed_position(stores, prices)
    main_journal.record_event(
        events.KIND_FALSIFIER_TRIPPED, {"memo_id": memo.memo_id, "index": 0},
    )
    outcome = _trade(stores, prices)
    assert outcome.exited == ["GHST"]


def test_hard_stop_covers_at_25_percent_adverse(stores):
    prices = {"GHST": D("40")}
    _seed_position(stores, prices)
    prices["GHST"] = D("50")   # +25% against the short
    outcome = _trade(stores, prices)
    assert outcome.exited == ["GHST"]


def test_target_hit_covers(stores):
    prices = {"GHST": D("40")}
    _seed_position(stores, prices)   # target_low = 25
    prices["GHST"] = D("24")
    outcome = _trade(stores, prices)
    assert outcome.exited == ["GHST"]
    # profit: shorted 400 at 40 (10 sh), covered at 24 -> +160
    assert outcome.equity == D("10160")


def test_time_stop_covers_after_capped_months(stores):
    prices = {"GHST": D("40")}
    _seed_position(stores, prices, entry_date="2025-12-01")  # >6mo * 30d ago
    outcome = _trade(stores, prices)
    assert outcome.exited == ["GHST"]


def test_holding_short_of_every_exit_holds(stores):
    prices = {"GHST": D("40")}
    _seed_position(stores, prices, entry_date="2026-07-01")
    outcome = _trade(stores, prices)
    assert outcome.exited == [] and outcome.entered == []


def test_exited_symbol_not_reentered_same_run(stores):
    memo_store, short_journal, _ = stores
    prices = {"GHST": D("40")}
    _seed_position(stores, prices)
    prices["GHST"] = D("50")   # hard stop fires; memo still open
    outcome = _trade(stores, prices)
    assert outcome.exited == ["GHST"]
    assert outcome.entered == []


def test_gross_cap_fences_across_same_run_entries(tmp_path):
    # Gross cap = 50% of $1000 equity = $500. Two high-tier memos want
    # 3% = $30 each... too small; use starting cash $10000 with the cap
    # nearly consumed instead: seed a $4,850 short, leaving $150 of gross
    # room. Two high-tier memos want $300 each: the first must be clamped
    # to $150 by the LIVE (incrementally updated) exposure map and the
    # second rejected outright — a stale snapshot would fill both at $150.
    memo_store = MemoStore(tmp_path / "short_memos.sqlite")
    memo_store.save(_memo(ticker="AAA", tier="high"))
    memo_store.save(_memo(ticker="BBB", tier="high"))
    prices = {"AAA": D("40"), "BBB": D("40"), "GHST": D("40")}
    with Journal(str(tmp_path / "s.sqlite")) as short_journal, \
            Journal(str(tmp_path / "m.sqlite")) as main_journal:
        broker = ShortPaperBroker(
            journal=short_journal, quote_source=lambda s: prices[s],
            starting_cash=D("10000"),
        )
        from ops.broker.types import Order, OrderType, Side

        broker.place_order(Order(
            client_order_id="seed-GHST", symbol="GHST", side=Side.SHORT,
            notional_dollars=D("4850"), order_type=OrderType.MARKET,
        ))
        ghst_memo = _memo(ticker="GHST")
        memo_store.save(ghst_memo)
        short_journal.record_event(
            events.KIND_SHORT_POSITION_OPENED,
            events.short_position_opened_payload(
                symbol="GHST", memo_id=ghst_memo.memo_id, conviction_tier="high",
                entry_date="2026-07-10", client_order_id="seed-GHST",
                notional="4850",
            ),
        )
        outcome = trade_short_sleeve(
            memo_store=memo_store, short_journal=short_journal,
            main_journal=main_journal, quote_source=lambda s: prices[s],
            starting_cash=D("10000"), deny_list=frozenset(), asof=ASOF, now=NOW,
            sector_lookup=lambda s: {"AAA": "Tech", "BBB": "Energy",
                                     "GHST": "Industrials"}[s],
            adv_fetcher=lambda t: D("1000000"),
        )
        assert outcome.entered == ["AAA"]          # clamped to the $150 room
        assert any("gross exposure cap" in s for s in outcome.skipped)  # BBB
        replayed = ShortPaperBroker.from_journal(
            journal=short_journal, quote_source=lambda s: prices[s],
            starting_cash=D("10000"),
        )
        exposures = {p.symbol: p.quantity * prices[p.symbol]
                     for p in replayed.get_positions()}
        assert exposures["AAA"] == D("150")
        assert "BBB" not in exposures
