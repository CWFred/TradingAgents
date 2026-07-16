"""`ops research pause` / `ops research resume`: the operator's ds4 kill
switch for the overnight research window (screen/drain/vet). Pausing drops
a flag file the daemon checks between names; resuming removes it and the
half-hourly overnight job picks work back up."""
from pathlib import Path
import json

import pytest
from click.testing import CliRunner

import ops.cli as cli_mod

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
