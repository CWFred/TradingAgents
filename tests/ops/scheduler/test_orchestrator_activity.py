"""Daily-cycle job breadcrumbs from the orchestrator."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ops import events
from ops.activity import ActivityReporter
from ops.journal import Journal
from ops.scheduler.orchestrator import Orchestrator


class _Calendar:
    def is_open_now(self, at=None):
        return True


class _Broker:
    def get_positions(self):
        return []

    def get_equity(self):
        return Decimal("10000")

    def get_cash(self):
        return Decimal("10000")

    def place_order(self, order):
        pass


class _Adapter:
    def session(self):
        from contextlib import nullcontext
        return nullcontext(self)


class _Strategy:
    def propose_orders(self, *, candidates, pipeline, current_equity,
                       asof_date, live_max_position_cap, decision_sink):
        return []


class _Config:
    deny_list = frozenset()
    max_open_positions = 7
    stopout_reentry_cooldown_days = 5
    broker_mode = "paper"


@pytest.fixture()
def journal(tmp_path):
    j = Journal(str(tmp_path / "j.db"))
    yield j
    j.close()


def _orchestrator(journal):
    return Orchestrator(
        broker=_Broker(),
        universe_builder=lambda **kw: [],
        strategy=_Strategy(),
        pipeline_adapter=_Adapter(),
        calendar=_Calendar(),
        journal=journal,
        config=_Config(),
        members_loader=lambda: [],
        momentum_finder=lambda eligible, asof_date: [],
        closes_fetch=lambda *a, **kw: {},
        now_fn=lambda: datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc),
        reporter=ActivityReporter(journal),
    )


def test_cycle_emits_job_pair_with_reason_and_outcome(journal):
    _orchestrator(journal).tick()
    evs = [e for e in journal.read_events() if e["kind"].startswith("activity_")]
    job_starts = [e for e in evs if e["kind"] == events.KIND_ACTIVITY_STARTED
                  and e["payload"]["scope"] == "job"]
    assert job_starts[0]["payload"]["job"] == "daily_cycle"
    assert job_starts[0]["payload"]["reason"] == "attempt 1 of 3"
    job_fins = [e for e in evs if e["kind"] == events.KIND_ACTIVITY_FINISHED
                and e["payload"]["scope"] == "job"]
    assert job_fins[0]["payload"]["ok"] is True
    assert job_fins[0]["payload"]["outcome"] == "analyzed 0, placed 0"


def test_gated_tick_emits_nothing(journal):
    orch = _orchestrator(journal)
    orch.tick()  # completes the day's cycle
    before = len([e for e in journal.read_events()
                  if e["kind"].startswith("activity_")])
    orch.tick()  # gated: already completed today
    after = len([e for e in journal.read_events()
                 if e["kind"].startswith("activity_")])
    assert after == before


def test_second_attempt_reason_says_retrying(journal):
    # First attempt fails inside the cycle -> no completed event; the next
    # tick is attempt 2 and must say so.
    class _FailingStrategy(_Strategy):
        calls = 0

        def propose_orders(self, **kw):
            _FailingStrategy.calls += 1
            if _FailingStrategy.calls == 1:
                raise RuntimeError("LLM backend unreachable")
            return []

    orch = _orchestrator(journal)
    orch._strategy = _FailingStrategy()
    orch.tick()  # attempt 1: fails (recorded via orchestrator_tick_error)
    orch.tick()  # attempt 2
    starts = [e["payload"] for e in journal.read_events()
              if e["kind"] == events.KIND_ACTIVITY_STARTED
              and e["payload"]["scope"] == "job"]
    assert starts[0]["reason"] == "attempt 1 of 3"
    assert starts[1]["reason"] == "attempt 2 of 3, retrying failed cycle"
    fins = [e["payload"] for e in journal.read_events()
            if e["kind"] == events.KIND_ACTIVITY_FINISHED
            and e["payload"]["scope"] == "job"]
    assert fins[0]["ok"] is False
    assert fins[1]["ok"] is True
