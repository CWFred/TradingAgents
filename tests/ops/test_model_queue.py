from datetime import datetime, timezone

import pytest

from ops.model_queue import background_window, coordinate_queue_tick
from ops.work_pause import set_pause

pytestmark = pytest.mark.unit


def at(hour, minute=0, *, day=16):
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def test_background_window_protects_weekday_paper_hours_and_opens_after_train():
    # July is EDT: 14:00 UTC = 10:00 ET; 21:00 UTC = 17:00 ET.
    assert not background_window(at(14)).allowed
    evening = background_window(at(21))
    assert evening.allowed
    assert evening.reason == "after-hours backfill"


def test_weekend_window_runs_until_monday_morning():
    saturday = datetime(2026, 7, 18, 16, tzinfo=timezone.utc)
    window = background_window(saturday)
    assert window.allowed
    assert window.deadline.weekday() == 0
    assert window.deadline.hour == 8


def test_coordinator_is_live_first_then_uses_backtest_backfill(tmp_path):
    order = []
    result = coordinate_queue_tick(
        pause_path=str(tmp_path / "paused"), now=at(21),
        morning_deadline_hour=8,
        run_live=lambda: order.append("live"),
        live_pending=lambda: False,
        run_backtest=lambda: order.append("backtest") or 1,
    )
    assert order == ["live", "backtest"]
    assert result.state == "backtest"


def test_coordinator_never_backfills_while_live_work_remains(tmp_path):
    order = []
    result = coordinate_queue_tick(
        pause_path=str(tmp_path / "paused"), now=at(21),
        morning_deadline_hour=8,
        run_live=lambda: order.append("live"),
        live_pending=lambda: True,
        run_backtest=lambda: order.append("backtest") or 1,
    )
    assert order == ["live"]
    assert result.state == "live-pending"


def test_pause_and_blackout_do_not_touch_any_queue(tmp_path):
    calls = []
    flag = tmp_path / "paused"
    set_pause(flag, now=at(21))
    paused = coordinate_queue_tick(
        pause_path=str(flag), now=at(21), morning_deadline_hour=8,
        run_live=lambda: calls.append("live"), live_pending=lambda: False,
        run_backtest=lambda: 1,
    )
    assert paused.state == "paused"
    assert calls == []

    flag.unlink()
    blackout = coordinate_queue_tick(
        pause_path=str(flag), now=at(14), morning_deadline_hour=8,
        run_live=lambda: calls.append("live"), live_pending=lambda: False,
        run_backtest=lambda: 1,
    )
    assert blackout.state == "blackout"
    assert calls == []
