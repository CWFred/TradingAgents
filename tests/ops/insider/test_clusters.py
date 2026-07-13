"""Cluster detection: buyer/dollar thresholds, strength scoring, cooldown."""

from datetime import date
from decimal import Decimal

import pytest

from ops.insider.clusters import find_clusters
from ops.insider.store import SignalStore
from tradingagents.dataflows.form4 import InsiderTransaction

pytestmark = pytest.mark.unit

ASOF = date(2026, 7, 13)
_acc = iter(range(1000))


def _buy(name, *, dollars="20000", title="Director", when=date(2026, 7, 1),
         code="P"):
    shares = Decimal("1000")
    price = Decimal(dollars) / shares
    return InsiderTransaction(
        insider_name=name, insider_title=title, is_director=True,
        is_officer=title != "Director", is_ten_pct_owner=False,
        transaction_date=when, code=code, shares=shares, price=price,
        acquired=True, ten_b5_1=False,
        accession=f"0001-26-{next(_acc):06d}", filed_date=when,
    )


@pytest.fixture
def store(tmp_path):
    return SignalStore(tmp_path / "signals.sqlite")


def test_two_qualified_buyers_make_a_basic_cluster(store):
    store.record_transactions("AAA", [_buy("A"), _buy("B")])
    (c,) = find_clusters(store, asof=ASOF)
    assert c.symbol == "AAA" and c.strength == "BASIC"
    assert c.buyers == ("A", "B")
    assert c.agg_dollars == Decimal("40000")
    assert len(c.accessions) == 2


def test_three_buyers_are_strong(store):
    store.record_transactions("AAA", [_buy("A"), _buy("B"), _buy("C")])
    (c,) = find_clusters(store, asof=ASOF)
    assert c.strength == "STRONG"


def test_chief_participation_is_strong(store):
    store.record_transactions("AAA", [
        _buy("A"), _buy("B", title="Chief Financial Officer"),
    ])
    (c,) = find_clusters(store, asof=ASOF)
    assert c.strength == "STRONG"


def test_big_aggregate_is_strong(store):
    store.record_transactions("AAA", [
        _buy("A", dollars="150000"), _buy("B", dollars="120000"),
    ])
    (c,) = find_clusters(store, asof=ASOF)
    assert c.strength == "STRONG"


def test_sub_threshold_buyer_does_not_count(store):
    store.record_transactions("AAA", [_buy("A"), _buy("B", dollars="5000")])
    assert find_clusters(store, asof=ASOF) == []


def test_old_buys_do_not_count(store):
    store.record_transactions("AAA", [
        _buy("A", when=date(2026, 5, 1)), _buy("B", when=date(2026, 5, 2)),
    ])
    assert find_clusters(store, asof=ASOF) == []


def test_cooldown_excludes_recent_entries(store):
    store.record_transactions("AAA", [_buy("A"), _buy("B")])
    store.record_entry("AAA", asof=date(2026, 6, 13))  # 30 days ago < 90
    assert find_clusters(store, asof=ASOF) == []
