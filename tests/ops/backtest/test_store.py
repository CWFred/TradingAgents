import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ops.backtest.generate import FrozenMemoRecord, GenerationRequest
from ops.backtest.lessons import DistilledLesson
from ops.backtest.models import (
    BacktestCase,
    CaseSource,
    ContextItem,
    ContextManifest,
    CutoffViolation,
    ExperimentRecord,
    Lesson,
    ProcessOutcomeQuadrant,
    ThesisAssessment,
    ThesisCorrectness,
    stable_hash,
)
from ops.backtest.store import SCHEMA_VERSION, BacktestStore, CaseConflictError


def _case(asof=date(2025, 6, 1), **overrides):
    values = {
        "sleeve": "research",
        "symbol": "acme",
        "asof": asof,
        "trigger": {"kind": "selloff", "magnitude": Decimal("-0.17")},
        "source": CaseSource.POINT_IN_TIME,
        "score": Decimal("9.5"),
        "created_at": datetime(2025, 7, 15, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return BacktestCase.create(**values)


def test_exact_cutoff_case_persists_and_round_trips(tmp_path):
    path = tmp_path / "backtest.sqlite"
    case = _case()

    with BacktestStore(path) as store:
        stored = store.insert_case(case)
        loaded = store.get_case(case.case_id)

    assert stored == case
    assert loaded == case
    assert loaded.symbol == "ACME"
    assert loaded.sleeve == "research"


def test_case_construction_and_insertion_both_enforce_cutoff(tmp_path):
    with pytest.raises(CutoffViolation, match="precedes"):
        _case(date(2025, 5, 31))

    # Direct dataclass construction cannot bypass the insertion boundary.
    unsafe = BacktestCase(
        case_id="case-unsafe", sleeve="research", symbol="ACME",
        asof=date(2025, 5, 31), created_at=datetime.now(timezone.utc),
    )
    with (
        BacktestStore(tmp_path / "backtest.sqlite") as store,
        pytest.raises(CutoffViolation, match="precedes"),
    ):
        store.insert_case(unsafe)


def test_replay_revalidates_persisted_cases_against_advanced_cutoff(tmp_path):
    path = tmp_path / "backtest.sqlite"
    case = _case()
    with BacktestStore(path) as store:
        store.insert_case(case)
        store.validate_cases_for_replay()

    with (
        BacktestStore(path, cutoff=date(2025, 6, 2)) as store,
        pytest.raises(CutoffViolation, match="precedes"),
    ):
        store.validate_cases_for_replay([case.case_id])


def test_reopening_and_reinserting_are_idempotent(tmp_path):
    path = tmp_path / "backtest.sqlite"
    case = _case()
    with BacktestStore(path) as store:
        assert store.schema_version == SCHEMA_VERSION
        store.insert_case(case)
        store.insert_case(case)

    with BacktestStore(path) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        assert reopened.insert_case(case) == case
        assert reopened.list_cases() == [case]


def test_same_identity_with_changed_frozen_content_is_a_conflict(tmp_path):
    original = _case()
    changed = _case(trigger={"kind": "different"})
    assert changed.case_id == original.case_id

    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        store.insert_case(original)
        with pytest.raises(CaseConflictError, match="different content"):
            store.insert_case(changed)


def test_foreign_keys_wal_and_busy_timeout_are_active(tmp_path):
    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        assert store.foreign_keys_enabled
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
            conn.execute(
                "INSERT INTO run_cases (run_id, case_id, ordinal) VALUES (?, ?, ?)",
                ("missing-run", "missing-case", 0),
            )
        with store.transaction() as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_failed_transaction_rolls_back_atomically(tmp_path):
    case = _case()
    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        with pytest.raises(RuntimeError), store.transaction() as conn:
            conn.execute(
                "INSERT INTO cases "
                "(case_id, sleeve, symbol, asof, trigger_json, source, score, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    case.case_id, case.sleeve, case.symbol, case.asof.isoformat(),
                    "{}", case.source.value, None, case.created_at.isoformat(),
                ),
            )
            raise RuntimeError("crash")
        assert store.get_case(case.case_id) is None


def test_manifest_persistence_is_canonical_and_point_in_time(tmp_path):
    case = _case()
    item = ContextItem.create(
        kind="filing", source_ref="0001:mdna", available_at=case.asof,
        content="Known by the close.", metadata={"form": "10-Q"},
    )
    manifest = ContextManifest.create(case_id=case.case_id, asof=case.asof, included=[item])

    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        store.insert_case(case)
        store.save_context_manifest(manifest)
        assert store.save_context_manifest(manifest) == manifest
        assert store.get_context_manifest(case.case_id) == manifest


def test_live_memo_store_is_never_opened_or_modified(tmp_path):
    live = tmp_path / "memos.sqlite"
    sentinel = b"live-memo-store-sentinel"
    live.write_bytes(sentinel)

    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        store.insert_case(_case())

    assert live.read_bytes() == sentinel


def test_stable_hash_is_order_independent_for_mapping_keys():
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})


def _generation_request(case):
    manifest = ContextManifest.create(case_id=case.case_id, asof=case.asof)
    return GenerationRequest.create(
        case=case, manifest=manifest,
        brain_version="brain-v1", prompt_version="prompt-v1",
        evidence_model_id=(
            "openai_compatible:test@http://127.0.0.1:8000/v1"
        ),
        thesis_model_id=(
            "openai_compatible:test@http://127.0.0.1:8000/v1"
        ),
    )


def test_generation_queue_claim_and_finish_are_atomic_and_durable(tmp_path):
    path = tmp_path / "backtest.sqlite"
    request = _generation_request(_case())
    record = FrozenMemoRecord.terminal(
        request, status="rejected", reason="fixture rejection",
    )
    with BacktestStore(path) as store:
        store.ensure_generation_job(request)
        store.ensure_generation_job(request)
        claim = store.claim_next_generation_job()
        assert claim.generation_key == request.generation_key
        assert claim.attempt_count == 1
        assert store.claim_next_generation_job() is None
        store.finish_generation_job(claim, record)
        store.finish_generation_job(claim, record)
        assert store.get_frozen_memo(request.memo_key) == record

    with BacktestStore(path) as reopened:
        assert reopened.get_frozen_memo(request.memo_key) == record
        assert reopened.frozen_memo_for_case(request.case.case_id) == record


def test_stale_generation_claim_is_requeued(tmp_path):
    request = _generation_request(_case())
    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        store.ensure_generation_job(request)
        first = store.claim_next_generation_job()
        with store.transaction() as conn:
            conn.execute(
                "UPDATE generation_jobs SET claimed_at = ? WHERE generation_key = ?",
                ("2025-06-01T00:00:00+00:00", request.generation_key),
            )
        assert store.requeue_stale_generation_jobs(
            stale_before=datetime(2025, 6, 2, tzinfo=timezone.utc),
        ) == 1
        second = store.claim_next_generation_job()
        assert second.attempt_count == first.attempt_count + 1


def test_generation_job_requires_explicit_auto_queue_opt_in_and_rehydrates(tmp_path):
    request = _generation_request(_case())
    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        store.ensure_generation_job(request)
        assert store.claim_next_generation_job(auto_only=True) is None
        assert store.queued_generation_requests(auto_only=True) == ()

        assert store.enqueue_generation_jobs([request.generation_key]) == 1
        assert store.queued_generation_requests(auto_only=True) == (request,)
        claim = store.claim_next_generation_job(auto_only=True)
        assert claim.generation_key == request.generation_key


def test_cutoff_probe_is_sealed_idempotently_without_changing_store_cutoff(tmp_path):
    created = datetime(2025, 7, 15, tzinfo=timezone.utc)
    values = {
        "probe_id": "probe-1", "model_id": "ds4",
        "tested_cutoff": date(2025, 6, 1),
        "prompts": [{"event": "post-cutoff"}],
        "responses": [{"answer": "unknown"}],
        "rubric": {"unsafe_if_known": True}, "contaminated": False,
        "recommended_cutoff": date(2025, 6, 1), "created_at": created,
    }
    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        store.record_cutoff_probe(**values)
        store.record_cutoff_probe(**values)
        with store.transaction() as conn:
            row = conn.execute("SELECT * FROM cutoff_probes").fetchone()
        assert row["model_id"] == "ds4"
        assert row["contaminated"] == 0
        assert store.cutoff == date(2025, 6, 1)
        assert store.effective_cutoff == date(2025, 6, 1)

        changed = {**values, "contaminated": True}
        with pytest.raises(CaseConflictError):
            store.record_cutoff_probe(**changed)


def test_replay_falsifier_observations_persist_with_names(tmp_path):
    from types import SimpleNamespace

    from ops.backtest.models import DecisionAction, PriceBar
    from ops.backtest.replay import InitialDecision, replay_case
    from ops.backtest.service import render_saved_report
    from ops.backtest.sleeves import make_research_exit_policy
    from ops.backtest.verdicts import evaluate_replay

    case = _case(asof=date(2025, 6, 6))

    def bar(symbol, session, close="100"):
        value = Decimal(close)
        return PriceBar(
            symbol=symbol, session=session, open=value, high=value,
            low=value, close=value, adjusted_open=value,
            adjusted_high=value, adjusted_low=value, adjusted_close=value,
        )

    fundamental = SimpleNamespace(
        description="gross margin floor", check_type="fundamental",
        metric="gross_margin_pct", operator="<", threshold=30,
        consecutive_periods=1,
    )
    memo = SimpleNamespace(
        status="open", price_target_high=999, falsifiers=(fundamental,),
        as_of_date=case.asof, thesis_type="value",
    )
    stock = [
        bar("ACME", date(2025, 6, 9)),
        bar("ACME", date(2025, 6, 10)),
    ]
    benchmark = [
        bar("SPY", date(2025, 6, 9)),
        bar("SPY", date(2025, 6, 10)),
    ]
    replay = replay_case(
        run_id="run", case=case,
        initial=InitialDecision(DecisionAction.BUY, "buy"), bars=stock,
        notional=Decimal("100"), settings={},
        exit_policy=make_research_exit_policy(memo=memo),
    )
    outcomes, result = evaluate_replay(
        replay, stock_bars=stock, benchmark_bars=benchmark,
        adjudication_date=date(2025, 6, 10), horizons=(1,),
        primary_horizon=1,
    )
    path = tmp_path / "backtest.sqlite"
    with BacktestStore(path) as store:
        store.insert_case(case)
        store.create_run(
            run_id="run", sleeve="research", start_date=case.asof,
            end_date=case.asof, benchmark="SPY", settings={},
            resolved_config={}, metadata={}, case_ids=[case.case_id],
            created_at=datetime(2025, 6, 11, tzinfo=timezone.utc),
        )
        store.save_replay_evaluation(replay, outcomes, result)
        with store.transaction() as conn:
            rows = conn.execute(
                "SELECT name, status FROM falsifier_observations "
                "ORDER BY session"
            ).fetchall()
        store.finish_run("run")
    assert [(row["name"], row["status"]) for row in rows] == [
        ("gross margin floor", "unevaluable"),
        ("gross margin floor", "unevaluable"),
    ]
    rendered = render_saved_report(path, "run")
    assert "| gross margin floor |" in rendered
    assert "| Unevaluable |" in rendered


def _seed_learning_rows(store, case):
    request = _generation_request(case)
    store.ensure_generation_job(request)
    claim = store.claim_next_generation_job()
    store.finish_generation_job(
        claim, FrozenMemoRecord.terminal(
            request, status="accepted", reason=None,
            recommendation="buy", conviction="high",
            memo_json='{"thesis":"durable"}',
        ),
    )
    store.create_run(
        run_id="run-learning", sleeve="research", start_date=case.asof,
        end_date=case.asof, benchmark="SPY", settings={}, resolved_config={},
        metadata={}, case_ids=[case.case_id], created_at=case.created_at,
    )
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO decisions "
            "(decision_id, run_id, case_id, sequence, observed_session, action, "
            "reason, settings_hash, memo_key, metadata_json) "
            "VALUES (?, ?, ?, 0, ?, 'BUY', 'fixture', ?, ?, '{}')",
            (
                "decision-learning", "run-learning", case.case_id,
                case.asof.isoformat(), stable_hash({}), request.memo_key,
            ),
        )
        conn.execute(
            "INSERT INTO case_results "
            "(run_id, case_id, initial_action, status, primary_horizon, "
            "primary_label, quadrant) VALUES (?, ?, 'BUY', 'complete', 63, "
            "'win', 'ungraded')",
            ("run-learning", case.case_id),
        )
    return request


def test_assessment_cache_round_trips_and_updates_run_quadrant(tmp_path):
    case = _case()
    assessment = None
    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        request = _seed_learning_rows(store, case)
        assessment = ThesisAssessment(
            assessment_key="assessment-1", memo_key=request.memo_key,
            case_id=case.case_id, correctness=ThesisCorrectness.WRONG,
            rationale="The mechanism failed.", evidence_cutoff=date(2025, 9, 1),
            model_id="local:judge", prompt_version="pm-v1", evidence=("fact-1",),
            created_at=datetime(2025, 9, 2, tzinfo=timezone.utc),
        )
        store.save_thesis_assessment(assessment)
        store.save_thesis_assessment(assessment)
        assert store.get_thesis_assessment(assessment.assessment_key) == assessment
        with store.transaction() as conn:
            quadrant = conn.execute(
                "SELECT quadrant FROM case_results WHERE run_id = 'run-learning'"
            ).fetchone()[0]
        assert quadrant == ProcessOutcomeQuadrant.WRONG_THESIS_LUCKY.value


def test_lesson_and_experiment_caches_are_durable_and_idempotent(tmp_path):
    path = tmp_path / "backtest.sqlite"
    case = _case()
    with BacktestStore(path) as store:
        request = _seed_learning_rows(store, case)
        assessment = ThesisAssessment(
            assessment_key="assessment-lesson", memo_key=request.memo_key,
            case_id=case.case_id, correctness=ThesisCorrectness.WRONG,
            rationale="Missed balance-sheet risk.", evidence_cutoff=date(2025, 9, 1),
            model_id="local:judge", prompt_version="pm-v1",
        )
        store.save_thesis_assessment(assessment)
        lesson = Lesson(
            lesson_id="lesson-1", sleeve="research", text="Check leverage.",
            source_case_ids=(case.case_id,), eligible_from=date(2025, 9, 1),
            fingerprint="lesson-fingerprint",
        )
        distilled = DistilledLesson(
            lesson, "distillation-1", (assessment.assessment_key,),
        )
        store.save_distilled_lessons("distillation-1", [distilled])
        store.save_distilled_lessons("distillation-1", [distilled])
        experiment = ExperimentRecord(
            experiment_id="experiment-1", sleeve="research", seed=7,
            holdout_case_ids=(case.case_id,),
            lesson_fingerprint=lesson.fingerprint,
        )
        store.save_experiment(experiment)
    with BacktestStore(path) as reopened:
        assert reopened.get_distilled_lessons("missing") is None
        assert reopened.get_distilled_lessons("distillation-1") == (distilled,)
        assert reopened.get_experiment(experiment.experiment_id) == experiment


def test_schema_one_store_migrates_learning_cache_tables(tmp_path):
    path = tmp_path / "backtest.sqlite"
    with BacktestStore(path):
        pass
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "DROP TABLE lesson_assessments; DROP TABLE lesson_distillations; "
            "DROP TABLE distillation_runs; PRAGMA user_version = 1; "
            "UPDATE schema_metadata SET value = '1' WHERE key = 'schema_version';"
        )
    with BacktestStore(path) as migrated:
        assert migrated.schema_version == SCHEMA_VERSION
        assert {
            "distillation_runs", "lesson_distillations", "lesson_assessments",
        } <= migrated.table_names()


def test_contaminated_probe_advances_effective_cutoff_and_seals_old_cases(tmp_path):
    path = tmp_path / "backtest.sqlite"
    old_case = _case(date(2025, 6, 15))
    with BacktestStore(path) as store:
        store.insert_case(old_case)
        store.record_cutoff_probe(
            probe_id="probe-contaminated", model_id="ds4",
            tested_cutoff=date(2025, 6, 1), prompts=[], responses=[], rubric={},
            contaminated=True, recommended_cutoff=date(2025, 7, 1),
            created_at=datetime(2025, 7, 15, tzinfo=timezone.utc),
        )
        assert store.cutoff == date(2025, 6, 1)
        assert store.effective_cutoff == date(2025, 7, 1)
        with pytest.raises(CutoffViolation, match="2025-07-01"):
            store.validate_cases_for_replay([old_case.case_id])
        with pytest.raises(CutoffViolation, match="2025-07-01"):
            store.insert_case(_case(date(2025, 6, 20)))

    with BacktestStore(path) as reopened:
        assert reopened.effective_cutoff == date(2025, 7, 1)
