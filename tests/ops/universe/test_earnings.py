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
    assert syms == ["AAPL", "MSFT"]


def test_returns_empty_when_no_hits():
    result = find_recent_earnings_beats(
        ["AAPL"], asof_date=date(2026, 6, 30),
        fetch=lambda sym: None,
    )
    assert result == []
