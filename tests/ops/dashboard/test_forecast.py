"""Gate-aware next-work forecast. All scenarios frozen-clock."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ops import events
from ops.dashboard.forecast import next_work
from ops.journal import Journal

ET = ZoneInfo("America/New_York")


class _Calendar:
    """Weekdays are trading days; no holidays."""

    def is_trading_day(self, d):
        return d.weekday() < 5


@pytest.fixture()
def config(tmp_path):
    class _C:
        journal_path = str(tmp_path / "j.db")
        screen_store_path = str(tmp_path / "screen.db")
        short_screen_store_path = str(tmp_path / "short_screen.db")
        memo_store_path = str(tmp_path / "memos.db")
        short_memo_store_path = str(tmp_path / "short_memos.db")
        insider_signal_store_path = str(tmp_path / "insider.db")
        research_screen_interval_days = 3
        research_drain_deadline_hour = 8
        daily_analysis_budget = 8
    Journal(_C.journal_path).close()
    return _C()


def _et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def _seed_screen_run(config, created_at_iso):
    # ScreenStore.record_run takes `results: list[ScreenResult]` (not
    # `passed`) and has no `created_at` param — it always stamps "now".
    # To pin an exact created_at for the test, create the schema via the
    # real store (so we don't hand-roll it and drift from the real DDL)
    # and then insert the row directly.
    import sqlite3

    from ops.research.store import ScreenStore
    ScreenStore(config.screen_store_path)
    conn = sqlite3.connect(config.screen_store_path)
    conn.execute(
        "INSERT INTO screen_runs (run_id, asof, created_at, universe_size, passed_count)"
        " VALUES ('r1', '2026-07-12', ?, 500, 0)", (created_at_iso,))
    conn.commit()
    conn.close()


# --- daily cycle ---

def test_cycle_not_done_predicts_next_halfhour_tick(config):
    # Tuesday 2026-07-14 13:12 ET, cycle not completed, 1 failed attempt
    with Journal(config.journal_path) as j:
        j.record_event(events.KIND_DAILY_CYCLE_RUN, {"asof_date": "2026-07-14"},
                       at=_et(2026, 7, 14, 10, 0))
    out = next_work(config, now=_et(2026, 7, 14, 13, 12), calendar=_Calendar())
    cycle = [w for w in out if w["job"] == "daily_cycle"][0]
    assert cycle["at"] == _et(2026, 7, 14, 13, 30).astimezone(timezone.utc)
    assert cycle["purpose"] == "retry daily cycle, attempt 2 of 3"


def test_cycle_done_today_predicts_tomorrow(config):
    with Journal(config.journal_path) as j:
        j.record_event(events.KIND_DAILY_CYCLE_COMPLETED,
                       {"asof_date": "2026-07-14"}, at=_et(2026, 7, 14, 10, 0))
    out = next_work(config, now=_et(2026, 7, 14, 13, 12), calendar=_Calendar())
    cycle = [w for w in out if w["job"] == "daily_cycle"][0]
    assert cycle["at"] == _et(2026, 7, 15, 9, 0).astimezone(timezone.utc)
    assert cycle["purpose"] == (
        "daily cycle: leaderboard, exits, up to 8 analyses")


def test_friday_evening_predicts_monday(config):
    # Friday 2026-07-17 16:00 ET, cycle done
    with Journal(config.journal_path) as j:
        j.record_event(events.KIND_DAILY_CYCLE_COMPLETED,
                       {"asof_date": "2026-07-17"}, at=_et(2026, 7, 17, 10, 0))
    out = next_work(config, now=_et(2026, 7, 17, 16, 0), calendar=_Calendar())
    cycle = [w for w in out if w["job"] == "daily_cycle"][0]
    assert cycle["at"] == _et(2026, 7, 20, 9, 0).astimezone(timezone.utc)


def test_attempts_exhausted_predicts_tomorrow(config):
    with Journal(config.journal_path) as j:
        for h in (9, 10, 11):
            j.record_event(events.KIND_DAILY_CYCLE_RUN,
                           {"asof_date": "2026-07-14"}, at=_et(2026, 7, 14, h, 0))
    out = next_work(config, now=_et(2026, 7, 14, 11, 40), calendar=_Calendar())
    cycle = [w for w in out if w["job"] == "daily_cycle"][0]
    assert cycle["at"].astimezone(ET).date() == date(2026, 7, 15)


# --- overnight ---

def test_overnight_purpose_lists_queues(config):
    from ops.research.store import ScreenStore
    store = ScreenStore(config.screen_store_path)
    # ScreenStore.enqueue_hit's TTL kwarg is `ttl_days`, not
    # `screen_ttl_days` as the brief's draft used — corrected here.
    store.enqueue_hit(symbol="AAA", asof=date(2026, 7, 14), payload={},
                      ttl_days=0)
    store.enqueue_hit(symbol="BBB", asof=date(2026, 7, 14), payload={},
                      ttl_days=0)
    out = next_work(config, now=_et(2026, 7, 14, 13, 0), calendar=_Calendar())
    night = [w for w in out if w["job"] == "overnight"][0]
    assert night["at"] == _et(2026, 7, 15, 0, 0).astimezone(timezone.utc)
    assert "2 hit(s) to research" in night["purpose"]
    assert "screen due" in night["purpose"]  # no screen run recorded yet


def test_overnight_idle_when_queues_empty_and_screen_fresh(config):
    _seed_screen_run(config, "2026-07-14T04:00:00+00:00")
    out = next_work(config, now=_et(2026, 7, 14, 13, 0), calendar=_Calendar())
    night = [w for w in out if w["job"] == "overnight"][0]
    assert night["purpose"].startswith("likely idle: queues empty")
    assert "2026-07-17" in night["purpose"]


def test_inside_window_predicts_next_halfhour(config):
    # 01:10 ET: the overnight window is live; next fire is 01:30
    out = next_work(config, now=_et(2026, 7, 15, 1, 10), calendar=_Calendar())
    night = [w for w in out if w["job"] == "overnight"][0]
    assert night["at"] == _et(2026, 7, 15, 1, 30).astimezone(timezone.utc)


def test_sorted_by_time(config):
    out = next_work(config, now=_et(2026, 7, 14, 13, 0), calendar=_Calendar())
    ats = [w["at"] for w in out]
    assert ats == sorted(ats)
