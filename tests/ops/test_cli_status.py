"""`ops status` CLI (A4): a thin plain-text renderer over
ops.status.build_status. Behavior tests live in test_status.py; these
only prove the command wires journal resolution + rendering correctly.
"""
from datetime import datetime, timezone
from decimal import Decimal

from click.testing import CliRunner

from ops.cli import cli
from ops.journal import Journal


def _seed(path: str) -> None:
    j = Journal(path)
    j.record_cash_adjustment(kind="seed", amount=Decimal("250"))
    j.record_order(
        client_order_id="c1", symbol="AAPL", side="BUY",
        notional_dollars=Decimal("20"), stop_loss_price=None,
    )
    j.record_fill(
        order_id="o1", client_order_id="c1", symbol="AAPL", side="BUY",
        quantity=Decimal("0.1"), price=Decimal("200"),
        filled_at=datetime.now(timezone.utc),
        stop_loss_price=Decimal("184"),
    )
    j.close()


def test_status_command_renders_journal_view(tmp_path):
    path = str(tmp_path / "j.sqlite")
    _seed(path)
    res = CliRunner().invoke(cli, ["status", "--journal", path])
    assert res.exit_code == 0, res.output
    assert path in res.output
    assert "journal view" in res.output
    assert "AAPL" in res.output
    assert "$230" in res.output        # replayed cash: 250 seed - 20 BUY


def test_status_without_journal_flag_uses_load_config_default(tmp_path, monkeypatch):
    """Same resolution rule as notify-once/run: without --journal the
    command reads load_config().journal_path, not a CWD-relative literal."""
    path = str(tmp_path / "default_j.sqlite")
    monkeypatch.setenv("OPS_JOURNAL_PATH", path)
    _seed(path)
    res = CliRunner().invoke(cli, ["status"])
    assert res.exit_code == 0, res.output
    assert path in res.output
    assert "AAPL" in res.output
