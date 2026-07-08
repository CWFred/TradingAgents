"""Unit tests for the research-sleeve trade step (no network; injected quotes)."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ops import events
from ops.journal import Journal
from ops.research.trading import TradeOutcome, trade_research_sleeve
from tradingagents.memos.schema import EvidenceItem, Falsifier, Memo, ValueThesis
from tradingagents.memos.store import MemoStore

pytestmark = pytest.mark.unit

TODAY = date(2026, 7, 7)
NOW = datetime(2026, 7, 7, 20, 30, tzinfo=timezone.utc)


def _memo(ticker="WIDG", *, tier="medium", targets=(15.0, 20.0)):
    return Memo(
        ticker=ticker, as_of_date=date(2026, 7, 1), thesis_type="value",
        thesis="Mispriced.", conviction_tier=tier,
        evidence=[EvidenceItem(claim="c", source_type="filing", source_ref="a:mdna")],
        value_block=ValueThesis(
            why_cheap="x", change_trigger="y",
            normalized_earnings_view="z", quality_assessment="q",
        ),
        entry_price_ref=10.0, price_target_low=targets[0], price_target_high=targets[1],
        expected_holding_months=12, must_be_true=["m"],
        falsifiers=[Falsifier(description="d", check_type="price",
                              metric="drawdown_from_cost_pct", operator="<",
                              threshold=-30.0)],
    )


@pytest.fixture
def env(tmp_path):
    memo_store = MemoStore(tmp_path / "memos.sqlite")
    research_journal = Journal(str(tmp_path / "research.sqlite"))
    main_journal = Journal(str(tmp_path / "main.sqlite"))
    return memo_store, research_journal, main_journal


QUOTES = {"WIDG": Decimal("10"), "SPIN": Decimal("5")}


def _trade(env, *, quotes=None, adv=Decimal("5000000"), sectors=None, asof=TODAY):
    memo_store, research_journal, main_journal = env
    q = quotes or dict(QUOTES)

    def quote_source(symbol):
        from ops.broker.base import QuoteUnavailable
        if symbol not in q:
            raise QuoteUnavailable(symbol)
        return q[symbol]

    return trade_research_sleeve(
        memo_store=memo_store, research_journal=research_journal,
        main_journal=main_journal, quote_source=quote_source,
        starting_cash=Decimal("100000"), asof=asof, now=NOW,
        sector_lookup=lambda t: (sectors or {}).get(t, "Industrials"),
        adv_fetcher=lambda t: adv,
    )


def test_enters_open_memos_tier_sized(env):
    memo_store, research_journal, main_journal = env
    memo_store.save(_memo("WIDG", tier="medium"))
    outcome = _trade(env)
    assert outcome.entered == ["WIDG"]
    prov = research_journal.latest_event_payload_by_symbol(
        events.KIND_RESEARCH_POSITION_OPENED)
    assert prov["WIDG"]["memo_id"] == memo_store.list(ticker="WIDG")[0].memo_id
    assert Decimal(prov["WIDG"]["notional"]) == Decimal("4000.00")  # 4% of 100k
    # Summary event in the MAIN journal (the dispatcher's journal).
    kinds = [e["kind"] for e in main_journal.read_events()]
    assert kinds == [events.KIND_RESEARCH_TRADE_RUN]
    # Equity snapshot in the RESEARCH journal.
    snaps = [s for s in research_journal.read_equity_snapshots()
             if s["kind"] == "research_run"]
    assert len(snaps) == 1


def test_no_double_entry_and_passed_memos_ignored(env):
    memo_store, _, _ = env
    memo_store.save(_memo("WIDG"))
    passed = _memo("SPIN")
    memo_store.save(passed)
    memo_store.mark_passed(passed.memo_id)
    _trade(env)
    outcome2 = _trade(env)  # second run: WIDG already held, SPIN passed
    assert outcome2.entered == []
    assert outcome2.exited == []


def test_exit_on_falsifier_trip(env):
    memo_store, research_journal, main_journal = env
    memo_store.save(_memo("WIDG"))
    _trade(env)
    memo_id = memo_store.list(ticker="WIDG")[0].memo_id
    main_journal.record_event(
        events.KIND_FALSIFIER_TRIPPED,
        events.falsifier_tripped_payload(
            memo_id=memo_id, ticker="WIDG", falsifier_index="0",
            description="d", metric="drawdown_from_cost_pct",
            observed="-31.0", threshold="-30.0", consecutive_periods=1,
        ),
    )
    outcome = _trade(env)
    assert outcome.exited == ["WIDG"]
    closed = [e for e in research_journal.read_events()
              if e["kind"] == events.KIND_RESEARCH_POSITION_CLOSED]
    assert closed[0]["payload"]["reason"] == "falsifier tripped"
    # And it does not re-enter in the same run.
    assert outcome.entered == []


def test_no_reentry_after_falsifier_exit_on_later_run(env):
    memo_store, research_journal, main_journal = env
    memo_store.save(_memo("WIDG"))
    _trade(env)  # day0: enter
    memo_id = memo_store.list(ticker="WIDG")[0].memo_id
    main_journal.record_event(
        events.KIND_FALSIFIER_TRIPPED,
        events.falsifier_tripped_payload(
            memo_id=memo_id, ticker="WIDG", falsifier_index="0",
            description="d", metric="drawdown_from_cost_pct",
            observed="-31.0", threshold="-30.0", consecutive_periods=1,
        ),
    )
    outcome = _trade(env)  # day1: exit on the falsifier trip
    assert outcome.exited == ["WIDG"]
    # day2 (a later run): the memo is still "open" (falsifier trip doesn't
    # resolve it), but the position lifecycle for this memo is done — it
    # must not re-enter.
    outcome2 = _trade(env)
    assert outcome2.entered == []
    assert any("WIDG" in s and "already had a position" in s for s in outcome2.skipped)
    # day3: still no re-entry.
    outcome3 = _trade(env)
    assert outcome3.entered == []


def test_exit_on_target_hit_and_resolution(env):
    memo_store, research_journal, _ = env
    memo_store.save(_memo("WIDG", targets=(15.0, 20.0)))
    _trade(env)
    outcome = _trade(env, quotes={"WIDG": Decimal("21")})  # >= 20 target
    assert outcome.exited == ["WIDG"]
    closed = [e for e in research_journal.read_events()
              if e["kind"] == events.KIND_RESEARCH_POSITION_CLOSED]
    assert closed[-1]["payload"]["reason"] == "target hit"


def test_fence_rejection_skips_with_reason(env):
    memo_store, _, _ = env
    memo_store.save(_memo("WIDG"))
    outcome = _trade(env, adv=Decimal("1000"))  # 5% of ADV = $50 < floor
    assert outcome.entered == []
    assert any("WIDG" in s and "adv" in s for s in outcome.skipped)


def test_quote_failure_skips_and_continues(env):
    memo_store, _, _ = env
    memo_store.save(_memo("DEAD"))
    memo_store.save(_memo("WIDG"))
    outcome = _trade(env)  # DEAD not in QUOTES -> QuoteUnavailable
    assert outcome.entered == ["WIDG"]
    assert any("DEAD" in e for e in outcome.errors + outcome.skipped)


def test_empty_book_and_no_memos_is_clean_noop(env):
    _, research_journal, main_journal = env
    outcome = _trade(env)
    assert isinstance(outcome, TradeOutcome)
    assert outcome.entered == [] and outcome.exited == []
    assert [e["kind"] for e in main_journal.read_events()] == [
        events.KIND_RESEARCH_TRADE_RUN]
