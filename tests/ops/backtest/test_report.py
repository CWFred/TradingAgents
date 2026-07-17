from datetime import date
from decimal import Decimal

import pytest

from ops.backtest.models import (
    CaseResult,
    DecisionAction,
    HorizonOutcome,
    OutcomeLabel,
    OutcomeState,
    ProcessOutcomeQuadrant,
)
from ops.backtest.report import (
    FalsifierCase,
    FalsifierFiring,
    ReportCase,
    build_falsifier_scorecard,
    build_report,
    render_report,
)

pytestmark = pytest.mark.unit


def _row(
    case_id,
    *,
    label=OutcomeLabel.WIN,
    excess=".10",
    utility=None,
    conviction="high",
    action=DecisionAction.BUY,
    state=OutcomeState.MATURE,
    actual=".04",
    drawdown="-.02",
    quadrant=ProcessOutcomeQuadrant.UNGRADED,
):
    excess_value = Decimal(excess) if excess is not None else None
    utility_value = (
        Decimal(utility) if utility is not None
        else excess_value
    )
    outcome = HorizonOutcome(
        run_id="run", case_id=case_id, horizon_sessions=63,
        state=state, label=label, excess_return=excess_value,
        utility=utility_value,
    )
    result = CaseResult(
        run_id="run", case_id=case_id, initial_action=action,
        status=("complete" if state == OutcomeState.MATURE else state.value),
        primary_horizon=63, primary_label=label,
        actual_return=Decimal(actual) if actual is not None else None,
        max_drawdown=Decimal(drawdown) if drawdown is not None else None,
        quadrant=quadrant,
    )
    return ReportCase(
        case_id=case_id, symbol=case_id.upper(), conviction=conviction,
        result=result, outcomes=(outcome,),
    )


def _report(rows, **kwargs):
    return build_report(
        run_id="run", rows=rows, min_mature_cases=1,
        promising_min_hit_rate=Decimal(".55"),
        promising_min_mean_excess=Decimal(".03"),
        dead_max_hit_rate=Decimal(".40"),
        dead_max_mean_excess=Decimal("0"),
        **kwargs,
    )


def test_promising_mixed_dead_and_insufficient_verdicts():
    promising = _report([_row("a"), _row("b", excess=".08")])
    assert promising.summary.verdict == "promising"

    mixed = _report([_row("a"), _row("b", label=OutcomeLabel.LOSS, excess="-.01")])
    assert mixed.summary.verdict == "mixed"

    dead = _report([
        _row("a", label=OutcomeLabel.LOSS, excess="-.1"),
        _row("b", label=OutcomeLabel.LOSS, excess="-.2"),
    ])
    assert dead.summary.verdict == "dead"

    insufficient = build_report(run_id="run", rows=[_row("a")], min_mature_cases=2)
    assert insufficient.summary.verdict == "insufficient"
    assert "need 2" in insufficient.summary.verdict_evidence


def test_pass_utility_drives_triage_while_raw_excess_remains_visible():
    report = _report([
        _row(
            "pass", action=DecisionAction.PASS, excess="-.10", utility=".10",
        )
    ])
    assert report.summary.verdict == "promising"
    assert report.summary.mean_excess == Decimal("-.10")
    assert report.summary.mean_utility == Decimal(".10")


def test_washes_do_not_enter_hit_rate_denominator():
    report = _report([
        _row("win"),
        _row("wash", label=OutcomeLabel.WASH, excess=".01"),
        _row("loss", label=OutcomeLabel.LOSS, excess="-.1"),
    ])
    assert report.summary.hit_rate == Decimal(".5")
    assert report.summary.washes == 1


def test_pending_and_unpriceable_are_visible_but_excluded_from_stats():
    report = _report([
        _row("win"),
        _row(
            "pending", state=OutcomeState.PENDING,
            label=OutcomeLabel.PENDING, excess=None, actual=None, drawdown=None,
        ),
        _row(
            "bad", state=OutcomeState.UNPRICEABLE,
            label=OutcomeLabel.UNPRICEABLE, excess=None, actual=None, drawdown=None,
        ),
    ])
    assert report.summary.total_cases == 3
    assert report.summary.mature_cases == 1
    assert report.summary.pending_cases == 1
    assert report.summary.unpriceable_cases == 1


def test_one_case_per_row_prevents_daily_hold_sample_inflation():
    with pytest.raises(ValueError, match="one row per case"):
        _report([_row("same"), _row("same")])


def test_calibration_groups_sparse_tiers_and_warns_when_high_loses_to_low():
    report = _report([
        _row("high", conviction="high", excess="-.1"),
        _row("low", conviction="low", excess=".1"),
        _row(
            "new", conviction="medium", state=OutcomeState.PENDING,
            label=OutcomeLabel.PENDING, excess=None, actual=None, drawdown=None,
        ),
    ])
    buckets = {bucket.conviction: bucket for bucket in report.calibration}
    assert buckets["medium"].mature_count == 0
    assert buckets["medium"].mean_excess is None
    assert report.calibration_warning is not None
    assert "not calibrated" in report.calibration_warning


def test_falsifier_scorecard_covers_before_after_never_save_and_false_alarm():
    cases = [
        FalsifierCase(
            case_id="loss-a", names=("margin", "guidance"), losing=True,
            damage_session=date(2025, 9, 1),
            firings=(
                FalsifierFiring("margin", date(2025, 8, 1), avoided_loss=True),
                FalsifierFiring("guidance", date(2025, 9, 1)),
            ),
        ),
        FalsifierCase(
            case_id="loss-b", names=("margin",), losing=True,
            damage_session=date(2025, 9, 1),
        ),
        FalsifierCase(
            case_id="winner", names=("margin",), losing=False,
            firings=(
                FalsifierFiring("margin", date(2025, 8, 1), avoided_loss=False),
            ),
        ),
    ]
    scores = {score.name: score for score in build_falsifier_scorecard(cases)}
    assert scores["margin"].before_damage == 1
    assert scores["margin"].never_fired == 1
    assert scores["margin"].true_saves == 1
    assert scores["margin"].false_alarms == 1
    assert scores["guidance"].after_damage == 1


def test_falsifier_scorecard_counts_unevaluable_observations():
    scores = build_falsifier_scorecard([
        FalsifierCase(
            case_id="case", names=("margin",), losing=False,
            firings=(FalsifierFiring(
                "margin", date(2025, 8, 1), status="unevaluable",
            ),),
        )
    ])
    assert scores[0].unevaluable_observations == 1


def test_triage_surfaces_stale_and_terminal_price_states():
    stale = type(_row("stale"))(**{
        **_row("stale").__dict__, "price_status": "stale",
    })
    terminal = type(_row("terminal"))(**{
        **_row("terminal").__dict__, "price_status": "terminal",
    })
    report = _report([stale, terminal])
    assert report.summary.stale_cases == 1
    assert report.summary.terminal_cases == 1
    rendered = render_report(report)
    assert "1 stale, 1 terminal" in rendered


def test_report_renders_stably_with_quadrants_and_sorted_metadata():
    report = _report(
        [
            _row(
                "b", quadrant=ProcessOutcomeQuadrant.WRONG_THESIS_LUCKY,
            ),
            _row(
                "a", quadrant=ProcessOutcomeQuadrant.RIGHT_THESIS_WORKED,
            ),
        ],
        metadata={"z_commit": "abc", "a_dirty": False},
    )
    first = render_report(report)
    second = render_report(report)
    assert first == second
    assert first.index("| a |") < first.index("| b |")
    assert "right-thesis-worked: 1" in first
    assert first.index("a_dirty") < first.index("z_commit")
    assert "Actual replay mean" in first


def test_empty_report_is_explicit_and_renderable():
    report = build_report(run_id="empty", rows=[], min_mature_cases=1)
    text = render_report(report)
    assert report.summary.total_cases == 0
    assert report.summary.verdict == "insufficient"
    assert "No falsifier observations" in text
