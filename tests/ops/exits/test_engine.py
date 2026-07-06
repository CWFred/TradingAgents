from datetime import date
from decimal import Decimal

from ops.config import OpsConfig
from ops.broker.types import Position
from ops.exits import evaluate_exits
from ops.universe.momentum import SMA_WINDOW, MomentumHit

ASOF = date(2026, 7, 6)  # a Monday
CFG = OpsConfig()


def _pos(sym):
    return Position(symbol=sym, quantity=Decimal("1"),
                    avg_entry_price=Decimal("100"))


def _prov(sym, source, entry="2026-07-02", rank=None):
    p = {"symbol": sym, "source": source, "entry_date": entry,
         "client_order_id": "x"}
    if rank is not None:
        p["entry_rank"] = rank
    return p


def _mhit(sym, rank):
    return MomentumHit(symbol=sym, asof_date=ASOF,
                       trailing_return_6m=Decimal("0.2"),
                       close=Decimal("110"), sma_200=Decimal("100"),
                       avg_dollar_volume_20d=Decimal("100000000"), rank=rank)


def _uptrend_closes():
    # 201 rising closes: both last closes comfortably above their MAs.
    return [Decimal(100) + Decimal(i) for i in range(SMA_WINDOW + 1)]


def _broken_closes():
    # Flat at 100 for 199 bars, then two closes far below both days' MAs.
    return [Decimal(100)] * (SMA_WINDOW - 1) + [Decimal(50), Decimal(50)]


def _fetch(mapping):
    return lambda sym: mapping.get(sym)


def _run(positions, provenance, leaderboard, closes, config=CFG):
    return evaluate_exits(
        positions=positions, provenance=provenance, leaderboard=leaderboard,
        closes_fetch=_fetch(closes), config=config, asof_date=ASOF,
    )


def test_rank_decay_fires_at_26_not_25():
    closes = {"A": (_uptrend_closes(), []), "B": (_uptrend_closes(), [])}
    prov = {"A": _prov("A", "MOMENTUM"), "B": _prov("B", "MOMENTUM")}
    board = [_mhit("B", 25), _mhit("A", 26)]
    report = _run([_pos("A"), _pos("B")], prov, board, closes)
    assert [d.symbol for d in report.decisions] == ["A"]
    assert report.decisions[0].rule == "rank_decay"


def test_trend_break_needs_two_consecutive_closes():
    one_below = _uptrend_closes()
    one_below[-1] = Decimal("1")  # only the LAST close dips below the MA
    closes = {"TWO": (_broken_closes(), []), "ONE": (one_below, [])}
    prov = {s: _prov(s, "MOMENTUM") for s in ("TWO", "ONE")}
    report = _run([_pos("TWO"), _pos("ONE")], prov, [], closes)
    assert [d.symbol for d in report.decisions] == ["TWO"]
    assert report.decisions[0].rule == "trend_break"
    # ONE is off the leaderboard with a single below-MA close: no exit.
    assert report.skips == []


def test_missing_data_skips_never_sells():
    prov = {"GONE": _prov("GONE", "MOMENTUM"),
            "SHORT": _prov("SHORT", "MOMENTUM")}
    closes = {"SHORT": ([Decimal(100)] * 50, [])}
    report = _run([_pos("GONE"), _pos("SHORT")], prov, [], closes)
    assert report.decisions == []
    assert sorted(s.symbol for s in report.skips) == ["GONE", "SHORT"]


def test_earnings_max_hold_fires_on_day_40_not_39():
    # 40 trading days before Mon 2026-07-06 is Mon 2026-05-11 (weekday count).
    prov39 = {"E": _prov("E", "EARNINGS", entry="2026-05-12")}
    prov40 = {"E": _prov("E", "EARNINGS", entry="2026-05-11")}
    assert _run([_pos("E")], prov39, [], {}).decisions == []
    report = _run([_pos("E")], prov40, [], {})
    assert [d.rule for d in report.decisions] == ["max_hold"]


def test_earnings_source_ignores_rank_and_ma():
    # Earnings-sourced overlap name ranked 200 with broken trend: still held.
    prov = {"E": _prov("E", "EARNINGS", entry="2026-07-02")}
    closes = {"E": (_broken_closes(), [])}
    report = _run([_pos("E")], prov, [_mhit("E", 200)], closes)
    assert report.decisions == []


def test_unknown_provenance_gets_momentum_rules_and_is_reported():
    closes = {"MYSTERY": (_broken_closes(), [])}
    report = _run([_pos("MYSTERY")], {}, [], closes)
    assert report.unknown_provenance == ["MYSTERY"]
    assert [d.rule for d in report.decisions] == ["trend_break"]
