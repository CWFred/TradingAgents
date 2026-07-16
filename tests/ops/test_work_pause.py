from datetime import datetime, timedelta, timezone

import pytest

from ops.work_pause import clear_pause, pause_state, set_pause

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)


def test_legacy_empty_flag_is_indefinite(tmp_path):
    flag = tmp_path / "research.paused"
    flag.touch()
    state = pause_state(flag, now=NOW)
    assert state.paused
    assert state.indefinite


def test_timed_pause_expires_and_worker_cleans_it_up(tmp_path):
    flag = tmp_path / "research.paused"
    state = set_pause(flag, duration=timedelta(hours=3), now=NOW)
    assert state.until == NOW + timedelta(hours=3)
    assert pause_state(flag, now=NOW + timedelta(hours=2)).paused

    expired = pause_state(
        flag, now=NOW + timedelta(hours=3), cleanup_expired=True,
    )
    assert not expired.paused
    assert not flag.exists()


def test_malformed_pause_fails_closed(tmp_path):
    flag = tmp_path / "research.paused"
    flag.write_text("not-json")
    assert pause_state(flag, now=NOW).indefinite


def test_set_pause_validates_duration_and_clear_is_idempotent(tmp_path):
    flag = tmp_path / "research.paused"
    with pytest.raises(ValueError, match="positive"):
        set_pause(flag, duration=timedelta(0), now=NOW)
    set_pause(flag, now=NOW)
    assert clear_pause(flag)
    assert not clear_pause(flag)
