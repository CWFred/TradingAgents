"""Dashboard plist rendering + install-service writing both agents."""
from click.testing import CliRunner

from ops.cli import cli
from ops.deploy import render_dashboard_plist


def test_render_dashboard_plist_substitutes_paths():
    rendered = render_dashboard_plist(
        repo_root="/repo", venv_python="/repo/.venv/bin/python",
        log_dir="/logs")
    assert "com.tradingagents.dashboard" in rendered
    assert "<string>dashboard</string>" in rendered
    assert "/logs/dashboard.out.log" in rendered
    assert "{{" not in rendered


def test_install_service_writes_both_plists(tmp_path):
    runner = CliRunner()
    ops_plist = tmp_path / "com.tradingagents.ops.plist"
    result = runner.invoke(cli, [
        "install-service", "--output", str(ops_plist),
        "--log-dir", str(tmp_path / "logs")])
    assert result.exit_code == 0, result.output
    assert ops_plist.exists()
    dash_plist = tmp_path / "com.tradingagents.dashboard.plist"
    assert dash_plist.exists()
    assert "com.tradingagents.dashboard" in dash_plist.read_text()
    # Both load commands printed.
    assert result.output.count("launchctl bootstrap") == 2
