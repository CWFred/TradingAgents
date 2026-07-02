from unittest.mock import patch
from click.testing import CliRunner
from ops.cli import cli


def test_ops_run_invokes_main_run():
    runner = CliRunner()
    with patch("ops.main.run", return_value=0) as m:
        result = runner.invoke(cli, ["run"])
    assert result.exit_code == 0
    m.assert_called_once()


def test_ops_run_propagates_exit_code_2():
    runner = CliRunner()
    with patch("ops.main.run", return_value=2):
        result = runner.invoke(cli, ["run"])
    assert result.exit_code == 2
