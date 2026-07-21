"""Pure per-position P&L math: long/short sign, guards."""
from decimal import Decimal

from ops.dashboard.pnl import position_pnl


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
