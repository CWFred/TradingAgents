from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ops.backtest.generate import FrozenMemoRecord, GenerationRequest
from ops.backtest.lessons import DistilledLesson
from ops.backtest.models import (
    BacktestCase,
    CaseResult,
    CaseSource,
    ContextManifest,
    DecisionAction,
    ExperimentRecord,
    HorizonOutcome,
    Lesson,
    OutcomeLabel,
    OutcomeState,
    ThesisAssessment,
    ThesisCorrectness,
)
from ops.backtest.service import MissingBacktestArtifacts, experiment_run
from ops.backtest.store import BacktestStore

LOCAL_MODEL = "openai_compatible:test@http://127.0.0.1:8000/v1"
EXPERIMENT_ID = "experiment-fixture"


def _case(symbol, asof, **overrides):
    values = {
        "sleeve": "research",
        "symbol": symbol,
        "asof": asof,
        "trigger": {"kind": "selloff", "magnitude": Decimal("-0.17")},
        "source": CaseSource.POINT_IN_TIME,
        "score": Decimal("9.5"),
        "created_at": datetime(2025, 7, 15, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return BacktestCase.create(**values)


def _request(case):
    manifest = ContextManifest.create(case_id=case.case_id, asof=case.asof)
    return GenerationRequest.create(
        case=case, manifest=manifest,
        brain_version="brain-v1", prompt_version="prompt-v1",
        evidence_model_id=LOCAL_MODEL, thesis_model_id=LOCAL_MODEL,
    )


def _seed_experiment(path):
    """Experiment with a 2-case holdout, one training case, one lesson."""
    training = _case("TRN", date(2025, 6, 2))
    holdout = (_case("AAA", date(2025, 7, 1)), _case("BBB", date(2025, 7, 2)))
    with BacktestStore(path) as store:
        for case in (training, *holdout):
            store.insert_case(case)
            store.save_context_manifest(
                ContextManifest.create(case_id=case.case_id, asof=case.asof)
            )
        request = _request(training)
        store.ensure_generation_job(request)
        claim = store.claim_next_generation_job()
        store.finish_generation_job(
            claim, FrozenMemoRecord.terminal(
                request, status="accepted", reason=None,
                recommendation="buy", conviction="high",
                memo_json='{"thesis":"durable"}',
            ),
        )
        assessment = ThesisAssessment(
            assessment_key="assessment-1", memo_key=request.memo_key,
            case_id=training.case_id, correctness=ThesisCorrectness.WRONG,
            rationale="Missed balance-sheet risk.",
            evidence_cutoff=date(2025, 6, 20),
            model_id="local:judge", prompt_version="pm-v1",
        )
        store.save_thesis_assessment(assessment)
        lesson = Lesson(
            lesson_id="lesson-1", sleeve="research", text="Check leverage.",
            source_case_ids=(training.case_id,), eligible_from=date(2025, 6, 21),
            fingerprint="lesson-fingerprint-1",
        )
        store.save_distilled_lessons("distillation-1", [
            DistilledLesson(lesson, "distillation-1", (assessment.assessment_key,)),
        ])
        store.save_experiment(ExperimentRecord(
            experiment_id=EXPERIMENT_ID, sleeve="research", seed=7,
            holdout_case_ids=tuple(case.case_id for case in holdout),
            lesson_fingerprint="pending",
        ))
    return training, holdout, lesson


@pytest.fixture
def seeded_experiment(tmp_path, monkeypatch):
    path = tmp_path / "backtest.sqlite"
    monkeypatch.setenv("OPS_BACKTEST_STORE_PATH", str(path))
    training, holdout, lesson = _seed_experiment(path)
    return path, training, holdout, lesson


def test_experiment_pairs_control_and_treated_over_holdout(seeded_experiment):
    path, _training, holdout, _lesson = seeded_experiment

    class _FakeEvaluator:
        def __init__(self):
            self.calls = []

        def evaluate(self, *, case_input, variant, lesson_fingerprint):
            self.calls.append((case_input.case_id, variant, lesson_fingerprint))
            return 1.0 if variant == "treated" else 0.0

    evaluator = _FakeEvaluator()
    summary = experiment_run(
        path=path, experiment_id=EXPERIMENT_ID, execute=True, evaluator=evaluator,
    )

    assert summary["pairs"] == 2
    assert summary["mean_delta"] == 1.0
    assert summary["improved"] == 2
    assert summary["worsened"] == 0
    assert summary["unchanged"] == 0
    assert summary["claim"] == "paired descriptive result; no significance claim"
    assert summary["experiment_id"] == EXPERIMENT_ID
    assert summary["executed"] is True
    assert summary["case_ids"] == tuple(sorted(case.case_id for case in holdout))
    # Control is always evaluated against no lessons; treated carries the
    # eligible-lesson fingerprint (both holdout cases postdate the lesson).
    assert [call[2] for call in evaluator.calls if call[1] == "control"] == [None, None]
    assert all(
        call[2] is not None for call in evaluator.calls if call[1] == "treated"
    )


def test_experiment_plan_only_lists_missing_treated_memos(seeded_experiment):
    path, _training, holdout, _lesson = seeded_experiment

    summary = experiment_run(path=path, experiment_id=EXPERIMENT_ID)

    assert summary["executed"] is False
    assert summary["holdout_cases"] == 2
    assert summary["lessons"] == 1
    assert summary["pairs"] is None
    assert summary["missing_treated"] == tuple(
        sorted(case.case_id for case in holdout)
    )
    # The to-do command must actually be able to create the TREATED arm.
    assert summary["generate_command"] == (
        "ops backtest generate --sleeve research "
        "--start 2025-07-01 --end 2025-07-02 "
        f"--experiment {EXPERIMENT_ID} --execute"
    )


def _outcome(horizon, utility, *, state=OutcomeState.MATURE):
    return HorizonOutcome(
        run_id="run", case_id="case-x", horizon_sessions=horizon, state=state,
        label=OutcomeLabel.WIN, utility=utility, detail="fixture",
    )


def _case_result(primary=63):
    return CaseResult(
        run_id="run", case_id="case-x", initial_action=DecisionAction.BUY,
        status="complete", primary_horizon=primary, primary_label=OutcomeLabel.WIN,
    )


def test_primary_utility_selects_the_primary_horizon():
    from ops.backtest.experiment import primary_utility

    outcomes = (
        _outcome(5, Decimal("0.10")),
        _outcome(63, Decimal("-0.25")),
        _outcome(126, None, state=OutcomeState.PENDING),
    )
    value = primary_utility(outcomes, _case_result(63))
    assert value == -0.25
    assert isinstance(value, float)


@pytest.mark.parametrize("state,utility", [
    (OutcomeState.PENDING, None),
    (OutcomeState.UNPRICEABLE, None),
    (OutcomeState.PENDING, Decimal("0.10")),
])
def test_primary_utility_refuses_ungraded_outcomes(state, utility):
    from ops.backtest.experiment import primary_utility

    with pytest.raises(MissingBacktestArtifacts) as excinfo:
        primary_utility((_outcome(63, utility, state=state),), _case_result(63))
    assert "backtest prices" in str(excinfo.value)
    assert "not mature" in str(excinfo.value)


def test_primary_utility_raises_when_the_primary_horizon_is_absent():
    from ops.backtest.experiment import primary_utility
    from ops.backtest.service import BacktestServiceError

    with pytest.raises(BacktestServiceError, match="no 63-session outcome"):
        primary_utility((_outcome(5, Decimal("0.10")),), _case_result(63))


def _sessions(start, count):
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _seed_bars(path, closes_by_symbol, sessions):
    from ops.backtest.models import PriceBar
    from ops.backtest.prices import PriceCache

    bars = []
    for symbol, closes in closes_by_symbol.items():
        for session, close in zip(sessions, closes, strict=True):
            raw = Decimal(close)
            bars.append(PriceBar(
                symbol=symbol, session=session,
                open=raw, high=raw + 1, low=raw - 1, close=raw,
                adjusted_open=raw, adjusted_high=raw + 1,
                adjusted_low=raw - 1, adjusted_close=raw,
                volume=Decimal("1000"), dividend=Decimal("0"),
                split_ratio=Decimal("1"), provider="fixture",
            ))
    PriceCache(path).upsert_bars(
        bars, fetched_at=datetime(2025, 8, 1, tzinfo=timezone.utc),
    )


def test_replay_evaluator_grades_a_real_treated_memo(seeded_experiment):
    """End-to-end treated arm, fully offline: real memo, real bars, real replay."""
    from ops.backtest.experiment import ReplayPairedEvaluator
    from ops.backtest.lessons import PairedCaseInput, eligible_lessons, lesson_set_hash
    from ops.backtest.service import (
        DEFAULT_BRAIN_VERSION,
        DEFAULT_PROMPT_VERSION,
        _generation_requests,
    )
    from ops.config import load_config

    path, _training, holdout, lesson = seeded_experiment
    case = holdout[0]
    config = load_config()
    fingerprint = lesson_set_hash(eligible_lessons((lesson,), asof=case.asof))
    sessions = _sessions(case.asof, 9)
    # Stock climbs, benchmark flat: a PASS memo gives up that excess return,
    # so its decision utility must come out negative.
    _seed_bars(path, {
        case.symbol: [str(100 + step) for step in range(9)],
        config.backtest_benchmark: ["50"] * 9,
    }, sessions)

    with BacktestStore(path) as store:
        request = _generation_requests(
            store, [case], config=config,
            brain_version=DEFAULT_BRAIN_VERSION, prompt_version=DEFAULT_PROMPT_VERSION,
            lessons=(lesson,),
        )[0]
        assert request.lesson_fingerprint == fingerprint
        assert request.lesson_texts == (lesson.text,)
        store.ensure_generation_job(request)
        claim = store.claim_next_generation_job()
        store.finish_generation_job(claim, FrozenMemoRecord.terminal(
            request, status="rejected", reason="fixture guardrail",
        ))

        evaluator = ReplayPairedEvaluator(
            store=store, config=config, lessons=(lesson,),
            settings={"horizons": [5], "primary_horizon": 5},
            today=sessions[-1], experiment_id=EXPERIMENT_ID,
        )
        quality = evaluator.evaluate(
            case_input=PairedCaseInput(
                case_id=case.case_id, asof=case.asof, pinned_input_hash="pinned",
            ),
            variant="treated", lesson_fingerprint=fingerprint,
        )

    assert isinstance(quality, float)
    assert quality < 0


def test_generate_with_experiment_creates_the_treated_variant(seeded_experiment):
    from ops.backtest.lessons import eligible_lessons, lesson_set_hash
    from ops.backtest.service import generate_cases
    from ops.config import load_config

    path, _training, holdout, lesson = seeded_experiment
    config = load_config()
    treated = lesson_set_hash(eligible_lessons((lesson,), asof=holdout[0].asof))
    window = {
        "config": config, "sleeve": "research",
        "start": date(2025, 7, 1), "end": date(2025, 7, 2),
        "case_count": 30, "today": date(2025, 8, 1), "enqueue": True,
    }

    control_plan = generate_cases(**window)
    treated_plan = generate_cases(**window, experiment_id=EXPERIMENT_ID)

    assert control_plan.total == 2
    assert treated_plan.total == 2
    with BacktestStore(path) as store:
        queued = store.queued_generation_requests()
    by_fingerprint = {}
    for request in queued:
        by_fingerprint.setdefault(request.lesson_fingerprint, []).append(request)
    # Without --experiment generation can only make the control arm; with it,
    # the treated arm appears with the matching fingerprint AND its lesson
    # texts, which must survive the queue round-trip or the background drain
    # would write a control memo under the treated identity.
    assert set(by_fingerprint) == {"none", treated}
    assert len(by_fingerprint[treated]) == 2
    assert all(
        request.lesson_texts == (lesson.text,)
        for request in by_fingerprint[treated]
    )
    assert all(request.lesson_texts == () for request in by_fingerprint["none"])


def test_generate_with_unknown_experiment_is_rejected(seeded_experiment):
    from ops.backtest.service import UnknownBacktestRun, generate_cases
    from ops.config import load_config

    with pytest.raises(UnknownBacktestRun):
        generate_cases(
            config=load_config(), sleeve="research",
            start=date(2025, 7, 1), end=date(2025, 7, 2), case_count=30,
            today=date(2025, 8, 1), enqueue=True, experiment_id="experiment-nope",
        )


def test_experiment_cli_echoes_plan_and_missing_variants(seeded_experiment):
    from click.testing import CliRunner

    from ops.cli import cli

    path, _training, holdout, _lesson = seeded_experiment
    result = CliRunner().invoke(
        cli, ["backtest", "experiment", EXPERIMENT_ID],
        env={"OPS_BACKTEST_STORE_PATH": str(path)},
    )

    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    echoed = dict(line.split(maxsplit=1) for line in lines)
    assert echoed["experiment_id"] == EXPERIMENT_ID
    assert echoed["holdout_cases"] == "2"
    assert echoed["lessons"] == "1"
    assert echoed["executed"] == "False"
    assert holdout[0].case_id in echoed["missing_treated"]
    # Values are echoed in a single aligned column.
    assert len({len(line) - len(line.split(maxsplit=1)[1]) for line in lines}) == 1


def test_experiment_cli_reports_unknown_experiment(tmp_path):
    from click.testing import CliRunner

    from ops.cli import cli

    result = CliRunner().invoke(
        cli, ["backtest", "experiment", "experiment-nope"],
        env={"OPS_BACKTEST_STORE_PATH": str(tmp_path / "backtest.sqlite")},
    )

    assert result.exit_code != 0
    assert "unknown experiment" in result.output


def test_replay_evaluator_missing_treated_memo_names_generate_command(
    seeded_experiment,
):
    from ops.backtest.experiment import ReplayPairedEvaluator
    from ops.backtest.lessons import PairedCaseInput, eligible_lessons, lesson_set_hash
    from ops.config import load_config

    path, _training, holdout, lesson = seeded_experiment
    case = holdout[0]
    fingerprint = lesson_set_hash(eligible_lessons((lesson,), asof=case.asof))
    with BacktestStore(path) as store:
        evaluator = ReplayPairedEvaluator(
            store=store, config=load_config(), lessons=(lesson,),
            settings={}, today=date(2025, 8, 1), experiment_id=EXPERIMENT_ID,
        )
        with pytest.raises(MissingBacktestArtifacts) as excinfo:
            evaluator.evaluate(
                case_input=PairedCaseInput(
                    case_id=case.case_id, asof=case.asof,
                    pinned_input_hash="pinned",
                ),
                variant="treated",
                lesson_fingerprint=fingerprint,
            )
    message = str(excinfo.value)
    assert case.symbol in message
    assert message.endswith(
        "ops backtest generate --sleeve research "
        f"--start {case.asof.isoformat()} --end {case.asof.isoformat()} "
        f"--experiment {EXPERIMENT_ID} --execute"
    )
