"""Guardian liveness file: mtime = last pass start (dashboard reads it)."""
import os
from decimal import Decimal

from ops import build_guarded_paper_broker
from ops.config import OpsConfig
from ops.journal import Journal
from ops.position_guardian import PositionGuardian


def _make_guardian(tmp_path, liveness_path):
    # Mirror the house guardian-construction pattern (see
    # tests/ops/test_position_guardian.py): a real GuardedPaperBroker wired
    # with keyword-only args, not the positional fake-broker sketch — the
    # guardian's __init__ is keyword-only.
    journal = Journal(str(tmp_path / "j.sqlite"))
    cfg = OpsConfig(
        journal_path=str(tmp_path / "j.sqlite"),
        guardian_liveness_path=str(liveness_path),
    )
    broker = build_guarded_paper_broker(
        config=cfg,
        journal=journal,
        quote_source=lambda symbol: Decimal("10"),
        starting_cash=Decimal("100"),
        start_of_day_equity=lambda: Decimal("100"),
        start_of_week_equity=lambda: Decimal("100"),
    )
    return PositionGuardian(
        broker=broker,
        quote_source=lambda symbol: Decimal("10"),
        config=cfg,
        journal=journal,
        # Market closed: the touch must happen BEFORE the market-hours
        # gate — liveness answers "is the loop scheduled?", which
        # overnight passes still answer yes to.
        market_open_fn=lambda: False,
    )


def test_pass_touches_liveness_file(tmp_path):
    liveness = tmp_path / "state" / "guardian.alive"
    g = _make_guardian(tmp_path, liveness)
    assert not liveness.exists()
    g.check_stops_once()
    assert liveness.exists()


def test_second_pass_updates_mtime(tmp_path):
    liveness = tmp_path / "guardian.alive"
    g = _make_guardian(tmp_path, liveness)
    g.check_stops_once()
    os.utime(liveness, (1, 1))  # backdate instead of sleeping
    g.check_stops_once()
    assert os.stat(liveness).st_mtime > 1


def test_touch_failure_never_breaks_pass(tmp_path):
    # A directory at the file's path makes touch() raise OSError.
    liveness = tmp_path / "guardian.alive"
    liveness.mkdir()
    g = _make_guardian(tmp_path, liveness)
    g.check_stops_once()  # must not raise
