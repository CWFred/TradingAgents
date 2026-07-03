from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from ops.journal import Journal
from ops.broker.types import Position
from ops.notify.summary import emit_daily_summary


def _broker(equity, positions):
    b = MagicMock()
    b.get_equity.return_value = Decimal(equity)
    b.get_positions.return_value = positions
    return b


def test_emits_once_per_day(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    now = datetime(2026, 7, 2, 20, 5, tzinfo=timezone.utc)
    b = _broker("260", [Position("AAPL", Decimal("0.1"), Decimal("200"))])
    assert emit_daily_summary(j, b, now=now) is True
    assert emit_daily_summary(j, b, now=now) is False   # idempotent
    events = [e for e in j.read_events() if e["kind"] == "daily_summary"]
    assert len(events) == 1
    assert events[0]["payload"]["equity"] == "260"


def test_summary_excludes_spot(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    now = datetime(2026, 7, 2, 20, 5, tzinfo=timezone.utc)
    b = _broker("260", [
        Position("AAPL", Decimal("0.1"), Decimal("200")),
        Position("SPOT", Decimal("0.1"), Decimal("500")),
    ])
    emit_daily_summary(j, b, now=now)
    body = [e for e in j.read_events() if e["kind"] == "daily_summary"][0]["payload"]["body"]
    assert "SPOT" not in body
