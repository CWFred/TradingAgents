"""Point-in-time fundamental screener: funnel stage 2.

Pure module — callers assemble ``NameInputs`` (fundamentals, triggers,
prices) and this decides. Two phases because the EV/EBIT bar is relative:
phase 1 computes every name's EV/EBIT so sector medians exist, phase 2
evaluates bars per name.

Pass rule (design doc): statistically cheap (>=2 of 3 valuation bars) AND
quality (>=2 of 3 quality bars) AND at least one change trigger. A bar with
missing data fails with a "missing:" detail — absence of evidence is never
treated as cheapness or quality.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from statistics import median

from ops.research.triggers import Trigger
from tradingagents.dataflows.fundamentals import Fundamentals

FCF_YIELD_MIN = Decimal("0.06")
ROIC_MIN = Decimal("0.12")
DEBT_EBITDA_MAX = Decimal("3")
GROSS_MARGIN_BAND_MAX = Decimal("0.10")
MIN_HISTORY_YEARS = 3
MIN_SECTOR_PEERS = 5

_ZERO = Decimal("0")


@dataclass(frozen=True)
class NameInputs:
    symbol: str
    sector: str
    price: Decimal        # asof close, in the latest reported fiscal year's share basis
    market_cap: Decimal   # snapshot cap rescaled to `price` by the caller
    fundamentals: Fundamentals
    triggers: tuple[Trigger, ...]
    year_end_prices: dict[date, Decimal]  # fiscal year end -> close


@dataclass(frozen=True)
class Bar:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ScreenResult:
    symbol: str
    asof: date
    passed: bool
    cheap: bool
    quality: bool
    valuation_bars: tuple[Bar, ...]
    quality_bars: tuple[Bar, ...]
    triggers: tuple[Trigger, ...]
    market_cap: Decimal
    ev_ebit: Decimal | None


def _ev_ebit(inputs: NameInputs) -> Decimal | None:
    f = inputs.fundamentals
    if f.ebit is None or f.ebit <= _ZERO or f.total_debt is None:
        return None
    ev = inputs.market_cap + f.total_debt - (f.cash or _ZERO)
    if ev <= _ZERO:
        return None
    return ev / f.ebit


def _ev_ebit_bar(ev_ebit: Decimal | None, benchmark: Decimal | None, label: str) -> Bar:
    name = "ev_ebit_vs_sector"
    if ev_ebit is None:
        return Bar(name, False, "missing: EV/EBIT not computable (no EBIT or balance sheet)")
    if benchmark is None:
        return Bar(name, False, "missing: no peer median available")
    passed = ev_ebit < benchmark
    return Bar(name, passed, f"EV/EBIT {ev_ebit:.1f} vs {label} median {benchmark:.1f}")


def _fcf_yield_bar(inputs: NameInputs) -> Bar:
    name = "fcf_yield"
    f = inputs.fundamentals
    if f.fcf is None or inputs.market_cap <= _ZERO:
        return Bar(name, False, "missing: no FCF (needs both CFO and capex)")
    yld = f.fcf / inputs.market_cap
    return Bar(name, yld > FCF_YIELD_MIN, f"FCF yield {(yld * 100):.1f}% vs {FCF_YIELD_MIN * 100}%")


def _pe_history_bar(inputs: NameInputs) -> Bar:
    name = "pe_vs_own_history"
    eps = inputs.fundamentals.eps_history
    if not eps or eps[-1].value <= _ZERO:
        return Bar(name, False, "missing: no positive current EPS")
    current_pe = inputs.price / eps[-1].value
    historical: list[Decimal] = []
    for yv in eps:
        px = inputs.year_end_prices.get(yv.fiscal_year_end)
        if px is not None and yv.value > _ZERO:
            historical.append(px / yv.value)
    if len(historical) < MIN_HISTORY_YEARS:
        return Bar(name, False, f"missing: only {len(historical)} usable historical P/E years")
    med = median(historical)
    return Bar(name, current_pe < med, f"P/E {current_pe:.1f} vs own 5y median {med:.1f}")


def _roic_bar(inputs: NameInputs) -> Bar:
    name = "roic_5y"
    hist = inputs.fundamentals.roic_history
    if len(hist) < MIN_HISTORY_YEARS:
        return Bar(name, False, f"missing: only {len(hist)} ROIC years")
    avg = sum(yv.value for yv in hist) / Decimal(len(hist))
    return Bar(name, avg > ROIC_MIN, f"mean ROIC {(avg * 100):.1f}% vs {ROIC_MIN * 100}%")


def _debt_ebitda_bar(inputs: NameInputs) -> Bar:
    name = "debt_to_ebitda"
    f = inputs.fundamentals
    if f.ebitda is None or f.ebitda <= _ZERO or f.total_debt is None:
        return Bar(name, False, "missing: no positive EBITDA or no balance sheet")
    ratio = f.total_debt / f.ebitda
    return Bar(name, ratio < DEBT_EBITDA_MAX, f"debt/EBITDA {ratio:.2f} vs {DEBT_EBITDA_MAX}")


def _gross_margin_bar(inputs: NameInputs) -> Bar:
    name = "gross_margin_stability"
    hist = inputs.fundamentals.gross_margin_history
    if len(hist) < MIN_HISTORY_YEARS:
        return Bar(name, False, f"missing: only {len(hist)} gross-margin years")
    values = [yv.value for yv in hist]
    band = max(values) - min(values)
    return Bar(
        name, band <= GROSS_MARGIN_BAND_MAX,
        f"gross-margin band {(band * 100):.1f}pp vs {GROSS_MARGIN_BAND_MAX * 100}pp",
    )


def screen_universe(inputs: list[NameInputs], *, asof: date) -> list[ScreenResult]:
    ev_ebit_by_symbol = {n.symbol: _ev_ebit(n) for n in inputs}
    valid = [
        (n.sector, v)
        for n, v in zip(inputs, ev_ebit_by_symbol.values(), strict=True)
        if v is not None
    ]
    by_sector: dict[str, list[Decimal]] = {}
    for sector, v in valid:
        by_sector.setdefault(sector, []).append(v)

    results: list[ScreenResult] = []
    for n in inputs:
        own = ev_ebit_by_symbol[n.symbol]
        # Identity (not equality) removal: drops exactly the candidate's own
        # entry, never an equal-valued peer. `own is None` drops nothing (None
        # values were never added to by_sector/valid), which is correct.
        peers = [v for v in by_sector.get(n.sector, []) if v is not own]
        if len(peers) >= MIN_SECTOR_PEERS:
            benchmark, label = median(peers), n.sector
        else:
            universe_peers = [v for _, v in valid if v is not own]
            benchmark = median(universe_peers) if universe_peers else None
            label = "universe"
        valuation = (
            _ev_ebit_bar(ev_ebit_by_symbol[n.symbol], benchmark, label),
            _fcf_yield_bar(n),
            _pe_history_bar(n),
        )
        quality = (_roic_bar(n), _debt_ebitda_bar(n), _gross_margin_bar(n))
        cheap = sum(b.passed for b in valuation) >= 2
        is_quality = sum(b.passed for b in quality) >= 2
        results.append(ScreenResult(
            symbol=n.symbol,
            asof=asof,
            passed=cheap and is_quality and len(n.triggers) >= 1,
            cheap=cheap,
            quality=is_quality,
            valuation_bars=valuation,
            quality_bars=quality,
            triggers=n.triggers,
            market_cap=n.market_cap,
            ev_ebit=ev_ebit_by_symbol[n.symbol],
        ))
    return results
