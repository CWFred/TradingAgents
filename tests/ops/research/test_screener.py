"""Unit tests for the pure screener (no I/O)."""

from datetime import date
from decimal import Decimal

import pytest

from ops.research.screener import NameInputs, screen_universe
from ops.research.triggers import Trigger
from tradingagents.dataflows.fundamentals import Fundamentals, YearValue

pytestmark = pytest.mark.unit

ASOF = date(2026, 7, 1)
D = Decimal


def _yv(pairs):
    return tuple(YearValue(date(y, 12, 31), D(str(v))) for y, v in pairs)


def _fund(ticker, **overrides):
    defaults = {
        "ticker": ticker,
        "asof": ASOF,
        "ebit": D("100"),
        "ebitda": D("120"),
        "total_debt": D("100"),
        "cash": D("50"),
        "fcf": D("80"),
        "eps_history": _yv(
            [(2021, "2.0"), (2022, "2.2"), (2023, "2.4"), (2024, "2.6"), (2025, "2.0")]
        ),
        "roic_history": _yv([(2023, "0.15"), (2024, "0.16"), (2025, "0.14")]),
        "gross_margin_history": _yv([(2023, "0.40"), (2024, "0.42"), (2025, "0.41")]),
    }
    defaults.update(overrides)
    return Fundamentals(**defaults)


def _trigger():
    return Trigger(kind="activist_stake", description="SC 13D", date=ASOF, source="a1")


def _inputs(symbol, *, sector="Industrials", market_cap=None, price=None,
            triggers=(), fund=None):
    if market_cap is None:
        market_cap = D("1000")
    if price is None:
        price = D("20")
    year_end_prices = {date(y, 12, 31): D("40") for y in range(2021, 2026)}
    return NameInputs(
        symbol=symbol, sector=sector, price=price, market_cap=market_cap,
        fundamentals=fund or _fund(symbol), triggers=tuple(triggers),
        year_end_prices=year_end_prices,
    )


def _expensive_peer(symbol):
    # Same sector, EV/EBIT far above the candidate's, to anchor the median.
    return _inputs(symbol, market_cap=D("5000"), fund=_fund(symbol, ebit=D("100")))


def test_cheap_quality_and_trigger_passes():
    # Candidate: EV = 1000 + 100 - 50 = 1050, EV/EBIT = 10.5 vs peers at 50.5.
    # FCF yield = 80/1000 = 8% > 6%. Current P/E = 20/2.0 = 10 vs history 40/eps ~ 15-20.
    universe = [
        _inputs("GOOD", triggers=[_trigger()]),
        *[_expensive_peer(f"PEER{i}") for i in range(5)],
    ]
    results = {r.symbol: r for r in screen_universe(universe, asof=ASOF)}
    good = results["GOOD"]
    assert good.cheap and good.quality and good.passed
    assert [b.passed for b in good.valuation_bars] == [True, True, True]
    assert [b.passed for b in good.quality_bars] == [True, True, True]


def test_no_trigger_means_no_pass_even_when_cheap_and_quality():
    universe = [
        _inputs("GOOD", triggers=[]),
        *[_expensive_peer(f"PEER{i}") for i in range(5)],
    ]
    good = {r.symbol: r for r in screen_universe(universe, asof=ASOF)}["GOOD"]
    assert good.cheap and good.quality and not good.passed


def test_missing_data_fails_bars_not_passes():
    fund = _fund("MISS", ebit=None, ebitda=None, fcf=None,
                 roic_history=(), gross_margin_history=(), eps_history=())
    universe = [
        _inputs("MISS", triggers=[_trigger()], fund=fund),
        *[_expensive_peer(f"PEER{i}") for i in range(5)],
    ]
    miss = {r.symbol: r for r in screen_universe(universe, asof=ASOF)}["MISS"]
    assert not miss.passed
    assert all(not b.passed for b in miss.valuation_bars)
    # Q2 (debt/EBITDA) fails because EBITDA is missing.
    assert all(not b.passed for b in miss.quality_bars)
    assert any("missing" in b.detail for b in miss.valuation_bars)


def test_small_sector_falls_back_to_universe_median():
    # Only 2 names in "Rare" sector (< MIN_SECTOR_PEERS): candidate must be
    # compared against the whole-universe median instead.
    universe = [
        _inputs("RARE", sector="Rare", triggers=[_trigger()]),
        _inputs("RARE2", sector="Rare", market_cap=D("5000")),
        *[_expensive_peer(f"PEER{i}") for i in range(5)],
    ]
    rare = {r.symbol: r for r in screen_universe(universe, asof=ASOF)}["RARE"]
    v1 = rare.valuation_bars[0]
    assert v1.passed


def test_sector_with_only_four_real_peers_falls_back_to_universe_median():
    # "Small" sector: candidate + 4 expensive same-sector peers = 4 REAL peers,
    # below MIN_SECTOR_PEERS once self is excluded -> universe median governs.
    universe = [
        _inputs("EDGE", sector="Small", triggers=[_trigger()]),
        *[_inputs(f"SM{i}", sector="Small", market_cap=D("5000")) for i in range(4)],
        *[_expensive_peer(f"PEER{i}") for i in range(5)],
    ]
    edge = {r.symbol: r for r in screen_universe(universe, asof=ASOF)}["EDGE"]
    v1 = edge.valuation_bars[0]
    assert v1.passed
    assert "universe" in v1.detail


def test_high_leverage_fails_quality_bar():
    fund = _fund("LEVD", total_debt=D("500"), ebitda=D("100"))
    universe = [
        _inputs("LEVD", triggers=[_trigger()], fund=fund),
        *[_expensive_peer(f"PEER{i}") for i in range(5)],
    ]
    levd = {r.symbol: r for r in screen_universe(universe, asof=ASOF)}["LEVD"]
    q2 = levd.quality_bars[1]
    assert not q2.passed
    # 2-of-3 still holds via Q1 + Q3.
    assert levd.quality


def test_unstable_gross_margins_fail_q3():
    fund = _fund("SWNG", gross_margin_history=tuple(
        YearValue(date(y, 12, 31), v)
        for y, v in [(2023, D("0.40")), (2024, D("0.55")), (2025, D("0.30"))]
    ))
    universe = [
        _inputs("SWNG", triggers=[_trigger()], fund=fund),
        *[_expensive_peer(f"PEER{i}") for i in range(5)],
    ]
    swng = {r.symbol: r for r in screen_universe(universe, asof=ASOF)}["SWNG"]
    assert not swng.quality_bars[2].passed
