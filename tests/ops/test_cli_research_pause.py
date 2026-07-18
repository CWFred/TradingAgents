"""Timed/manual pause controls for the live-first background model queue."""
import json
import os
import signal
from pathlib import Path

import pytest
from click.testing import CliRunner

import ops.cli as cli_mod
from ops import events
from ops.journal import Journal
from ops.work_pause import request_immediate_pause

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_RESEARCH_PAUSE_FLAG_PATH", str(tmp_path / "research.paused"))
    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "journal.sqlite"))
    return tmp_path


def test_pause_creates_flag_and_resume_removes_it(env):
    flag = Path(env / "research.paused")
    runner = CliRunner()

    result = runner.invoke(cli_mod.cli, ["research", "pause"])
    assert result.exit_code == 0
    assert flag.exists()
    assert "paused" in result.output

    result = runner.invoke(cli_mod.cli, ["research", "resume"])
    assert result.exit_code == 0
    assert not flag.exists()
    assert "resumed" in result.output


def test_pause_is_idempotent(env):
    runner = CliRunner()
    assert runner.invoke(cli_mod.cli, ["research", "pause"]).exit_code == 0
    result = runner.invoke(cli_mod.cli, ["research", "pause"])
    assert result.exit_code == 0
    assert Path(env / "research.paused").exists()


def test_resume_when_not_paused_is_a_noop(env):
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["research", "resume"])
    assert result.exit_code == 0
    assert "not paused" in result.output


def test_pause_creates_parent_directory(tmp_path, monkeypatch):
    flag = tmp_path / "deep" / "nested" / "research.paused"
    monkeypatch.setenv("OPS_RESEARCH_PAUSE_FLAG_PATH", str(flag))
    result = CliRunner().invoke(cli_mod.cli, ["research", "pause"])
    assert result.exit_code == 0
    assert flag.exists()


def test_timed_pause_records_automatic_expiry(env):
    flag = Path(env / "research.paused")
    result = CliRunner().invoke(cli_mod.cli, ["research", "pause", "--hours", "3"])
    assert result.exit_code == 0
    payload = json.loads(flag.read_text())
    assert payload["until"] > payload["created_at"]
    assert "automatic resume" in result.output


def test_timed_pause_rejects_nonpositive_duration(env):
    result = CliRunner().invoke(cli_mod.cli, ["research", "pause", "--hours", "0"])
    assert result.exit_code != 0
    assert "must be positive" in result.output


def test_immediate_pause_targets_pid_from_running_service_journal(env, monkeypatch):
    journal_path = env / "journal.sqlite"
    with Journal(str(journal_path)) as journal:
        journal.record_event(
            events.KIND_SERVICE_STARTED,
            events.service_started_payload(
                broker_mode="paper", journal_path=str(journal_path), pid=os.getpid(),
            ),
        )
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))

    assert request_immediate_pause(journal_path) is True
    assert sent == [(os.getpid(), signal.SIGURG)]


def test_cli_reports_immediate_request_for_running_daemon(env, monkeypatch):
    journal_path = env / "journal.sqlite"
    with Journal(str(journal_path)) as journal:
        journal.record_event(
            events.KIND_SERVICE_STARTED,
            events.service_started_payload(
                broker_mode="paper", journal_path=str(journal_path), pid=os.getpid(),
            ),
        )
    monkeypatch.setattr(os, "kill", lambda _pid, _sig: None)

    result = CliRunner().invoke(cli_mod.cli, ["research", "pause"])

    assert result.exit_code == 0
    assert "Hard stop sent" in result.output
    assert "locked out" in result.output


def test_timed_pause_cannot_be_shortened_or_resumed_early(env):
    flag = Path(env / "research.paused")
    runner = CliRunner()
    first = runner.invoke(cli_mod.cli, ["research", "pause", "--hours", "3"])
    assert first.exit_code == 0
    original_until = json.loads(flag.read_text())["until"]

    shorter = runner.invoke(cli_mod.cli, ["research", "pause", "--hours", "1"])
    assert shorter.exit_code == 0
    assert json.loads(flag.read_text())["until"] == original_until

    resume = runner.invoke(cli_mod.cli, ["research", "resume"])
    assert resume.exit_code == 0
    assert "resume refused" in resume.output
    assert flag.exists()
