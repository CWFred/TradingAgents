"""Composite universe builder: earnings sleeve + momentum sleeve, merged,
deduped (earnings wins, both payloads kept), exclusions applied, capped at
min(daily_analysis_budget, free_slots).

Every returned candidate costs a full TradingAgentsGraph (LLM) run, so with
zero free slots this returns [] and the day costs zero pipeline runs —
instead of budget-many analyses whose orders are all guaranteed rejects at
the max-open-positions guardrail."""
from __future__ import annotations

import dataclasses
from datetime import date

from ops.config import OpsConfig
from ops.universe import (
    _MIN_ADV,
    _MIN_PRICE,
    Candidate,
    CandidateSource,
    build_universe,
)
from ops.universe.filters import apply_deny_list, apply_liquidity_filter
from ops.universe.momentum import MomentumHit, find_momentum_leaders
from ops.universe.sp500 import load_sp500_members


def build_composite_universe(
    *,
    asof_date: date,
    config: OpsConfig,
    held_symbols: frozenset[str] = frozenset(),
    free_slots: int | None = None,
    excluded_symbols: frozenset[str] = frozenset(),
    momentum_leaders: list[MomentumHit] | None = None,
    members_loader=None,
    earnings_finder=None,
    metrics_fetcher=None,
    momentum_finder=None,
) -> list[Candidate]:
    members_loader = members_loader or load_sp500_members
    momentum_finder = momentum_finder or find_momentum_leaders

    # 1. Earnings sleeve (existing path, alphabetically sorted).
    earnings_candidates = build_universe(
        asof_date=asof_date, config=config,
        members_loader=members_loader, earnings_finder=earnings_finder,
        metrics_fetcher=metrics_fetcher,
    )

    # 2. Momentum sleeve. The leaderboard may be precomputed by the caller
    #    (the tick computes it once for both this builder and the exit
    #    engine); otherwise compute it here.
    if momentum_leaders is None:
        eligible = apply_deny_list(members_loader(), config.deny_list)
        momentum_leaders = momentum_finder(eligible, asof_date=asof_date)

    # 3. Shared liquidity filter, fed from data already on the hits —
    #    reuses the filter logic with zero extra I/O.
    hits_by_sym = {h.symbol: h for h in momentum_leaders}
    liquid = apply_liquidity_filter(
        [h.symbol for h in momentum_leaders],
        min_adv=_MIN_ADV, min_price=_MIN_PRICE,
        fetch_metrics=lambda s: (hits_by_sym[s].close,
                                 hits_by_sym[s].avg_dollar_volume_20d),
    )

    # 4. Merge + dedup: earnings wins on overlap and keeps both payloads.
    ineligible = held_symbols | excluded_symbols
    merged: list[Candidate] = []
    earnings_syms = set()
    for cand in earnings_candidates:
        if cand.symbol in ineligible:
            continue
        hit = hits_by_sym.get(cand.symbol)
        if hit is not None:
            cand = dataclasses.replace(cand, momentum=hit)
        merged.append(cand)
        earnings_syms.add(cand.symbol)
    for sym, price, adv in liquid:  # already in leaderboard (rank) order
        if sym in ineligible or sym in earnings_syms:
            continue
        merged.append(Candidate(
            symbol=sym, source=CandidateSource.MOMENTUM,
            last_price=price, avg_dollar_volume_20d=adv,
            momentum=hits_by_sym[sym],
        ))

    # 5. Slot-aware cap.
    cap = config.daily_analysis_budget
    if free_slots is not None:
        cap = max(0, min(cap, free_slots))
    return merged[:cap]
