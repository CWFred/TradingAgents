"""Overnight tick job bracket: reason string, aggregated outcome, and
idle-night silence (no job events when the tick short-circuits before ever
touching ds4)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from ops import events
from ops.config import OpsConfig
from ops.journal import Journal
from ops.main import _research_overnight_tick
from ops.research.drain import DrainSummary
from ops.research.store import ScreenStore
from ops.research.vetting import VettingSummary
from tradingagents.memos.store import MemoStore

pytestmark = pytest.mark.unit


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    # Isolate every sleeve path under tmp — same isolation as
    # tests/ops/test_main_short.py / test_main_insider.py.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")
    return OpsConfig()


@pytest.fixture
def journal(tmp_path):
    with Journal(str(tmp_path / "main.sqlite")) as j:
        yield j


class _Backend:
    def ensure_up(self):
        pass

    def shutdown(self):
        pass


def _fix_window(monkeypatch):
    """Pin the tick's deadline/now to fixed instants — the overnight window
    check otherwise depends on the real America/New_York wall clock
    (_overnight_deadline uses datetime.now() when not passed a `now`), which
    is the repo's known "only passes 00:00-08:00 local" test gotcha."""
    import ops.main as main_mod

    now = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)
    deadline = now + timedelta(hours=6)
    monkeypatch.setattr(main_mod, "_overnight_deadline", lambda hour, **kw: deadline)
    monkeypatch.setattr(main_mod, "build_managed_backend", lambda c: _Backend())
    return now


def _pending_vetting_memo(ticker="ACME"):
    """A minimal brain-buy memo in the pending_vetting state, mirroring the
    fixture in tests/ops/test_main.py::_pending_vetting_memo."""
    from tradingagents.memos.schema import EvidenceItem, Falsifier, Memo, ValueThesis

    return Memo(
        ticker=ticker, as_of_date=date(2026, 7, 1), thesis_type="value",
        thesis="cheap for a fixable reason",
        evidence=[EvidenceItem(claim="c", source_type="filing", source_ref="0001:mdna")],
        value_block=ValueThesis(
            why_cheap="segment decline", change_trigger="new CEO",
            normalized_earnings_view="2x", quality_assessment="net cash",
        ),
        conviction_tier="starter", entry_price_ref=10.0,
        price_target_low=15.0, price_target_high=20.0,
        expected_holding_months=12, must_be_true=["m"],
        falsifiers=[Falsifier(description="margin collapse",
                              check_type="fundamental", metric="gross_margin_pct",
                              operator="<", threshold=30.0)],
        status="pending_vetting",
    )


def test_overnight_job_bracket_reason_and_outcome(journal, cfg, monkeypatch):
    now = _fix_window(monkeypatch)

    # Screen not due: a run recorded moments ago satisfies the interval gate.
    store = ScreenStore(cfg.screen_store_path)
    store.record_run(asof=date.today(), universe_size=0, results=[])
    store.enqueue_hit("AAA", asof=date.today(), payload={"symbol": "AAA"}, source="test")
    store.enqueue_hit("BBB", asof=date.today(), payload={"symbol": "BBB"}, source="test")

    memo_store = MemoStore(cfg.memo_store_path)
    memo_store.save(_pending_vetting_memo())

    # Short + insider queues stay empty (never seeded) — their passes must
    # no-op without touching ds4 or emitting activity events of their own.

    def _fake_vet(**kw):
        for m in memo_store.pending_vetting_memos():
            memo_store.mark_passed(m.memo_id)
        return VettingSummary(vetted=1, confirmed=1, rejected=0, failed=0,
                              still_pending=0, hit_deadline=False)

    monkeypatch.setattr("ops.research.vetting.vet_pending", _fake_vet)

    def _fake_drain(**kw):
        for hit in store.pending_hits():
            store.mark_researched(hit["id"])
        return DrainSummary(researched=2, failed=0, still_pending=0, hit_deadline=False)

    monkeypatch.setattr("ops.research.drain.drain_pending", _fake_drain)

    _research_overnight_tick(journal, cfg, now=lambda: now)

    starts = [e["payload"] for e in journal.read_events()
              if e["kind"] == events.KIND_ACTIVITY_STARTED
              and e["payload"]["scope"] == "job"]
    assert len(starts) == 1
    assert starts[0]["job"] == "overnight"
    assert starts[0]["reason"] == "2 hit(s) to research; 1 memo(s) to vet"

    fins = [e["payload"] for e in journal.read_events()
            if e["kind"] == events.KIND_ACTIVITY_FINISHED
            and e["payload"]["scope"] == "job"]
    assert len(fins) == 1
    assert fins[0]["ok"] is True
    assert fins[0]["outcome"].startswith("researched ")
    assert fins[0]["outcome"] == "researched 2, vetted 1, failed 0"


def test_idle_night_emits_no_job_events(journal, cfg, monkeypatch):
    now = _fix_window(monkeypatch)

    # All queues empty and screen not due -> the tick returns before ever
    # constructing the reporter/job bracket.
    store = ScreenStore(cfg.screen_store_path)
    store.record_run(asof=date.today(), universe_size=0, results=[])

    _research_overnight_tick(journal, cfg, now=lambda: now)

    assert not [e for e in journal.read_events() if e["kind"].startswith("activity_")]
