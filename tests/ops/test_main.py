import os
from decimal import Decimal

import pytest

from ops.config import OpsConfig
from ops.journal import Journal
from ops.main import _build_broker, _emit_halt_events, _wire
from ops.reconcile import PositionDiff, ReconcileResult


@pytest.fixture(autouse=True)
def _isolate_new_sleeve_state(monkeypatch, tmp_path_factory):
    """The overnight tick now also services the short and insider sleeves.
    Every test in this module predates them, so isolate their state paths
    to tmp (never the operator's real XDG state). Empty stores read as "no
    short/insider work" (screen cadence belongs to the tick's combined
    run_screens stage, not the sleeve passes), so quiet-night semantics
    hold without any seeding."""
    base = tmp_path_factory.mktemp("sleeves")
    monkeypatch.setenv("OPS_SHORT_JOURNAL_PATH", str(base / "short_journal.sqlite"))
    monkeypatch.setenv("OPS_SHORT_MEMO_STORE_PATH", str(base / "short_memos.sqlite"))
    monkeypatch.setenv("OPS_SHORT_SCREEN_STORE_PATH", str(base / "short_screen.sqlite"))
    monkeypatch.setenv("OPS_INSIDER_JOURNAL_PATH", str(base / "insider_journal.sqlite"))
    monkeypatch.setenv("OPS_INSIDER_MEMO_STORE_PATH", str(base / "insider_memos.sqlite"))
    monkeypatch.setenv("OPS_INSIDER_SIGNAL_STORE_PATH", str(base / "insider_signals.sqlite"))


@pytest.fixture
def preset_shutdown():
    """Pre-set the module-level shutdown event so run() falls straight
    through _run_until_signal instead of blocking the test forever."""
    import ops.main as ops_main
    ops_main._shutdown_event.set()
    yield
    ops_main._shutdown_event.clear()


def test_build_broker_paper(tmp_path):
    cfg = OpsConfig()  # broker_mode default "paper"
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _build_broker(cfg, j)
    # We don't assert internal type — just that place_order is callable
    assert callable(broker.place_order)


def test_build_broker_robinhood(monkeypatch, tmp_path):
    cfg = OpsConfig(broker_mode="robinhood")
    j = Journal(str(tmp_path / "j.sqlite"))
    # Stub RealRobinhoodMCPClient so no OAuth flow triggers.
    from tests.ops.broker.fakes import FakeMCPClient
    monkeypatch.setattr(
        "ops.broker.mcp_client.RealRobinhoodMCPClient",
        lambda: FakeMCPClient(),
    )
    broker = _build_broker(cfg, j)
    assert callable(broker.place_order)


def test_wire_returns_orchestrator_guardian_calendar(tmp_path):
    cfg = OpsConfig()
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _build_broker(cfg, j)
    orch, guardian, cal, backend = _wire(broker, j, cfg)
    assert callable(orch.tick)
    assert callable(guardian.check_stops_once)
    assert callable(cal.is_open_now)


class _RecordingBackend:
    def __init__(self):
        self.shutdown_calls = 0

    def ensure_up(self):
        pass

    def shutdown(self):
        self.shutdown_calls += 1


def test_wire_injects_and_returns_managed_backend(tmp_path):
    """The managed backend is threaded into the pipeline adapter and returned
    so the service can tear it down on shutdown."""
    cfg = OpsConfig()
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _build_broker(cfg, j)
    fake = _RecordingBackend()
    orch, guardian, cal, backend = _wire(broker, j, cfg, backend=fake)
    assert backend is fake
    assert orch._pipeline_adapter._backend is fake


def test_run_shuts_down_managed_backend_in_finally(
    monkeypatch, tmp_path, preset_shutdown, capsys,
):
    """Safety net: the service tears the managed backend down on the way out,
    even though the per-tick session normally already has."""
    from ops.main import run

    fake = _RecordingBackend()
    monkeypatch.setattr("ops.main.build_managed_backend", lambda cfg: fake)
    monkeypatch.delenv("OPS_BROKER_MODE", raising=False)
    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))

    exit_code = run()
    assert exit_code == 0
    assert fake.shutdown_calls >= 1


def test_wire_uses_composite_universe_builder(tmp_path):
    from ops.universe.composite import build_composite_universe

    cfg = OpsConfig()
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _build_broker(cfg, j)
    orch, _guardian, _cal, _backend = _wire(broker, j, cfg)
    assert orch._universe_builder is build_composite_universe


def test_emit_halt_events_writes_inconsistency_and_startup_halted(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    result = ReconcileResult(
        diffs=[PositionDiff(symbol="AAPL", journal_qty=Decimal("5"),
                            broker_qty=Decimal("3"), kind="qty_mismatch")],
        cash_journal=Decimal("100"), cash_broker=Decimal("100"),
        cash_diff=Decimal("0"),
    )
    _emit_halt_events(j, result)
    kinds = [e["kind"] for e in j.read_events()]
    assert "inconsistency" in kinds
    assert "startup_halted" in kinds


def test_paper_mode_restart_preserves_positions(tmp_path):
    """Two ops-run sessions against the same journal must see the same
    positions and stops. Session B rehydrates from the journal that
    session A wrote, so a BUY placed in A is visible (with its stop)
    in B without any reconciler diffs."""
    from ops import build_guarded_paper_broker_from_journal
    from ops.broker.types import Order, OrderType, Side
    from ops.reconcile import reconcile as _reconcile

    class _Q:
        def __init__(self): self._m = {}
        def set(self, s, p): self._m[s] = p
        def get(self, s): return self._m[s]

    journal_path = str(tmp_path / "j.sqlite")
    quotes = _Q()
    quotes.set("AAPL", Decimal("10"))

    # Session A — place a BUY with a stop.
    j_a = Journal(journal_path)
    broker_a = build_guarded_paper_broker_from_journal(
        config=OpsConfig(), journal=j_a, quote_source=quotes.get,
        starting_cash=Decimal("250"),
        start_of_day_equity=lambda: Decimal("250"),
        start_of_week_equity=lambda: Decimal("250"),
    )
    broker_a.place_order(Order(
        client_order_id="b-1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("20"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.05"),
    ))
    positions_a = broker_a.get_positions()
    assert len(positions_a) == 1
    assert positions_a[0].stop_loss_price == Decimal("9.5")
    j_a.close()

    # Session B — restart on the same journal.
    j_b = Journal(journal_path)
    broker_b = build_guarded_paper_broker_from_journal(
        config=OpsConfig(), journal=j_b, quote_source=quotes.get,
        starting_cash=Decimal("250"),
        start_of_day_equity=lambda: Decimal("250"),
        start_of_week_equity=lambda: Decimal("250"),
    )
    positions_b = broker_b.get_positions()
    assert len(positions_b) == 1
    assert positions_b[0].symbol == "AAPL"
    assert positions_b[0].quantity == positions_a[0].quantity
    assert positions_b[0].stop_loss_price == Decimal("9.5")

    # Reconcile in paper mode must produce zero diffs on the restarted broker.
    result = _reconcile(journal=j_b, broker=broker_b, broker_mode="paper")
    assert result.diffs == []
    j_b.close()


def test_wire_gates_guardian_on_market_calendar(tmp_path):
    """Regression: the always-on service must pass the market calendar to the
    guardian — an ungated guardian trades on after-hours quotes."""
    cfg = OpsConfig()
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _build_broker(cfg, j)
    orch, guardian, cal, backend = _wire(broker, j, cfg)
    assert guardian._market_open == cal.is_open_now


def test_ensure_paper_seed_records_once(tmp_path):
    """First paper startup seeds the journal with starting cash as an
    explicit adjustment; later startups must not duplicate it."""
    from ops.main import _ensure_paper_seed
    cfg = OpsConfig()
    j = Journal(str(tmp_path / "j.sqlite"))
    _ensure_paper_seed(j, cfg)
    _ensure_paper_seed(j, cfg)
    adjs = j.read_cash_adjustments()
    assert len(adjs) == 1
    assert adjs[0]["kind"] == "seed"
    assert adjs[0]["amount"] == Decimal("250")


def test_build_broker_paper_cash_comes_from_seed(tmp_path):
    """The paper broker's cash must equal the seeded starting cash — no
    hardcoded $250 separate from the journal."""
    cfg = OpsConfig(starting_cash=Decimal("400"))
    j = Journal(str(tmp_path / "j.sqlite"))
    broker = _build_broker(cfg, j)
    assert broker.get_cash() == Decimal("400")
    # Restart on the same journal: same cash, no double-seed.
    broker2 = _build_broker(cfg, j)
    assert broker2.get_cash() == Decimal("400")


def test_ensure_live_baseline_records_cash_delta_once(tmp_path):
    """First robinhood startup records a one-time baseline adjustment equal
    to (broker cash - journal-replayed cash), so reconciliation can pass on
    an account whose funding predates the journal. Never recorded twice."""
    from ops.broker.robinhood import RobinhoodBroker
    from ops.main import _ensure_live_baseline
    from tests.ops.broker.fakes import FakeMCPClient

    j = Journal(str(tmp_path / "j.sqlite"))
    client = FakeMCPClient(cash=Decimal("250"))
    broker = RobinhoodBroker(client=client, journal=j)
    _ensure_live_baseline(j, broker)
    adjs = j.read_cash_adjustments()
    assert len(adjs) == 1
    assert adjs[0]["kind"] == "live_baseline"
    assert adjs[0]["amount"] == Decimal("250")
    _ensure_live_baseline(j, broker)
    assert len(j.read_cash_adjustments()) == 1


def test_live_baseline_makes_reconcile_cash_clean(tmp_path):
    """End-to-end: after the baseline, robinhood reconciliation must not
    flag cash drift on a freshly-funded account with an empty journal."""
    from ops.broker.robinhood import RobinhoodBroker
    from ops.main import _ensure_live_baseline
    from ops.reconcile import reconcile
    from tests.ops.broker.fakes import FakeMCPClient

    j = Journal(str(tmp_path / "j.sqlite"))
    client = FakeMCPClient(cash=Decimal("250"))
    broker = RobinhoodBroker(client=client, journal=j)
    _ensure_live_baseline(j, broker)
    result = reconcile(journal=j, broker=broker, broker_mode="robinhood")
    assert result.diffs == []


def test_start_of_day_equity_ignores_stale_snapshot(tmp_path):
    """A start-of-day baseline from a previous day must not be used — return
    0 (drawdown rules treat <=0 as 'no baseline yet → allow')."""
    from datetime import datetime, timedelta, timezone

    from ops.main import _start_of_day_equity, _start_of_week_equity
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_equity_snapshot(
        kind="open_day", equity=Decimal("500"), cash=Decimal("500"),
        at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    j.record_equity_snapshot(
        kind="open_week", equity=Decimal("500"), cash=Decimal("500"),
        at=datetime.now(timezone.utc) - timedelta(days=21),
    )
    assert _start_of_day_equity(j) == Decimal("0")
    assert _start_of_week_equity(j) == Decimal("0")


def test_start_of_day_equity_uses_fresh_snapshot(tmp_path):
    from datetime import datetime, timezone

    from ops.main import _start_of_day_equity, _start_of_week_equity
    j = Journal(str(tmp_path / "j.sqlite"))
    now = datetime.now(timezone.utc)
    j.record_equity_snapshot(kind="open_day", equity=Decimal("300"),
                             cash=Decimal("300"), at=now)
    j.record_equity_snapshot(kind="open_week", equity=Decimal("310"),
                             cash=Decimal("310"), at=now)
    assert _start_of_day_equity(j) == Decimal("300")
    assert _start_of_week_equity(j) == Decimal("310")


def test_resolve_and_announce_journal_path_prints_absolute_path(tmp_path, capsys):
    from ops.main import _resolve_and_announce_journal_path

    cfg = OpsConfig(journal_path=str(tmp_path / "sub" / "j.sqlite"))
    resolved = _resolve_and_announce_journal_path(cfg)
    assert resolved == str(tmp_path / "sub" / "j.sqlite")
    captured = capsys.readouterr()
    assert resolved in captured.out


def test_resolve_and_announce_journal_path_expands_relative_and_tilde(monkeypatch, tmp_path, capsys):
    from ops.main import _resolve_and_announce_journal_path

    monkeypatch.chdir(tmp_path)
    cfg = OpsConfig(journal_path="relative.sqlite")
    resolved = _resolve_and_announce_journal_path(cfg)
    assert resolved == str(tmp_path / "relative.sqlite")


def test_resolve_and_announce_journal_path_warns_on_new_file(tmp_path, capsys):
    from ops.main import _resolve_and_announce_journal_path

    cfg = OpsConfig(journal_path=str(tmp_path / "brand_new.sqlite"))
    _resolve_and_announce_journal_path(cfg)
    captured = capsys.readouterr()
    assert "new" in captured.err.lower()


def test_resolve_and_announce_journal_path_no_warning_when_file_exists(tmp_path, capsys):
    from ops.main import _resolve_and_announce_journal_path

    existing = tmp_path / "existing.sqlite"
    existing.write_bytes(b"")
    cfg = OpsConfig(journal_path=str(existing))
    _resolve_and_announce_journal_path(cfg)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_run_exits_3_and_journals_on_broker_unreachable(monkeypatch, tmp_path, capsys):
    """M6: a broker that can't be reached at startup (build or reconcile)
    must halt loudly — exit 3, both broker_unreachable and startup_halted
    events journaled, no bare traceback — and must NOT start the guardian
    (there is nothing for it to guard with an unreachable broker)."""
    from ops.broker.mcp_client import MCPUnavailable
    from ops.main import run
    from tests.ops.broker.fakes import FakeMCPClient

    fake = FakeMCPClient()
    fake.fail_next(MCPUnavailable("connection refused"))
    monkeypatch.setattr(
        "ops.broker.mcp_client.RealRobinhoodMCPClient", lambda: fake,
    )
    journal_path = str(tmp_path / "j.sqlite")
    monkeypatch.setenv("OPS_BROKER_MODE", "robinhood")
    monkeypatch.setenv("OPS_JOURNAL_PATH", journal_path)

    exit_code = run()

    assert exit_code == 3

    j = Journal(journal_path)
    events = j.read_events()
    kinds = [e["kind"] for e in events]
    assert "broker_unreachable" in kinds
    assert "startup_halted" in kinds
    startup_halted = next(e for e in events if e["kind"] == "startup_halted")
    assert startup_halted["payload"]["reason"] == "broker_unreachable"
    # The journaled broker_unreachable payload must carry only the exception
    # TYPE name — never the raw str(exc), which can embed connection
    # details — for consistency with the notify_dispatch_error sanitization.
    broker_unreachable = next(e for e in events if e["kind"] == "broker_unreachable")
    assert broker_unreachable["payload"] == {"error_type": "BrokerError"}
    assert "connection refused" not in str(broker_unreachable["payload"])
    j.close()

    captured = capsys.readouterr()
    assert "unreachable" in captured.err.lower()
    assert "Traceback" not in captured.err

    # A1.2: even a startup-halted run must leave an uptime record —
    # service_started before the failure, service_stopping with the exit code.
    j = Journal(journal_path)
    events = j.read_events()
    started = [e for e in events if e["kind"] == "service_started"]
    stopping = [e for e in events if e["kind"] == "service_stopping"]
    assert len(started) == 1 and len(stopping) == 1
    assert stopping[0]["payload"]["exit_code"] == 3
    j.close()


def test_run_journals_service_started_and_stopping_on_clean_run(
    monkeypatch, tmp_path, preset_shutdown, capsys,
):
    """A1.2: a normal paper-mode run records service_started (broker_mode,
    journal path, pid) right after the journal opens and service_stopping
    (exit code 0) before it closes — the uptime record the graduation
    evaluation reads back."""
    from ops.main import run

    journal_path = str(tmp_path / "j.sqlite")
    monkeypatch.delenv("OPS_BROKER_MODE", raising=False)
    monkeypatch.setenv("OPS_JOURNAL_PATH", journal_path)

    exit_code = run()
    assert exit_code == 0

    j = Journal(journal_path)
    events = j.read_events()
    started = [e for e in events if e["kind"] == "service_started"]
    stopping = [e for e in events if e["kind"] == "service_stopping"]
    assert len(started) == 1
    assert started[0]["payload"]["broker_mode"] == "paper"
    assert started[0]["payload"]["journal_path"] == journal_path
    assert started[0]["payload"]["pid"] == os.getpid()
    assert len(stopping) == 1
    assert stopping[0]["payload"]["exit_code"] == 0
    # service_started must come first (it is the session-open marker).
    kinds = [e["kind"] for e in events]
    assert kinds.index("service_started") < kinds.index("service_stopping")
    j.close()


def test_run_service_stopping_carries_exit_code_2_on_reconcile_halt(
    monkeypatch, tmp_path, preset_shutdown, capsys,
):
    """A1.2: the reconcile-halt path (guardian-only, exit 2) must record
    its exit code in service_stopping."""
    from ops.main import run

    journal_path = str(tmp_path / "j.sqlite")
    monkeypatch.delenv("OPS_BROKER_MODE", raising=False)
    monkeypatch.setenv("OPS_JOURNAL_PATH", journal_path)
    diffy = ReconcileResult(
        diffs=[PositionDiff(symbol="AAPL", journal_qty=Decimal("5"),
                            broker_qty=Decimal("3"), kind="qty_mismatch")],
        cash_journal=Decimal("100"), cash_broker=Decimal("100"),
        cash_diff=Decimal("0"),
    )
    monkeypatch.setattr("ops.main.reconcile", lambda **kwargs: diffy)

    exit_code = run()
    assert exit_code == 2

    j = Journal(journal_path)
    stopping = [e for e in j.read_events() if e["kind"] == "service_stopping"]
    assert len(stopping) == 1
    assert stopping[0]["payload"]["exit_code"] == 2
    j.close()


def test_service_lifecycle_events_are_audit_only():
    """A1.2: service_started/service_stopping are uptime bookkeeping, not
    alerts — they must never enter the notify POLICY."""
    from ops.notify.policy import POLICY

    assert "service_started" not in POLICY
    assert "service_stopping" not in POLICY


def test_daily_summary_job_callable_does_not_name_error(tmp_path):
    """Regression: _start_full_scheduler's daily_summary lambda referenced a
    `calendar` name that was not in scope — every 16:05 firing raised
    NameError inside APScheduler and the summary never ran, silently. The
    registered job callable must be invokable with only the arguments the
    scheduler gives it (none)."""
    from unittest.mock import MagicMock
    from ops.main import _start_full_scheduler

    orchestrator = MagicMock()
    guardian = MagicMock()
    dispatcher = MagicMock()
    journal = MagicMock()
    journal.has_event_today.return_value = True  # summary idempotent no-op
    broker = MagicMock()
    sched = _start_full_scheduler(orchestrator, guardian, dispatcher, journal, broker)
    try:
        job = sched.get_job("daily_summary")
        job.func()  # must not raise NameError
    finally:
        sched.shutdown(wait=False)


def test_research_monitor_job_registered_and_callable(tmp_path):
    """The Phase C monitor job mirrors daily_summary: registered only when a
    config is supplied, and its callable must be invokable with no args."""
    from unittest.mock import MagicMock
    from ops.main import _start_full_scheduler

    journal = MagicMock()
    journal.has_event_today.return_value = True  # monitor idempotent no-op
    config = MagicMock()
    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), MagicMock(), journal, MagicMock(), config=config,
    )
    try:
        job = sched.get_job("research_monitor")
        assert job is not None
        job.func()  # gate returns early; must not raise
    finally:
        sched.shutdown(wait=False)


def test_research_monitor_job_absent_without_config():
    from unittest.mock import MagicMock
    from ops.main import _start_full_scheduler

    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
    )
    try:
        assert sched.get_job("research_monitor") is None
    finally:
        sched.shutdown(wait=False)


def test_research_monitor_tick_records_error_instead_of_raising(tmp_path):
    from unittest.mock import MagicMock
    from ops import events
    from ops.main import _research_monitor_tick

    journal = MagicMock()
    journal.has_event_today.return_value = False
    config = MagicMock()
    config.memo_store_path = str(tmp_path / "nope" / "memos.sqlite")
    # Force a failure inside: monitor_memos will blow up on a MagicMock path
    # or the stores will; either way the tick must swallow and journal it.
    config.screen_store_path = object()  # guaranteed TypeError downstream
    _research_monitor_tick(journal, config)  # must not raise
    kinds = [c.args[0] for c in journal.record_event.call_args_list]
    assert events.KIND_RESEARCH_MONITOR_ERROR in kinds


def test_research_trade_job_registered_and_gated(tmp_path):
    from unittest.mock import MagicMock
    from ops.main import _start_full_scheduler

    journal = MagicMock()
    journal.has_event_today.return_value = True
    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), MagicMock(), journal, MagicMock(),
        config=MagicMock(),
    )
    try:
        job = sched.get_job("research_trade")
        assert job is not None
        job.func()  # gate returns early; must not raise
    finally:
        sched.shutdown(wait=False)


def test_research_trade_job_absent_without_config():
    from unittest.mock import MagicMock
    from ops.main import _start_full_scheduler

    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
    )
    try:
        assert sched.get_job("research_trade") is None
    finally:
        sched.shutdown(wait=False)


def test_research_trade_tick_records_error_instead_of_raising(tmp_path):
    from unittest.mock import MagicMock
    from ops import events
    from ops.main import _research_trade_tick

    journal = MagicMock()
    journal.has_event_today.return_value = False
    config = MagicMock()
    config.research_journal_path = object()  # guaranteed failure downstream
    _research_trade_tick(journal, config)  # must not raise
    kinds = [c.args[0] for c in journal.record_event.call_args_list]
    assert events.KIND_RESEARCH_TRADE_ERROR in kinds


def test_daily_overview_jobs_registered_and_callable(tmp_path):
    """DO-Task 3: the overview job is registered TWICE — mon-fri under id
    'daily_overview' and Saturday (after the noon research brain) under
    'daily_overview_saturday' — mirroring research_monitor/research_trade's
    config-guarded registration. The once-per-day gate makes the double
    registration safe."""
    from unittest.mock import MagicMock
    from ops.main import _start_full_scheduler

    journal = MagicMock()
    journal.has_event_today.return_value = True  # gate returns early; must not raise
    config = MagicMock()
    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), MagicMock(), journal, MagicMock(), config=config,
    )
    try:
        weekday_job = sched.get_job("daily_overview")
        saturday_job = sched.get_job("daily_overview_saturday")
        assert weekday_job is not None
        assert saturday_job is not None
        weekday_job.func()  # gate returns early; must not raise
        saturday_job.func()  # gate returns early; must not raise
    finally:
        sched.shutdown(wait=False)


def test_daily_overview_jobs_absent_without_config():
    from unittest.mock import MagicMock
    from ops.main import _start_full_scheduler

    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
    )
    try:
        assert sched.get_job("daily_overview") is None
        assert sched.get_job("daily_overview_saturday") is None
    finally:
        sched.shutdown(wait=False)


def test_daily_overview_tick_records_error_instead_of_raising(tmp_path):
    from unittest.mock import MagicMock
    from ops import events
    from ops.main import _daily_overview_tick

    journal = MagicMock()
    journal.has_event_today.return_value = False
    config = MagicMock()
    config.memo_store_path = str(tmp_path / "memos.sqlite")
    config.baseline_journal_path = object()  # guaranteed TypeError downstream
    _daily_overview_tick(journal, config)  # must not raise
    kinds = [c.args[0] for c in journal.record_event.call_args_list]
    assert events.KIND_DAILY_OVERVIEW_ERROR in kinds
    # And the gate event must NOT have been recorded on the error path.
    assert events.KIND_DAILY_OVERVIEW not in kinds


def test_daily_overview_tick_writes_file_and_records_gate_event(tmp_path, monkeypatch):
    """Happy path against real (empty) journals + memo store: the tick must
    write the markdown file under ${XDG_STATE_HOME}/tradingagents/overviews/
    and record exactly the gate event (a quiet day, no push configured)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("OPS_NOTIFY_ENABLED", raising=False)
    from datetime import date
    from ops import events
    from ops.config import OpsConfig
    from ops.main import _daily_overview_tick

    journal = Journal(str(tmp_path / "main.sqlite"))
    config = OpsConfig(
        journal_path=journal.path,
        baseline_journal_path=str(tmp_path / "baseline.sqlite"),
        research_journal_path=str(tmp_path / "research.sqlite"),
        memo_store_path=str(tmp_path / "memos.sqlite"),
    )
    try:
        _daily_overview_tick(journal, config)
        expected_path = (
            tmp_path / "tradingagents" / "overviews"
            / f"overview-{date.today().isoformat()}.md"
        )
        assert expected_path.exists()
        assert "Daily overview" in expected_path.read_text()

        gate_events = [e for e in journal.read_events() if e["kind"] == events.KIND_DAILY_OVERVIEW]
        assert len(gate_events) == 1
        assert gate_events[0]["payload"]["path"] == str(expected_path)

        error_events = [
            e for e in journal.read_events() if e["kind"] == events.KIND_DAILY_OVERVIEW_ERROR
        ]
        assert error_events == []

        # Gate: a second call today is a no-op (no duplicate gate event).
        _daily_overview_tick(journal, config)
        gate_events_again = [
            e for e in journal.read_events() if e["kind"] == events.KIND_DAILY_OVERVIEW
        ]
        assert len(gate_events_again) == 1
    finally:
        journal.close()


def test_overnight_tick_screens_when_due_then_drains(monkeypatch, tmp_path):
    import ops.main as main_mod
    from ops.config import load_config
    from ops.research.drain import DrainSummary

    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")
    monkeypatch.setattr("ops.research.models.build_stage_llm", lambda s: f"llm:{s}")

    class _NoBackend:
        def ensure_up(self): pass
        def shutdown(self): pass
    monkeypatch.setattr(main_mod, "build_managed_backend", lambda c: _NoBackend())

    from datetime import date as _date

    from ops.research.store import ScreenStore

    events_seen = []

    def _fake_screen(**kw):
        events_seen.append("screen")
        # A real screen would enqueue hits; the empty-queue guard added in
        # _research_overnight_tick short-circuits before touching ds4 if the
        # queue is still empty after screening, so simulate a hit here to
        # reach the drain step.
        ScreenStore(str(tmp_path / "screen.sqlite")).enqueue_hit(
            "AAPL", asof=_date.today(), payload={"symbol": "AAPL"}, source="screen",
        )

    monkeypatch.setattr("ops.research.run.run_screens", _fake_screen)
    screen_store = ScreenStore(str(tmp_path / "screen.sqlite"))

    def _fake_drain(**kw):
        events_seen.append("drain")
        for hit in screen_store.pending_hits():
            screen_store.mark_researched(hit["id"])
        return DrainSummary(3, 0, 0, False)

    monkeypatch.setattr("ops.research.drain.drain_pending", _fake_drain)

    config = load_config()
    with Journal(str(tmp_path / "j.sqlite")) as journal:
        # No prior screen run -> screen is due.
        main_mod._research_overnight_tick(journal, config)
        assert events_seen == ["screen", "drain"]
        assert journal.has_event_today(main_mod.events.KIND_RESEARCH_DRAIN_RUN)


def test_overnight_tick_skips_screen_when_recent(monkeypatch, tmp_path):
    import ops.main as main_mod
    from datetime import date
    from ops.config import load_config
    from ops.research.drain import DrainSummary
    from ops.research.store import ScreenStore

    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")
    monkeypatch.setattr("ops.research.models.build_stage_llm", lambda s: f"llm:{s}")

    class _NoBackend:
        def ensure_up(self): pass
        def shutdown(self): pass
    monkeypatch.setattr(main_mod, "build_managed_backend", lambda c: _NoBackend())

    # A screen run recorded just now -> interval (3 days) not elapsed.
    store = ScreenStore(str(tmp_path / "screen.sqlite"))
    store.record_run(asof=date.today(), universe_size=0, results=[])
    # Seed a pending hit from a prior run so the queue isn't empty — the
    # empty-queue guard would otherwise skip the drain entirely, which is
    # not what this test is exercising (it's testing the screen-skip path).
    store.enqueue_hit("AAPL", asof=date.today(), payload={"symbol": "AAPL"}, source="test")

    seen = []
    monkeypatch.setattr("ops.research.run.run_screens",
                        lambda **kw: seen.append("screen"))

    def _fake_drain(**kw):
        seen.append("drain")
        for hit in store.pending_hits():
            store.mark_researched(hit["id"])
        return DrainSummary(1, 0, 0, False)

    monkeypatch.setattr("ops.research.drain.drain_pending", _fake_drain)

    config = load_config()
    with Journal(str(tmp_path / "j.sqlite")) as journal:
        main_mod._research_overnight_tick(journal, config)
    assert seen == ["drain"]  # screened recently -> only drain


def test_overnight_tick_empty_queue_skips_backend(monkeypatch, tmp_path):
    """The ~2-of-3 nights case: no screen due and the pending queue is
    already empty. The tick must record a zeroed drain_run event and return
    before ever touching the ds4 backend (ensure_up must not be called)."""
    import ops.main as main_mod
    from datetime import date
    from ops.config import load_config
    from ops.research.store import ScreenStore

    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")

    class _SpyBackend:
        def ensure_up(self):
            raise AssertionError("ensure_up must not be called on an empty queue")

        def shutdown(self):
            raise AssertionError("shutdown must not be called on an empty queue")

    monkeypatch.setattr(main_mod, "build_managed_backend", lambda c: _SpyBackend())

    # A screen run recorded just now -> interval (3 days) not elapsed, so
    # screen is skipped. No hits were ever enqueued -> queue is empty.
    store = ScreenStore(str(tmp_path / "screen.sqlite"))
    store.record_run(asof=date.today(), universe_size=0, results=[])
    assert store.pending_hits() == []

    config = load_config()
    with Journal(str(tmp_path / "j.sqlite")) as journal:
        main_mod._research_overnight_tick(journal, config)

        drain_events = [
            e for e in journal.read_events()
            if e["kind"] == main_mod.events.KIND_RESEARCH_DRAIN_RUN
        ]
        assert len(drain_events) == 1
        payload = drain_events[0]["payload"]
        assert payload["researched"] == 0
        assert payload["failed"] == 0
        assert payload["still_pending"] == 0
        assert payload["hit_deadline"] is False
        assert payload["screened_this_run"] is False

        error_events = [
            e for e in journal.read_events()
            if e["kind"] == main_mod.events.KIND_RESEARCH_DRAIN_ERROR
        ]
        assert error_events == []


def test_overnight_tick_records_error_event_not_raises(monkeypatch, tmp_path):
    import ops.main as main_mod
    from ops.config import load_config

    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")
    monkeypatch.setattr("ops.research.run.run_screens",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    config = load_config()
    with Journal(str(tmp_path / "j.sqlite")) as journal:
        main_mod._research_overnight_tick(journal, config)  # must not raise
        assert journal.has_event_today(main_mod.events.KIND_RESEARCH_DRAIN_ERROR)


class _FakeBackend:
    """Managed-backend double with ensure_up/shutdown counters, shared across
    the tick via a build_managed_backend monkeypatch that returns one instance."""

    def __init__(self):
        self.ensure_up_calls = 0
        self.shutdown_calls = 0

    def ensure_up(self):
        self.ensure_up_calls += 1

    def shutdown(self):
        self.shutdown_calls += 1


def _pending_vetting_memo(ticker="ACME"):
    """A minimal brain-buy memo in the pending_vetting state, mirroring the
    fixture in tests/ops/research/test_vetting.py."""
    from datetime import date

    from tradingagents.memos.schema import (
        EvidenceItem, Falsifier, Memo, ValueThesis,
    )

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


def test_overnight_tick_vets_before_drain_and_records_events(monkeypatch, tmp_path):
    """Vetting runs FIRST (confirms land nightly instead of waiting for the
    drain backlog to clear), then the drain gets the remaining window with
    the nightly name cap; both stages share one deadline and one backend
    bracket, and both events are recorded."""
    import ops.main as main_mod
    from datetime import date as _date
    from ops.config import load_config
    from ops.research.drain import DrainSummary
    from ops.research.store import ScreenStore
    from ops.research.vetting import VettingSummary
    from tradingagents.memos.store import MemoStore

    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")
    monkeypatch.setattr("ops.research.models.build_stage_llm", lambda s: f"llm:{s}")

    backend = _FakeBackend()
    monkeypatch.setattr(main_mod, "build_managed_backend", lambda c: backend)

    # Seed the vetting queue with one pending_vetting memo.
    MemoStore(str(tmp_path / "memos.sqlite")).save(_pending_vetting_memo())

    def _fake_screen(**kw):
        ScreenStore(str(tmp_path / "screen.sqlite")).enqueue_hit(
            "AAPL", asof=_date.today(), payload={"symbol": "AAPL"}, source="screen",
        )

    monkeypatch.setattr("ops.research.run.run_screens", _fake_screen)

    stage_order = []
    drain_kw = {}
    screen_store = ScreenStore(str(tmp_path / "screen.sqlite"))
    memo_store = MemoStore(str(tmp_path / "memos.sqlite"))

    def _fake_drain(**kw):
        stage_order.append("drain")
        drain_kw.update(kw)
        for hit in screen_store.pending_hits():
            screen_store.mark_researched(hit["id"])
        return DrainSummary(3, 0, 0, False)

    monkeypatch.setattr("ops.research.drain.drain_pending", _fake_drain)

    sentinel_adapter = object()
    vet_kw = {}

    def _fake_vet(**kw):
        stage_order.append("vet")
        vet_kw.update(kw)
        for memo in memo_store.pending_vetting_memos():
            memo_store.mark_passed(memo.memo_id)
        return VettingSummary(vetted=1, confirmed=1, rejected=0, failed=0,
                              still_pending=0, hit_deadline=False)

    monkeypatch.setattr("ops.research.vetting.vet_pending", _fake_vet)

    config = load_config()
    with Journal(str(tmp_path / "j.sqlite")) as journal:
        main_mod._research_overnight_tick(
            journal, config,
            vet_adapter_factory=lambda backend: sentinel_adapter,
        )

        # Vet-first ordering: confirms must not wait on the drain backlog.
        assert stage_order == ["vet", "drain"]

        # vet_pending got the drain's deadline and the sentinel adapter.
        assert vet_kw["adapter"] is sentinel_adapter
        assert vet_kw["deadline"] == drain_kw["deadline"]

        # The drain is capped per night so it cannot monopolize future
        # windows (each drained buy adds ~30min of vetting debt).
        assert drain_kw["max_names"] == config.research_drain_nightly_cap

        assert journal.has_event_today(main_mod.events.KIND_RESEARCH_DRAIN_RUN)
        assert journal.has_event_today(main_mod.events.KIND_RESEARCH_VETTING_RUN)

        # ONE managed-backend bracket for both stages.
        assert backend.shutdown_calls == 1


def test_overnight_tick_vet_only_night_runs_vetting_without_drain(monkeypatch, tmp_path):
    """Empty screen queue + non-empty vetting queue: the drain records its
    zero event and vetting still runs (a backlog night)."""
    import ops.main as main_mod
    from datetime import date
    from ops.config import load_config
    from ops.research.store import ScreenStore
    from ops.research.vetting import VettingSummary
    from tradingagents.memos.store import MemoStore

    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")
    monkeypatch.setattr("ops.research.models.build_stage_llm", lambda s: f"llm:{s}")

    backend = _FakeBackend()
    monkeypatch.setattr(main_mod, "build_managed_backend", lambda c: backend)

    # Recent screen -> not due; no pending hits -> empty screen queue.
    store = ScreenStore(str(tmp_path / "screen.sqlite"))
    store.record_run(asof=date.today(), universe_size=0, results=[])
    assert store.pending_hits() == []

    # Non-empty vetting queue.
    MemoStore(str(tmp_path / "memos.sqlite")).save(_pending_vetting_memo())

    seen = []
    monkeypatch.setattr("ops.research.run.run_screens",
                        lambda **kw: seen.append("screen"))
    monkeypatch.setattr("ops.research.drain.drain_pending",
                        lambda **kw: seen.append("drain"))

    memo_store = MemoStore(str(tmp_path / "memos.sqlite"))

    def _fake_vet(**kw):
        seen.append("vet")
        for memo in memo_store.pending_vetting_memos():
            memo_store.mark_passed(memo.memo_id)
        return VettingSummary(vetted=1, confirmed=0, rejected=1, failed=0,
                              still_pending=0, hit_deadline=False)

    monkeypatch.setattr("ops.research.vetting.vet_pending", _fake_vet)

    config = load_config()
    with Journal(str(tmp_path / "j.sqlite")) as journal:
        main_mod._research_overnight_tick(
            journal, config, vet_adapter_factory=lambda backend: object(),
        )

        assert seen == ["vet"]  # no screen, no drain — vet-only backlog night
        drain_events = [
            e for e in journal.read_events()
            if e["kind"] == main_mod.events.KIND_RESEARCH_DRAIN_RUN
        ]
        assert len(drain_events) == 1
        assert drain_events[0]["payload"]["researched"] == 0
        assert drain_events[0]["payload"]["still_pending"] == 0
        assert journal.has_event_today(main_mod.events.KIND_RESEARCH_VETTING_RUN)


def test_overnight_tick_both_queues_empty_skips_backend(monkeypatch, tmp_path):
    """Extends the empty-queue-skips-backend guarantee to the vetting queue:
    ds4 must not spin when BOTH queues are empty."""
    import ops.main as main_mod
    from datetime import date
    from ops.config import load_config
    from ops.research.store import ScreenStore

    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")

    def _no_backend(c):
        raise AssertionError("ds4 backend must not be built when both queues are empty")

    monkeypatch.setattr(main_mod, "build_managed_backend", _no_backend)

    def _no_vet(**kw):
        raise AssertionError("vet_pending must not run when the vetting queue is empty")

    monkeypatch.setattr("ops.research.vetting.vet_pending", _no_vet)

    # Recent screen -> not due; no pending hits; memo store left empty.
    store = ScreenStore(str(tmp_path / "screen.sqlite"))
    store.record_run(asof=date.today(), universe_size=0, results=[])
    assert store.pending_hits() == []

    config = load_config()
    with Journal(str(tmp_path / "j.sqlite")) as journal:
        main_mod._research_overnight_tick(journal, config)

        drain_events = [
            e for e in journal.read_events()
            if e["kind"] == main_mod.events.KIND_RESEARCH_DRAIN_RUN
        ]
        assert len(drain_events) == 1
        assert drain_events[0]["payload"]["researched"] == 0
        vetting_events = [
            e for e in journal.read_events()
            if e["kind"] == main_mod.events.KIND_RESEARCH_VETTING_RUN
        ]
        assert vetting_events == []
        error_events = [
            e for e in journal.read_events()
            if e["kind"] == main_mod.events.KIND_RESEARCH_DRAIN_ERROR
        ]
        assert error_events == []


def test_overnight_tick_vetting_error_recorded_not_raised(monkeypatch, tmp_path):
    """A vetting-stage failure records research_vetting_error, keeps the
    drain's success event, and still shuts the backend down."""
    import ops.main as main_mod
    from datetime import date as _date
    from ops.config import load_config
    from ops.research.drain import DrainSummary
    from ops.research.store import ScreenStore
    from tradingagents.memos.store import MemoStore

    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")
    monkeypatch.setattr("ops.research.models.build_stage_llm", lambda s: f"llm:{s}")

    backend = _FakeBackend()
    monkeypatch.setattr(main_mod, "build_managed_backend", lambda c: backend)

    MemoStore(str(tmp_path / "memos.sqlite")).save(_pending_vetting_memo())

    def _fake_screen(**kw):
        ScreenStore(str(tmp_path / "screen.sqlite")).enqueue_hit(
            "AAPL", asof=_date.today(), payload={"symbol": "AAPL"}, source="screen",
        )

    monkeypatch.setattr("ops.research.run.run_screens", _fake_screen)
    screen_store = ScreenStore(str(tmp_path / "screen.sqlite"))

    def _fake_drain(**kw):
        for hit in screen_store.pending_hits():
            screen_store.mark_researched(hit["id"])
        return DrainSummary(3, 0, 0, False)

    monkeypatch.setattr("ops.research.drain.drain_pending", _fake_drain)
    monkeypatch.setattr("ops.research.vetting.vet_pending",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("kaboom")))

    config = load_config()
    with Journal(str(tmp_path / "j.sqlite")) as journal:
        # Must not raise.
        main_mod._research_overnight_tick(
            journal, config, vet_adapter_factory=lambda backend: object(),
        )

        # Drain's success event survives.
        assert journal.has_event_today(main_mod.events.KIND_RESEARCH_DRAIN_RUN)
        # Vetting error is recorded, not raised.
        vetting_errors = [
            e for e in journal.read_events()
            if e["kind"] == main_mod.events.KIND_RESEARCH_VETTING_ERROR
        ]
        assert len(vetting_errors) == 1
        assert vetting_errors[0]["payload"]["error"] == "RuntimeError: kaboom"
        # No successful vetting event.
        assert not journal.has_event_today(main_mod.events.KIND_RESEARCH_VETTING_RUN)
        # Backend still torn down.
        assert backend.shutdown_calls == 1


def test_overnight_deadline_weekday_is_same_day():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from ops.main import _overnight_deadline

    ny = ZoneInfo("America/New_York")
    wed_tick = datetime(2026, 7, 8, 0, 0, 5, tzinfo=ny)  # Wednesday 00:00
    assert _overnight_deadline(8, now=wed_tick) == datetime(2026, 7, 8, 8, 0, tzinfo=ny)


def test_overnight_deadline_weekend_extends_to_monday():
    """Sat/Sun ticks get the whole weekend: no momentum ticks compete for
    ds4, and the backlog must be fully drained AND vetted before the
    trading week starts."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from ops.main import _overnight_deadline

    ny = ZoneInfo("America/New_York")
    sat_tick = datetime(2026, 7, 11, 0, 0, 5, tzinfo=ny)  # Saturday 00:00
    sun_tick = datetime(2026, 7, 12, 0, 0, 5, tzinfo=ny)  # Sunday 00:00
    monday_8 = datetime(2026, 7, 13, 8, 0, tzinfo=ny)
    assert _overnight_deadline(8, now=sat_tick) == monday_8
    assert _overnight_deadline(8, now=sun_tick) == monday_8


def test_overnight_tick_loops_until_both_queues_clear(monkeypatch, tmp_path):
    """The stage loop alternates vet -> drain -> vet ... so buys minted by
    tonight's drain are vetted in the SAME window when time remains (on
    weekends this is what fully clears the backlog before Monday). Events
    are aggregated: one drain_run and one vetting_run per tick."""
    import ops.main as main_mod
    from datetime import date as _date
    from ops.config import load_config
    from ops.research.drain import DrainSummary
    from ops.research.store import ScreenStore
    from ops.research.vetting import VettingSummary
    from tradingagents.memos.store import MemoStore

    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")
    monkeypatch.setattr("ops.research.models.build_stage_llm", lambda s: f"llm:{s}")

    backend = _FakeBackend()
    monkeypatch.setattr(main_mod, "build_managed_backend", lambda c: backend)

    screen_store = ScreenStore(str(tmp_path / "screen.sqlite"))
    memo_store = MemoStore(str(tmp_path / "memos.sqlite"))

    # Recent screen -> not due. One memo queued, one screen hit pending.
    screen_store.record_run(asof=_date.today(), universe_size=0, results=[])
    screen_store.enqueue_hit(
        "AAPL", asof=_date.today(), payload={"symbol": "AAPL"}, source="screen",
    )
    memo_store.save(_pending_vetting_memo(ticker="AAA"))

    stage_order = []

    def _fake_vet(**kw):
        stage_order.append("vet")
        queue = memo_store.pending_vetting_memos()
        for memo in queue:
            memo_store.mark_passed(memo.memo_id)
        return VettingSummary(vetted=len(queue), confirmed=0, rejected=len(queue),
                              failed=0, still_pending=0, hit_deadline=False)

    monkeypatch.setattr("ops.research.vetting.vet_pending", _fake_vet)

    def _fake_drain(**kw):
        stage_order.append("drain")
        for hit in screen_store.pending_hits():
            screen_store.mark_researched(hit["id"])
        # The drained name is a brain buy: it lands in the vetting queue.
        memo_store.save(_pending_vetting_memo(ticker="BBB"))
        return DrainSummary(1, 0, 0, False)

    monkeypatch.setattr("ops.research.drain.drain_pending", _fake_drain)

    config = load_config()
    with Journal(str(tmp_path / "j.sqlite")) as journal:
        main_mod._research_overnight_tick(
            journal, config, vet_adapter_factory=lambda backend: object(),
        )

        # vet AAA -> drain (mints BBB) -> vet BBB -> both queues empty.
        assert stage_order == ["vet", "drain", "vet"]
        assert memo_store.pending_vetting_memos() == []
        assert screen_store.pending_hits() == []

        vetting_events = [
            e for e in journal.read_events()
            if e["kind"] == main_mod.events.KIND_RESEARCH_VETTING_RUN
        ]
        assert len(vetting_events) == 1
        assert vetting_events[0]["payload"]["vetted"] == 2       # aggregated
        assert vetting_events[0]["payload"]["still_pending"] == 0
        drain_events = [
            e for e in journal.read_events()
            if e["kind"] == main_mod.events.KIND_RESEARCH_DRAIN_RUN
        ]
        assert len(drain_events) == 1
        assert drain_events[0]["payload"]["researched"] == 1
        assert backend.shutdown_calls == 1


def test_overnight_tick_paused_flag_skips_everything(monkeypatch, tmp_path):
    """`ops research pause` drops the flag: the tick must return before the
    screen, the backend, or any event — the operator wants their machine."""
    import ops.main as main_mod
    from ops.config import load_config

    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("OPS_RESEARCH_PAUSE_FLAG_PATH", str(tmp_path / "paused"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")

    (tmp_path / "paused").touch()

    def _boom(*a, **kw):
        raise AssertionError("paused tick must touch nothing")

    monkeypatch.setattr("ops.research.run.run_screens", _boom)
    monkeypatch.setattr(main_mod, "build_managed_backend", _boom)

    config = load_config()
    with Journal(str(tmp_path / "j.sqlite")) as journal:
        main_mod._research_overnight_tick(journal, config)
        assert journal.read_events() == []


def test_overnight_tick_stop_callable_honors_pause_flag(monkeypatch, tmp_path):
    """Pausing MID-run stops the loop between names: the should_stop threaded
    into drain/vet must flip true the moment the flag appears."""
    import ops.main as main_mod
    from datetime import date as _date
    from ops.config import load_config
    from ops.research.drain import DrainSummary
    from ops.research.store import ScreenStore

    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("OPS_RESEARCH_PAUSE_FLAG_PATH", str(tmp_path / "paused"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")
    monkeypatch.setattr("ops.research.models.build_stage_llm", lambda s: f"llm:{s}")

    backend = _FakeBackend()
    monkeypatch.setattr(main_mod, "build_managed_backend", lambda c: backend)

    screen_store = ScreenStore(str(tmp_path / "screen.sqlite"))
    screen_store.record_run(asof=_date.today(), universe_size=0, results=[])
    screen_store.enqueue_hit(
        "AAPL", asof=_date.today(), payload={"symbol": "AAPL"}, source="test",
    )
    monkeypatch.setattr("ops.research.run.run_screens", lambda **kw: None)

    captured = {}

    def _fake_drain(**kw):
        captured["should_stop"] = kw["should_stop"]
        for hit in screen_store.pending_hits():
            screen_store.mark_researched(hit["id"])
        return DrainSummary(1, 0, 0, False)

    monkeypatch.setattr("ops.research.drain.drain_pending", _fake_drain)

    config = load_config()
    with Journal(str(tmp_path / "j.sqlite")) as journal:
        main_mod._research_overnight_tick(journal, config)

    stop = captured["should_stop"]
    assert stop() is False
    (tmp_path / "paused").touch()
    assert stop() is True


def test_overnight_tick_out_of_window_returns_silently(monkeypatch, tmp_path):
    """The job now fires every 30 minutes; a weekday-daytime fire (deadline
    already past) must return without events or backend — that's what makes
    the frequent trigger safe."""
    import ops.main as main_mod
    from datetime import date as _date, datetime, timedelta, timezone
    from ops.config import load_config
    from ops.research.store import ScreenStore

    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("OPS_RESEARCH_PAUSE_FLAG_PATH", str(tmp_path / "paused"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")

    monkeypatch.setattr(
        main_mod, "_overnight_deadline",
        lambda hour, now=None: datetime.now(timezone.utc) - timedelta(hours=1),
    )
    monkeypatch.setattr(main_mod, "build_managed_backend",
                        lambda c: (_ for _ in ()).throw(AssertionError("no ds4 out of window")))

    # Queue non-empty so only the deadline guard can be the reason we skip.
    store = ScreenStore(str(tmp_path / "screen.sqlite"))
    store.record_run(asof=_date.today(), universe_size=0, results=[])
    store.enqueue_hit("AAPL", asof=_date.today(), payload={"symbol": "AAPL"}, source="test")

    config = load_config()
    with Journal(str(tmp_path / "j.sqlite")) as journal:
        main_mod._research_overnight_tick(journal, config)
        assert journal.read_events() == []


def test_overnight_tick_zero_event_recorded_once_per_day(monkeypatch, tmp_path):
    """With half-hourly fires, an idle night must not spam zero drain events:
    the empty-queues bookkeeping event records once per day."""
    import ops.main as main_mod
    from datetime import date
    from ops.config import load_config
    from ops.research.store import ScreenStore

    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("OPS_RESEARCH_PAUSE_FLAG_PATH", str(tmp_path / "paused"))
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "T t@e.com")

    store = ScreenStore(str(tmp_path / "screen.sqlite"))
    store.record_run(asof=date.today(), universe_size=0, results=[])

    config = load_config()
    with Journal(str(tmp_path / "j.sqlite")) as journal:
        main_mod._research_overnight_tick(journal, config)
        main_mod._research_overnight_tick(journal, config)
        drain_events = [
            e for e in journal.read_events()
            if e["kind"] == main_mod.events.KIND_RESEARCH_DRAIN_RUN
        ]
        assert len(drain_events) == 1


def test_full_scheduler_background_queue_fires_every_five_minutes():
    """The live-first queue notices new work and timed resume promptly."""
    from unittest.mock import MagicMock

    from ops.main import _start_full_scheduler

    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        config=MagicMock(),
    )
    try:
        job = sched.get_job("research_overnight")
        assert job is not None
        trigger = str(job.trigger)
        assert "minute='*/5'" in trigger
        assert "hour='0'" not in trigger  # no longer once-nightly
    finally:
        sched.shutdown(wait=False)


def test_dynamic_memo_queue_runs_live_before_one_backtest_batch(monkeypatch, tmp_path):
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    import ops.main as main_mod

    order = []
    monkeypatch.setattr(
        main_mod, "_research_overnight_tick",
        lambda *_args, **_kwargs: order.append("live"),
    )
    monkeypatch.setattr(main_mod, "_live_memo_work_pending", lambda _cfg: False)
    monkeypatch.setattr(
        "ops.backtest.service.process_enqueued_generation",
        lambda **_kwargs: order.append("backtest") or SimpleNamespace(attempted=1),
    )
    cfg = SimpleNamespace(
        research_pause_flag_path=str(tmp_path / "paused"),
        research_drain_deadline_hour=8,
    )
    now = datetime(2026, 7, 16, 17, tzinfo=ZoneInfo("America/New_York"))
    main_mod._dynamic_memo_queue_tick(None, cfg, now=lambda: now)
    assert order == ["live", "backtest"]


def test_full_scheduler_registers_overnight_job():
    """Mirrors test_research_trade_job_registered_and_gated — a MagicMock
    journal/config is enough to verify registration without touching the
    paper-trading _startup path (real backend/broker wiring is out of scope
    here; that path is exercised elsewhere)."""
    from unittest.mock import MagicMock
    from ops.main import _start_full_scheduler

    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        config=MagicMock(),
    )
    try:
        job = sched.get_job("research_overnight")
        assert job is not None
    finally:
        sched.shutdown(wait=False)


def test_full_scheduler_registers_gc_reap_job():
    """The hourly GC pass reaps per-thread library state stranded by dead
    tool-executor threads (yfinance's peewee tz-cache connection and
    curl_cffi session hold fds until a gen-2 collection). Without it the
    daemon accumulates fds toward errno 24 during market-hours analyses
    (observed live 2026-07-09/10)."""
    from unittest.mock import MagicMock
    from ops.main import _start_full_scheduler

    sched = _start_full_scheduler(
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        config=MagicMock(),
    )
    try:
        job = sched.get_job("gc_reap")
        assert job is not None
    finally:
        sched.shutdown(wait=False)


def test_gc_reap_tick_runs_a_full_collection(monkeypatch):
    import gc

    from ops import main as main_mod

    calls = []
    monkeypatch.setattr(gc, "collect", lambda: calls.append(True) or 0)
    main_mod._gc_reap_tick()
    assert calls == [True]
