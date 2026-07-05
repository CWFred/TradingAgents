"""A1.1: launchd agent template + `ops install-service` renderer.

The renderer only writes a file and prints the launchctl command — it must
never invoke launchctl itself (installing/loading is the user's explicit,
reviewable action)."""
import shutil
import subprocess

import pytest
from click.testing import CliRunner

from ops.cli import cli
from ops.deploy import render_launchd_plist


def _render():
    return render_launchd_plist(
        repo_root="/Users/alice/Code/TradingAgents",
        venv_python="/Users/alice/Code/TradingAgents/.venv/bin/python",
        log_dir="/Users/alice/.local/state/tradingagents/logs",
    )


def test_render_substitutes_all_placeholders():
    rendered = _render()
    assert "{{" not in rendered and "}}" not in rendered
    assert "/Users/alice/Code/TradingAgents/.venv/bin/python" in rendered
    assert "<string>ops.cli</string>" in rendered
    assert "<string>run</string>" in rendered
    assert "/Users/alice/.local/state/tradingagents/logs" in rendered


def test_render_contains_required_launchd_keys():
    rendered = _render()
    # Restart on crash / nonzero exit, but throttled: exit code 3 (broker
    # unreachable) must not hot-loop.
    assert "<key>KeepAlive</key>" in rendered
    assert "<key>Crashed</key>" in rendered
    assert "<key>SuccessfulExit</key>" in rendered
    assert "<key>ThrottleInterval</key>" in rendered
    assert "<integer>60</integer>" in rendered
    assert "<key>RunAtLoad</key>" in rendered
    # Broker mode deliberately NOT set: paper is the default and the live
    # flip must go through the interactive A5 ritual, never a supervisor.
    # (The template's comment may mention the variable; it must never be
    # an actual EnvironmentVariables key.)
    assert "<key>OPS_BROKER_MODE</key>" not in rendered


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil not available (off-macOS)")
def test_rendered_template_is_valid_plist(tmp_path):
    plist = tmp_path / "com.tradingagents.ops.plist"
    plist.write_text(_render())
    result = subprocess.run(
        ["plutil", "-lint", str(plist)], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_install_service_writes_rendered_file_and_prints_bootstrap(tmp_path, monkeypatch):
    output = tmp_path / "LaunchAgents" / "com.tradingagents.ops.plist"

    def _no_subprocess(*args, **kwargs):
        raise AssertionError(f"install-service must not spawn processes: {args}")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _no_subprocess)

    runner = CliRunner()
    result = runner.invoke(cli, ["install-service", "--output", str(output)])
    assert result.exit_code == 0, result.output
    rendered = output.read_text()
    assert "{{" not in rendered
    assert "com.tradingagents.ops" in rendered
    # Prints (never runs) the load command.
    assert "launchctl bootstrap" in result.output
    assert str(output) in result.output
