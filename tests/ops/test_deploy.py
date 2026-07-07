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


def _render_screen():
    from ops.deploy import render_screen_plist

    return render_screen_plist(
        python_path="/Users/alice/Code/TradingAgents/.venv/bin/python",
        repo_dir="/Users/alice/Code/TradingAgents",
        log_dir="/Users/alice/.local/state/tradingagents/logs",
        sec_edgar_user_agent="mail@example.com",
    )


def test_screen_render_substitutes_all_placeholders():
    rendered = _render_screen()
    assert "{{" not in rendered and "}}" not in rendered
    assert "/Users/alice/Code/TradingAgents/.venv/bin/python" in rendered
    assert "<string>screen</string>" in rendered
    assert "<string>--notify</string>" in rendered
    assert "/Users/alice/.local/state/tradingagents/logs" in rendered


def test_screen_render_has_correct_label():
    rendered = _render_screen()
    assert "com.tradingagents.screen" in rendered


def test_screen_render_has_start_calendar_interval():
    rendered = _render_screen()
    assert "<key>StartCalendarInterval</key>" in rendered
    assert "<key>Weekday</key>" in rendered
    assert "<integer>6</integer>" in rendered


def test_screen_render_has_no_keepalive():
    rendered = _render_screen()
    assert "<key>KeepAlive</key>" not in rendered


def test_screen_render_includes_sec_edgar_env():
    rendered = _render_screen()
    assert "<key>SEC_EDGAR_USER_AGENT</key>" in rendered
    assert "mail@example.com" in rendered


@ pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil not available (off-macOS)")
def test_screen_rendered_template_is_valid_plist(tmp_path):
    plist = tmp_path / "com.tradingagents.screen.plist"
    plist.write_text(_render_screen())
    result = subprocess.run(
        ["plutil", "-lint", str(plist)], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_install_screen_service_writes_rendered_file_and_prints_bootstrap(tmp_path, monkeypatch):
    output = tmp_path / "LaunchAgents" / "com.tradingagents.screen.plist"

    def _no_subprocess(*args, **kwargs):
        raise AssertionError(f"install-screen-service must not spawn processes: {args}")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _no_subprocess)
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "Test test@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, ["install-screen-service", "--output", str(output)])
    assert result.exit_code == 0, result.output
    rendered = output.read_text()
    assert "{{" not in rendered
    assert "com.tradingagents.screen" in rendered
    # Prints (never runs) the load command.
    assert "launchctl bootstrap" in result.output
    assert str(output) in result.output


def test_install_screen_service_refuses_empty_user_agent(tmp_path, monkeypatch):
    """Baking an empty SEC_EDGAR_USER_AGENT into the plist guarantees the
    weekly job dies (EdgarNotConfiguredError) before every run — refuse at
    install time instead."""
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    output = tmp_path / "com.tradingagents.screen.plist"
    result = CliRunner().invoke(
        cli, ["install-screen-service", "--output", str(output)])
    assert result.exit_code != 0
    assert "SEC_EDGAR_USER_AGENT" in result.output
    assert not output.exists()


def test_screen_render_xml_escapes_substitutions():
    """SEC fair-access user agents are 'Org contact@email' — an '&' or '<'
    must render as valid plist XML, not a launchctl bootstrap failure."""
    import plistlib

    from ops.deploy import render_screen_plist

    rendered = render_screen_plist(
        python_path="/x/.venv/bin/python", repo_dir="/x",
        log_dir="/x/logs",
        sec_edgar_user_agent="Smith & Co <admin@smith.co>",
    )
    assert "Smith &amp; Co &lt;admin@smith.co&gt;" in rendered
    parsed = plistlib.loads(rendered.encode())
    env = parsed["EnvironmentVariables"]
    assert env["SEC_EDGAR_USER_AGENT"] == "Smith & Co <admin@smith.co>"
