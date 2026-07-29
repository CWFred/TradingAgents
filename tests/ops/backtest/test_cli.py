import re
import sys
from datetime import date, datetime, timezone
from types import ModuleType

import pytest
from click.testing import CliRunner

from ops.backtest.cases import CaseCandidate
from ops.backtest.generate import FrozenMemoRecord, GenerationRequest
from ops.backtest.models import BacktestCase, CaseSource, ContextItem, ContextManifest
from ops.backtest.service import (
    DEFAULT_BRAIN_VERSION,
    DEFAULT_PROMPT_VERSION,
    GenerationResult,
    InvalidBacktestRequest,
    generate_cases,
    prepare_cases,
    process_enqueued_generation,
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


def test_generate_enqueue_opts_jobs_into_background_processing(tmp_path):
    path = tmp_path / "backtest.sqlite"
    case = _seed_case(path, frozen=False)
    result = _invoke(CliRunner(), path, [
        "backtest", "generate", "--start", "2025-06-01",
        "--end", "2025-06-30", "--enqueue",
    ])
    assert result.exit_code == 0, result.output
    assert "queued for automatic" in result.output
    with BacktestStore(path) as store:
        queued = store.queued_generation_requests(auto_only=True)
    assert [request.case.case_id for request in queued] == [case.case_id]


def test_generate_rejects_execute_and_enqueue_together(tmp_path):
    path = tmp_path / "backtest.sqlite"
    _seed_case(path, frozen=False)
    result = _invoke(CliRunner(), path, [
        "backtest", "generate", "--start", "2025-06-01",
        "--end", "2025-06-30", "--execute", "--enqueue",
    ])
    assert result.exit_code != 0
    assert "either immediate execution or background enqueue" in result.output


def test_background_processor_resumes_explicit_queue_without_touching_plan_only_jobs(
    tmp_path, monkeypatch,
):
    path = tmp_path / "backtest.sqlite"
    auto_case = _seed_case(path, frozen=False, symbol="AUTO")
    manual_case = _seed_case(path, frozen=False, symbol="MANUAL")
    cfg = OpsConfig(
        backtest_store_path=str(path),
        research_pause_flag_path=str(tmp_path / "paused"),
    )
    with BacktestStore(path) as store:
        for case in (auto_case, manual_case):
            request = GenerationRequest.create(
                case=case, manifest=store.get_context_manifest(case.case_id),
                brain_version=DEFAULT_BRAIN_VERSION,
                prompt_version=DEFAULT_PROMPT_VERSION,
                evidence_model_id=cfg.research_evidence_model,
                thesis_model_id=cfg.research_thesis_model,
            )
            store.ensure_generation_job(request)
        requests = store.queued_generation_requests()
        by_symbol = {request.case.symbol: request for request in requests}
        store.enqueue_generation_jobs([by_symbol["AUTO"].generation_key])

    class Backend:
        def ensure_up(self):
            pass

        def shutdown(self):
            pass

    class Client:
        def get_llm(self):
            return object()

    monkeypatch.setattr(
        "ops.llm_backend.build_managed_backend", lambda _cfg: Backend(),
    )
    monkeypatch.setattr(
        "tradingagents.llm_clients.create_llm_client", lambda **_kwargs: Client(),
    )
    monkeypatch.setattr(
        "ops.backtest.service.generate_research_memo",
        lambda request, **_kwargs: FrozenMemoRecord.terminal(
            request, status="rejected", reason="fixture",
        ),
    )

    summary = process_enqueued_generation(config=cfg, max_jobs=1)
    assert summary.attempted == 1
    with BacktestStore(path) as store:
        assert store.frozen_memo_for_case(auto_case.case_id) is not None
        assert store.frozen_memo_for_case(manual_case.case_id) is None


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


def _prepared_from_candidate(candidate):
    def prepare(**kwargs):
        return prepare_cases(
            store=kwargs["store"], sleeve=kwargs["sleeve"],
            start=kwargs["start"], end=kwargs["end"],
            case_count=kwargs["case_count"],
            case_source=lambda **_window: [candidate],
            context_builder=lambda case, _candidate: ContextManifest.create(
                case_id=case.case_id, asof=case.asof,
            ),
        )

    return prepare


def test_generate_source_reconstruction_selects_reconstruction_preparer(
    tmp_path, monkeypatch,
):
    path = tmp_path / "backtest.sqlite"
    cfg = OpsConfig(backtest_store_path=str(path))
    candidate = CaseCandidate(
        symbol="AAA", asof=date(2025, 6, 15), score=3,
        trigger={"kind": "reconstruction"},
        screen_payload={"symbol": "AAA", "asof": "2025-06-15"},
        source_ref="reconstruction:1",
    )
    calls: list[str] = []

    def fake_reconstruction(**kwargs):
        calls.append("reconstruction")
        return _prepared_from_candidate(candidate)(**kwargs)

    def forbidden_default(**_kwargs):
        raise AssertionError("recorded preparer must not run for reconstruction")

    monkeypatch.setattr(
        "ops.backtest.service._reconstruction_prepare_cases", fake_reconstruction,
    )
    monkeypatch.setattr(
        "ops.backtest.service._default_prepare_cases", forbidden_default,
    )

    result = generate_cases(
        config=cfg, sleeve="research", start=date(2025, 6, 1),
        end=date(2025, 6, 30), case_count=40, today=date(2025, 7, 1),
        source="reconstruction",
    )

    assert calls == ["reconstruction"]
    assert (result.total, result.cached, result.pending) == (1, 0, 1)


def test_generate_default_source_selects_recorded_preparer(tmp_path, monkeypatch):
    path = tmp_path / "backtest.sqlite"
    cfg = OpsConfig(backtest_store_path=str(path))
    candidate = CaseCandidate(
        symbol="AAA", asof=date(2025, 6, 15), score=3,
        trigger={"kind": "recorded_live_screen"},
        screen_payload={"symbol": "AAA", "asof": "2025-06-15"},
        source_ref="screen:run:1",
    )
    calls: list[str] = []

    def fake_default(**kwargs):
        calls.append("recorded")
        return _prepared_from_candidate(candidate)(**kwargs)

    def forbidden_reconstruction(**_kwargs):
        raise AssertionError("reconstruction preparer must not run by default")

    monkeypatch.setattr(
        "ops.backtest.service._default_prepare_cases", fake_default,
    )
    monkeypatch.setattr(
        "ops.backtest.service._reconstruction_prepare_cases", forbidden_reconstruction,
    )

    generate_cases(
        config=cfg, sleeve="research", start=date(2025, 6, 1),
        end=date(2025, 6, 30), case_count=40, today=date(2025, 7, 1),
    )

    assert calls == ["recorded"]


def test_generate_explicit_preparer_overrides_source(tmp_path, monkeypatch):
    path = tmp_path / "backtest.sqlite"
    cfg = OpsConfig(backtest_store_path=str(path))
    candidate = CaseCandidate(
        symbol="AAA", asof=date(2025, 6, 15), score=3,
        trigger={"kind": "explicit"},
        screen_payload={"symbol": "AAA", "asof": "2025-06-15"},
        source_ref="explicit:1",
    )
    calls: list[str] = []

    def explicit_preparer(**kwargs):
        calls.append("explicit")
        return _prepared_from_candidate(candidate)(**kwargs)

    def forbidden(**_kwargs):
        raise AssertionError("source selection must not run when preparer is passed")

    monkeypatch.setattr(
        "ops.backtest.service._reconstruction_prepare_cases", forbidden,
    )
    monkeypatch.setattr("ops.backtest.service._default_prepare_cases", forbidden)

    generate_cases(
        config=cfg, sleeve="research", start=date(2025, 6, 1),
        end=date(2025, 6, 30), case_count=40, today=date(2025, 7, 1),
        source="reconstruction", preparer=explicit_preparer,
    )

    assert calls == ["explicit"]


def test_generate_unknown_source_is_rejected(tmp_path):
    path = tmp_path / "backtest.sqlite"
    cfg = OpsConfig(backtest_store_path=str(path))
    with pytest.raises(InvalidBacktestRequest, match="unknown case source"):
        generate_cases(
            config=cfg, sleeve="research", start=date(2025, 6, 1),
            end=date(2025, 6, 30), case_count=40, today=date(2025, 7, 1),
            source="bogus",
        )


def test_generate_cli_threads_source_into_service(tmp_path, monkeypatch):
    path = tmp_path / "backtest.sqlite"
    captured: dict = {}

    def fake_generate_cases(**kwargs):
        captured.update(kwargs)
        return GenerationResult(total=0, cached=0, pending=0)

    monkeypatch.setattr(
        "ops.backtest.service.generate_cases", fake_generate_cases,
    )
    result = _invoke(CliRunner(), path, [
        "backtest", "generate", "--source", "reconstruction",
        "--start", "2025-06-02", "--end", "2025-10-01",
    ])

    assert result.exit_code == 0, result.output
    assert captured["source"] == "reconstruction"


def test_generate_cli_defaults_source_to_recorded(tmp_path, monkeypatch):
    path = tmp_path / "backtest.sqlite"
    captured: dict = {}

    def fake_generate_cases(**kwargs):
        captured.update(kwargs)
        return GenerationResult(total=0, cached=0, pending=0)

    monkeypatch.setattr(
        "ops.backtest.service.generate_cases", fake_generate_cases,
    )
    result = _invoke(CliRunner(), path, [
        "backtest", "generate", "--start", "2025-06-02", "--end", "2025-10-01",
    ])

    assert result.exit_code == 0, result.output
    assert captured["source"] == "recorded"


def test_generate_cli_passes_spacing_to_service(tmp_path, monkeypatch):
    path = tmp_path / "backtest.sqlite"
    captured: dict = {}

    def fake_generate_cases(**kwargs):
        captured.update(kwargs)
        return GenerationResult(total=0, cached=0, pending=0)

    monkeypatch.setattr(
        "ops.backtest.service.generate_cases", fake_generate_cases,
    )
    result = _invoke(CliRunner(), path, [
        "backtest", "generate", "--source", "reconstruction",
        "--start", "2025-06-02", "--end", "2025-10-01", "--spacing", "5",
    ])

    assert result.exit_code == 0, result.output
    assert captured["spacing_sessions"] == 5


def test_generate_cli_defaults_spacing_to_ten(tmp_path, monkeypatch):
    path = tmp_path / "backtest.sqlite"
    captured: dict = {}

    def fake_generate_cases(**kwargs):
        captured.update(kwargs)
        return GenerationResult(total=0, cached=0, pending=0)

    monkeypatch.setattr(
        "ops.backtest.service.generate_cases", fake_generate_cases,
    )
    result = _invoke(CliRunner(), path, [
        "backtest", "generate", "--start", "2025-06-02", "--end", "2025-10-01",
    ])

    assert result.exit_code == 0, result.output
    assert captured["spacing_sessions"] == 10


def test_generate_explicit_preparer_receives_spacing_sessions(tmp_path):
    path = tmp_path / "backtest.sqlite"
    cfg = OpsConfig(backtest_store_path=str(path))
    seen: dict = {}

    def preparer(**kwargs):
        seen.update(kwargs)
        raise SystemExit

    with pytest.raises(SystemExit):
        generate_cases(
            config=cfg, sleeve="research", start=date(2025, 6, 2),
            end=date(2026, 3, 31), case_count=40, today=date(2026, 7, 28),
            source="reconstruction", preparer=preparer, spacing_sessions=5,
        )

    assert seen["spacing_sessions"] == 5


def test_generate_append_with_recorded_source_is_rejected(tmp_path):
    path = tmp_path / "backtest.sqlite"
    cfg = OpsConfig(backtest_store_path=str(path))
    with pytest.raises(InvalidBacktestRequest, match="append is reconstruction-only"):
        generate_cases(
            config=cfg, sleeve="research", start=date(2025, 6, 1),
            end=date(2025, 6, 30), case_count=40, today=date(2025, 7, 1),
            source="recorded", append=True,
        )


def test_generate_cli_threads_append_into_service(tmp_path, monkeypatch):
    path = tmp_path / "backtest.sqlite"
    captured: dict = {}

    def fake_generate_cases(**kwargs):
        captured.update(kwargs)
        return GenerationResult(total=0, cached=0, pending=0)

    monkeypatch.setattr(
        "ops.backtest.service.generate_cases", fake_generate_cases,
    )
    result = _invoke(CliRunner(), path, [
        "backtest", "generate", "--source", "reconstruction",
        "--start", "2025-06-02", "--end", "2025-10-01", "--append",
    ])

    assert result.exit_code == 0, result.output
    assert captured["append"] is True


def test_generate_cli_defaults_append_to_false(tmp_path, monkeypatch):
    path = tmp_path / "backtest.sqlite"
    captured: dict = {}

    def fake_generate_cases(**kwargs):
        captured.update(kwargs)
        return GenerationResult(total=0, cached=0, pending=0)

    monkeypatch.setattr(
        "ops.backtest.service.generate_cases", fake_generate_cases,
    )
    result = _invoke(CliRunner(), path, [
        "backtest", "generate", "--start", "2025-06-02", "--end", "2025-10-01",
    ])

    assert result.exit_code == 0, result.output
    assert captured["append"] is False


def test_generate_cli_passes_controls_to_service(tmp_path, monkeypatch):
    path = tmp_path / "backtest.sqlite"
    captured: dict = {}

    def fake_generate_cases(**kwargs):
        captured.update(kwargs)
        return GenerationResult(total=0, cached=0, pending=0)

    monkeypatch.setattr(
        "ops.backtest.service.generate_cases", fake_generate_cases,
    )
    result = _invoke(CliRunner(), path, [
        "backtest", "generate", "--source", "reconstruction",
        "--start", "2025-06-02", "--end", "2025-10-01", "--controls", "5",
    ])

    assert result.exit_code == 0, result.output
    assert captured["controls_count"] == 5


def test_generate_cli_defaults_controls_to_zero(tmp_path, monkeypatch):
    path = tmp_path / "backtest.sqlite"
    captured: dict = {}

    def fake_generate_cases(**kwargs):
        captured.update(kwargs)
        return GenerationResult(total=0, cached=0, pending=0)

    monkeypatch.setattr(
        "ops.backtest.service.generate_cases", fake_generate_cases,
    )
    result = _invoke(CliRunner(), path, [
        "backtest", "generate", "--start", "2025-06-02", "--end", "2025-10-01",
    ])

    assert result.exit_code == 0, result.output
    assert captured["controls_count"] == 0


def test_generate_explicit_preparer_receives_controls_count(tmp_path):
    path = tmp_path / "backtest.sqlite"
    cfg = OpsConfig(backtest_store_path=str(path))
    seen: dict = {}

    def preparer(**kwargs):
        seen.update(kwargs)
        raise SystemExit

    with pytest.raises(SystemExit):
        generate_cases(
            config=cfg, sleeve="research", start=date(2025, 6, 2),
            end=date(2026, 3, 31), case_count=40, today=date(2026, 7, 28),
            source="reconstruction", preparer=preparer, controls_count=3,
        )

    assert seen["controls_count"] == 3


def test_prices_cli_backfills_window_and_prints_summary(tmp_path, monkeypatch):
    path = tmp_path / "backtest.sqlite"
    BacktestStore(path).close()
    from ops.backtest.price_backfill import BackfillSummary

    captured: dict = {}

    def fake_backfill_prices(config, store, **kwargs):
        captured.update(kwargs)
        captured["store"] = store
        return BackfillSummary(
            symbols=3, bars=120, failures=(("ZZZ", "no data"),),
        )

    monkeypatch.setattr(
        "ops.backtest.price_backfill.backfill_prices", fake_backfill_prices,
    )
    result = _invoke(CliRunner(), path, [
        "backtest", "prices", "--start", "2025-06-02", "--end", "2025-07-15",
    ])

    assert result.exit_code == 0, result.output
    assert captured["sleeve"] == "research"
    assert captured["start"] == date(2025, 6, 2)
    assert captured["end"] == date(2025, 7, 15)
    assert isinstance(captured["store"], BacktestStore)
    assert "3" in result.output and "120" in result.output
    assert "ZZZ" in result.output and "no data" in result.output


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
    assert "approved range 30..100" in bad_count.output

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


def test_selected_cases_includes_all_controls_beyond_case_count_budget(tmp_path):
    """Finding 2 (2026-07-28 review): case_count budgets passers only.
    Near-miss control cases are additive -- they must never be truncated or
    displace a passer from the top-up, since the whole point of a control is
    that it failed the screen and is still there to falsify it."""
    from ops.backtest.service import _selected_cases

    path = tmp_path / "backtest.sqlite"
    with BacktestStore(path) as store:
        for offset, symbol in enumerate(["AAA", "BBB", "CCC"]):
            store.insert_case(BacktestCase.create(
                sleeve="research", symbol=symbol, asof=date(2025, 6, 1 + offset),
                trigger={"kind": "historical_screener_replay"},
            ))
        for offset, symbol in enumerate(["NM1", "NM2"]):
            store.insert_case(BacktestCase.create(
                sleeve="research", symbol=symbol, asof=date(2025, 6, 10 + offset),
                trigger={"kind": "near_miss_control"},
            ))

        cases = _selected_cases(
            store, sleeve="research", start=date(2025, 6, 1), end=date(2025, 6, 30),
            case_count=2,
        )

    passers = [c.symbol for c in cases if c.trigger.get("kind") != "near_miss_control"]
    controls = [c.symbol for c in cases if c.trigger.get("kind") == "near_miss_control"]
    assert passers == ["AAA", "BBB"]
    assert controls == ["NM1", "NM2"]
    assert len(cases) == 4


def test_generate_append_top_up_ignores_controls_in_passer_budget(tmp_path):
    """Finding 2 (2026-07-28 review): the append top-up must compute how many
    more passers to fetch from the stored PASSER count, not the stored total.
    Otherwise stored controls silently shrink (or zero out) the top-up."""
    path = tmp_path / "backtest.sqlite"
    cfg = OpsConfig(backtest_store_path=str(path))
    with BacktestStore(path) as store:
        store.insert_case(BacktestCase.create(
            sleeve="research", symbol="AAA", asof=date(2025, 6, 15),
            trigger={"kind": "historical_screener_replay"},
        ))
        store.insert_case(BacktestCase.create(
            sleeve="research", symbol="NM1", asof=date(2025, 6, 16),
            trigger={"kind": "near_miss_control"},
        ))
        store.insert_case(BacktestCase.create(
            sleeve="research", symbol="NM2", asof=date(2025, 6, 17),
            trigger={"kind": "near_miss_control"},
        ))

    seen: dict = {}

    def preparer(**kwargs):
        seen.update(kwargs)
        raise SystemExit

    with pytest.raises(SystemExit):
        generate_cases(
            config=cfg, sleeve="research", start=date(2025, 6, 1),
            end=date(2025, 6, 30), case_count=30, today=date(2025, 7, 1),
            source="reconstruction", preparer=preparer, append=True,
        )

    # 1 stored passer + 2 controls; case_count=30 must top up by 29 (30-1),
    # never 27 (30-3) -- controls must not eat into the passer budget.
    assert seen["case_count"] == 29
    # Dedupe must still see ALL stored cases so a control is never
    # re-inserted as a duplicate.
    assert set(seen["existing"]) == {
        ("AAA", date(2025, 6, 15)),
        ("NM1", date(2025, 6, 16)),
        ("NM2", date(2025, 6, 17)),
    }
