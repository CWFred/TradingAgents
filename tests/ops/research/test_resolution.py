"""Unit tests for computed resolution arithmetic (no network; canned prices)."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ops.journal import Journal
from ops.research.prices import PriceContext
from ops.research.resolution import BENCHMARK_SYMBOL, ResolutionError, compute_resolution_numbers
from tradingagents.memos.schema import EvidenceItem, Falsifier, Memo, ValueThesis

pytestmark = pytest.mark.unit

CREATED = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
AS_OF = date(2026, 1, 5)
NOW = datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def _memo(ticker="WIDG", *, status="open", entry=10.0, created_at=CREATED):
    memo = Memo(
        ticker=ticker, as_of_date=AS_OF, thesis_type="value",
        thesis="Mispriced.", created_at=created_at,
        evidence=[EvidenceItem(claim="c", source_type="filing", source_ref="a:mdna")],
        value_block=ValueThesis(
            why_cheap="x", change_trigger="y",
            normalized_earnings_view="z", quality_assessment="q",
        ),
        conviction_tier="medium",
        entry_price_ref=entry, price_target_low=15.0, price_target_high=20.0,
        expected_holding_months=6, must_be_true=["m"],
        falsifiers=[Falsifier(description="d", check_type="price",
                              metric="drawdown_from_cost_pct", operator="<",
                              threshold=-30.0)],
    )
    memo.status = status
    return memo


def _prices(*, ticker="WIDG", entry_close=10.0, exit_close=13.0,
            bench_entry=200.0, bench_exit=216.0):
    """A price_fetcher keyed by symbol, PriceContext per-symbol with a single
    entry-date close and a single today-close (mirrors test_monitor.py's
    canned-price style, but with two dates since the benchmark math needs
    both ends of the window)."""
    contexts = {
        ticker: PriceContext(closes={
            AS_OF: Decimal(str(entry_close)), TODAY: Decimal(str(exit_close)),
        }),
        BENCHMARK_SYMBOL: PriceContext(closes={
            AS_OF: Decimal(str(bench_entry)), TODAY: Decimal(str(bench_exit)),
        }),
    }

    def fetcher(symbol):
        return contexts.get(symbol)

    return fetcher


@pytest.fixture
def journal(tmp_path):
    with Journal(str(tmp_path / "research.sqlite")) as j:
        yield j


def test_explicit_exit_price_wins_over_fill_and_close(journal):
    memo = _memo(entry=10.0)
    # A SELL fill exists too — explicit --exit-price must still win.
    journal.record_fill(
        order_id="o1", client_order_id="c1", symbol="WIDG", side="SELL",
        quantity=Decimal("10"), price=Decimal("99"), filled_at=NOW,
    )
    numbers = compute_resolution_numbers(
        memo, research_journal=journal, price_fetcher=_prices(),
        now=NOW, exit_price=17.0,
    )
    assert numbers["exit_price"] == 17.0
    assert numbers["realized_return_pct"] == pytest.approx((17.0 - 10.0) / 10.0)


def test_sell_fill_exit_price_used_when_no_explicit_price(journal):
    memo = _memo(entry=10.0)
    journal.record_fill(
        order_id="o1", client_order_id="c1", symbol="WIDG", side="BUY",
        quantity=Decimal("10"), price=Decimal("10"), filled_at=CREATED,
    )
    journal.record_fill(
        order_id="o2", client_order_id="c2", symbol="WIDG", side="SELL",
        quantity=Decimal("10"), price=Decimal("14.5"), filled_at=NOW,
    )
    numbers = compute_resolution_numbers(
        memo, research_journal=journal, price_fetcher=_prices(), now=NOW,
    )
    assert numbers["exit_price"] == 14.5
    assert numbers["realized_return_pct"] == pytest.approx((14.5 - 10.0) / 10.0)


def test_passed_memo_uses_current_close_but_reports_no_exit_price(journal):
    memo = _memo(status="passed", entry=10.0)
    numbers = compute_resolution_numbers(
        memo, research_journal=journal,
        price_fetcher=_prices(exit_close=13.0), now=NOW,
    )
    # Shadow-tracked passed memos never fill — the schema's documented
    # convention is exit_price=None even though the return math still uses
    # the current close.
    assert numbers["exit_price"] is None
    assert numbers["realized_return_pct"] == pytest.approx((13.0 - 10.0) / 10.0)


def test_open_memo_with_no_fill_falls_back_to_current_close(journal):
    memo = _memo(status="open", entry=10.0)
    numbers = compute_resolution_numbers(
        memo, research_journal=journal,
        price_fetcher=_prices(exit_close=8.0), now=NOW,
    )
    assert numbers["exit_price"] == 8.0
    assert numbers["realized_return_pct"] == pytest.approx((8.0 - 10.0) / 10.0)


def test_benchmark_return_pct_over_identical_window(journal):
    memo = _memo(entry=10.0)
    numbers = compute_resolution_numbers(
        memo, research_journal=journal,
        price_fetcher=_prices(bench_entry=200.0, bench_exit=216.0),
        now=NOW, exit_price=11.0,
    )
    assert numbers["benchmark_return_pct"] == pytest.approx((216.0 - 200.0) / 200.0)


def test_holding_days_and_fraction_convention_matches_memo_store_fixture(journal):
    # tests/test_memo_store.py's _resolution() helper uses realized_return_pct
    # = 0.30 and benchmark_return_pct = 0.08 to mean 30% / 8% (a fraction, not
    # a percent) — this pins compute_resolution_numbers to that convention.
    memo = _memo(entry=10.0, created_at=NOW.replace(year=2026, month=1, day=1))
    numbers = compute_resolution_numbers(
        memo, research_journal=journal,
        price_fetcher=_prices(bench_entry=100.0, bench_exit=108.0),
        now=NOW, exit_price=13.0,
    )
    assert numbers["realized_return_pct"] == pytest.approx(0.30)
    assert numbers["benchmark_return_pct"] == pytest.approx(0.08)
    assert numbers["holding_days"] == (NOW - memo.created_at).days
    assert numbers["resolved_at"] == NOW


def test_explicit_exit_price_on_passed_memo_stays_none(journal):
    # Schema docstring: exit_price is None for shadow-tracked "passed" memos,
    # unqualified — even when the caller supplies an explicit --exit-price.
    # The return math still uses the explicit price.
    memo = _memo(status="passed", entry=10.0)
    numbers = compute_resolution_numbers(
        memo, research_journal=journal, price_fetcher=_prices(),
        now=NOW, exit_price=14.0,
    )
    assert numbers["exit_price"] is None
    assert numbers["realized_return_pct"] == pytest.approx((14.0 - 10.0) / 10.0)


def test_multiple_sell_fills_latest_by_filled_at_wins(journal):
    # Insert the later fill first to prove selection is by filled_at, not
    # insertion/row-id order.
    journal.record_fill(
        order_id="o2", client_order_id="c2", symbol="WIDG", side="SELL",
        quantity=Decimal("10"), price=Decimal("20.0"),
        filled_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    journal.record_fill(
        order_id="o1", client_order_id="c1", symbol="WIDG", side="SELL",
        quantity=Decimal("10"), price=Decimal("14.5"),
        filled_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
    )
    memo = _memo(entry=10.0)
    numbers = compute_resolution_numbers(
        memo, research_journal=journal, price_fetcher=_prices(), now=NOW,
    )
    assert numbers["exit_price"] == 20.0
    assert numbers["realized_return_pct"] == pytest.approx((20.0 - 10.0) / 10.0)


def test_missing_benchmark_data_raises_resolution_error(journal):
    memo = _memo(entry=10.0)

    def fetcher(symbol):
        return None  # neither the ticker nor IWM has data

    with pytest.raises(ResolutionError, match=BENCHMARK_SYMBOL):
        compute_resolution_numbers(
            memo, research_journal=journal, price_fetcher=fetcher,
            now=NOW, exit_price=11.0,
        )
