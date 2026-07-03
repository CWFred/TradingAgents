from datetime import date
from decimal import Decimal

from ops.universe.earnings import EarningsHit, find_recent_earnings_beats


def _hit(symbol, report_date, *, eps_beat=True, revenue_beat=True):
    return EarningsHit(
        symbol=symbol, report_date=report_date,
        eps_actual=Decimal("1"), eps_estimate=Decimal("0.9"),
        revenue_actual=Decimal("100"), revenue_estimate=Decimal("90"),
        eps_beat=eps_beat, revenue_beat=revenue_beat,
    )


def test_keeps_beats_within_lookback():
    today = date(2026, 6, 30)
    table = {
        "AAPL": _hit("AAPL", date(2026, 6, 27)),   # 1 trading day back
        "MSFT": _hit("MSFT", date(2026, 6, 30)),   # today
        "NVDA": _hit("NVDA", date(2026, 6, 24)),   # too old (>2 trading days)
        "META": _hit("META", date(2026, 6, 30), eps_beat=False),   # miss
        "AMZN": _hit("AMZN", date(2026, 6, 30), revenue_beat=False),  # miss
        "GOOG": None,                              # no earnings recently
    }
    result = find_recent_earnings_beats(
        ["AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOG"],
        asof_date=today, lookback_days=2,
        fetch=lambda sym: table[sym],
    )
    syms = sorted(h.symbol for h in result)
    # After spec change: only EPS beat is required (revenue is informational).
    # AMZN now passes despite revenue_beat=False.
    assert syms == ["AAPL", "AMZN", "MSFT"]


def test_returns_empty_when_no_hits():
    result = find_recent_earnings_beats(
        ["AAPL"], asof_date=date(2026, 6, 30),
        fetch=lambda sym: None,
    )
    assert result == []


def test_safe_decimal_handles_nan_and_missing_values():
    from ops.universe.earnings import EarningsHit, find_recent_earnings_beats

    def _make(symbol, revenue_beat):
        return EarningsHit(
            symbol=symbol, report_date=date(2026, 6, 30),
            eps_actual=Decimal("1"), eps_estimate=Decimal("0.9"),
            revenue_actual=Decimal("0"), revenue_estimate=Decimal("0"),
            eps_beat=True, revenue_beat=revenue_beat,
        )
    hits = find_recent_earnings_beats(
        ["A", "B"], asof_date=date(2026, 6, 30), lookback_days=1,
        fetch=lambda s: _make(s, revenue_beat=(s == "A")),
    )
    # Both should pass now that revenue_beat is informational
    assert sorted(h.symbol for h in hits) == ["A", "B"]


def test_fetch_from_yfinance_returns_none_on_exception(monkeypatch, capsys):
    """One flaky ticker must not abort the batch — the fetcher swallows and logs."""
    import ops.universe.earnings as mod

    class BoomTicker:
        earnings_dates = property(lambda self: (_ for _ in ()).throw(KeyError("['Earnings Date']")))

    monkeypatch.setattr(mod.yf, "Ticker", lambda symbol: BoomTicker())
    result = mod._fetch_from_yfinance("ZZZZ")
    assert result is None
    err = capsys.readouterr().err
    assert "ZZZZ" in err
    assert "KeyError" in err


def _earnings_dates_df(rows: dict, report_date: date):
    """Build a minimal DataFrame shaped like yfinance's `earnings_dates`
    attribute: a DatetimeIndex row per report, columns as given in `rows`."""
    import pandas as pd

    idx = pd.DatetimeIndex([pd.Timestamp(report_date)])
    return pd.DataFrame(rows, index=idx)


def test_fetch_from_yfinance_revenue_absent_is_honest_none(monkeypatch):
    """yfinance's earnings_dates frame does not carry revenue columns in
    practice — the fetcher must not fabricate zeros for missing data."""
    import ops.universe.earnings as mod

    report_date = date(2026, 6, 30)
    df = _earnings_dates_df(
        {"EPS Estimate": [0.9], "Reported EPS": [1.0]}, report_date
    )

    class FakeTicker:
        earnings_dates = df

    monkeypatch.setattr(mod.yf, "Ticker", lambda symbol: FakeTicker())
    hit = mod._fetch_from_yfinance("AAPL")

    assert hit is not None
    assert hit.revenue_actual is None
    assert hit.revenue_estimate is None
    assert hit.revenue_beat is None
    assert hit.eps_beat is True

    # And the universe-level filter still passes it through (EPS-only gate).
    result = find_recent_earnings_beats(
        ["AAPL"], asof_date=report_date, lookback_days=2,
        fetch=lambda sym: hit,
    )
    assert [h.symbol for h in result] == ["AAPL"]


def test_fetch_from_yfinance_revenue_present_computes_beat(monkeypatch):
    """When both revenue columns are genuinely present, revenue_beat
    reflects actual vs. estimate rather than staying None."""
    import ops.universe.earnings as mod

    report_date = date(2026, 6, 30)
    df = _earnings_dates_df(
        {
            "EPS Estimate": [0.9],
            "Reported EPS": [1.0],
            "Reported Revenue": [110.0],
            "Revenue Estimate": [100.0],
        },
        report_date,
    )

    class FakeTicker:
        earnings_dates = df

    monkeypatch.setattr(mod.yf, "Ticker", lambda symbol: FakeTicker())
    hit = mod._fetch_from_yfinance("AAPL")

    assert hit is not None
    assert hit.revenue_actual == Decimal("110.0")
    assert hit.revenue_estimate == Decimal("100.0")
    assert hit.revenue_beat is True


def test_fetch_from_yfinance_revenue_partial_columns_is_honest_none(monkeypatch):
    """Only one of the two revenue columns present is still absent data —
    must not fabricate a comparison against a fabricated zero."""
    import ops.universe.earnings as mod

    report_date = date(2026, 6, 30)
    df = _earnings_dates_df(
        {
            "EPS Estimate": [0.9],
            "Reported EPS": [1.0],
            "Reported Revenue": [110.0],
        },
        report_date,
    )

    class FakeTicker:
        earnings_dates = df

    monkeypatch.setattr(mod.yf, "Ticker", lambda symbol: FakeTicker())
    hit = mod._fetch_from_yfinance("AAPL")

    assert hit is not None
    assert hit.revenue_actual is None
    assert hit.revenue_estimate is None
    assert hit.revenue_beat is None
