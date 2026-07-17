"""Deterministic triage and learning reports over one row per case."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from statistics import median

from ops.backtest.models import (
    CaseResult,
    HorizonOutcome,
    OutcomeLabel,
    OutcomeState,
)


@dataclass(frozen=True)
class ReportCase:
    case_id: str
    symbol: str
    conviction: str
    result: CaseResult
    outcomes: tuple[HorizonOutcome, ...]
    price_status: str = "ready"

    def primary(self) -> HorizonOutcome:
        matches = [
            item for item in self.outcomes
            if item.horizon_sessions == self.result.primary_horizon
        ]
        if len(matches) != 1:
            raise ValueError(
                f"case {self.case_id}: expected one primary-horizon outcome"
            )
        return matches[0]


@dataclass(frozen=True)
class CalibrationBucket:
    conviction: str
    case_count: int
    mature_count: int
    hit_rate: Decimal | None
    mean_excess: Decimal | None
    mean_utility: Decimal | None


@dataclass(frozen=True)
class FalsifierFiring:
    name: str
    session: date
    avoided_loss: bool | None = None
    status: str = "tripped"


@dataclass(frozen=True)
class FalsifierCase:
    case_id: str
    names: tuple[str, ...]
    losing: bool
    damage_session: date | None = None
    firings: tuple[FalsifierFiring, ...] = ()


@dataclass(frozen=True)
class FalsifierScore:
    name: str
    losing_cases: int
    before_damage: int
    after_damage: int
    never_fired: int
    true_saves: int
    false_alarms: int
    ungraded_firings: int
    unevaluable_observations: int


@dataclass(frozen=True)
class TriageSummary:
    verdict: str
    verdict_evidence: str
    total_cases: int
    mature_cases: int
    pending_cases: int
    unpriceable_cases: int
    stale_cases: int
    terminal_cases: int
    wins: int
    washes: int
    losses: int
    hit_rate: Decimal | None
    mean_excess: Decimal | None
    median_excess: Decimal | None
    mean_utility: Decimal | None
    mean_actual_return: Decimal | None
    worst_drawdown: Decimal | None


@dataclass(frozen=True)
class BacktestReport:
    run_id: str
    rows: tuple[ReportCase, ...]
    summary: TriageSummary
    calibration: tuple[CalibrationBucket, ...]
    calibration_warning: str | None
    falsifiers: tuple[FalsifierScore, ...]
    quadrant_counts: Mapping[str, int]
    metadata: Mapping[str, object]


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal("0")) / Decimal(len(values)) if values else None


def _mature(rows: Sequence[ReportCase]) -> list[tuple[ReportCase, HorizonOutcome]]:
    return [
        (row, primary)
        for row in rows
        if (primary := row.primary()).state == OutcomeState.MATURE
    ]


def build_calibration(rows: Sequence[ReportCase]) -> tuple[CalibrationBucket, ...]:
    by_tier: dict[str, list[ReportCase]] = {}
    for row in rows:
        by_tier.setdefault(row.conviction or "unrated", []).append(row)
    buckets: list[CalibrationBucket] = []
    for tier in sorted(by_tier):
        cases = by_tier[tier]
        mature = [row.primary() for row in cases if row.primary().state == OutcomeState.MATURE]
        decisive = [item for item in mature if item.label != OutcomeLabel.WASH]
        hit_rate = (
            Decimal(sum(item.label == OutcomeLabel.WIN for item in decisive))
            / Decimal(len(decisive))
            if decisive else None
        )
        buckets.append(CalibrationBucket(
            conviction=tier,
            case_count=len(cases),
            mature_count=len(mature),
            hit_rate=hit_rate,
            mean_excess=_mean([item.excess_return for item in mature if item.excess_return is not None]),
            mean_utility=_mean([item.utility for item in mature if item.utility is not None]),
        ))
    return tuple(buckets)


def calibration_warning(
    buckets: Sequence[CalibrationBucket],
) -> str | None:
    by_name = {bucket.conviction.lower(): bucket for bucket in buckets}
    high = next((by_name[key] for key in ("tier-1", "tier1", "high") if key in by_name), None)
    low = next((by_name[key] for key in ("tier-3", "tier3", "low") if key in by_name), None)
    if (
        high is not None and low is not None
        and high.mean_excess is not None and low.mean_excess is not None
        and high.mean_excess <= low.mean_excess
    ):
        return (
            f"{high.conviction} does not beat {low.conviction}; "
            "the conviction rating is not calibrated"
        )
    return None


def build_falsifier_scorecard(
    cases: Sequence[FalsifierCase],
) -> tuple[FalsifierScore, ...]:
    counters: dict[str, Counter] = {}
    for case in cases:
        firings: dict[str, list[FalsifierFiring]] = {}
        for firing in case.firings:
            counter = counters.setdefault(firing.name, Counter())
            if firing.status == "unevaluable":
                counter["unevaluable_observations"] += 1
                continue
            if firing.status != "tripped":
                continue
            firings.setdefault(firing.name, []).append(firing)
            if firing.avoided_loss is True:
                counter["true_saves"] += 1
            elif firing.avoided_loss is False:
                counter["false_alarms"] += 1
            else:
                counter["ungraded_firings"] += 1
        for name in case.names:
            counter = counters.setdefault(name, Counter())
            if not case.losing:
                continue
            counter["losing_cases"] += 1
            named = firings.get(name, [])
            if not named:
                counter["never_fired"] += 1
            elif case.damage_session is not None and any(
                firing.session < case.damage_session for firing in named
            ):
                counter["before_damage"] += 1
            else:
                counter["after_damage"] += 1
    return tuple(
        FalsifierScore(
            name=name,
            losing_cases=counter["losing_cases"],
            before_damage=counter["before_damage"],
            after_damage=counter["after_damage"],
            never_fired=counter["never_fired"],
            true_saves=counter["true_saves"],
            false_alarms=counter["false_alarms"],
            ungraded_firings=counter["ungraded_firings"],
            unevaluable_observations=counter["unevaluable_observations"],
        )
        for name, counter in sorted(counters.items())
    )


def build_triage_summary(
    rows: Sequence[ReportCase],
    *,
    min_mature_cases: int,
    promising_min_hit_rate: Decimal,
    promising_min_mean_excess: Decimal,
    dead_max_hit_rate: Decimal,
    dead_max_mean_excess: Decimal,
) -> TriageSummary:
    mature = _mature(rows)
    labels = Counter(item.label for _, item in mature)
    decisive = [item for _, item in mature if item.label != OutcomeLabel.WASH]
    hit_rate = (
        Decimal(labels[OutcomeLabel.WIN]) / Decimal(len(decisive))
        if decisive else None
    )
    excesses = [item.excess_return for _, item in mature if item.excess_return is not None]
    utilities = [item.utility for _, item in mature if item.utility is not None]
    mean_utility = _mean(utilities)
    if len(mature) < min_mature_cases:
        verdict = "insufficient"
        evidence = f"{len(mature)} mature cases; need {min_mature_cases}"
    elif (
        hit_rate is not None and mean_utility is not None
        and hit_rate >= promising_min_hit_rate
        and mean_utility >= promising_min_mean_excess
    ):
        verdict = "promising"
        evidence = "hit rate and mean action-adjusted excess clear promising thresholds"
    elif (
        hit_rate is not None and mean_utility is not None
        and hit_rate <= dead_max_hit_rate
        and mean_utility <= dead_max_mean_excess
    ):
        verdict = "dead"
        evidence = "hit rate and mean action-adjusted excess meet dead thresholds"
    else:
        verdict = "mixed"
        evidence = "evidence falls between promising and dead thresholds"
    actual = [row.result.actual_return for row in rows if row.result.actual_return is not None]
    drawdowns = [row.result.max_drawdown for row in rows if row.result.max_drawdown is not None]
    return TriageSummary(
        verdict=verdict,
        verdict_evidence=evidence,
        total_cases=len(rows),
        mature_cases=len(mature),
        pending_cases=sum(row.primary().state == OutcomeState.PENDING for row in rows),
        unpriceable_cases=sum(
            row.primary().state == OutcomeState.UNPRICEABLE for row in rows
        ),
        stale_cases=sum(row.price_status == "stale" for row in rows),
        terminal_cases=sum(row.price_status == "terminal" for row in rows),
        wins=labels[OutcomeLabel.WIN],
        washes=labels[OutcomeLabel.WASH],
        losses=labels[OutcomeLabel.LOSS],
        hit_rate=hit_rate,
        mean_excess=_mean(excesses),
        median_excess=Decimal(str(median(excesses))) if excesses else None,
        mean_utility=mean_utility,
        mean_actual_return=_mean(actual),
        worst_drawdown=min(drawdowns) if drawdowns else None,
    )


def build_report(
    *,
    run_id: str,
    rows: Sequence[ReportCase],
    falsifier_cases: Sequence[FalsifierCase] = (),
    metadata: Mapping[str, object] | None = None,
    min_mature_cases: int = 20,
    promising_min_hit_rate: Decimal = Decimal("0.55"),
    promising_min_mean_excess: Decimal = Decimal("0.03"),
    dead_max_hit_rate: Decimal = Decimal("0.40"),
    dead_max_mean_excess: Decimal = Decimal("0"),
) -> BacktestReport:
    if not run_id:
        raise ValueError("run_id must not be empty")
    ordered = tuple(sorted(rows, key=lambda row: row.case_id))
    ids = [row.case_id for row in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError("report requires exactly one row per case")
    for row in ordered:
        if row.result.case_id != row.case_id:
            raise ValueError(f"case/result mismatch for {row.case_id}")
        row.primary()
    calibration = build_calibration(ordered)
    return BacktestReport(
        run_id=run_id,
        rows=ordered,
        summary=build_triage_summary(
            ordered,
            min_mature_cases=min_mature_cases,
            promising_min_hit_rate=promising_min_hit_rate,
            promising_min_mean_excess=promising_min_mean_excess,
            dead_max_hit_rate=dead_max_hit_rate,
            dead_max_mean_excess=dead_max_mean_excess,
        ),
        calibration=calibration,
        calibration_warning=calibration_warning(calibration),
        falsifiers=build_falsifier_scorecard(falsifier_cases),
        quadrant_counts=dict(sorted(Counter(
            row.result.quadrant.value for row in ordered
        ).items())),
        metadata=dict(sorted((metadata or {}).items())),
    )


def _percent(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def render_report(report: BacktestReport) -> str:
    """Render stable Markdown; rerendering never needs prices or models."""
    summary = report.summary
    lines = [
        f"# Backtest report: {report.run_id}",
        "",
        f"**Verdict: {summary.verdict.upper()}** — {summary.verdict_evidence}",
        "",
        (
            f"Cases: {summary.total_cases} total, {summary.mature_cases} mature, "
            f"{summary.pending_cases} pending, {summary.unpriceable_cases} unpriceable, "
            f"{summary.stale_cases} stale, {summary.terminal_cases} terminal"
        ),
        (
            f"Primary: {summary.wins} wins / {summary.washes} washes / "
            f"{summary.losses} losses; hit rate {_percent(summary.hit_rate)}; "
            f"mean excess {_percent(summary.mean_excess)}; "
            f"mean utility {_percent(summary.mean_utility)}"
        ),
        (
            f"Actual replay mean {_percent(summary.mean_actual_return)}; "
            f"worst per-case drawdown {_percent(summary.worst_drawdown)}"
        ),
        "",
        "## Cases",
        "",
        "| Case | Symbol | Price state | Action | Conviction | Label | Excess | Utility | Actual | Drawdown |",
        "|---|---|---|---|---|---|---:|---:|---:|---:|",
    ]
    for row in report.rows:
        primary = row.primary()
        lines.append(
            f"| {row.case_id} | {row.symbol} | {row.price_status} | "
            f"{row.result.initial_action.value} | "
            f"{row.conviction or 'unrated'} | {primary.label.value} | "
            f"{_percent(primary.excess_return)} | {_percent(primary.utility)} | "
            f"{_percent(row.result.actual_return)} | {_percent(row.result.max_drawdown)} |"
        )
    lines += ["", "## Conviction calibration", ""]
    if report.calibration_warning:
        lines += [f"Warning: {report.calibration_warning}", ""]
    lines += [
        "| Tier | Cases | Mature | Hit rate | Mean excess | Mean utility |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for bucket in report.calibration:
        lines.append(
            f"| {bucket.conviction} | {bucket.case_count} | {bucket.mature_count} | "
            f"{_percent(bucket.hit_rate)} | {_percent(bucket.mean_excess)} | "
            f"{_percent(bucket.mean_utility)} |"
        )
    lines += ["", "## Falsifier scorecard", ""]
    if report.falsifiers:
        lines += [
            "| Falsifier | Losing cases | Before | After | Never | Saves | False alarms | Ungraded | Unevaluable |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for score in report.falsifiers:
            lines.append(
                f"| {score.name} | {score.losing_cases} | {score.before_damage} | "
                f"{score.after_damage} | {score.never_fired} | {score.true_saves} | "
                f"{score.false_alarms} | {score.ungraded_firings} | "
                f"{score.unevaluable_observations} |"
            )
    else:
        lines.append("No falsifier observations.")
    lines += ["", "## Process quadrants", ""]
    for quadrant, count in report.quadrant_counts.items():
        lines.append(f"- {quadrant}: {count}")
    lines += ["", "## Reproducibility", ""]
    if report.metadata:
        for key, value in report.metadata.items():
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- No metadata recorded.")
    return "\n".join(lines) + "\n"
