"""Unit tests for the memo monitoring loop (no network; real stores on tmp)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ops import events
from ops.journal import Journal
from ops.research.monitor import (
    DRAWDOWN_ESCALATION_PCT,
    MonitorOutcome,
    monitor_memos,
)
from ops.research.prices import PriceContext
from ops.research.store import ScreenStore
from tradingagents.memos.schema import (
    Catalyst,
    EventThesis,
    EvidenceItem,
    Falsifier,
    Memo,
    ValueThesis,
)
from tradingagents.memos.store import MemoStore

pytestmark = pytest.mark.unit

TODAY = date(2026, 7, 7)
NOW = datetime(2026, 7, 7, 20, 30, tzinfo=timezone.utc)


def _memo(ticker="WIDG", *, thesis_type="value", entry=10.0, falsifiers=None,
          catalysts=None, key_dates=None, months=12, created_at=None):
    kwargs = {
        "ticker": ticker, "as_of_date": date(2026, 1, 5), "thesis_type": thesis_type,
        "thesis": "Mispriced on distributor loss.",
        "evidence": [EvidenceItem(claim="c", source_type="filing", source_ref="a:mdna")],
        "conviction_tier": "starter", "entry_price_ref": entry,
        "price_target_low": 15.0, "price_target_high": 20.0,
        "expected_holding_months": months,
        "must_be_true": ["volume replaced"],
        "falsifiers": falsifiers or [Falsifier(
            description="drawdown breach", check_type="price",
            metric="drawdown_from_cost_pct", operator=">", threshold=25.0,
        )],
        "catalysts": catalysts or [],
    }
    if thesis_type == "value":
        kwargs["value_block"] = ValueThesis(
            why_cheap="lost distributor", change_trigger="selloff",
            normalized_earnings_view="$1.20", quality_assessment="net cash",
        )
    else:
        kwargs["event_block"] = EventThesis(
            event_type="spinoff", seller_identity="index funds",
            why_non_economic="index deletion", key_dates=key_dates or [],
        )
    if created_at is not None:
        kwargs["created_at"] = created_at
    return Memo(**kwargs)


@pytest.fixture
def stores(tmp_path):
    return (
        MemoStore(tmp_path / "memos.sqlite"),
        ScreenStore(tmp_path / "screen.sqlite"),
        Journal(str(tmp_path / "journal.sqlite")),
    )


def _prices(close):
    return lambda symbol: PriceContext(closes={TODAY: Decimal(str(close))})


def _run(stores, *, close=9.5, facts_fetcher=None, today=TODAY, now=NOW):
    memo_store, screen_store, journal = stores
    return monitor_memos(
        memo_store=memo_store, screen_store=screen_store, journal=journal,
        price_fetcher=_prices(close),
        facts_fetcher=facts_fetcher or (lambda t: (_ for _ in ()).throw(AssertionError("no facts needed"))),
        today=today, now=now,
    )


def _events_of(journal, kind):
    return [e for e in journal.read_events() if e["kind"] == kind]


def test_quiet_memo_produces_only_run_summary(stores):
    memo_store, _, journal = stores
    memo_store.save(_memo())  # entry 10, close 9.5 -> -5%, nothing trips
    outcome = _run(stores)
    assert outcome.memos_checked == 1
    assert outcome.tripped == 0 and outcome.escalations == 0
    kinds = [e["kind"] for e in journal.read_events()]
    assert kinds == [events.KIND_RESEARCH_MONITOR_RUN]


def test_falsifier_trip_notifies_and_escalates(stores):
    memo_store, screen_store, journal = stores
    memo_store.save(_memo())
    outcome = _run(stores, close=7.0)  # down 30% > 25 threshold
    assert outcome.tripped == 1
    assert outcome.escalations == 1
    tripped = _events_of(journal, events.KIND_FALSIFIER_TRIPPED)
    assert len(tripped) == 1
    assert tripped[0]["payload"]["falsifier_index"] == "0"
    assert [h["symbol"] for h in screen_store.pending_hits()] == ["WIDG"]
    payload = screen_store.pending_hits()[0]["payload"]
    # _screen_summary-compatible: bracket-accessed keys all present.
    for key in ("symbol", "asof", "passed", "cheap", "quality", "market_cap", "ev_ebit"):
        assert key in payload
    assert payload["triggers"][0]["kind"] == "monitor_escalation"
    assert len(_events_of(journal, events.KIND_RESEARCH_ESCALATION)) == 1


def test_renotify_dedupe_within_window(stores):
    memo_store, screen_store, journal = stores
    memo_store.save(_memo())
    _run(stores, close=7.0)
    outcome2 = _run(stores, close=7.0)  # same day re-run: still tripped...
    assert outcome2.tripped == 1
    # ...but no second notification and no second escalation (hit still pending).
    assert len(_events_of(journal, events.KIND_FALSIFIER_TRIPPED)) == 1
    assert len(_events_of(journal, events.KIND_RESEARCH_ESCALATION)) == 1
    assert len(screen_store.pending_hits()) == 1


def test_drawdown_escalates_without_any_falsifier_trip(stores):
    memo_store, screen_store, journal = stores
    memo_store.save(_memo(falsifiers=[Falsifier(
        description="margin", check_type="fundamental",
        metric="gross_margin_pct", operator="<", threshold=30.0,
    )]))
    # No facts fetchable -> fundamental falsifier unevaluable; close 6.5 = down 35%.
    outcome = _run(
        stores, close=6.5,
        facts_fetcher=lambda t: (_ for _ in ()).throw(RuntimeError("EDGAR down")),
    )
    assert outcome.tripped == 0
    assert outcome.unevaluable == 1
    assert outcome.escalations == 1
    assert DRAWDOWN_ESCALATION_PCT == 30.0
    esc = _events_of(journal, events.KIND_RESEARCH_ESCALATION)[0]
    assert "drawdown" in esc["payload"]["reason"]
    # The facts failure was recorded, not fatal.
    assert any("EDGAR down" in e for e in outcome.errors)


def test_lapsed_hard_catalyst_surfaces_for_event_memo(stores):
    memo_store, screen_store, journal = stores
    memo_store.save(_memo(
        ticker="SPIN", thesis_type="event",
        key_dates=[Catalyst(description="distribution date",
                            expected_date=date(2026, 6, 30), hard_date=True)],
    ))
    outcome = _run(stores)
    assert outcome.catalyst_due == 1
    assert outcome.escalations == 1
    due = _events_of(journal, events.KIND_CATALYST_DUE)
    assert len(due) == 1 and due[0]["payload"]["ticker"] == "SPIN"
    assert [h["symbol"] for h in screen_store.pending_hits()] == ["SPIN"]
    # Same day re-run: catalyst is still due, but no second notification
    # (journal dedupe) and no second escalation (hit still pending —
    # enqueue_hit is called again but its own pending-hit dedupe returns None).
    outcome2 = _run(stores)
    assert outcome2.catalyst_due == 0
    assert outcome2.escalations == 0
    assert len(_events_of(journal, events.KIND_CATALYST_DUE)) == 1
    assert len(screen_store.pending_hits()) == 1


def test_researched_catalyst_escalation_is_not_requeued(stores):
    memo_store, screen_store, journal = stores
    memo_store.save(_memo(catalysts=[Catalyst(
        description="scheduled reassessment", expected_date=TODAY, hard_date=True,
    )]))
    _run(stores)
    first_hit = screen_store.pending_hits()[0]
    screen_store.mark_researched(first_hit["id"])

    outcome = _run(stores)

    assert outcome.escalations == 0
    assert screen_store.pending_hits() == []
    assert len(_events_of(journal, events.KIND_RESEARCH_ESCALATION)) == 1


def test_failed_catalyst_escalation_is_retried(stores):
    memo_store, screen_store, journal = stores
    memo_store.save(_memo(catalysts=[Catalyst(
        description="scheduled reassessment", expected_date=TODAY, hard_date=True,
    )]))
    _run(stores)
    first_hit = screen_store.pending_hits()[0]
    screen_store.mark_failed(first_hit["id"])

    outcome = _run(stores)

    assert outcome.escalations == 1
    assert len(screen_store.pending_hits()) == 1
    assert screen_store.pending_hits()[0]["id"] != first_hit["id"]
    assert len(_events_of(journal, events.KIND_RESEARCH_ESCALATION)) == 2


def test_multiple_due_catalysts_are_aggregated_into_one_hit(stores):
    memo_store, screen_store, journal = stores
    memo_store.save(_memo(catalysts=[
        Catalyst(description="first event", expected_date=TODAY, hard_date=True),
        Catalyst(description="second event", expected_date=TODAY, hard_date=True),
    ]))

    outcome = _run(stores)

    assert outcome.catalyst_due == 2
    assert outcome.escalations == 1
    hit = screen_store.pending_hits()[0]
    description = hit["payload"]["triggers"][0]["description"]
    assert "first event" in description
    assert "second event" in description


def test_newly_due_catalyst_queues_after_prior_set_was_researched(stores):
    memo_store, screen_store, _ = stores
    tomorrow = TODAY + timedelta(days=1)
    memo_store.save(_memo(catalysts=[
        Catalyst(description="first event", expected_date=TODAY, hard_date=True),
        Catalyst(description="second event", expected_date=tomorrow, hard_date=True),
    ]))
    _run(stores)
    first_hit = screen_store.pending_hits()[0]
    screen_store.mark_researched(first_hit["id"])

    outcome = _run(stores, today=tomorrow, now=NOW + timedelta(days=1))

    assert outcome.catalyst_due == 1
    assert outcome.escalations == 1
    description = screen_store.pending_hits()[0]["payload"]["triggers"][0]["description"]
    assert "first event" in description
    assert "second event" in description


def test_pm_reassess_catalyst_on_value_memo_also_escalates(stores):
    """A value-thesis memo can carry a PM-scheduled reassess date (Task 4)
    even though it has no event_block — the monitor must not silently
    ignore top-level catalysts just because thesis_type != 'event'."""
    memo_store, screen_store, journal = stores
    memo_store.save(_memo(
        ticker="SPCX", thesis_type="value",
        catalysts=[Catalyst(description="Starship Flight 13 outcome",
                            expected_date=date(2026, 6, 30), hard_date=True)],
    ))
    outcome = _run(stores)
    assert outcome.catalyst_due == 1
    assert outcome.escalations == 1
    assert [h["symbol"] for h in screen_store.pending_hits()] == ["SPCX"]


def test_resolution_due_with_checklist(stores):
    memo_store, _, journal = stores
    old = NOW - timedelta(days=400)
    memo_store.save(_memo(months=12, created_at=old))
    outcome = _run(stores)
    assert outcome.resolution_due == 1
    due = _events_of(journal, events.KIND_RESOLUTION_DUE)[0]
    checklist = due["payload"]["checklist"]
    assert "drawdown breach" in checklist
    assert "must-be-true: volume replaced" in checklist
    assert "15.0" in checklist and "20.0" in checklist
    # Dedupe on re-run.
    assert _run(stores).resolution_due == 0


def test_bad_ticker_does_not_kill_the_loop(stores):
    memo_store, _, journal = stores
    memo_store.save(_memo(ticker="BAD1"))
    memo_store.save(_memo(ticker="GOOD"))

    def flaky_prices(symbol):
        if symbol == "BAD1":
            raise RuntimeError("yahoo exploded")
        return PriceContext(closes={TODAY: Decimal("9.5")})

    outcome = monitor_memos(
        memo_store=memo_store, screen_store=stores[1], journal=journal,
        price_fetcher=flaky_prices, facts_fetcher=lambda t: {},
        today=TODAY, now=NOW,
    )
    assert outcome.memos_checked == 2
    assert any("BAD1" in e for e in outcome.errors)
    assert len(_events_of(journal, events.KIND_RESEARCH_MONITOR_RUN)) == 1


def test_empty_store_is_a_clean_noop(stores):
    outcome = _run(stores)
    assert isinstance(outcome, MonitorOutcome)
    assert outcome.memos_checked == 0
    # The run summary is still journaled (it is the daemon's daily gate).
    assert len(_events_of(stores[2], events.KIND_RESEARCH_MONITOR_RUN)) == 1
