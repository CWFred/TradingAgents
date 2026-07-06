from datetime import date
from decimal import Decimal

from ops.universe.momentum import find_momentum_leaders

ASOF = date(2026, 7, 2)


def _series(start: str, end: str, n: int = 210) -> list[Decimal]:
    """Linear ramp of n closes from start to end."""
    s, e = Decimal(start), Decimal(end)
    step = (e - s) / Decimal(n - 1)
    return [s + step * i for i in range(n)]


def _fake_fetch(data):
    def fetch(symbol):
        return data.get(symbol)
    return fetch


def test_ranks_by_6mo_return_descending_above_ma():
    vols = [Decimal("1000000")] * 210
    data = {
        "FAST": (_series("100", "200"), vols),
        "SLOW": (_series("100", "120"), vols),
    }
    hits = find_momentum_leaders(["SLOW", "FAST"], ASOF, fetch=_fake_fetch(data))
    assert [h.symbol for h in hits] == ["FAST", "SLOW"]
    assert [h.rank for h in hits] == [1, 2]
    assert hits[0].trailing_return_6m > hits[1].trailing_return_6m
    assert all(h.close > h.sma_200 for h in hits)
    assert all(h.asof_date == ASOF for h in hits)


def test_below_200d_ma_is_gated_out():
    # Rises for 200 bars, then collapses: last closes sit below the 200d MA.
    closes = _series("100", "200", 200) + [Decimal("90")] * 10
    data = {"FALL": (closes, [Decimal("1000000")] * 210)}
    assert find_momentum_leaders(["FALL"], ASOF, fetch=_fake_fetch(data)) == []


def test_insufficient_history_is_skipped_not_zero_filled():
    data = {"IPO": (_series("10", "50", 150), [Decimal("1000000")] * 150)}
    assert find_momentum_leaders(["IPO"], ASOF, fetch=_fake_fetch(data)) == []


def test_fetch_failure_is_skipped():
    assert find_momentum_leaders(["GONE"], ASOF, fetch=lambda s: None) == []


def test_adv_is_20day_mean_dollar_volume():
    closes = [Decimal("100")] * 210
    closes[-1] = Decimal("101")  # nudge above the flat MA so it passes the gate
    volumes = [Decimal("2000000")] * 210
    hits = find_momentum_leaders(
        ["FLAT"], ASOF, fetch=_fake_fetch({"FLAT": (closes, volumes)}),
    )
    assert len(hits) == 1
    # 19 bars at 100*2e6 + 1 bar at 101*2e6, averaged over 20
    assert hits[0].avg_dollar_volume_20d == Decimal("200100000")
