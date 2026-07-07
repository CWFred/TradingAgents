"""Falsifier metric evaluation — the mechanical half of the monitoring loop.

The memo schema's ``Falsifier.metric`` is a free-text name; this module is
the registry that makes those names mean something. Everything here is pure
and stateless: ``consecutive_periods`` is evaluated against the observation
HISTORY (last N trading days for price metrics, last N fiscal years for
fundamental ones), never against persisted counter state — the journal is
the only state store in ops, and it doesn't need to be involved here.

Metric values are floats: monitoring/calibration data, not money.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ops.research.prices import PriceContext
from tradingagents.dataflows.edgar_facts import annual_series
from tradingagents.dataflows.fundamentals import REVENUE_CONCEPTS, Fundamentals

SUPPORTED_METRICS = frozenset({
    "drawdown_from_cost_pct",
    "gross_margin_pct",
    "revenue_yoy_pct",
    "net_debt_to_ebitda",
})

_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


@dataclass(frozen=True)
class MetricContext:
    """Everything a metric evaluator may draw on. Fetching happens upstream
    (the monitor decides what to fetch per memo); evaluators only read."""

    entry_price_ref: float
    asof: date
    entry_era: date  # memo.as_of_date: the share-count era of entry_price_ref
    price_ctx: PriceContext | None = None
    fundamentals: Fundamentals | None = None
    facts: dict | None = None


@dataclass(frozen=True)
class FalsifierCheck:
    status: str  # "tripped" | "ok" | "unevaluable"
    observed: float | None
    detail: str


def _drawdown_series(ctx: MetricContext) -> list[float] | None:
    if ctx.price_ctx is None or ctx.entry_price_ref <= 0:
        return None
    closes = ctx.price_ctx.recent_closes(asof=ctx.asof, days=60)  # oldest-first
    if not closes:
        return None
    # Yahoo closes are split-adjusted to TODAY's share count; entry_price_ref
    # was quoted in the entry era's. Undo every split after the entry era so
    # the comparison is apples-to-apples (Phase A split machinery).
    factor = ctx.price_ctx.split_factor_after(ctx.entry_era)
    entry = ctx.entry_price_ref
    return [
        (float(close * factor) - entry) / entry * 100.0
        for close in reversed(closes)  # most-recent-first
    ]


def _gross_margin_series(ctx: MetricContext) -> list[float] | None:
    f = ctx.fundamentals
    if f is None or not f.gross_margin_history:
        return None
    ordered = sorted(f.gross_margin_history, key=lambda yv: yv.fiscal_year_end,
                     reverse=True)
    return [float(yv.value) * 100.0 for yv in ordered]


def _revenue_yoy_series(ctx: MetricContext) -> list[float] | None:
    if ctx.facts is None:
        return None
    points = annual_series(ctx.facts, REVENUE_CONCEPTS, asof=ctx.asof, max_years=6)
    by_year = sorted(points, key=lambda p: p.end)  # oldest-first
    if len(by_year) < 2:
        return None
    yoy = []
    for prev, cur in zip(by_year, by_year[1:], strict=False):
        if prev.value == 0:
            continue
        yoy.append((float(cur.value) / float(prev.value) - 1.0) * 100.0)
    return list(reversed(yoy)) or None  # most-recent-first


def _net_debt_to_ebitda(ctx: MetricContext) -> list[float] | None:
    f = ctx.fundamentals
    if f is None or f.total_debt is None or f.ebitda is None or f.ebitda <= 0:
        return None
    cash = f.cash if f.cash is not None else 0
    return [float((f.total_debt - cash) / f.ebitda)]


_EVALUATORS = {
    "drawdown_from_cost_pct": _drawdown_series,
    "gross_margin_pct": _gross_margin_series,
    "revenue_yoy_pct": _revenue_yoy_series,
    "net_debt_to_ebitda": _net_debt_to_ebitda,
}


def observations(metric: str, ctx: MetricContext) -> list[float] | None:
    """Most-recent-first observation series for a metric; None = unevaluable."""
    evaluator = _EVALUATORS.get(metric)
    return evaluator(ctx) if evaluator else None


def drawdown_pct(ctx: MetricContext) -> float | None:
    """Latest drawdown vs entry_price_ref — the implicit escalation check."""
    series = _drawdown_series(ctx)
    return series[0] if series else None


def evaluate_falsifier(falsifier, ctx: MetricContext) -> FalsifierCheck:
    """Trip iff the most recent ``consecutive_periods`` observations ALL
    satisfy ``value OP threshold``. Anything unanswerable is 'unevaluable' —
    honest uncertainty, surfaced in the run summary, never a silent pass."""
    if not falsifier.metric or falsifier.operator is None or falsifier.threshold is None:
        return FalsifierCheck("unevaluable", None, "not machine-checkable")
    series = observations(falsifier.metric, ctx)
    if series is None:
        return FalsifierCheck(
            "unevaluable", None,
            f"metric {falsifier.metric!r} not evaluable (unknown or inputs missing)",
        )
    need = falsifier.consecutive_periods
    if len(series) < need:
        return FalsifierCheck(
            "unevaluable", series[0],
            f"only {len(series)} observation(s), need {need}",
        )
    op = _OPS[falsifier.operator]
    window = series[:need]
    tripped = all(op(v, falsifier.threshold) for v in window)
    detail = (
        f"{falsifier.metric} {falsifier.operator} {falsifier.threshold}: "
        f"last {need} = {[round(v, 2) for v in window]}"
    )
    return FalsifierCheck("tripped" if tripped else "ok", series[0], detail)
