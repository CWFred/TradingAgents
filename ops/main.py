"""ops run — always-on orchestrator + guardian service.

Runs in the foreground. SIGINT/SIGTERM triggers graceful shutdown:
scheduler drains in-flight jobs, journal closes cleanly. Exit codes:
- 0: clean shutdown
- 2: reconciliation-halted shutdown (journal has inconsistency events)
- 3: startup-halted — broker unreachable while building/reconciling
     (journal has broker_unreachable + startup_halted events)
"""
from __future__ import annotations

import os
import signal
import sys
import threading
from datetime import datetime, timezone
from decimal import Decimal

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ops import (
    build_guarded_paper_broker_from_journal,
    build_guarded_robinhood_broker,
)
from ops.broker.base import BrokerError
from ops.config import OpsConfig, load_config
from ops.journal import Journal
from ops.live_gate import record_flip_marker
from ops.notify.config import load_notify_config
from ops.notify.dispatcher import NotifyDispatcher
from ops.notify.email import build_email_transport
from ops.notify.push import build_push_transport
from ops.notify.transport import DisabledTransport
from ops.notify.summary import emit_daily_summary
from ops.position_guardian import PositionGuardian
from ops.reconcile import ReconcileResult, emit_reconcile_events, reconcile
from ops.scheduler.market_calendar import MarketCalendar
from ops.scheduler.orchestrator import Orchestrator
from ops.trading_time import trading_day_start, trading_week_start

_shutdown_event = threading.Event()


def _shutdown_handler(signum, frame) -> None:
    _shutdown_event.set()


def _resolve_and_announce_journal_path(config: OpsConfig) -> str:
    """Resolve config.journal_path to an absolute path, print it to stdout,
    and warn on stderr when this run is about to create a brand-new journal
    file (the path didn't exist before open). A CWD-relative journal_path
    used to silently create a fresh journal — and fresh paper account —
    whenever `ops run` launched from the wrong directory; printing the
    resolved path makes that immediately visible (L4)."""
    resolved = os.path.abspath(os.path.expanduser(config.journal_path))
    is_new = not os.path.exists(resolved)
    print(f"Journal: {resolved}")
    if is_new:
        print(
            f"WARNING: journal file does not exist yet — creating a new one "
            f"at {resolved}",
            file=sys.stderr,
        )
    return resolved


def _start_of_day_equity(journal: Journal) -> Decimal:
    """Today's open_day baseline, or 0 when none exists yet.

    The since= filter matters: without it a stale snapshot from a previous
    day/week becomes the drawdown baseline after downtime. The drawdown
    rules treat a start of <= 0 as "no baseline yet" and allow the order;
    the orchestrator records a fresh snapshot at its first tick of the day.
    """
    now = datetime.now(timezone.utc)
    start = trading_day_start(now)
    snap = journal.get_latest_equity_snapshot(kind="open_day", since=start)
    return snap.equity if snap is not None else Decimal("0")


def _start_of_week_equity(journal: Journal) -> Decimal:
    now = datetime.now(timezone.utc)
    monday = trading_week_start(now)
    snap = journal.get_latest_equity_snapshot(kind="open_week", since=monday)
    return snap.equity if snap is not None else Decimal("0")


def _ensure_paper_seed(journal: Journal, config: OpsConfig) -> None:
    """Record the paper account's starting cash as an explicit journal
    adjustment, exactly once. Replay then starts from 0 and reconstructs
    cash entirely from journaled adjustments + fills — no hardcoded number
    that silently diverges once deposits exist."""
    adjustments = journal.read_cash_adjustments()
    if any(a["kind"] == "seed" for a in adjustments):
        return
    journal.record_cash_adjustment(
        kind="seed", amount=config.starting_cash, note="paper starting cash",
    )


def _ensure_live_baseline(journal: Journal, broker) -> None:
    """One-time live-mode cash baseline.

    The real account was funded before the journal existed, so replayed
    cash (from 0) can never match broker cash. On the first live startup,
    record the difference as a `live_baseline` adjustment; from then on
    reconciliation compares like with like, and any NEW drift (an
    unjournaled deposit, a manual trade's cash effect) still halts startup
    until the user records it explicitly."""
    from ops.broker.paper import PaperBroker

    adjustments = journal.read_cash_adjustments()
    if any(a["kind"] == "live_baseline" for a in adjustments):
        return
    replay = PaperBroker.from_journal(
        journal=journal, quote_source=broker.get_quote,
        starting_cash=Decimal("0"),
    )
    delta = broker.get_cash() - replay.get_cash()
    journal.record_cash_adjustment(
        kind="live_baseline", amount=delta,
        note="cash on hand at first live startup, minus journal-replayed cash",
    )


def _build_broker(config: OpsConfig, journal: Journal):
    """Construct the guarded broker for the configured mode.

    Paper mode rebuilds the inner PaperBroker from the journal (via
    build_guarded_paper_broker_from_journal) so a restarted ops run
    picks up prior positions and cash. Robinhood mode reads live state
    from the MCP.
    """
    from ops.quotes import make_yfinance_quote_source
    quote_source = make_yfinance_quote_source()

    def _sod():
        return _start_of_day_equity(journal)

    def _sow():
        return _start_of_week_equity(journal)

    if config.broker_mode == "robinhood":
        broker = build_guarded_robinhood_broker(
            config=config, journal=journal,
            start_of_day_equity=_sod, start_of_week_equity=_sow,
        )
        _ensure_live_baseline(journal, broker)
        # Startup is single-threaded here, so recording the flip marker
        # right after the live baseline (and before any scheduled job can
        # run) is race-free.
        record_flip_marker(journal)
        return broker
    _ensure_paper_seed(journal, config)
    return build_guarded_paper_broker_from_journal(
        config=config, journal=journal,
        quote_source=quote_source,
        starting_cash=Decimal("0"),
        start_of_day_equity=_sod, start_of_week_equity=_sow,
    )


def _wire(broker, journal: Journal, config: OpsConfig):
    """Wire the orchestrator + guardian + calendar for the given broker+config."""
    from ops.pipeline_adapter import TradingAgentsPipelineAdapter
    from ops.strategy.post_earnings_momentum import PostEarningsMomentumStrategy
    from ops.universe import build_universe

    calendar = MarketCalendar()
    orchestrator = Orchestrator(
        broker=broker,
        universe_builder=build_universe,
        strategy=PostEarningsMomentumStrategy(config=config),
        pipeline_adapter=TradingAgentsPipelineAdapter(),
        calendar=calendar, journal=journal, config=config,
    )
    guardian = PositionGuardian(
        broker=broker, quote_source=broker.get_quote, config=config,
        journal=journal, broker_mode=config.broker_mode,
        market_open_fn=calendar.is_open_now,
    )
    return orchestrator, guardian, calendar


def _emit_halt_events(journal: Journal, result: ReconcileResult) -> None:
    emit_reconcile_events(journal, result)
    journal.record_event("startup_halted", {"reason": "reconciliation"})


def _startup(config: OpsConfig, journal: Journal):
    """Build the broker, wire the orchestrator/guardian/calendar, and
    reconcile against broker state — the sequence that must complete
    before the service starts scheduling any jobs.

    Both broker construction (live mode calls _ensure_live_baseline ->
    get_cash) and reconcile() talk to the broker and can raise
    BrokerError when it's unreachable; callers handle that distinctly
    from a reconciliation diff (see M6)."""
    broker = _build_broker(config, journal)
    orchestrator, guardian, calendar = _wire(broker, journal, config)
    result = reconcile(journal=journal, broker=broker, broker_mode=config.broker_mode)
    return broker, orchestrator, guardian, calendar, result


def _build_dispatcher(journal: Journal) -> NotifyDispatcher:
    """Assemble the notify dispatcher from environment-configured transports.
    Transports with missing credentials come back disabled (they no-op on
    send), so this is safe to call unconditionally in both paper and live
    mode — no crash, no accidental delivery without creds."""
    cfg = load_notify_config()
    if not cfg.notify_enabled:
        transports = {
            "push": DisabledTransport("notify disabled: OPS_NOTIFY_ENABLED not set"),
            "email": DisabledTransport("notify disabled: OPS_NOTIFY_ENABLED not set"),
        }
    else:
        transports = {
            "push": build_push_transport(cfg),
            "email": build_email_transport(cfg),
        }
    return NotifyDispatcher(journal, transports)


def _notify_tick(dispatcher) -> None:
    """Scheduler-safe: a transport/network failure must never kill the
    APScheduler job, or all future notifications (and the guardian/
    orchestrator jobs sharing the scheduler) would be at risk."""
    try:
        dispatcher.dispatch_once()
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
        print(f"notify tick error: {exc}", file=sys.stderr)


def _daily_summary_tick(journal: Journal, broker) -> None:
    """Scheduler-safe wrapper around emit_daily_summary: a broker/journal
    error is recorded as an event rather than raised, since raising would
    kill the daily_summary APScheduler job."""
    try:
        emit_daily_summary(journal, broker)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
        journal.record_event(
            "daily_summary_error", {"error": f"{type(exc).__name__}: {exc}"},
        )


def _start_full_scheduler(
    orchestrator: Orchestrator, guardian: PositionGuardian,
    dispatcher: NotifyDispatcher, journal: Journal, broker,
) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="America/New_York")
    sched.add_job(
        orchestrator.tick,
        CronTrigger(minute="0,30", hour="9-15", day_of_week="mon-fri"),
        id="orchestrator_tick", max_instances=1, misfire_grace_time=60,
    )
    sched.add_job(
        guardian.check_stops_once,
        IntervalTrigger(seconds=60),
        id="guardian_poll", max_instances=1, misfire_grace_time=15,
    )
    sched.add_job(
        lambda: _notify_tick(dispatcher),
        IntervalTrigger(seconds=20),
        id="notify_poll", max_instances=1, misfire_grace_time=15,
    )
    sched.add_job(
        lambda: _daily_summary_tick(journal, broker),
        CronTrigger(hour=16, minute=5, day_of_week="mon-fri"),
        id="daily_summary", max_instances=1, misfire_grace_time=300,
    )
    sched.start()
    return sched


def _start_guardian_only(
    guardian: PositionGuardian, dispatcher: NotifyDispatcher,
) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="America/New_York")
    sched.add_job(
        guardian.check_stops_once,
        IntervalTrigger(seconds=60),
        id="guardian_poll", max_instances=1, misfire_grace_time=15,
    )
    sched.add_job(
        lambda: _notify_tick(dispatcher),
        IntervalTrigger(seconds=20),
        id="notify_poll", max_instances=1, misfire_grace_time=15,
    )
    sched.start()
    return sched


def _run_until_signal() -> None:
    _shutdown_event.wait()


def run() -> int:
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    config = load_config()
    journal_path = _resolve_and_announce_journal_path(config)
    journal = Journal(journal_path)
    try:
        try:
            broker, orchestrator, guardian, calendar, result = _startup(config, journal)
        except BrokerError as exc:
            # Do NOT journal str(exc): broker-connectivity exceptions can
            # embed credentials/hostnames. Only the exception type name is
            # safe to persist in the durable, potentially-shared journal —
            # same rationale as NotifyDispatcher.dispatch_once's
            # notify_dispatch_error sanitization.
            journal.record_event(
                "broker_unreachable", {"error_type": type(exc).__name__},
            )
            journal.record_event("startup_halted", {"reason": "broker_unreachable"})
            print(
                f"Startup halted: broker unreachable ({exc}). "
                "Check connectivity/credentials and restart.",
                file=sys.stderr,
            )
            return 3
        if result.positions_recovered_without_stops:
            print(
                "WARNING: "
                f"{len(result.positions_recovered_without_stops)} position(s) "
                "opened without recorded stops — guardian will use config "
                f"fallback: {result.positions_recovered_without_stops}",
                file=sys.stderr,
            )
        dispatcher = _build_dispatcher(journal)
        if result.diffs:
            _emit_halt_events(journal, result)
            print(
                f"Reconciliation halted orchestrator — {len(result.diffs)} diff(s). "
                "Guardian continues. Investigate journal 'inconsistency' events.",
                file=sys.stderr,
            )
            sched = _start_guardian_only(guardian, dispatcher)
            _run_until_signal()
            sched.shutdown(wait=True)
            return 2
        sched = _start_full_scheduler(orchestrator, guardian, dispatcher, journal, broker)
        _run_until_signal()
        sched.shutdown(wait=True)
        return 0
    finally:
        journal.close()
