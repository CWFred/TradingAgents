import re
import sys
from datetime import date, datetime, timezone
from types import ModuleType

from click.testing import CliRunner

from ops.backtest.cases import CaseCandidate
from ops.backtest.generate import FrozenMemoRecord, GenerationRequest
from ops.backtest.models import BacktestCase, CaseSource, ContextItem, ContextManifest
from ops.backtest.service import (
    DEFAULT_BRAIN_VERSION,
    DEFAULT_PROMPT_VERSION,
    generate_cases,
    prepare_cases,
)
from ops.backtest.store import BacktestStore
from ops.cli import cli
from ops.config import OpsConfig


def _seed_case(path, *, frozen=True, symbol="AAA"):
    case = BacktestCase.create(
        sleeve="research", symbol=symbol, asof=date(2025, 6, 1),
        created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    manifest = ContextManifest.create(case_id=case.case_id, asof=case.asof)
    cfg = OpsConfig(backtest_store_path=str(path))
    request = GenerationRequest.create(
        case=case, manifest=manifest,
        brain_version=DEFAULT_BRAIN_VERSION, prompt_version=DEFAULT_PROMPT_VERSION,
        evidence_model_id=cfg.research_evidence_model,
        thesis_model_id=cfg.research_thesis_model,
    )
    with BacktestStore(path) as store:
        store.insert_case(case)
        store.save_context_manifest(manifest)
        if frozen:
            store.ensure_generation_job(request)
            claim = store.claim_next_generation_job()
            store.finish_generation_job(
                claim,
                FrozenMemoRecord.terminal(
                    request, status="rejected", reason="fixture pass",
                ),
            )
    return case


def _invoke(runner, path, args):
    return runner.invoke(cli, args, env={"OPS_BACKTEST_STORE_PATH": str(path)})


def test_run_uses_preloaded_artifacts_and_prints_report(tmp_path):
    path = tmp_path / "backtest.sqlite"
    _seed_case(path)
    result = _invoke(CliRunner(), path, [
        "backtest", "run", "--start", "2025-06-01", "--end", "2025-06-30",
    ])

    assert result.exit_code == 0, result.output
    assert "# Backtest report: backtest-" in result.output
    assert "Cases: 1 total" in result.output
    with BacktestStore(path) as store, store.transaction() as conn:
        row = conn.execute("SELECT run_id, status FROM runs").fetchone()
    assert row["status"] == "complete"


def test_run_uses_exact_unconditioned_memo_not_newest_lesson_variant(tmp_path):
    path = tmp_path / "backtest.sqlite"
    case = _seed_case(path)
    cfg = OpsConfig(backtest_store_path=str(path))
    with BacktestStore(path) as store:
        manifest = store.get_context_manifest(case.case_id)
        baseline = GenerationRequest.create(
            case=case, manifest=manifest,
            brain_version=DEFAULT_BRAIN_VERSION, prompt_version=DEFAULT_PROMPT_VERSION,
            evidence_model_id=cfg.research_evidence_model,
            thesis_model_id=cfg.research_thesis_model,
        )
        treated = GenerationRequest.create(
            case=case, manifest=manifest,
            brain_version=DEFAULT_BRAIN_VERSION, prompt_version=DEFAULT_PROMPT_VERSION,
            evidence_model_id=cfg.research_evidence_model,
            thesis_model_id=cfg.research_thesis_model,
            lesson_fingerprint="treated-lessons",
        )
        store.ensure_generation_job(treated)
        claim = store.claim_next_generation_job()
        store.finish_generation_job(
            claim, FrozenMemoRecord.terminal(
                treated, status="rejected", reason="treated fixture",
            ),
        )

    result = _invoke(CliRunner(), path, [
        "backtest", "run", "--start", "2025-06-01", "--end", "2025-06-30",
    ])
    assert result.exit_code == 0, result.output
    with BacktestStore(path) as store, store.transaction() as conn:
        chosen = conn.execute(
            "SELECT memo_key FROM decisions WHERE sequence = 0",
        ).fetchone()[0]
    assert chosen == baseline.memo_key


def test_run_missing_memo_fails_without_starting_a_run(tmp_path):
    path = tmp_path / "backtest.sqlite"
    _seed_case(path, frozen=False)
    result = _invoke(CliRunner(), path, [
        "backtest", "run", "--start", "2025-06-01", "--end", "2025-06-30",
    ])

    assert result.exit_code != 0
    assert "memo(s) missing" in result.output
    assert "backtest generate" in result.output
    with BacktestStore(path) as store, store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_generate_defaults_to_offline_plan_only(tmp_path):
    path = tmp_path / "backtest.sqlite"
    _seed_case(path, frozen=False)
    result = _invoke(CliRunner(), path, [
        "backtest", "generate", "--start", "2025-06-01", "--end", "2025-06-30",
    ])

    assert result.exit_code == 0, result.output
    assert "1 case(s), 0 cached, 1 pending" in result.output
    assert "plan only" in result.output


def test_generate_reports_cached_plan(tmp_path):
    path = tmp_path / "backtest.sqlite"
    _seed_case(path)
    result = _invoke(CliRunner(), path, [
        "backtest", "generate", "--start", "2025-06-01", "--end", "2025-06-30",
    ])
    assert result.exit_code == 0
    assert "1 case(s), 1 cached, 0 pending" in result.output


def test_generate_prepares_empty_store_through_injected_pit_seams(tmp_path):
    path = tmp_path / "backtest.sqlite"
    cfg = OpsConfig(backtest_store_path=str(path))
    candidate = CaseCandidate(
        symbol="AAA", asof=date(2025, 6, 15), score=3,
        trigger={"kind": "recorded_live_screen"},
        screen_payload={"symbol": "AAA", "asof": "2025-06-15"},
        source_ref="screen:run:1",
    )

    def preparer(**kwargs):
        return prepare_cases(
            store=kwargs["store"], sleeve=kwargs["sleeve"],
            start=kwargs["start"], end=kwargs["end"],
            case_count=kwargs["case_count"],
            case_source=lambda **_window: [candidate],
            context_builder=lambda case, _candidate: ContextManifest.create(
                case_id=case.case_id, asof=case.asof,
            ),
        )

    result = generate_cases(
        config=cfg, sleeve="research", start=date(2025, 6, 1),
        end=date(2025, 6, 30), case_count=40, today=date(2025, 7, 1),
        preparer=preparer,
    )

    assert (result.total, result.cached, result.pending) == (1, 0, 1)
    with BacktestStore(path) as store:
        cases = store.list_cases(sleeve="research")
        assert [case.source for case in cases] == [CaseSource.LIVE_IMPORT]
        assert store.get_context_manifest(cases[0].case_id) is not None


def test_contaminated_probe_cutoff_blocks_run_and_generation_selection(tmp_path):
    path = tmp_path / "backtest.sqlite"
    _seed_case(path)
    with BacktestStore(path) as store:
        store.record_cutoff_probe(
            probe_id="unsafe", model_id="ds4", tested_cutoff=date(2025, 6, 1),
            prompts=[], responses=[], rubric={}, contaminated=True,
            recommended_cutoff=date(2025, 7, 1),
            created_at=datetime(2025, 7, 2, tzinfo=timezone.utc),
        )

    runner = CliRunner()
    for command in ("run", "generate"):
        result = _invoke(runner, path, [
            "backtest", command, "--start", "2025-06-01", "--end", "2025-07-31",
        ])
        assert result.exit_code != 0
        assert "effective cutoff 2025-07-01" in result.output


def test_report_rerenders_a_past_run(tmp_path):
    path = tmp_path / "backtest.sqlite"
    _seed_case(path)
    runner = CliRunner()
    first = _invoke(runner, path, [
        "backtest", "run", "--start", "2025-06-01", "--end", "2025-06-30",
    ])
    run_id = re.search(r"# Backtest report: (\S+)", first.output).group(1)

    second = _invoke(runner, path, ["backtest", "report", run_id])
    assert second.exit_code == 0, second.output
    assert second.output == first.output


def test_report_missing_store_is_read_only_and_does_not_create_it(tmp_path):
    path = tmp_path / "missing.sqlite"
    result = _invoke(CliRunner(), path, ["backtest", "report", "unknown"])

    assert result.exit_code != 0
    assert "store does not exist" in result.output
    assert not path.exists()


def test_unknown_run_has_stable_nonzero_error(tmp_path):
    path = tmp_path / "backtest.sqlite"
    BacktestStore(path).close()
    result = _invoke(CliRunner(), path, ["backtest", "report", "not-a-run"])
    assert result.exit_code != 0
    assert "unknown backtest run 'not-a-run'" in result.output


def test_invalid_dates_case_count_and_settings_fail_cleanly(tmp_path):
    path = tmp_path / "backtest.sqlite"
    _seed_case(path)
    runner = CliRunner()

    bad_date = _invoke(runner, path, [
        "backtest", "run", "--start", "yesterday", "--end", "today",
    ])
    assert bad_date.exit_code != 0
    assert "expected YYYY-MM-DD or 'today'" in bad_date.output

    bad_count = _invoke(runner, path, [
        "backtest", "run", "--start", "2025-06-01", "--cases", "29",
    ])
    assert bad_count.exit_code != 0
    assert "approved range 30..50" in bad_count.output

    settings = tmp_path / "settings.toml"
    settings.write_text("unknown = true\n")
    bad_settings = _invoke(runner, path, [
        "backtest", "run", "--start", "2025-06-01",
        "--settings", str(settings),
    ])
    assert bad_settings.exit_code != 0
    assert "unknown settings" in bad_settings.output


def test_postmortem_plans_cached_work_without_llm(tmp_path):
    path = tmp_path / "backtest.sqlite"
    _seed_case(path)
    runner = CliRunner()
    first = _invoke(runner, path, [
        "backtest", "run", "--start", "2025-06-01", "--end", "2025-06-30",
    ])
    run_id = re.search(r"# Backtest report: (\S+)", first.output).group(1)

    result = _invoke(runner, path, ["backtest", "postmortem", run_id])
    assert result.exit_code == 0, result.output
    assert "1 memo(s), 0 cached, 1 pending, 0 updated" in result.output


def test_postmortem_execute_fails_closed_without_pit_assessor(tmp_path):
    path = tmp_path / "backtest.sqlite"
    _seed_case(path)
    runner = CliRunner()
    first = _invoke(runner, path, [
        "backtest", "run", "--start", "2025-06-01", "--end", "2025-06-30",
    ])
    run_id = re.search(r"# Backtest report: (\S+)", first.output).group(1)

    result = _invoke(
        runner, path, ["backtest", "postmortem", run_id, "--execute"],
    )
    assert result.exit_code != 0
    assert "requires a configured PIT facts/assessor adapter" in result.output


def test_postmortem_cli_loads_adapter_persists_assessment_and_updates_quadrant(
    tmp_path, monkeypatch,
):
    path = tmp_path / "backtest.sqlite"
    _seed_case(path)
    runner = CliRunner()
    first = _invoke(runner, path, [
        "backtest", "run", "--start", "2025-06-01", "--end", "2025-06-30",
    ])
    run_id = re.search(r"# Backtest report: (\S+)", first.output).group(1)
    with BacktestStore(path) as store, store.transaction() as conn:
        conn.execute("UPDATE frozen_memos SET memo_json = '{\"thesis\":\"fixture\"}'")
        conn.execute(
            "UPDATE case_results SET primary_label = 'win' WHERE run_id = ?",
            (run_id,),
        )

    class Assessor:
        def assess(self, **_kwargs):
            return {
                "thesis_correct": False, "narrative": "Mechanism failed.",
                "evidence": ["fact-1"],
            }

    class Evidence:
        def evidence_for(self, **_kwargs):
            return [ContextItem.create(
                kind="news", source_ref="fact-1", available_at=date(2025, 8, 1),
                content="Known by adjudication.",
            )]

    module = ModuleType("test_postmortem_adapter")
    module.build = lambda: {
        "assessor": Assessor(), "evidence_provider": Evidence(),
        "model_id": "local:judge", "prompt_version": "pm-v1",
    }
    monkeypatch.setitem(sys.modules, module.__name__, module)

    result = _invoke(runner, path, [
        "backtest", "postmortem", run_id, "--execute",
        "--adapter", "test_postmortem_adapter:build",
        "--facts-through", "2025-09-01",
    ])

    assert result.exit_code == 0, result.output
    assert "1 cached, 0 pending, 1 updated" in result.output
    with BacktestStore(path) as store, store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM thesis_assessments").fetchone()[0] == 1
        assert conn.execute(
            "SELECT quadrant FROM case_results WHERE run_id = ?", (run_id,),
        ).fetchone()[0] == "wrong-thesis-lucky"
