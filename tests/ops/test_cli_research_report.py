"""`ops research report` CLI: thin renderer over ops.research.report.
Behavior is covered by tests/ops/research/test_report.py; these only prove
the command wires config paths + stdout/--output correctly."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

import ops.cli as cli_mod

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("OPS_RESEARCH_JOURNAL_PATH", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("OPS_BASELINE_JOURNAL_PATH", str(tmp_path / "baseline.sqlite"))
    return tmp_path


def test_report_echoes_markdown_to_stdout_on_empty_store(env):
    result = CliRunner().invoke(cli_mod.cli, ["research", "report"])
    assert result.exit_code == 0, result.output
    assert "# Research calibration report" in result.output
    assert "## 1. Corpus" in result.output
    assert result.output.count("no data yet") == 6


def test_report_writes_output_file_instead_of_stdout(env, tmp_path):
    out = tmp_path / "report.md"
    result = CliRunner().invoke(cli_mod.cli, ["research", "report", "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert result.output == ""
    text = out.read_text()
    assert "# Research calibration report" in text
    assert "## 6. Per-model attribution" in text
