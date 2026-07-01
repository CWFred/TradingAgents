from decimal import Decimal
from unittest.mock import patch, MagicMock
import pytest
from ops.quotes import make_yfinance_quote_source


def _fake_ticker(price: float) -> MagicMock:
    t = MagicMock()
    # yfinance Ticker.fast_info has last_price; tolerate either path
    t.fast_info = MagicMock()
    t.fast_info.last_price = price
    return t


def test_quote_source_returns_decimal_from_yfinance():
    with patch("ops.quotes.yf.Ticker", return_value=_fake_ticker(200.05)):
        q = make_yfinance_quote_source(ttl_seconds=60)
        assert q("AAPL") == Decimal("200.05")


def test_quote_source_caches_within_ttl():
    fake = _fake_ticker(200.05)
    with patch("ops.quotes.yf.Ticker", return_value=fake) as mock_ticker:
        q = make_yfinance_quote_source(ttl_seconds=60)
        q("AAPL")
        q("AAPL")
        q("AAPL")
        # Only one yf.Ticker call within TTL
        assert mock_ticker.call_count == 1


def test_quote_source_refreshes_after_ttl(monkeypatch):
    # Drive the cache's clock manually
    clock = [1000.0]
    monkeypatch.setattr("ops.quotes._now", lambda: clock[0])
    with patch("ops.quotes.yf.Ticker", return_value=_fake_ticker(200.05)) as mock_ticker:
        q = make_yfinance_quote_source(ttl_seconds=60)
        q("AAPL")
        clock[0] += 30
        q("AAPL")  # still cached
        assert mock_ticker.call_count == 1
        clock[0] += 31  # now past TTL
        q("AAPL")
        assert mock_ticker.call_count == 2


def test_quote_source_raises_on_missing_price():
    from ops.broker.base import QuoteUnavailable
    bad = MagicMock()
    bad.fast_info = MagicMock()
    bad.fast_info.last_price = None
    with patch("ops.quotes.yf.Ticker", return_value=bad):
        q = make_yfinance_quote_source()
        with pytest.raises(QuoteUnavailable, match="ZZZZ"):
            q("ZZZZ")


def test_quote_source_raises_quote_unavailable_on_yfinance_exception():
    from ops.broker.base import QuoteUnavailable
    def boom(symbol):
        raise KeyError("['fast_info']")
    with patch("ops.quotes.yf.Ticker", side_effect=boom):
        q = make_yfinance_quote_source()
        with pytest.raises(QuoteUnavailable, match="AAPL"):
            q("AAPL")
