"""Composition root for a screen run: universe -> screen -> store -> baseline.

Per-name failures (SEC map misses, vendor errors, missing prices) are logged
to stderr and skipped — a sweep over ~1500 names must never die on name #937.
Every stage is injectable so tests run with zero network.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date

from ops.broker.paper import PaperBroker
from ops.config import OpsConfig
from ops.journal import Journal
from ops.quotes import make_yfinance_quote_source
from ops.research.baseline import update_baseline_portfolio
from ops.research.prices import PriceContext, fetch_price_context
from ops.research.screener import NameInputs, screen_universe
from ops.research.store import ScreenStore
from ops.research.triggers import (
    SELLOFF_LOOKBACK_DAYS,
    find_edgar_triggers,
    find_selloff_trigger,
)
from ops.universe.smallcap import UniverseName, build_smallcap_universe
from tradingagents.dataflows.edgar_facts import get_company_facts
from tradingagents.dataflows.fundamentals import compute_fundamentals


@dataclass(frozen=True)
class ScreenRunSummary:
    run_id: str | None
    asof: date
    universe_size: int
    screened: int
    passed: tuple[str, ...]
    errors: tuple[str, ...]
    baseline: dict | None
    coverage: dict[str, dict[str, int]]


def _name_inputs(
    name: UniverseName,
    *,
    asof: date,
    facts_fetcher,
    triggers_finder,
    price_context_fetcher,
) -> NameInputs | None:
    symbol = name.member.symbol
    ctx: PriceContext | None = price_context_fetcher(symbol)
    if ctx is None:
        return None
    price = ctx.close_on_or_before(asof)
    if price is None or name.member.last_price <= 0:
        return None
    # Rescale the snapshot market cap to the as-of price: shares from the
    # snapshot, price from now — keeps cheapness bars honest between the
    # quarterly universe refresh and a weekly screen run. Both legs must be
    # in the snapshot's share basis (era_end) or a split in between scales
    # the cap by the split ratio.
    asof_price_snapshot_era = ctx.unadjusted_close_on_or_before(
        asof, era_end=name.snapshot_at,
    )
    market_cap = (
        name.member.market_cap * asof_price_snapshot_era / name.member.last_price
    )
    facts = facts_fetcher(symbol)
    fundamentals = compute_fundamentals(symbol, facts, asof=asof)
    # The current-P/E leg divides by as-reported EPS (the latest fiscal
    # year's share basis), so its price must be in that era too — a split
    # after the fiscal year end otherwise understates current P/E by the
    # split ratio while the historical median stays correct.
    if fundamentals.eps_history:
        price = ctx.unadjusted_close_on_or_before(
            asof, era_end=fundamentals.eps_history[-1].fiscal_year_end,
        )
    triggers = list(triggers_finder(symbol, asof=asof))
    selloff = find_selloff_trigger(
        symbol, ctx.recent_closes(asof=asof, days=SELLOFF_LOOKBACK_DAYS), asof=asof,
    )
    if selloff is not None:
        triggers.append(selloff)
    year_end_prices = {
        yv.fiscal_year_end: px
        for yv in fundamentals.eps_history
        if (px := ctx.unadjusted_close_on_or_before(yv.fiscal_year_end)) is not None
    }
    return NameInputs(
        symbol=symbol,
        sector=name.member.sector,
        price=price,
        market_cap=market_cap,
        fundamentals=fundamentals,
        triggers=tuple(triggers),
        year_end_prices=year_end_prices,
    )


def run_screen(
    *,
    config: OpsConfig,
    asof: date,
    dry_run: bool = False,
    limit: int | None = None,
    universe_builder=None,
    facts_fetcher=None,
    triggers_finder=None,
    price_context_fetcher=None,
    quote_source=None,
) -> ScreenRunSummary:
    universe_builder = universe_builder or build_smallcap_universe
    if facts_fetcher is None:
        # Fail fast: without the SEC user agent every name in the sweep
        # would raise EdgarNotConfiguredError individually and be swallowed
        # by the per-name skip, recording a junk all-errors run.
        from tradingagents.dataflows import edgar

        edgar.get_user_agent()
        facts_fetcher = get_company_facts
    triggers_finder = triggers_finder or find_edgar_triggers
    price_context_fetcher = price_context_fetcher or fetch_price_context

    universe = universe_builder()
    if limit is not None:
        universe = universe[:limit]

    inputs: list[NameInputs] = []
    errors: list[str] = []
    for name in universe:
        symbol = name.member.symbol
        try:
            ni = _name_inputs(
                name, asof=asof, facts_fetcher=facts_fetcher,
                triggers_finder=triggers_finder,
                price_context_fetcher=price_context_fetcher,
            )
        except Exception as exc:  # a sweep must survive any single name
            msg = f"{symbol}: {type(exc).__name__}: {exc}"
            print(f"[screen] skipped {msg}", file=sys.stderr)
            errors.append(msg)
            continue
        if ni is not None:
            inputs.append(ni)
        else:
            msg = f"{symbol}: skipped (no price history or no close at asof)"
            print(f"[screen] skipped {msg}", file=sys.stderr)
            errors.append(msg)

    results = screen_universe(inputs, asof=asof)
    passed = tuple(r.symbol for r in results if r.passed)

    coverage: dict[str, dict[str, int]] = {}
    for result in results:
        for bar in (*result.valuation_bars, *result.quality_bars):
            slot = coverage.setdefault(bar.name, {"computed": 0, "missing": 0})
            slot["missing" if bar.detail.startswith("missing:") else "computed"] += 1

    run_id = None
    baseline_summary = None
    if not dry_run:
        store = ScreenStore(config.screen_store_path)
        run_id = store.record_run(
            asof=asof, universe_size=len(universe), results=results,
            coverage=coverage,
        )
        try:
            with Journal(config.baseline_journal_path) as baseline_journal:
                broker = PaperBroker.from_journal(
                    journal=baseline_journal,
                    quote_source=quote_source or make_yfinance_quote_source(),
                    starting_cash=config.baseline_starting_cash,
                )
                baseline_summary = update_baseline_portfolio(
                    broker=broker, journal=baseline_journal,
                    passers=list(passed), asof=asof,
                )
        except Exception as exc:  # the control must never take down a screen run
            msg = f"baseline: {type(exc).__name__}: {exc}"
            print(f"[screen] {msg}", file=sys.stderr)
            errors.append(msg)
            baseline_summary = None

    return ScreenRunSummary(
        run_id=run_id,
        asof=asof,
        universe_size=len(universe),
        screened=len(inputs),
        passed=passed,
        errors=tuple(errors),
        baseline=baseline_summary,
        coverage=coverage,
    )
