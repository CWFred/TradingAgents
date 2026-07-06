import os
from decimal import Decimal

import pytest

from ops.config import OpsConfig
from ops.journal import Journal
from ops.main import _build_broker, _emit_halt_events, _wire
from ops.reconcile import PositionDiff, ReconcileResult


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
