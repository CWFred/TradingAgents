from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ops import events
from ops.backtest.live_adapter import normalize_live_research
from ops.backtest.models import DecisionAction

pytestmark = pytest.mark.unit


class _Memos:
    def __init__(self, rows):
        self.rows = rows

    def list(self):
        return list(self.rows)


class _Journal:
    def __init__(self, rows):
        self.rows = rows

    def read_events(self):
        return list(self.rows)


def test_live_adapter_is_stable_and_filters_pre_cutoff():
    old = SimpleNamespace(memo_id="old", as_of_date=date(2025, 5, 31))
    memo = SimpleNamespace(memo_id="memo", as_of_date=date(2025, 6, 1))
    at = datetime(2025, 6, 2, tzinfo=timezone.utc)
    event = {
        "at": at, "kind": events.KIND_RESEARCH_POSITION_OPENED,
        "payload": {"memo_id": "memo", "symbol": "aaa"},
    }
    kwargs = {
        "memo_store": _Memos([memo, old]), "research_journal": _Journal([event]),
    }
    first = normalize_live_research(**kwargs)
    second = normalize_live_research(**kwargs)
    assert first == second
    assert len(first) == 1
    assert first[0].decisions[0].action == DecisionAction.BUY


def test_unknown_or_unlinked_events_are_preserved_outside_adapter_scope():
    memo = SimpleNamespace(memo_id="memo", as_of_date=date(2025, 6, 1))
    rows = [{
        "at": datetime(2025, 6, 2, tzinfo=timezone.utc),
        "kind": "unknown", "payload": {"memo_id": "memo"},
    }]
    triples = normalize_live_research(
        memo_store=_Memos([memo]), research_journal=_Journal(rows),
    )
    assert triples[0].decisions == ()
    assert triples.provenance_issues[0].reason == (
        "event kind is outside the live research adapter"
    )
    assert triples[0].provenance_issues == triples.provenance_issues


def test_linked_fills_produce_realized_outcome_inputs():
    memo = SimpleNamespace(memo_id="memo", as_of_date=date(2025, 6, 1))
    opened_at = datetime(2025, 6, 2, 14, tzinfo=timezone.utc)
    closed_at = opened_at + timedelta(days=5)
    rows = [
        {
            "at": opened_at, "kind": events.KIND_FILL,
            "payload": events.fill_payload(
                client_order_id="buy-1", order_id="order-buy", symbol="AAA",
                side="BUY", quantity=Decimal("5"), price=Decimal("10"),
                filled_at=opened_at, context="research", broker_mode="paper",
            ),
        },
        {
            "at": opened_at, "kind": events.KIND_RESEARCH_POSITION_OPENED,
            "payload": events.research_position_opened_payload(
                symbol="AAA", memo_id="memo", conviction_tier="high",
                entry_date=opened_at.date().isoformat(), client_order_id="buy-1",
                notional="50",
            ),
        },
        {
            "at": closed_at, "kind": events.KIND_FILL,
            "payload": events.fill_payload(
                client_order_id="sell-1", order_id="order-sell", symbol="AAA",
                side="SELL", quantity=Decimal("5"), price=Decimal("12"),
                filled_at=closed_at, context="research", broker_mode="paper",
            ),
        },
        {
            "at": closed_at, "kind": events.KIND_RESEARCH_POSITION_CLOSED,
            "payload": events.research_position_closed_payload(
                symbol="AAA", memo_id="memo", reason="target hit",
                exit_date=closed_at.date().isoformat(), price="12",
            ),
        },
    ]

    normalized = normalize_live_research(
        memo_store=_Memos([memo]), research_journal=_Journal(rows),
    )

    assert normalized.provenance_issues == ()
    triple = normalized[0]
    assert [decision.price for decision in triple.decisions] == [
        Decimal("10"), Decimal("12"),
    ]
    assert len(triple.outcomes) == 1
    assert triple.outcomes[0].realized_return == Decimal("0.2")
    assert triple.outcomes[0].quantity == Decimal("5")


def test_malformed_and_unlinked_rows_are_explicit_provenance_issues():
    memo = SimpleNamespace(memo_id="memo", as_of_date=date(2025, 6, 1))
    at = datetime(2025, 6, 2, tzinfo=timezone.utc)
    rows = [
        {"at": at, "kind": events.KIND_RESEARCH_POSITION_OPENED, "payload": {}},
        {
            "at": at, "kind": events.KIND_FILL,
            "payload": events.fill_payload(
                client_order_id="orphan", order_id="o", symbol="AAA", side="BUY",
                quantity=Decimal("1"), price=Decimal("10"), filled_at=at,
                context="research", broker_mode="paper",
            ),
        },
    ]

    normalized = normalize_live_research(
        memo_store=_Memos([memo]), research_journal=_Journal(rows),
    )

    assert {issue.reason for issue in normalized.provenance_issues} == {
        "missing memo/symbol/timestamp provenance",
        "fill is not linked to a research lifecycle event",
    }
