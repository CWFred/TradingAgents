"""ops run — always-on orchestrator + guardian service.

Runs in the foreground. SIGINT/SIGTERM triggers graceful shutdown:
scheduler drains in-flight jobs, journal closes cleanly. Exit codes:
- 0: clean shutdown
- 2: reconciliation-halted shutdown (journal has inconsistency events)
- 3: startup-halted — broker unreachable while building/reconciling
     (journal has broker_unreachable + startup_halted events)
- 4: live-flip ritual refused — first robinhood start without a TTY, or
     the typed equity confirmation did not match (journal has a
     live_flip_refused event); nothing was scheduled

Every session brackets itself with service_started / service_stopping
journal events (the uptime record used by the graduation evaluation);
service_stopping carries the exit code.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ops import (
    build_guarded_paper_broker_from_journal,
    build_guarded_robinhood_broker,
    events,
)
from ops.broker.base import BrokerError
from ops.config import OpsConfig, load_config
from ops.journal import Journal
from ops.live_gate import flip_epoch, record_flip_marker
from ops.llm_backend import build_managed_backend, load_managed_backend_config
from ops.notify.config import load_notify_config
from ops.notify.dispatcher import NotifyDispatcher
from ops.notify.email import build_email_transport
from ops.notify.push import build_push_transport
from ops.notify.summary import emit_daily_summary
from ops.notify.transport import DisabledTransport
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


def _git_sha() -> str | None:
    """Short git sha of the running checkout, or None.

    Best-effort provenance for the service_started uptime record. Any
    failure (no git, not a checkout, slow disk) is swallowed — startup
    must never depend on git being present or fast."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=2,
        )
    except Exception:  # noqa: BLE001 - provenance is strictly optional
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


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
        # The flip marker is deliberately NOT recorded here: it must mean
        # "the live-flip ritual passed" (A5), and _build_broker also runs
        # on attempts the ritual goes on to refuse. See _live_flip_ritual.
        return broker
    _ensure_paper_seed(journal, config)
    return build_guarded_paper_broker_from_journal(
        config=config, journal=journal,
        quote_source=quote_source,
        starting_cash=Decimal("0"),
        start_of_day_equity=_sod, start_of_week_equity=_sow,
    )


class LiveFlipRefused(Exception):
    """First live start was not confirmed — see _live_flip_ritual."""


def _live_flip_ritual(journal: Journal, broker, config: OpsConfig) -> None:
    """First-live confirmation gate (A5, graduation criterion #4).

    OPS_BROKER_MODE=robinhood is one stale shell export away from live
    trading, so the FIRST live start requires a human at a terminal to
    type the account equity back verbatim. Once the flip marker exists the
    ritual is skipped entirely — restarts must be unattended (launchd) —
    and the marker is recorded HERE, only after the ritual passes, so a
    refused attempt can never satisfy "marker exists" later.

    Raises LiveFlipRefused (caller exits 4, schedules nothing) on: non-TTY
    stdin (a supervisor must never perform the first flip), EOF, or a typed
    figure that does not exactly match the printed Decimal. Startup is
    single-threaded here, so recording the marker before any scheduled job
    can run is race-free."""
    if flip_epoch(journal) is not None:
        return
    if not sys.stdin.isatty():
        journal.record_event(
            events.KIND_LIVE_FLIP_REFUSED,
            events.live_flip_refused_payload(reason="non_tty"),
        )
        raise LiveFlipRefused(
            "first live start requires an interactive terminal; "
            "run `ops run` by hand once — restarts are then unattended"
        )
    equity = broker.get_equity()
    expected = str(equity)
    print("FIRST LIVE START — broker mode is 'robinhood' (real money).")
    print(f"Account equity: {expected}")
    print(
        f"Live gate: max ${config.live_max_position} per position for the "
        f"first {config.live_fill_gate_count} live BUY fills."
    )
    try:
        typed = input("Type the account equity exactly as printed to proceed: ")
    except EOFError:
        journal.record_event(
            events.KIND_LIVE_FLIP_REFUSED,
            events.live_flip_refused_payload(reason="eof"),
        )
        raise LiveFlipRefused("stdin closed before confirmation") from None
    if typed.strip() != expected:
        journal.record_event(
            events.KIND_LIVE_FLIP_REFUSED,
            events.live_flip_refused_payload(reason="equity_mismatch"),
        )
        raise LiveFlipRefused("typed equity did not match the printed figure")
    record_flip_marker(journal)


def _wire(broker, journal: Journal, config: OpsConfig, *, backend=None):
    """Wire the orchestrator + guardian + calendar for the given broker+config.

    ``backend`` is the managed local-model lifecycle manager threaded into the
    pipeline adapter (off unless OPS_LLM_MANAGED_BACKEND is set). It is also
    returned so the service can tear it down on shutdown. Injectable for tests.
    """
    from ops.pipeline_adapter import TradingAgentsPipelineAdapter
    from ops.strategy.post_earnings_momentum import PostEarningsMomentumStrategy
    from ops.universe.composite import build_composite_universe

    if backend is None:
        backend = build_managed_backend(load_managed_backend_config())
    calendar = MarketCalendar()
    orchestrator = Orchestrator(
        broker=broker,
        universe_builder=build_composite_universe,
        strategy=PostEarningsMomentumStrategy(config=config),
        pipeline_adapter=TradingAgentsPipelineAdapter(backend=backend),
        calendar=calendar, journal=journal, config=config,
    )
    guardian = PositionGuardian(
        broker=broker, quote_source=broker.get_quote, config=config,
        journal=journal, broker_mode=config.broker_mode,
        market_open_fn=calendar.is_open_now,
    )
    return orchestrator, guardian, calendar, backend


def _emit_halt_events(journal: Journal, result: ReconcileResult) -> None:
    emit_reconcile_events(journal, result)
    journal.record_event(
        events.KIND_STARTUP_HALTED,
        events.startup_halted_payload(reason="reconciliation"),
    )


def _startup(config: OpsConfig, journal: Journal):
    """Build the broker, wire the orchestrator/guardian/calendar, and
    reconcile against broker state — the sequence that must complete
    before the service starts scheduling any jobs.

    Both broker construction (live mode calls _ensure_live_baseline ->
    get_cash) and reconcile() talk to the broker and can raise
    BrokerError when it's unreachable; callers handle that distinctly
    from a reconciliation diff (see M6)."""
    broker = _build_broker(config, journal)
    if config.broker_mode == "robinhood":
        # Before reconcile/scheduling: nothing live may proceed until the
        # first flip is confirmed (or the marker already exists).
        _live_flip_ritual(journal, broker, config)
    orchestrator, guardian, calendar, backend = _wire(broker, journal, config)
    result = reconcile(journal=journal, broker=broker, broker_mode=config.broker_mode)
    return broker, orchestrator, guardian, calendar, result, backend


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


def _daily_summary_tick(journal: Journal, broker, calendar=None) -> None:
    """Scheduler-safe wrapper around emit_daily_summary: a broker/journal
    error is recorded as an event rather than raised, since raising would
    kill the daily_summary APScheduler job."""
    try:
        emit_daily_summary(journal, broker, calendar=calendar)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
        journal.record_event(
            events.KIND_DAILY_SUMMARY_ERROR,
            events.daily_summary_error_payload(
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


def _research_monitor_tick(journal: Journal, config) -> None:
    """Scheduler-safe wrapper around the Phase C memo monitor: gate on the
    run-summary event (restart-safe once-per-day, same pattern as the
    orchestrator's daily_cycle_run), and record errors as events rather than
    raising — raising would kill the APScheduler job."""
    try:
        if journal.has_event_today(events.KIND_RESEARCH_MONITOR_RUN):
            return
        from ops.research.monitor import monitor_memos
        from ops.research.store import ScreenStore
        from tradingagents.memos.store import MemoStore

        monitor_memos(
            memo_store=MemoStore(config.memo_store_path),
            screen_store=ScreenStore(config.screen_store_path),
            journal=journal,
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
        journal.record_event(
            events.KIND_RESEARCH_MONITOR_ERROR,
            events.research_monitor_error_payload(
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


def _overview_path(when: date) -> str:
    """`${XDG_STATE_HOME:-~/.local/state}/tradingagents/overviews/overview-YYYY-MM-DD.md`
    — mirrors the base-dir logic in ops/config.py's _default_*_path helpers."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(
        os.path.expanduser(base), "tradingagents", "overviews",
        f"overview-{when.isoformat()}.md",
    )


def _daily_overview_tick(journal: Journal, config) -> None:
    """Scheduler-safe wrapper around the cross-sleeve daily overview: gate on
    the run-summary event (restart-safe once-per-day, same pattern as
    _research_monitor_tick — registered twice, weekday + Saturday, so the
    gate is what makes the double registration safe), write the markdown
    file, push the headline, then record the gate event. Errors are recorded
    as events rather than raising — raising would kill the APScheduler job.

    The push is wrapped in its own try/except: a push failure must not
    prevent the file (already written) or the gate event (recorded right
    after) — only the catch-all below may turn a failure into
    KIND_DAILY_OVERVIEW_ERROR, and it must never do so for a push-only
    failure once the file is safely on disk."""
    try:
        if journal.has_event_today(events.KIND_DAILY_OVERVIEW):
            return
        from ops.notify.overview import (
            build_daily_overview,
            format_daily_overview,
            overview_headline,
        )
        from tradingagents.memos.store import MemoStore

        memo_store = MemoStore(config.memo_store_path)
        with (
            Journal(config.baseline_journal_path) as baseline_journal,
            Journal(config.research_journal_path) as research_journal,
        ):
            report = build_daily_overview(
                main_journal=journal, baseline_journal=baseline_journal,
                research_journal=research_journal, memo_store=memo_store,
                config=config,
            )
        rendered = format_daily_overview(report)
        headline = overview_headline(report)
        path = _overview_path(report["date"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(rendered + "\n")

        try:
            from ops.notify.config import load_notify_config

            notify_cfg = load_notify_config()
            if notify_cfg.notify_enabled:
                from ops.notify.push import build_push_transport
                from ops.notify.transport import NotifyMessage

                build_push_transport(notify_cfg).send(
                    NotifyMessage(title="Daily overview", body=headline)
                )
        except Exception as exc:  # noqa: BLE001 - push failure must not block the gate event
            print(f"daily overview push error: {exc}", file=sys.stderr)

        journal.record_event(
            events.KIND_DAILY_OVERVIEW,
            events.daily_overview_payload(
                date=report["date"].isoformat(), headline=headline, path=path,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
        journal.record_event(
            events.KIND_DAILY_OVERVIEW_ERROR,
            events.daily_overview_error_payload(
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


def _research_trade_tick(journal: Journal, config) -> None:
    """Scheduler-safe wrapper around the Phase D research-sleeve trade step:
    gate on the run-summary event (restart-safe once-per-day, same pattern as
    _research_monitor_tick), and record errors as events rather than raising
    — raising would kill the APScheduler job."""
    try:
        if journal.has_event_today(events.KIND_RESEARCH_TRADE_RUN):
            return
        from ops.quotes import make_yfinance_quote_source
        from ops.research.trading import trade_research_sleeve
        from tradingagents.memos.store import MemoStore

        with Journal(config.research_journal_path) as research_journal:
            trade_research_sleeve(
                memo_store=MemoStore(config.memo_store_path),
                research_journal=research_journal,
                main_journal=journal,
                quote_source=make_yfinance_quote_source(),
                starting_cash=config.research_starting_cash,
                asof=date.today(),
            )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
        journal.record_event(
            events.KIND_RESEARCH_TRADE_ERROR,
            events.research_trade_error_payload(
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


def _days_since_iso(iso: str) -> float:
    """Whole-plus-fractional days between an ISO-8601 UTC timestamp and now."""
    then = datetime.fromisoformat(iso)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 86400.0


def _drain_deadline(hour: int) -> datetime:
    """Today's local (America/New_York) HH:00 as a tz-aware datetime — the
    wall-clock the overnight drain must stop before, ahead of the 09:00
    first momentum tick (CronTrigger minute="0,30" hour="9-15"). The
    scheduler's thread pool does NOT serialize the drain against that tick;
    this time margin is the sole guard against two models holding ds4 at
    once, which is why research_drain_deadline_hour is validated < 9."""
    ny = ZoneInfo("America/New_York")
    return datetime.now(ny).replace(hour=hour, minute=0, second=0, microsecond=0)


def _research_overnight_tick(
    journal: Journal, config, *, now=None, should_stop=None, vet_adapter_factory=None,
) -> None:
    """Nightly 00:00 job, two stages under one deadline and one ds4 bracket:
    (1) screen if >= research_screen_interval_days, then drain pending screen
    hits into brain memos; (2) graph-vet the pending_vetting queue (brain
    buys) oldest-first. Both stages stop at the same local deadline hour /
    on shutdown; whatever isn't vetted tonight stays pending_vetting and
    carries to the next night. Scheduler-safe: a drain failure records
    research_drain_error, a vetting failure records research_vetting_error
    (see _research_vetting_stage); neither raises.

    No has_event_today gate here (unlike the sibling research ticks): the
    3-day screen-due check plus the two queue states already make re-firing
    idempotent/safe — a second run same night either finds both queues empty
    (no-op, skipping ds4 entirely) or correctly resumes whatever is pending.
    """
    screened_this_run = False
    try:
        from ops.research.store import ScreenStore
        from tradingagents.memos.store import MemoStore

        store = ScreenStore(config.screen_store_path)
        last = store.last_run()
        due = last is None or _days_since_iso(last["created_at"]) >= config.research_screen_interval_days
        if due:
            from ops.research.run import run_screen

            run_screen(config=config, asof=date.today())
            screened_this_run = True

        memo_store = MemoStore(config.memo_store_path)
        pending = store.pending_hits()
        if not pending and not memo_store.pending_vetting_memos():
            # Nothing to drain OR vet — skip waking the 86 GB ds4 model
            # entirely. This is the common case (~2 of 3 nights).
            journal.record_event(
                events.KIND_RESEARCH_DRAIN_RUN,
                events.research_drain_run_payload(
                    asof=date.today().isoformat(), screened_this_run=screened_this_run,
                    researched=0, failed=0, still_pending=0, hit_deadline=False,
                ),
            )
            return

        deadline = _drain_deadline(config.research_drain_deadline_hour)
        stop = should_stop or _shutdown_event.is_set
        tick_now = now or (lambda: datetime.now(deadline.tzinfo))
        backend = build_managed_backend(load_managed_backend_config())
        try:
            if pending:
                from tradingagents.dataflows import edgar
                edgar.get_user_agent()  # fail fast before spinning ds4

                from ops.research.drain import drain_pending
                from ops.research.models import build_stage_llm

                evidence_llm = build_stage_llm(config.research_evidence_model)
                thesis_llm = build_stage_llm(config.research_thesis_model)
                backend.ensure_up()
                summary = drain_pending(
                    store=store, memo_store=memo_store,
                    evidence_llm=evidence_llm, thesis_llm=thesis_llm,
                    thesis_model_spec=config.research_thesis_model,
                    deadline=deadline, should_stop=stop, now=tick_now,
                )
                journal.record_event(
                    events.KIND_RESEARCH_DRAIN_RUN,
                    events.research_drain_run_payload(
                        asof=date.today().isoformat(), screened_this_run=screened_this_run,
                        researched=summary.researched, failed=summary.failed,
                        still_pending=summary.still_pending, hit_deadline=summary.hit_deadline,
                    ),
                )
            else:
                journal.record_event(
                    events.KIND_RESEARCH_DRAIN_RUN,
                    events.research_drain_run_payload(
                        asof=date.today().isoformat(), screened_this_run=screened_this_run,
                        researched=0, failed=0, still_pending=0, hit_deadline=False,
                    ),
                )
            _research_vetting_stage(
                journal, config, memo_store=memo_store, backend=backend,
                deadline=deadline, should_stop=stop, now=tick_now,
                adapter_factory=vet_adapter_factory,
            )
        finally:
            backend.shutdown()
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        journal.record_event(
            events.KIND_RESEARCH_DRAIN_ERROR,
            events.research_drain_error_payload(
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


def _research_vetting_stage(
    journal: Journal, config, *, memo_store, backend, deadline, should_stop,
    now, adapter_factory=None,
) -> None:
    """Stage 2 of the overnight tick: graph-vet the pending_vetting queue.

    Scheduler-safe and drain-independent: any failure records
    research_vetting_error and returns — the drain's success event is
    already journaled, memos stay pending_vetting for the next night, and
    the caller's finally still tears ds4 down. The graph adapter shares the
    tick's managed backend; its lazy ensure_up means a vet-only night spins
    ds4 only when a memo actually runs.
    """
    try:
        if not memo_store.pending_vetting_memos():
            return
        from ops.research.models import build_stage_llm
        from ops.research.vetting import vet_pending
        from tradingagents.default_config import DEFAULT_CONFIG

        if adapter_factory is None:
            from ops.pipeline_adapter import TradingAgentsPipelineAdapter

            adapter = TradingAgentsPipelineAdapter(backend=backend)
        else:
            adapter = adapter_factory(backend)
        summary = vet_pending(
            memo_store=memo_store, adapter=adapter,
            falsifier_llm=build_stage_llm(config.research_thesis_model),
            vetted_by_model=f"{DEFAULT_CONFIG['llm_provider']}:{DEFAULT_CONFIG['deep_think_llm']}",
            deadline=deadline, should_stop=should_stop, now=now,
        )
        journal.record_event(
            events.KIND_RESEARCH_VETTING_RUN,
            events.research_vetting_run_payload(
                asof=date.today().isoformat(), vetted=summary.vetted,
                confirmed=summary.confirmed, rejected=summary.rejected,
                failed=summary.failed, still_pending=summary.still_pending,
                hit_deadline=summary.hit_deadline,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        journal.record_event(
            events.KIND_RESEARCH_VETTING_ERROR,
            events.research_vetting_error_payload(
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


# Dead-man's switch tuning (A1.3). Staleness is 3x the guardian poll
# interval: one slow pass must not flap the external check, but a loop
# that has missed three consecutive polls is wedged and must look dead.
_HEARTBEAT_STALENESS_S = 180.0
_HEARTBEAT_ERROR_COOLDOWN_S = 600.0


def _make_heartbeat_job(
    *, guardian, journal: Journal, url: str,
    http_get=None, clock=time.monotonic,
):
    """Build the heartbeat job: ping `url` only while the guardian loop is
    demonstrably alive (last pass started < 180s ago on the monotonic
    clock). The intended target is a healthchecks.io-style check that
    alerts when pings STOP — the one alarm that fires when this process
    cannot speak for itself.

    Ping failures are swallowed (a monitoring outage must never disturb
    trading) and journaled as heartbeat_error at most once per 10 minutes;
    only the exception TYPE is journaled, since requests exception text
    embeds the ping URL (a secret-bearing token). `http_get`/`clock` are
    injected for tests, following the sleep_fn/clock_fn pattern from
    ops.broker.mcp_client._await_fill."""
    if http_get is None:
        import requests

        def http_get(u: str) -> None:
            requests.get(u, timeout=5)

    last_error_at: float | None = None

    def _heartbeat() -> None:
        nonlocal last_error_at
        last_pass = guardian.last_pass_started_at
        if last_pass is None or clock() - last_pass >= _HEARTBEAT_STALENESS_S:
            return
        try:
            http_get(url)
        except Exception as exc:  # noqa: BLE001 - monitoring must not disturb trading
            now = clock()
            if (last_error_at is None
                    or now - last_error_at >= _HEARTBEAT_ERROR_COOLDOWN_S):
                last_error_at = now
                journal.record_event(
                    events.KIND_HEARTBEAT_ERROR,
                    events.heartbeat_error_payload(
                        error_type=type(exc).__name__,
                    ),
                )

    return _heartbeat


def _build_heartbeat_job(guardian, journal: Journal):
    """Heartbeat job per env config, or None when OPS_HEARTBEAT_URL is
    unset (feature off — no job is ever registered)."""
    cfg = load_notify_config()
    if not cfg.heartbeat_url:
        return None
    return _make_heartbeat_job(
        guardian=guardian, journal=journal, url=cfg.heartbeat_url,
    )


def _start_full_scheduler(
    orchestrator: Orchestrator, guardian: PositionGuardian,
    dispatcher: NotifyDispatcher, journal: Journal, broker,
    calendar=None, heartbeat_job=None, config=None,
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
        lambda: _daily_summary_tick(journal, broker, calendar=calendar),
        CronTrigger(hour=16, minute=5, day_of_week="mon-fri"),
        id="daily_summary", max_instances=1, misfire_grace_time=300,
    )
    if config is not None:
        sched.add_job(
            lambda: _research_monitor_tick(journal, config),
            CronTrigger(hour=16, minute=20, day_of_week="mon-fri"),
            id="research_monitor", max_instances=1, misfire_grace_time=300,
        )
        sched.add_job(
            lambda: _research_trade_tick(journal, config),
            CronTrigger(hour=16, minute=25, day_of_week="mon-fri"),
            id="research_trade", max_instances=1, misfire_grace_time=300,
        )
        sched.add_job(
            lambda: _research_overnight_tick(journal, config),
            CronTrigger(hour=0, minute=0),
            id="research_overnight", max_instances=1, misfire_grace_time=600,
        )
        sched.add_job(
            lambda: _daily_overview_tick(journal, config),
            CronTrigger(hour=16, minute=35, day_of_week="mon-fri"),
            id="daily_overview", max_instances=1, misfire_grace_time=600,
        )
        sched.add_job(
            lambda: _daily_overview_tick(journal, config),
            CronTrigger(hour=18, minute=0, day_of_week="sat"),
            id="daily_overview_saturday", max_instances=1, misfire_grace_time=600,
        )
    if heartbeat_job is not None:
        sched.add_job(
            heartbeat_job,
            IntervalTrigger(seconds=60),
            id="heartbeat", max_instances=1, misfire_grace_time=15,
        )
    sched.start()
    return sched


def _start_guardian_only(
    guardian: PositionGuardian, dispatcher: NotifyDispatcher,
    heartbeat_job=None,
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
    if heartbeat_job is not None:
        sched.add_job(
            heartbeat_job,
            IntervalTrigger(seconds=60),
            id="heartbeat", max_instances=1, misfire_grace_time=15,
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
    # Every graceful return path below assigns exit_code before returning;
    # 1 therefore means "crashed out through an unhandled exception" in the
    # service_stopping uptime record (A1.2).
    exit_code = 1
    backend = None  # managed local-model backend, set once startup wires it
    try:
        journal.record_event(
            events.KIND_SERVICE_STARTED,
            events.service_started_payload(
                broker_mode=config.broker_mode,
                journal_path=journal_path,
                pid=os.getpid(),
                git_sha=_git_sha(),
            ),
        )
        try:
            broker, orchestrator, guardian, calendar, result, backend = _startup(config, journal)
        except LiveFlipRefused as exc:
            # live_flip_refused is already journaled (audit-only) by the
            # ritual; nothing was scheduled.
            print(f"Startup refused: {exc}", file=sys.stderr)
            exit_code = 4
            return exit_code
        except BrokerError as exc:
            # Do NOT journal str(exc): broker-connectivity exceptions can
            # embed credentials/hostnames. Only the exception type name is
            # safe to persist in the durable, potentially-shared journal —
            # same rationale as NotifyDispatcher.dispatch_once's
            # notify_dispatch_error sanitization.
            journal.record_event(
                events.KIND_BROKER_UNREACHABLE,
                events.broker_unreachable_payload(error_type=type(exc).__name__),
            )
            journal.record_event(
                events.KIND_STARTUP_HALTED,
                events.startup_halted_payload(reason="broker_unreachable"),
            )
            print(
                f"Startup halted: broker unreachable ({exc}). "
                "Check connectivity/credentials and restart.",
                file=sys.stderr,
            )
            exit_code = 3
            return exit_code
        if result.positions_recovered_without_stops:
            print(
                "WARNING: "
                f"{len(result.positions_recovered_without_stops)} position(s) "
                "opened without recorded stops — guardian will use config "
                f"fallback: {result.positions_recovered_without_stops}",
                file=sys.stderr,
            )
        dispatcher = _build_dispatcher(journal)
        heartbeat_job = _build_heartbeat_job(guardian, journal)
        if result.diffs:
            _emit_halt_events(journal, result)
            print(
                f"Reconciliation halted orchestrator — {len(result.diffs)} diff(s). "
                "Guardian continues. Investigate journal 'inconsistency' events.",
                file=sys.stderr,
            )
            sched = _start_guardian_only(
                guardian, dispatcher, heartbeat_job=heartbeat_job,
            )
            _run_until_signal()
            sched.shutdown(wait=True)
            exit_code = 2
            return exit_code
        sched = _start_full_scheduler(
            orchestrator, guardian, dispatcher, journal, broker,
            calendar=calendar, heartbeat_job=heartbeat_job, config=config,
        )
        _run_until_signal()
        sched.shutdown(wait=True)
        exit_code = 0
        return exit_code
    finally:
        # Safety net: the per-tick session normally tears the managed backend
        # down already; this frees it even if startup half-completed. Idempotent.
        if backend is not None:
            backend.shutdown()
        journal.record_event(
            events.KIND_SERVICE_STOPPING,
            events.service_stopping_payload(exit_code=exit_code),
        )
        journal.close()
