from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ops.backtest.generate import FrozenMemoRecord, GenerationRequest
from ops.backtest.lessons import DistilledLesson
from ops.backtest.models import (
    BacktestCase,
    CaseSource,
    ContextManifest,
    ExperimentRecord,
    Lesson,
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
    assert "backtest generate" in message
    assert case.symbol in message
