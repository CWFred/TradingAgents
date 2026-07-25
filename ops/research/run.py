"""Composition root for a screen run: universe -> screen -> store -> baseline.

Per-name failures (SEC map misses, vendor errors, missing prices) are logged
to stderr and skipped — a sweep over ~1500 names must never die on name #937.
Every stage is injectable so tests run with zero network.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, timedelta

from ops.broker.paper import PaperBroker
from ops.config import OpsConfig
from ops.journal import Journal
from ops.quotes import make_yfinance_quote_source
from ops.research.baseline import auto_write_off_delisted, update_baseline_portfolio
from ops.research.prices import PriceContext, fetch_price_context
from ops.research.screener import NameInputs, ScreenResult, screen_universe
from ops.research.store import ScreenStore
from ops.research.triggers import (
    SELLOFF_LOOKBACK_DAYS,
    TRIGGER_LOOKBACK_DAYS,
    find_selloff_trigger,
    find_triggers,
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


@dataclass(frozen=True)
class _NameBase:
    """The shared per-name fetch layer: everything a screen needs EXCEPT
    triggers — the long and short screens derive different trigger sets
    from the same underlying data, so trigger derivation is the caller's
    job (see run_screens, which fetches filings/Form 4s once per name)."""

    ctx: PriceContext
    price: object            # Decimal, in the latest fiscal year's share basis
    market_cap: object       # Decimal, rescaled to the as-of price
    fundamentals: object
    year_end_prices: dict


def _name_base(
    name: UniverseName, *, asof: date, facts_fetcher, price_context_fetcher,
) -> _NameBase | None:
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
    year_end_prices = {
        yv.fiscal_year_end: px
        for yv in fundamentals.eps_history
        if (px := ctx.unadjusted_close_on_or_before(yv.fiscal_year_end)) is not None
    }
    return _NameBase(ctx=ctx, price=price, market_cap=market_cap,
                     fundamentals=fundamentals, year_end_prices=year_end_prices)


def _with_selloff(triggers: list, base: _NameBase, symbol: str, asof: date) -> list:
    selloff = find_selloff_trigger(
        symbol, base.ctx.recent_closes(asof=asof, days=SELLOFF_LOOKBACK_DAYS),
        asof=asof,
    )
    if selloff is not None:
        triggers.append(selloff)
    return triggers


def _name_inputs(
    name: UniverseName,
    *,
    asof: date,
    facts_fetcher,
    triggers_finder,
    price_context_fetcher,
) -> NameInputs | None:
    symbol = name.member.symbol
    base = _name_base(name, asof=asof, facts_fetcher=facts_fetcher,
                      price_context_fetcher=price_context_fetcher)
    if base is None:
        return None
    triggers = _with_selloff(list(triggers_finder(symbol, asof=asof)),
                             base, symbol, asof)
    return NameInputs(
        symbol=symbol,
        sector=name.member.sector,
        price=base.price,
        market_cap=base.market_cap,
        fundamentals=base.fundamentals,
        triggers=tuple(triggers),
        year_end_prices=base.year_end_prices,
    )


def _build_screen_inputs(
    universe, *, asof, facts_fetcher, triggers_finder, price_context_fetcher,
) -> tuple[list[NameInputs], list[str]]:
    """Per-name input assembly shared by the long and short screens. A
    sweep over ~1500 names must never die on a single name."""
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
    return inputs, errors


def screen_inputs_and_results(
    universe, *, asof, facts_fetcher, triggers_finder, price_context_fetcher,
) -> tuple[tuple[NameInputs, ScreenResult], ...]:
    """Public PIT screen returning (NameInputs, ScreenResult) pairs, without
    touching the screen store. Shared by the live screen and the backtest
    historical case source so both use identical input assembly.

    screen_universe returns results in input order; strict=True fails loudly
    if that invariant ever changes.
    """
    inputs, _errors = _build_screen_inputs(
        universe, asof=asof, facts_fetcher=facts_fetcher,
        triggers_finder=triggers_finder,
        price_context_fetcher=price_context_fetcher,
    )
    results = screen_universe(inputs, asof=asof)
    return tuple(zip(inputs, results, strict=True))


def run_short_screen(
    *,
    config: OpsConfig,
    asof: date,
    dry_run: bool = False,
    limit: int | None = None,
    universe_builder=None,
    facts_fetcher=None,
    triggers_finder=None,
    price_context_fetcher=None,
) -> ScreenRunSummary:
    """Universe -> inverted short screen -> short screen store.

    Mirror of run_screen with three deltas: triggers come from
    find_short_triggers (red flags, not change triggers), results from
    screen_short_universe, and hits land in the SHORT screen store. No
    baseline leg — the short sleeve's null is holding cash.
    """
    from ops.research.short_screen import screen_short_universe
    from ops.research.short_triggers import find_short_triggers

    universe_builder = universe_builder or build_smallcap_universe
    if facts_fetcher is None:
        from tradingagents.dataflows import edgar

        edgar.get_user_agent()  # fail fast, same as run_screen
        facts_fetcher = get_company_facts
    triggers_finder = triggers_finder or find_short_triggers
    price_context_fetcher = price_context_fetcher or fetch_price_context

    universe = universe_builder()
    if limit is not None:
        universe = universe[:limit]

    inputs, errors = _build_screen_inputs(
        universe, asof=asof, facts_fetcher=facts_fetcher,
        triggers_finder=triggers_finder,
        price_context_fetcher=price_context_fetcher,
    )

    results = screen_short_universe(inputs, asof=asof)
    passed = tuple(r.symbol for r in results if r.passed)

    coverage: dict[str, dict[str, int]] = {}
    for result in results:
        for bar in result.bars:
            slot = coverage.setdefault(bar.name, {"computed": 0, "missing": 0})
            slot["missing" if bar.detail.startswith("missing:") else "computed"] += 1

    run_id = None
    if not dry_run:
        store = ScreenStore(config.short_screen_store_path)
        run_id = store.record_run(
            asof=asof, universe_size=len(universe), results=results,
            coverage=coverage, ttl_days=config.research_screen_ttl_days,
        )

    return ScreenRunSummary(
        run_id=run_id,
        asof=asof,
        universe_size=len(universe),
        screened=len(inputs),
        passed=passed,
        errors=tuple(errors),
        baseline=None,
        coverage=coverage,
    )


def _update_baseline(config, *, passed, asof, errors, quote_source=None):
    """The null-baseline leg of a screen run. The control must never take
    down a screen run: any failure is logged, appended to errors, and
    swallowed."""
    try:
        with Journal(config.baseline_journal_path) as baseline_journal:
            qs = quote_source or make_yfinance_quote_source()
            writeoffs = auto_write_off_delisted(
                journal=baseline_journal, quote_source=qs,
                starting_cash=config.baseline_starting_cash, asof=asof,
            )
            broker = PaperBroker.from_journal(
                journal=baseline_journal,
                quote_source=qs,
                starting_cash=config.baseline_starting_cash,
            )
            baseline_summary = update_baseline_portfolio(
                broker=broker, journal=baseline_journal,
                passers=list(passed), asof=asof,
            )
            baseline_summary["writeoffs"] = [w["symbol"] for w in writeoffs]
            return baseline_summary
    except Exception as exc:
        msg = f"baseline: {type(exc).__name__}: {exc}"
        print(f"[screen] {msg}", file=sys.stderr)
        errors.append(msg)
        return None


def run_screens(
    *,
    config: OpsConfig,
    asof: date,
    dry_run: bool = False,
    limit: int | None = None,
    universe_builder=None,
    facts_fetcher=None,
    price_context_fetcher=None,
    list_filings=None,
    transactions_fetcher=None,
    full_text_search=None,
    fetch_text=None,
    cik_resolver=None,
    quote_source=None,
) -> tuple[ScreenRunSummary, ScreenRunSummary]:
    """One universe sweep, BOTH screens — the nightly entry point.

    Every per-name SEC fetch happens exactly once and feeds both trigger
    derivations: company facts, the price context, the submissions listing
    (fetched once without a forms filter and re-filtered locally for each
    trigger finder), and the Form 4 XMLs (one get_insider_transactions pass
    serves the long buy-cluster AND the short sell-cluster checks). Running
    run_screen + run_short_screen separately doubles a multi-hour throttled
    sweep on exactly the nights the overnight window has the most work
    (review finding P2). The going-concern full-text search remains the one
    short-only extra call per name.

    Returns (long_summary, short_summary). The baseline leg rides the long
    passers, exactly as run_screen does.
    """
    from ops.research.short_screen import screen_short_universe
    from ops.research.short_triggers import find_short_triggers
    from tradingagents.dataflows import edgar
    from tradingagents.dataflows.form4 import get_insider_transactions

    universe_builder = universe_builder or build_smallcap_universe
    if facts_fetcher is None:
        edgar.get_user_agent()  # fail fast, same as run_screen
        facts_fetcher = get_company_facts
    price_context_fetcher = price_context_fetcher or fetch_price_context
    list_filings_fn = list_filings or edgar.list_filings
    full_text_search = full_text_search or edgar.full_text_search
    fetch_text = fetch_text or edgar.fetch_filing_text
    cik_resolver = cik_resolver or edgar.get_cik

    universe = universe_builder()
    if limit is not None:
        universe = universe[:limit]
    since = asof - timedelta(days=TRIGGER_LOOKBACK_DAYS)

    long_inputs: list[NameInputs] = []
    short_inputs: list[NameInputs] = []
    errors: list[str] = []
    for name in universe:
        symbol = name.member.symbol
        try:
            base = _name_base(name, asof=asof, facts_fetcher=facts_fetcher,
                              price_context_fetcher=price_context_fetcher)
            if base is None:
                msg = f"{symbol}: skipped (no price history or no close at asof)"
                print(f"[screen] skipped {msg}", file=sys.stderr)
                errors.append(msg)
                continue

            # ONE submissions fetch per name; both trigger finders filter it
            # locally through this wrapper instead of re-fetching.
            filings = list_filings_fn(symbol, since=since, limit=200)

            def cached_filings(t, *, forms=None, since=None, limit=100,
                               _filings=filings):
                out = [
                    f for f in _filings
                    if (forms is None or f.form in forms)
                    and (since is None or f.filing_date is None
                         or f.filing_date >= since)
                ]
                return out[:limit]

            # ONE Form 4 XML pass per name; buy- and sell-cluster checks
            # both read from it.
            if transactions_fetcher is not None:
                txns = transactions_fetcher(symbol, since=since)
            else:
                txns = get_insider_transactions(
                    symbol, since=since, list_filings=cached_filings,
                )

            def cached_txns(t, *, since, _txns=txns):
                return [x for x in _txns
                        if x.transaction_date is None
                        or x.transaction_date >= since]

            long_triggers = _with_selloff(
                list(find_triggers(
                    symbol, asof=asof, list_filings=cached_filings,
                    transactions_fetcher=cached_txns,
                )),
                base, symbol, asof,
            )
            short_triggers = find_short_triggers(
                symbol, asof=asof, list_filings=cached_filings,
                transactions_fetcher=cached_txns,
                full_text_search=full_text_search, fetch_text=fetch_text,
                cik_resolver=cik_resolver,
            )
        except Exception as exc:  # a sweep must survive any single name
            msg = f"{symbol}: {type(exc).__name__}: {exc}"
            print(f"[screen] skipped {msg}", file=sys.stderr)
            errors.append(msg)
            continue

        common = dict(
            symbol=symbol, sector=name.member.sector, price=base.price,
            market_cap=base.market_cap, fundamentals=base.fundamentals,
            year_end_prices=base.year_end_prices,
        )
        long_inputs.append(NameInputs(triggers=tuple(long_triggers), **common))
        short_inputs.append(NameInputs(triggers=tuple(short_triggers), **common))

    long_results = screen_universe(long_inputs, asof=asof)
    short_results = screen_short_universe(short_inputs, asof=asof)
    long_passed = tuple(r.symbol for r in long_results if r.passed)
    short_passed = tuple(r.symbol for r in short_results if r.passed)

    long_coverage: dict[str, dict[str, int]] = {}
    for result in long_results:
        for bar in (*result.valuation_bars, *result.quality_bars):
            slot = long_coverage.setdefault(bar.name, {"computed": 0, "missing": 0})
            slot["missing" if bar.detail.startswith("missing:") else "computed"] += 1
    short_coverage: dict[str, dict[str, int]] = {}
    for result in short_results:
        for bar in result.bars:
            slot = short_coverage.setdefault(bar.name, {"computed": 0, "missing": 0})
            slot["missing" if bar.detail.startswith("missing:") else "computed"] += 1

    long_run_id = short_run_id = None
    baseline_summary = None
    if not dry_run:
        long_run_id = ScreenStore(config.screen_store_path).record_run(
            asof=asof, universe_size=len(universe), results=long_results,
            coverage=long_coverage, ttl_days=config.research_screen_ttl_days,
        )
        short_run_id = ScreenStore(config.short_screen_store_path).record_run(
            asof=asof, universe_size=len(universe), results=short_results,
            coverage=short_coverage, ttl_days=config.research_screen_ttl_days,
        )
        baseline_summary = _update_baseline(
            config, passed=long_passed, asof=asof, errors=errors,
            quote_source=quote_source,
        )

    long_summary = ScreenRunSummary(
        run_id=long_run_id, asof=asof, universe_size=len(universe),
        screened=len(long_inputs), passed=long_passed, errors=tuple(errors),
        baseline=baseline_summary, coverage=long_coverage,
    )
    short_summary = ScreenRunSummary(
        run_id=short_run_id, asof=asof, universe_size=len(universe),
        screened=len(short_inputs), passed=short_passed, errors=tuple(errors),
        baseline=None, coverage=short_coverage,
    )
    return long_summary, short_summary


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
    triggers_finder = triggers_finder or find_triggers
    price_context_fetcher = price_context_fetcher or fetch_price_context

    universe = universe_builder()
    if limit is not None:
        universe = universe[:limit]

    inputs, errors = _build_screen_inputs(
        universe, asof=asof, facts_fetcher=facts_fetcher,
        triggers_finder=triggers_finder,
        price_context_fetcher=price_context_fetcher,
    )

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
            coverage=coverage, ttl_days=config.research_screen_ttl_days,
        )
        baseline_summary = _update_baseline(
            config, passed=passed, asof=asof, errors=errors,
            quote_source=quote_source,
        )

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
