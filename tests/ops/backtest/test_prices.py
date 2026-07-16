from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ops.backtest.models import PriceBar
from ops.backtest.prices import (
    PriceCache,
    PriceSeriesStatus,
    align_symbol_and_benchmark,
    next_session_after,
    next_session_bar,
)

pytestmark = pytest.mark.unit


def _bar(
    symbol: str,
    day: date,
    close: str = "100",
    *,
    adjusted: str | None = None,
    dividend: str = "0",
    split: str = "1",
) -> PriceBar:
    raw = Decimal(close)
    adj = Decimal(adjusted if adjusted is not None else close)
    return PriceBar(
        symbol=symbol, session=day,
        open=raw, high=raw + 1, low=raw - 1, close=raw,
        adjusted_open=adj, adjusted_high=adj + 1,
        adjusted_low=adj - 1, adjusted_close=adj,
        volume=Decimal("1000"), dividend=Decimal(dividend), split_ratio=Decimal(split),
        provider="fixture",
    )


@pytest.fixture
def cache(tmp_path):
    return PriceCache(tmp_path / "backtest.sqlite")


def test_round_trip_persists_raw_adjusted_and_actions(cache):
    fetched = datetime(2026, 7, 10, tzinfo=timezone.utc)
    cache.upsert_bars([
        _bar("abc", date(2026, 7, 2), "101", adjusted="99", dividend="1", split="2")
    ], fetched_at=fetched)
    got = cache.bar("ABC", date(2026, 7, 2))
    assert got.open == Decimal("101")
    assert got.adjusted_close == Decimal("99")
    assert got.dividend == Decimal("1")
    assert got.split_ratio == Decimal("2")
    assert got.provider == "fixture"
    assert got.fetched_at == fetched


def test_cache_uses_the_versioned_store_price_schema(tmp_path):
    from ops.backtest.store import BacktestStore

    path = tmp_path / "shared.sqlite"
    BacktestStore(path)
    shared = PriceCache(path)
    shared.upsert_bars([_bar("ABC", date(2026, 7, 2))])
    assert shared.bar("ABC", date(2026, 7, 2)).adjusted_close == Decimal("100")


def test_explicit_update_uses_injected_fetcher_and_rejects_future_rows(cache):
    calls = []

    def fetch(symbol, start, end):
        calls.append((symbol, start, end))
        return [_bar(symbol, end)]

    assert cache.update(
        "abc", start=date(2026, 7, 1), end=date(2026, 7, 2), fetcher=fetch,
    ) == 1
    assert calls == [("ABC", date(2026, 7, 1), date(2026, 7, 2))]

    with pytest.raises(ValueError, match="outside"):
        cache.update(
            "ABC", start=date(2026, 7, 1), end=date(2026, 7, 2),
            fetcher=lambda *_: [_bar("ABC", date(2026, 7, 6))],
        )


def test_next_session_is_strict_and_skips_observed_holiday_and_weekend():
    # Friday July 3, 2026 was the observed Independence Day holiday.
    assert next_session_after(date(2026, 7, 2)) == date(2026, 7, 6)
    assert next_session_after(date(2026, 7, 3)) == date(2026, 7, 6)
    assert next_session_after(date(2026, 7, 4)) == date(2026, 7, 6)


def test_next_session_bar_never_slides_past_a_missing_execution_session(cache):
    # Monday is absent; Tuesday exists.  A Friday decision must remain missing.
    cache.upsert_bars([_bar("ABC", date(2026, 7, 7))])
    result = next_session_bar(cache, "ABC", date(2026, 7, 2))
    assert result.session_date == date(2026, 7, 6)
    assert result.bar is None


def test_visible_through_excludes_future_bars_independently_of_price_basis(cache):
    cache.upsert_bars([
        _bar("ABC", date(2026, 7, 1), "100"),
        _bar("ABC", date(2026, 7, 2), "101"),
        _bar("ABC", date(2026, 7, 6), "999"),
    ])
    got = cache.bars(
        "ABC", adjusted_to=date(2026, 7, 2),
        visible_through=date(2026, 7, 2),
    )
    assert [b.session for b in got] == [date(2026, 7, 1), date(2026, 7, 2)]
    assert [b.close for b in got] == [Decimal("100"), Decimal("101")]


def test_adjusted_series_is_rebased_to_remove_a_future_split(cache):
    # Provider's current adjusted history includes a 2:1 split on July 6.
    # A July 2 case must see the pre-split $100 basis, not today's $50 basis.
    cache.upsert_bars([
        _bar("ABC", date(2026, 7, 1), "100", adjusted="50"),
        _bar("ABC", date(2026, 7, 2), "102", adjusted="51"),
        _bar("ABC", date(2026, 7, 6), "52", adjusted="52", split="2"),
    ])
    before = cache.bars(
        "ABC", adjusted_to=date(2026, 7, 2),
        visible_through=date(2026, 7, 2),
    )
    assert [b.adjusted_close for b in before] == [Decimal("100"), Decimal("102")]

    replay = cache.bars("ABC", adjusted_to=date(2026, 7, 2))
    assert [b.adjusted_close for b in replay] == [
        Decimal("100"), Decimal("102"), Decimal("104"),
    ]

    after = cache.bars("ABC", adjusted_to=date(2026, 7, 6))
    assert [b.adjusted_close for b in after] == [Decimal("50"), Decimal("51"), Decimal("52")]

    # Even a narrow pre-split slice retains an adjustment known by July 6.
    narrow = cache.bars(
        "ABC", start=date(2026, 7, 1), end=date(2026, 7, 2),
        adjusted_to=date(2026, 7, 6),
    )
    assert [b.adjusted_close for b in narrow] == [Decimal("50"), Decimal("51")]


def test_rebase_bounds_future_dividend_adjustments_too(cache):
    cache.upsert_bars([
        _bar("ABC", date(2026, 7, 1), "100", adjusted="98"),
        _bar("ABC", date(2026, 7, 2), "102", adjusted="99.96"),
        _bar("ABC", date(2026, 7, 6), "100", adjusted="100", dividend="2"),
    ])
    before = cache.bars(
        "ABC", adjusted_to=date(2026, 7, 2),
        visible_through=date(2026, 7, 2),
    )
    assert before[-1].adjusted_close == before[-1].close
    assert before[0].adjusted_close == Decimal("100")


def test_alignment_requires_exact_same_sessions_and_reports_gaps(cache):
    sessions = [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 6)]
    cache.upsert_bars([
        _bar("ABC", sessions[0]), _bar("ABC", sessions[1]),
        _bar("SPY", sessions[0]), _bar("SPY", sessions[2]),
    ])
    result = align_symbol_and_benchmark(
        cache, "ABC", "SPY", start=sessions[0], end=sessions[2],
    )
    assert [pair.session_date for pair in result.pairs] == [sessions[0]]
    assert result.missing_symbol_sessions == (sessions[2],)
    assert result.missing_benchmark_sessions == (sessions[1],)


def test_alignment_asof_excludes_future_sessions_for_both_series(cache):
    cache.upsert_bars([
        _bar(sym, day)
        for sym in ("ABC", "SPY")
        for day in (date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 6))
    ])
    result = align_symbol_and_benchmark(
        cache, "ABC", "SPY", start=date(2026, 7, 1), end=date(2026, 7, 6),
        asof=date(2026, 7, 2),
    )
    assert [pair.session_date for pair in result.pairs] == [
        date(2026, 7, 1), date(2026, 7, 2),
    ]


def test_series_status_distinguishes_explicit_states_stale_and_ready(cache):
    cache.set_state("NEW", PriceSeriesStatus.PENDING, reason="horizon not mature")
    cache.set_state("BAD", PriceSeriesStatus.UNPRICEABLE, reason="no provider history")
    cache.set_state("DEAD", PriceSeriesStatus.TERMINAL, reason="delisted")
    assert cache.classify("NEW", required_through=date(2026, 7, 6)) == PriceSeriesStatus.PENDING
    assert cache.classify("BAD", required_through=date(2026, 7, 6)) == PriceSeriesStatus.UNPRICEABLE
    assert cache.classify("DEAD", required_through=date(2026, 7, 6)) == PriceSeriesStatus.TERMINAL

    cache.upsert_bars([_bar("OLD", date(2026, 7, 1))])
    assert cache.classify("OLD", required_through=date(2026, 7, 6)) == PriceSeriesStatus.STALE
    cache.upsert_bars([_bar("OK", date(2026, 7, 6))])
    assert cache.classify("OK", required_through=date(2026, 7, 6)) == PriceSeriesStatus.READY


def test_upsert_rejects_invalid_ohlc(cache):
    bad = _bar("ABC", date(2026, 7, 1))
    bad = PriceBar(**{**bad.__dict__, "high": Decimal("90")})
    with pytest.raises(ValueError, match="high"):
        cache.upsert_bars([bad])
