from click.testing import CliRunner

from ops.cli import cli
from ops.journal import Journal


def test_notify_once_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("OPS_PUSHOVER_USER_KEY", raising=False)
    path = str(tmp_path / "j.sqlite")
    j = Journal(path)
    j.record_event("fill", {"symbol": "AAPL", "side": "BUY",
                            "quantity": "0.1", "price": "200", "context": "place"})
    j.close()
    res = CliRunner().invoke(cli, ["notify-once", "--journal", path])
    assert res.exit_code == 0
    assert "dispatched" in res.output.lower()


def test_notify_once_without_journal_flag_uses_load_config_default(tmp_path, monkeypatch):
    """Without --journal, notify-once must resolve the journal path the same
    way the always-on service does: load_config().journal_path (the XDG
    state default), not a CWD-relative hardcoded literal. OPS_JOURNAL_PATH
    is monkeypatched to a tmp path so this doesn't touch the real home
    directory or the user's actual journal."""
    path = str(tmp_path / "default_j.sqlite")
    monkeypatch.setenv("OPS_JOURNAL_PATH", path)
    monkeypatch.delenv("OPS_PUSHOVER_USER_KEY", raising=False)
    j = Journal(path)
    j.record_event("fill", {"symbol": "AAPL", "side": "BUY",
                            "quantity": "0.1", "price": "200", "context": "place"})
    j.close()

    res = CliRunner().invoke(cli, ["notify-once"])

    assert res.exit_code == 0
    assert "dispatched" in res.output.lower()
    j2 = Journal(path)
    assert j2.get_cursor("notify") == 1    # the tmp-path journal was opened
    j2.close()
