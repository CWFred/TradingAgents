"""CLI `screen --notify` alarm behavior (no network; run_screen patched)."""
from datetime import date
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ops.cli import cli
from ops.research.run import ScreenRunSummary

pytestmark = pytest.mark.unit


class _RecordingTransport:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


def _summary(universe_size, errors=()):
    return ScreenRunSummary(
        run_id="r1", asof=date.today(), universe_size=universe_size,
        screened=max(0, universe_size - len(errors)), passed=(),
        errors=tuple(errors), baseline=None, coverage={},
    )


def test_empty_universe_is_blind_not_success():
    """The 07-06 incident mode: fetch failures empty the universe entirely.
    That must alarm, not push a cheerful 'screen complete: 0 screened'."""
    transport = _RecordingTransport()
    with patch("ops.research.run.run_screen", return_value=_summary(0)), \
         patch("ops.notify.push.build_push_transport", return_value=transport):
        result = CliRunner().invoke(cli, ["screen", "--notify"])
    assert result.exit_code == 2
    assert len(transport.sent) == 1
    assert "BLIND" in transport.sent[0].title


def test_majority_errors_is_blind():
    transport = _RecordingTransport()
    summary = _summary(10, errors=tuple(f"S{i}: boom" for i in range(6)))
    with patch("ops.research.run.run_screen", return_value=summary), \
         patch("ops.notify.push.build_push_transport", return_value=transport):
        result = CliRunner().invoke(cli, ["screen", "--notify"])
    assert result.exit_code == 2
    assert transport.sent[0].urgency == "high"


def test_whole_run_crash_still_notifies_and_fails():
    """--notify must not go silent when run_screen itself raises (Nasdaq 403,
    EdgarNotConfiguredError, ...) — the unattended Saturday job's only
    signal is this push."""
    transport = _RecordingTransport()
    with patch("ops.research.run.run_screen",
               side_effect=RuntimeError("nasdaq screener 403")), \
         patch("ops.notify.push.build_push_transport", return_value=transport):
        result = CliRunner().invoke(cli, ["screen", "--notify"])
    assert result.exit_code != 0
    assert len(transport.sent) == 1
    assert "FAILED" in transport.sent[0].title
    assert transport.sent[0].urgency == "high"
    assert "nasdaq screener 403" in transport.sent[0].body


def test_healthy_run_pushes_normal_summary():
    transport = _RecordingTransport()
    with patch("ops.research.run.run_screen", return_value=_summary(10)), \
         patch("ops.notify.push.build_push_transport", return_value=transport):
        result = CliRunner().invoke(cli, ["screen", "--notify"])
    assert result.exit_code == 0
    assert transport.sent[0].title == "screen complete"
    assert transport.sent[0].urgency == "normal"
