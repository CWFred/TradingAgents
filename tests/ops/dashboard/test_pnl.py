"""Pure per-position P&L math: long/short sign, guards."""
from datetime import datetime, timezone
from decimal import Decimal

from ops.broker.base import QuoteUnavailable
from ops.dashboard.pnl import build_sleeve_pnl, position_pnl
from ops.journal import Journal

_AT = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)


def _seed_long(path):
    # Real replay-accepted BUY: side="BUY" plus a matching order row, mirroring
    # tests/ops/dashboard/test_snapshot_sleeves.py::_seed_momentum (the brief's
    # illustrative seed used different kwargs; these are the true signatures).
    with Journal(path) as j:
        j.record_cash_adjustment(kind="seed", amount=Decimal("10000"), note="t")
        j.record_order(client_order_id="c1", symbol="BAH", side="BUY",
                       notional_dollars=Decimal("1000"),
                       stop_loss_price=Decimal("92"))
        j.record_fill(order_id="o1", client_order_id="c1", symbol="BAH",
                      side="BUY", quantity=Decimal("10"), price=Decimal("100"),
                      filled_at=_AT, stop_loss_price=Decimal("92"))


def test_build_sleeve_pnl_long(tmp_path):
    path = str(tmp_path / "s.sqlite")
    _seed_long(path)
    quotes = lambda s: {"BAH": Decimal("110")}[s]
    out = build_sleeve_pnl(path, is_short=False, quote_source=quotes)
    row = out["positions"][0]
    assert row["symbol"] == "BAH"
    assert row["price"] == "110"
    assert row["pnl_dollar"] == "100"        # (110-100)*10
    assert row["pnl_pct"] == "0.1"
    assert "error" not in row


def test_build_sleeve_pnl_per_symbol_quote_failure(tmp_path):
    path = str(tmp_path / "s.sqlite")
    _seed_long(path)
    def quotes(s):
        raise QuoteUnavailable("boom")
    out = build_sleeve_pnl(path, is_short=False, quote_source=quotes)
    row = out["positions"][0]
    assert row["price"] is None
    assert row["pnl_dollar"] is None and row["pnl_pct"] is None
    assert "boom" in row["error"]


def test_long_gain():
    d, p = position_pnl(Decimal("100"), Decimal("10"), Decimal("110"),
                        is_short=False)
    assert d == Decimal("100")            # (110-100)*10
    assert p == Decimal("0.1")            # (110-100)/100


def test_long_loss():
    d, p = position_pnl(Decimal("100"), Decimal("10"), Decimal("90"),
                        is_short=False)
    assert d == Decimal("-100")
    assert p == Decimal("-0.1")


def test_short_gain_when_price_falls():
    # short journals positive magnitude qty; profit when price drops
    d, p = position_pnl(Decimal("100"), Decimal("10"), Decimal("90"),
                        is_short=True)
    assert d == Decimal("100")            # (100-90)*10
    assert p == Decimal("0.1")


def test_none_price_yields_none():
    assert position_pnl(Decimal("100"), Decimal("10"), None,
                        is_short=False) == (None, None)


def test_zero_or_none_entry_guards_pct_only():
    d, p = position_pnl(Decimal("0"), Decimal("10"), Decimal("50"),
                        is_short=False)
    assert d == Decimal("500")            # dollar still computable
    assert p is None                      # pct guarded (divide-by-zero)
    d2, p2 = position_pnl(None, Decimal("10"), Decimal("50"), is_short=False)
    assert d2 is None and p2 is None      # no entry basis at all
