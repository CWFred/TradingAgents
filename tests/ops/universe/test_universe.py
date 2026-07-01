from datetime import date
from decimal import Decimal

from ops.config import OpsConfig
from ops.universe import build_universe
from ops.universe.earnings import EarningsHit


def _hit(sym):
    return EarningsHit(
        symbol=sym, report_date=date(2026, 6, 30),
        eps_actual=Decimal("1"), eps_estimate=Decimal("0.9"),
        revenue_actual=Decimal("100"), revenue_estimate=Decimal("90"),
        eps_beat=True, revenue_beat=True,
    )


def test_build_universe_composes_pipeline():
    cfg = OpsConfig()

    def members():
        return ["AAPL", "SPOT", "MSFT", "TQQQ", "PENNY"]

    def earnings(syms, asof_date, lookback_days, fetch=None):
        # SPOT and TQQQ are deny-listed, so should never reach earnings
        assert "SPOT" not in syms
        assert "TQQQ" not in syms
        return [_hit(s) for s in syms]

    def metrics(sym):
        if sym == "PENNY":
            return Decimal("2"), Decimal("100000000")
        return Decimal("200"), Decimal("100000000")

    result = build_universe(
        asof_date=date(2026, 6, 30),
        config=cfg,
        members_loader=members,
        earnings_finder=earnings,
        metrics_fetcher=metrics,
    )
    syms = [c.symbol for c in result]
    assert syms == sorted(syms)            # deterministic ordering
    assert syms == ["AAPL", "MSFT"]        # SPOT/TQQQ denied, PENNY price filter
    assert all(c.last_price == Decimal("200") for c in result)
