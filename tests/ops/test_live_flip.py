"""A5 live-flip ritual: the FIRST robinhood start requires a human at a
terminal to type the account equity back verbatim. The flip marker means
"ritual passed" — it must not be recorded on a refused attempt, and once
it exists, restarts are unattended (launchd must never be prompted)."""
import io
from decimal import Decimal

import pytest

from ops.journal import Journal
from ops.live_gate import flip_epoch, record_flip_marker


@pytest.fixture
def preset_shutdown():
    import ops.main as ops_main
    ops_main._shutdown_event.set()
    yield
    ops_main._shutdown_event.clear()


@pytest.fixture
def live_env(monkeypatch, tmp_path):
    """robinhood-mode env with a FakeMCPClient (equity $1000) and a tmp
    journal; returns the journal path."""
    from tests.ops.broker.fakes import FakeMCPClient

    journal_path = str(tmp_path / "j.sqlite")
    monkeypatch.setenv("OPS_BROKER_MODE", "robinhood")
    monkeypatch.setenv("OPS_JOURNAL_PATH", journal_path)
    monkeypatch.setattr(
        "ops.broker.mcp_client.RealRobinhoodMCPClient",
        lambda: FakeMCPClient(cash=Decimal("1000")),
    )
    return journal_path


class _TTYStringIO(io.StringIO):
    """Monkeypatched sys.stdin that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class _ForbiddenStdin:
    """stdin that must never be consulted (marker-already-present path)."""

    def isatty(self) -> bool:
        raise AssertionError("stdin must not be touched when flip marker exists")

    def readline(self, *args):
        raise AssertionError("stdin must not be read when flip marker exists")

    def read(self, *args):
        raise AssertionError("stdin must not be read when flip marker exists")


def _events(journal_path):
    j = Journal(journal_path)
    try:
        return j.read_events()
    finally:
        j.close()


def test_first_live_correct_equity_records_marker_and_starts(
    monkeypatch, live_env, preset_shutdown, capsys,
):
    from ops.main import run

    monkeypatch.setattr("sys.stdin", _TTYStringIO("1000\n"))
    exit_code = run()
    assert exit_code == 0
    j = Journal(live_env)
    assert flip_epoch(j) is not None
    j.close()
    # The equity figure the user had to re-type was printed verbatim.
    assert "1000" in capsys.readouterr().out


def test_first_live_wrong_equity_refuses_exit_4_no_marker_no_scheduler(
    monkeypatch, live_env, preset_shutdown, capsys,
):
    from ops.main import run

    started = []
    monkeypatch.setattr(
        "ops.main._start_full_scheduler",
        lambda *a, **k: started.append("full") or (_ for _ in ()).throw(AssertionError),
    )
    monkeypatch.setattr(
        "ops.main._start_guardian_only",
        lambda *a, **k: started.append("guardian") or (_ for _ in ()).throw(AssertionError),
    )
    monkeypatch.setattr("sys.stdin", _TTYStringIO("999.99\n"))

    exit_code = run()
    assert exit_code == 4
    assert started == []
    events = _events(live_env)
    kinds = [e["kind"] for e in events]
    assert "live_flip_refused" in kinds
    j = Journal(live_env)
    assert flip_epoch(j) is None
    j.close()
    stopping = [e for e in events if e["kind"] == "service_stopping"]
    assert stopping[-1]["payload"]["exit_code"] == 4


def test_first_live_non_tty_refuses_outright(
    monkeypatch, live_env, preset_shutdown, capsys,
):
    """A supervisor (launchd) must never be able to perform the first
    live flip: non-TTY stdin refuses before any prompt."""
    from ops.main import run

    monkeypatch.setattr("sys.stdin", io.StringIO("1000\n"))  # isatty() is False
    exit_code = run()
    assert exit_code == 4
    events = _events(live_env)
    refused = [e for e in events if e["kind"] == "live_flip_refused"]
    assert len(refused) == 1
    assert refused[0]["payload"]["reason"] == "non_tty"
    j = Journal(live_env)
    assert flip_epoch(j) is None
    j.close()


def test_marker_already_present_skips_ritual_entirely(
    monkeypatch, live_env, preset_shutdown,
):
    """Graduated already: restarts must be unattended — stdin is never
    consulted, no prompt, normal startup."""
    from ops.main import run

    j = Journal(live_env)
    record_flip_marker(j)
    j.close()
    monkeypatch.setattr("sys.stdin", _ForbiddenStdin())
    exit_code = run()
    assert exit_code == 0


def test_live_flip_refused_is_audit_only():
    from ops.notify.policy import POLICY

    assert "live_flip_refused" not in POLICY


def test_build_broker_no_longer_records_flip_marker(monkeypatch, tmp_path):
    """The marker must mean 'ritual passed' — merely building a robinhood
    broker (which also happens on refused attempts) must not record it."""
    from ops.config import OpsConfig
    from ops.main import _build_broker
    from tests.ops.broker.fakes import FakeMCPClient

    monkeypatch.setattr(
        "ops.broker.mcp_client.RealRobinhoodMCPClient",
        lambda: FakeMCPClient(),
    )
    j = Journal(str(tmp_path / "j.sqlite"))
    _build_broker(OpsConfig(broker_mode="robinhood"), j)
    assert flip_epoch(j) is None
    j.close()
