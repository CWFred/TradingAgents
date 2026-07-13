"""Shared fixtures for the ops test suite."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_xdg_state(monkeypatch, tmp_path):
    # Ops tests construct OpsConfig() with default paths; without this,
    # running the suite touches the REAL ~/.local/state/tradingagents/
    # (guardian.alive, research.paused) and can forge the dashboard's
    # health verdict on the box where dev and prod coincide.
    #
    # Tests that set XDG_STATE_HOME themselves or pass explicit paths still
    # win: monkeypatch.setenv composes, and a later setenv within the test
    # overrides this default.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
