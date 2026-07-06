from datetime import date
from decimal import Decimal

from ops.config import OpsConfig
from ops.universe import CandidateSource
from ops.universe.composite import build_composite_universe
from ops.universe.earnings import EarningsHit
from ops.universe.momentum import MomentumHit

ASOF = date(2026, 7, 2)


def _ehit(sym):
    return EarningsHit(
        symbol=sym, report_date=ASOF, eps_actual=Decimal("1"),
        eps_estimate=Decimal("0.9"), revenue_actual=None,
        revenue_estimate=None, eps_beat=True, revenue_beat=None,
    )


def _mhit(sym, rank, close="200", adv="100000000"):
    return MomentumHit(
        symbol=sym, asof_date=ASOF,
        trailing_return_6m=Decimal("1") / Decimal(rank),
        close=Decimal(close), sma_200=Decimal("150"),
        avg_dollar_volume_20d=Decimal(adv), rank=rank,
    )


def _build(*, earnings_syms=(), leaders=(), **kwargs):
    return build_composite_universe(
        asof_date=ASOF, config=OpsConfig(),
        members_loader=lambda: ["AAPL", "MSFT", "NVDA", "AVGO", "META",
                                "AMD", "CRM", "ORCL", "NOW", "PLTR"],
        earnings_finder=lambda syms, asof_date, lookback_days, fetch=None:
            [_ehit(s) for s in syms if s in earnings_syms],
        metrics_fetcher=lambda sym: (Decimal("200"), Decimal("100000000")),
        momentum_leaders=list(leaders),
        **kwargs,
    )


def test_earnings_first_then_momentum_by_rank():
    result = _build(earnings_syms={"MSFT"},
                    leaders=[_mhit("NVDA", 1), _mhit("AMD", 2)])
    assert [c.symbol for c in result] == ["MSFT", "NVDA", "AMD"]
    assert result[0].source is CandidateSource.EARNINGS
    assert result[1].source is CandidateSource.MOMENTUM


def test_overlap_keeps_earnings_source_and_both_payloads():
    result = _build(earnings_syms={"NVDA"}, leaders=[_mhit("NVDA", 1)])
    assert len(result) == 1
    c = result[0]
    assert c.source is CandidateSource.EARNINGS
    assert c.earnings is not None and c.momentum is not None


def test_cap_is_min_of_budget_and_free_slots():
    leaders = [_mhit(s, i + 1) for i, s in enumerate(
        ["NVDA", "AMD", "AVGO", "META", "CRM", "ORCL", "NOW", "PLTR", "AAPL"])]
    assert len(_build(leaders=leaders)) == 8                    # budget caps
    assert len(_build(leaders=leaders, free_slots=2)) == 2      # slots cap
    assert _build(leaders=leaders, free_slots=0) == []          # full book -> zero LLM runs


def test_held_and_excluded_symbols_never_returned():
    result = _build(earnings_syms={"MSFT"},
                    leaders=[_mhit("NVDA", 1), _mhit("AMD", 2)],
                    held_symbols=frozenset({"NVDA"}),
                    excluded_symbols=frozenset({"MSFT"}))
    assert [c.symbol for c in result] == ["AMD"]


def test_illiquid_momentum_leader_is_dropped():
    leaders = [_mhit("NVDA", 1, adv="1000"), _mhit("AMD", 2)]
    assert [c.symbol for c in _build(leaders=leaders)] == ["AMD"]


def test_momentum_finder_used_when_no_precomputed_leaderboard():
    calls = []

    def finder(members, asof_date):
        calls.append(list(members))
        return [_mhit("NVDA", 1)]

    result = build_composite_universe(
        asof_date=ASOF, config=OpsConfig(),
        members_loader=lambda: ["NVDA", "SPOT"],
        earnings_finder=lambda syms, asof_date, lookback_days, fetch=None: [],
        metrics_fetcher=lambda sym: (Decimal("200"), Decimal("100000000")),
        momentum_finder=finder,
    )
    assert [c.symbol for c in result] == ["NVDA"]
    assert calls == [["NVDA"]]  # deny-listed SPOT never reaches the finder
