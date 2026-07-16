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
from datetime import date, datetime, timedelta, timezone
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
from ops.scheduler import times
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
    from ops.activity import ActivityReporter
    from ops.pipeline_adapter import TradingAgentsPipelineAdapter
    from ops.strategy.post_earnings_momentum import PostEarningsMomentumStrategy
    from ops.universe.composite import build_composite_universe

    if backend is None:
        backend = build_managed_backend(load_managed_backend_config())
    reporter = ActivityReporter(journal)
    calendar = MarketCalendar()
    orchestrator = Orchestrator(
        broker=broker,
        universe_builder=build_composite_universe,
        strategy=PostEarningsMomentumStrategy(config=config),
        pipeline_adapter=TradingAgentsPipelineAdapter(
            backend=backend, reporter=reporter),
        calendar=calendar, journal=journal, config=config,
        reporter=reporter,
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
            Journal(config.short_journal_path) as short_journal,
            Journal(config.insider_journal_path) as insider_journal,
        ):
            report = build_daily_overview(
                main_journal=journal, baseline_journal=baseline_journal,
                research_journal=research_journal, memo_store=memo_store,
                config=config, short_journal=short_journal,
                insider_journal=insider_journal,
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


def _short_trade_tick(journal: Journal, config) -> None:
    """Scheduler-safe wrapper around the short-sleeve trade step: gate on
    the run-summary event (restart-safe once-per-day, same pattern as
    _research_trade_tick), and record errors as events rather than raising
    — raising would kill the APScheduler job."""
    try:
        if journal.has_event_today(events.KIND_SHORT_TRADE_RUN):
            return
        from ops.quotes import make_yfinance_quote_source
        from ops.research.short_trading import trade_short_sleeve
        from tradingagents.memos.store import MemoStore

        with Journal(config.short_journal_path) as short_journal:
            trade_short_sleeve(
                memo_store=MemoStore(config.short_memo_store_path),
                short_journal=short_journal,
                main_journal=journal,
                quote_source=make_yfinance_quote_source(),
                starting_cash=config.short_starting_cash,
                deny_list=config.deny_list,
                asof=date.today(),
            )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
        journal.record_event(
            events.KIND_SHORT_TRADE_ERROR,
            events.short_trade_error_payload(
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


def _insider_scan_tick(journal: Journal, config) -> None:
    """Nightly Form 4 daily-index scan (00:15). No LLM, no ds4 — pure
    EDGAR I/O into the signal store. Gate + error discipline as always."""
    try:
        if journal.has_event_today(events.KIND_INSIDER_SCAN_RUN):
            return
        from ops.insider.scan import run_insider_scan
        from ops.insider.store import SignalStore

        summaries = run_insider_scan(store=SignalStore(config.insider_signal_store_path))
        journal.record_event(
            events.KIND_INSIDER_SCAN_RUN,
            events.insider_scan_run_payload(
                days=len(summaries),
                form4_seen=sum(s.form4_seen for s in summaries),
                universe_matches=sum(s.universe_matches for s in summaries),
                transactions_recorded=sum(s.transactions_recorded for s in summaries),
                errors=sum(len(s.errors) for s in summaries),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, scheduler-safe
        journal.record_event(
            events.KIND_INSIDER_SCAN_ERROR,
            events.insider_scan_error_payload(
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


def _insider_trade_tick(journal: Journal, config) -> None:
    """Scheduler-safe wrapper around the insider-sleeve trade step (16:29).
    Mechanical — no LLM. The exit-time memo resolver points at the INSIDER
    memo store; its failures are isolated inside the trade step."""
    try:
        if journal.has_event_today(events.KIND_INSIDER_TRADE_RUN):
            return
        from ops.insider.memo_lite import resolve_on_exit
        from ops.insider.store import SignalStore
        from ops.insider.trading import trade_insider_sleeve
        from ops.quotes import make_yfinance_quote_source
        from tradingagents.memos.store import MemoStore

        memo_store = MemoStore(config.insider_memo_store_path)

        def resolver(**kwargs):
            resolve_on_exit(memo_store=memo_store, **kwargs)

        with Journal(config.insider_journal_path) as insider_journal:
            trade_insider_sleeve(
                signal_store=SignalStore(config.insider_signal_store_path),
                insider_journal=insider_journal,
                main_journal=journal,
                quote_source=make_yfinance_quote_source(),
                starting_cash=config.insider_starting_cash,
                deny_list=config.deny_list,
                asof=date.today(),
                resolver=resolver,
            )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, scheduler-safe
        journal.record_event(
            events.KIND_INSIDER_TRADE_ERROR,
            events.insider_trade_error_payload(
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


def _days_since_iso(iso: str) -> float:
    """Whole-plus-fractional days between an ISO-8601 UTC timestamp and now."""
    then = datetime.fromisoformat(iso)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 86400.0


def _overnight_deadline(hour: int, *, now: datetime | None = None) -> datetime:
    """The wall-clock the overnight window must stop before.

    Weekday ticks: today's local (America/New_York) HH:00 — ahead of the
    09:00 first momentum tick (CronTrigger minute="0,30" hour="9-15"). The
    scheduler's thread pool does NOT serialize the window against that
    tick; this time margin is the sole guard against two models holding
    ds4 at once, which is why research_drain_deadline_hour is validated <9.

    Weekend ticks (Sat/Sun 00:00): the deadline extends to the NEXT
    MONDAY's HH:00 — no momentum ticks compete for ds4 on weekends, and
    the whole backlog should be drained AND vetted before the trading
    week starts. A Sunday 00:00 tick that would overlap a still-running
    Saturday tick is skipped by the job's max_instances=1.
    """
    current = now or datetime.now(ZoneInfo("America/New_York"))
    deadline = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    while deadline.weekday() >= 5:  # Sat=5 / Sun=6 -> roll to Monday
        deadline += timedelta(days=1)
    return deadline


def _overnight_reason(config, *, screened_this_run: bool, store, memo_store) -> str:
    """Human reason string for the overnight job breadcrumb. Counts are
    best-effort — a missing store contributes nothing rather than raising."""
    parts: list[str] = []
    if screened_this_run:
        parts.append("screened")
    n = len(store.pending_hits())
    if n:
        parts.append(f"{n} hit(s) to research")
    n = len(memo_store.pending_vetting_memos())
    if n:
        parts.append(f"{n} memo(s) to vet")
    try:
        from ops.research.store import ScreenStore
        from tradingagents.memos.store import MemoStore

        n = len(ScreenStore(config.short_screen_store_path).pending_hits())
        if n:
            parts.append(f"{n} short hit(s)")
        n = len(MemoStore(config.short_memo_store_path).pending_vetting_memos())
        if n:
            parts.append(f"{n} short memo(s) to vet")
    except Exception:  # noqa: BLE001 - reason text must never kill the tick
        pass
    try:
        from ops.insider.store import SignalStore

        n = len(SignalStore(config.insider_signal_store_path).entries_without_memo())
        if n:
            parts.append(f"{n} insider memo(s) to author")
    except Exception:  # noqa: BLE001
        pass
    return "; ".join(parts) or "work pending"


def _research_overnight_tick(
    journal: Journal, config, *, now=None, should_stop=None, vet_adapter_factory=None,
) -> None:
    """Nightly 00:00 job: screen if >= research_screen_interval_days, then
    alternate graph-vetting and drain chunks under one deadline and one ds4
    bracket until both queues are empty or the deadline/shutdown lands:

        vet (whole queue) -> drain (research_drain_nightly_cap names)
            -> vet the drain's new buys -> drain the next chunk -> ...

    Vetting runs FIRST so confirmed memos become tradeable nightly instead
    of waiting for the drain backlog to clear (a drain-first order starved
    vetting for days on the first backlog, 2026-07-11), and the loop vets
    each drain chunk's buys in the SAME window when time remains. Weekday
    deadline is HH:00 today (frees ds4 before the 09:00 momentum tick);
    weekend ticks extend to Monday HH:00 so the whole backlog clears before
    the trading week starts (see _overnight_deadline). One aggregated
    research_drain_run and research_vetting_run event per tick.
    Scheduler-safe: a drain failure records research_drain_error, a vetting
    failure records research_vetting_error (see _research_vetting_stage)
    and disables further vet iterations that night; neither raises.

    The job fires every 30 minutes (see _start_full_scheduler) so
    `ops research resume` picks work back up quickly; fires while paused or
    outside the window return instantly and touch nothing. Re-firing is
    idempotent/safe: the 3-day screen-due check plus the two queue states
    mean a second run same night either finds both queues empty (no-op,
    skipping ds4 entirely; the zero bookkeeping event records once per day)
    or correctly resumes whatever is pending. A fire that lands while a
    previous one still runs is skipped by the job's max_instances=1.
    """
    screened_this_run = False
    try:
        # Operator pause (`ops research pause`): the operator wants their
        # machine — touch nothing, journal nothing, retry next fire.
        if os.path.exists(config.research_pause_flag_path):
            return
        deadline = _overnight_deadline(config.research_drain_deadline_hour)
        tick_now = now or (lambda: datetime.now(deadline.tzinfo))
        if tick_now() >= deadline:
            # Out-of-window fire (weekday daytime): silent no-op — this is
            # what makes the half-hourly trigger safe around market hours.
            return

        from ops.research.store import ScreenStore
        from tradingagents.memos.store import MemoStore

        store = ScreenStore(config.screen_store_path)
        last = store.last_run()
        due = last is None or _days_since_iso(last["created_at"]) >= config.research_screen_interval_days
        if due:
            # ONE combined sweep fills BOTH screen stores: every per-name
            # SEC fetch (facts, submissions, Form 4 XMLs, prices) happens
            # once — running the long and short screens separately doubled
            # the throttled sweep on exactly the busiest nights.
            from ops.research.run import run_screens

            run_screens(config=config, asof=date.today())
            screened_this_run = True

        memo_store = MemoStore(config.memo_store_path)
        research_idle = (
            not store.pending_hits() and not memo_store.pending_vetting_memos()
        )
        if research_idle:
            # Nothing to drain OR vet on the research side. Once per day,
            # not per fire: the half-hourly trigger must not spam it.
            if not journal.has_event_today(events.KIND_RESEARCH_DRAIN_RUN):
                journal.record_event(
                    events.KIND_RESEARCH_DRAIN_RUN,
                    events.research_drain_run_payload(
                        asof=date.today().isoformat(), screened_this_run=screened_this_run,
                        researched=0, failed=0, still_pending=0, hit_deadline=False,
                    ),
                )
            if (not _short_overnight_work_pending(config)
                    and not _insider_memo_work_pending(config)):
                # Nothing anywhere — skip waking the 86 GB ds4 model
                # entirely. This is the common case (~2 of 3 nights).
                return

        from ops.activity import ActivityReporter

        reporter = ActivityReporter(journal)
        reason = _overnight_reason(
            config, screened_this_run=screened_this_run,
            store=store, memo_store=memo_store,
        )
        with reporter.job("overnight", reason=reason) as activity:
            base_stop = should_stop or _shutdown_event.is_set
            pause_flag = config.research_pause_flag_path

            def stop() -> bool:
                # Pausing mid-run stops the loop between names, freeing ds4
                # within one name of `ops research pause`.
                return base_stop() or os.path.exists(pause_flag)

            backend = build_managed_backend(load_managed_backend_config())

            vetted = confirmed = rejected = vet_failed = 0
            vet_ran = False
            vet_errored = False
            vet_hit_deadline = False
            researched = drain_failed = 0
            drain_hit_deadline = False
            drain_llms = None  # (evidence, thesis) — built once, on first chunk
            try:
                while not research_idle and not stop() and tick_now() < deadline:
                    pending = store.pending_hits()
                    if not memo_store.pending_vetting_memos() and not pending:
                        break
                    progress = 0
                    # Vet the queue first (the adapter brings ds4 up lazily, so
                    # an empty vetting queue never spins it by itself).
                    if memo_store.pending_vetting_memos() and not vet_errored:
                        summary = _research_vetting_stage(
                            journal, config, memo_store=memo_store, backend=backend,
                            deadline=deadline, should_stop=stop, now=tick_now,
                            adapter_factory=vet_adapter_factory, reporter=reporter,
                        )
                        if summary is None:
                            vet_errored = True
                        else:
                            vet_ran = True
                            vetted += summary.vetted
                            confirmed += summary.confirmed
                            rejected += summary.rejected
                            vet_failed += summary.failed
                            vet_hit_deadline = vet_hit_deadline or summary.hit_deadline
                            progress += summary.vetted + summary.failed
                    # Then drain one chunk of screen hits into brain memos.
                    if pending:
                        if drain_llms is None:
                            from tradingagents.dataflows import edgar
                            edgar.get_user_agent()  # fail fast before spinning ds4

                            from ops.research.models import build_stage_llm

                            drain_llms = (
                                build_stage_llm(config.research_evidence_model),
                                build_stage_llm(config.research_thesis_model),
                            )
                        from ops.research.drain import drain_pending

                        backend.ensure_up()
                        summary = drain_pending(
                            store=store, memo_store=memo_store,
                            evidence_llm=drain_llms[0], thesis_llm=drain_llms[1],
                            thesis_model_spec=config.research_thesis_model,
                            max_names=config.research_drain_nightly_cap,
                            deadline=deadline, should_stop=stop, now=tick_now,
                            reporter=reporter,
                        )
                        researched += summary.researched
                        drain_failed += summary.failed
                        drain_hit_deadline = drain_hit_deadline or summary.hit_deadline
                        progress += summary.researched + summary.failed
                    if progress == 0:
                        break  # nothing moved (deadline/stop/errors) — don't spin
                if not research_idle:  # idle nights recorded their zero event above
                    journal.record_event(
                        events.KIND_RESEARCH_DRAIN_RUN,
                        events.research_drain_run_payload(
                            asof=date.today().isoformat(), screened_this_run=screened_this_run,
                            researched=researched, failed=drain_failed,
                            still_pending=len(store.pending_hits()),
                            hit_deadline=drain_hit_deadline,
                        ),
                    )
                if vet_ran:
                    journal.record_event(
                        events.KIND_RESEARCH_VETTING_RUN,
                        events.research_vetting_run_payload(
                            asof=date.today().isoformat(), vetted=vetted,
                            confirmed=confirmed, rejected=rejected, failed=vet_failed,
                            still_pending=len(memo_store.pending_vetting_memos()),
                            hit_deadline=vet_hit_deadline,
                        ),
                    )
                # Short sleeve gets whatever window remains — research (the
                # proven sleeve) always drains and vets first, and both share
                # this tick's single ds4 bracket (the sole contention guard).
                _short_overnight_pass(
                    journal, config, backend=backend, deadline=deadline,
                    should_stop=stop, tick_now=tick_now,
                    adapter_factory=vet_adapter_factory,
                    screened_this_run=screened_this_run,
                    reporter=reporter,
                )
                _insider_memo_pass(
                    journal, config, backend=backend, deadline=deadline,
                    should_stop=stop, tick_now=tick_now,
                    reporter=reporter,
                )
                activity.outcome = (
                    f"researched {researched}, vetted {vetted}, "
                    f"failed {drain_failed + vet_failed}"
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


def _short_overnight_work_pending(config) -> bool:
    """True when the short sleeve has overnight work: short-screen hits
    pending or short memos awaiting vetting. Used by
    _research_overnight_tick to decide whether an otherwise-idle night can
    skip the backend bracket entirely. Screening is NOT this sleeve's job —
    the tick's screen-if-due stage runs run_screens, which fills BOTH
    screen stores in one SEC sweep."""
    from ops.research.store import ScreenStore
    from tradingagents.memos.store import MemoStore

    if ScreenStore(config.short_screen_store_path).pending_hits():
        return True
    return bool(MemoStore(config.short_memo_store_path).pending_vetting_memos())


def _short_overnight_pass(
    journal: Journal, config, *, backend, deadline, should_stop, tick_now,
    adapter_factory=None, screened_this_run: bool = False, reporter=None,
) -> None:
    """Short-sleeve overnight work, run AFTER the research stages:
    alternate graph-vetting (inverted confirm map) and drain chunks against
    the SHORT stores under the caller's deadline and ds4 bracket. The
    screen itself already ran in the tick's combined run_screens stage —
    ``screened_this_run`` is passed through for honest bookkeeping only.
    Scheduler-safe: any failure records short_drain_error and returns — the
    caller's finally still tears the backend down. One aggregated
    short_drain_run / short_vetting_run event per tick, with the zero-work
    event gated once per day (half-hourly trigger must not spam)."""
    try:
        from ops.research.store import ScreenStore
        from tradingagents.memos.store import MemoStore

        store = ScreenStore(config.short_screen_store_path)
        memo_store = MemoStore(config.short_memo_store_path)

        if not store.pending_hits() and not memo_store.pending_vetting_memos():
            if not journal.has_event_today(events.KIND_SHORT_DRAIN_RUN):
                journal.record_event(
                    events.KIND_SHORT_DRAIN_RUN,
                    events.short_drain_run_payload(
                        asof=date.today().isoformat(),
                        screened_this_run=screened_this_run,
                        researched=0, failed=0, still_pending=0, hit_deadline=False,
                    ),
                )
            return

        vetted = confirmed = rejected = vet_failed = 0
        vet_ran = False
        vet_errored = False
        vet_hit_deadline = False
        researched = drain_failed = 0
        drain_hit_deadline = False
        drain_llms = None  # (evidence, thesis) — built once, on first chunk
        while not should_stop() and tick_now() < deadline:
            pending = store.pending_hits()
            if not memo_store.pending_vetting_memos() and not pending:
                break
            progress = 0
            if memo_store.pending_vetting_memos() and not vet_errored:
                summary = _short_vetting_stage(
                    journal, config, memo_store=memo_store, backend=backend,
                    deadline=deadline, should_stop=should_stop, now=tick_now,
                    adapter_factory=adapter_factory, reporter=reporter,
                )
                if summary is None:
                    vet_errored = True
                else:
                    vet_ran = True
                    vetted += summary.vetted
                    confirmed += summary.confirmed
                    rejected += summary.rejected
                    vet_failed += summary.failed
                    vet_hit_deadline = vet_hit_deadline or summary.hit_deadline
                    progress += summary.vetted + summary.failed
            if pending:
                if drain_llms is None:
                    from tradingagents.dataflows import edgar
                    edgar.get_user_agent()  # fail fast before spinning ds4

                    from ops.research.models import build_stage_llm

                    drain_llms = (
                        build_stage_llm(config.research_evidence_model),
                        build_stage_llm(config.research_thesis_model),
                    )
                from ops.research.drain import drain_pending
                from ops.research.short_brain import research_short_hit

                backend.ensure_up()
                summary = drain_pending(
                    store=store, memo_store=memo_store,
                    evidence_llm=drain_llms[0], thesis_llm=drain_llms[1],
                    thesis_model_spec=config.research_thesis_model,
                    max_names=config.research_drain_nightly_cap,
                    deadline=deadline, should_stop=should_stop, now=tick_now,
                    research_fn=research_short_hit, reporter=reporter,
                )
                researched += summary.researched
                drain_failed += summary.failed
                drain_hit_deadline = drain_hit_deadline or summary.hit_deadline
                progress += summary.researched + summary.failed
            if progress == 0:
                break  # nothing moved (deadline/stop/errors) — don't spin
        journal.record_event(
            events.KIND_SHORT_DRAIN_RUN,
            events.short_drain_run_payload(
                asof=date.today().isoformat(), screened_this_run=screened_this_run,
                researched=researched, failed=drain_failed,
                still_pending=len(store.pending_hits()),
                hit_deadline=drain_hit_deadline,
            ),
        )
        if vet_ran:
            journal.record_event(
                events.KIND_SHORT_VETTING_RUN,
                events.short_vetting_run_payload(
                    asof=date.today().isoformat(), vetted=vetted,
                    confirmed=confirmed, rejected=rejected, failed=vet_failed,
                    still_pending=len(memo_store.pending_vetting_memos()),
                    hit_deadline=vet_hit_deadline,
                ),
            )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        journal.record_event(
            events.KIND_SHORT_DRAIN_ERROR,
            events.short_drain_error_payload(
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


def _insider_memo_work_pending(config) -> bool:
    """True when insider-sleeve entries are awaiting their memo-lite pass."""
    from ops.insider.store import SignalStore

    return bool(SignalStore(config.insider_signal_store_path).entries_without_memo())


def _insider_memo_pass(
    journal: Journal, config, *, backend, deadline, should_stop, tick_now,
    reporter=None,
) -> None:
    """Overnight memo-lite authoring for insider entries — one cheap
    structured call per entry, LAST in the window (after research and short
    stages). Builds the LLM and touches ds4 only when the queue is
    non-empty; failures record insider_memo_error and never raise."""
    try:
        from ops.insider.store import SignalStore

        signal_store = SignalStore(config.insider_signal_store_path)
        if not signal_store.entries_without_memo():
            return
        if should_stop() or tick_now() >= deadline:
            return
        from ops.insider.memo_lite import author_pending_memos
        from ops.research.models import build_stage_llm
        from tradingagents.memos.store import MemoStore

        backend.ensure_up()
        author_pending_memos(
            signal_store=signal_store,
            memo_store=MemoStore(config.insider_memo_store_path),
            thesis_llm=build_stage_llm(config.research_thesis_model),
            thesis_model_spec=config.research_thesis_model,
            deadline=deadline, should_stop=should_stop, now=tick_now,
            reporter=reporter,
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, scheduler-safe
        journal.record_event(
            events.KIND_INSIDER_MEMO_ERROR,
            events.insider_memo_error_payload(
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


def _short_vetting_stage(
    journal: Journal, config, *, memo_store, backend, deadline, should_stop,
    now, adapter_factory=None, reporter=None,
):
    """One graph-vetting pass over the SHORT pending_vetting queue with the
    inverted confirm map (Sell -> high, Underweight -> medium). Same
    contract as _research_vetting_stage: returns the VettingSummary, or
    None when the queue was empty or the pass failed (recorded as
    short_vetting_error; memos stay pending for the next night)."""
    try:
        if not memo_store.pending_vetting_memos():
            return None
        from ops.research.models import build_stage_llm
        from ops.research.vetting import SHORT_CONFIRM_TIERS, vet_pending
        from tradingagents.default_config import DEFAULT_CONFIG

        if adapter_factory is None:
            from ops.pipeline_adapter import TradingAgentsPipelineAdapter

            adapter = TradingAgentsPipelineAdapter(
                backend=backend, reporter=reporter, activity_job="overnight",
                activity_stage="vetting",
            )
        else:
            adapter = adapter_factory(backend)
        return vet_pending(
            memo_store=memo_store, adapter=adapter,
            falsifier_llm=build_stage_llm(config.research_thesis_model),
            vetted_by_model=f"{DEFAULT_CONFIG['llm_provider']}:{DEFAULT_CONFIG['deep_think_llm']}",
            deadline=deadline, should_stop=should_stop, now=now,
            confirm_tiers=SHORT_CONFIRM_TIERS,
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        journal.record_event(
            events.KIND_SHORT_VETTING_ERROR,
            events.short_vetting_error_payload(
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


def _research_vetting_stage(
    journal: Journal, config, *, memo_store, backend, deadline, should_stop,
    now, adapter_factory=None, reporter=None,
):
    """One graph-vetting pass over the pending_vetting queue.

    Returns the VettingSummary, or None when the queue was empty or the
    pass failed. Scheduler-safe and drain-independent: any failure records
    research_vetting_error and returns None — the caller disables further
    vet passes that night, memos stay pending_vetting for the next night,
    and the tick's finally still tears ds4 down. The aggregated
    research_vetting_run event is the tick's job, not this stage's. The
    graph adapter shares the tick's managed backend; its lazy ensure_up
    means a vet-only night spins ds4 only when a memo actually runs.
    """
    try:
        if not memo_store.pending_vetting_memos():
            return None
        from ops.research.models import build_stage_llm
        from ops.research.vetting import vet_pending
        from tradingagents.default_config import DEFAULT_CONFIG

        if adapter_factory is None:
            from ops.pipeline_adapter import TradingAgentsPipelineAdapter

            adapter = TradingAgentsPipelineAdapter(
                backend=backend, reporter=reporter, activity_job="overnight",
                activity_stage="vetting",
            )
        else:
            adapter = adapter_factory(backend)
        return vet_pending(
            memo_store=memo_store, adapter=adapter,
            falsifier_llm=build_stage_llm(config.research_thesis_model),
            vetted_by_model=f"{DEFAULT_CONFIG['llm_provider']}:{DEFAULT_CONFIG['deep_think_llm']}",
            deadline=deadline, should_stop=should_stop, now=now,
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
        CronTrigger(minute=times.TICK_CRON_MINUTE, hour=times.TICK_CRON_HOUR,
                    day_of_week=times.TICK_CRON_DOW),
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
            lambda: _short_trade_tick(journal, config),
            CronTrigger(hour=16, minute=27, day_of_week="mon-fri"),
            id="short_trade", max_instances=1, misfire_grace_time=300,
        )
        sched.add_job(
            lambda: _insider_trade_tick(journal, config),
            CronTrigger(hour=16, minute=29, day_of_week="mon-fri"),
            id="insider_trade", max_instances=1, misfire_grace_time=300,
        )
        # 00:15 daily (weekends included: Friday's index lands Saturday
        # 00:15; a holiday's missing index is an empty day, not an error).
        sched.add_job(
            lambda: _insider_scan_tick(journal, config),
            CronTrigger(hour=0, minute=15),
            id="insider_scan", max_instances=1, misfire_grace_time=600,
        )
        # Half-hourly, not once-nightly: fires while paused (`ops research
        # pause`) or outside the overnight window return instantly, and a
        # fire during a still-running window is skipped by max_instances=1
        # — the frequent trigger exists so `ops research resume` picks the
        # queues back up within 30 minutes.
        sched.add_job(
            lambda: _research_overnight_tick(journal, config),
            CronTrigger(minute=times.OVERNIGHT_CRON_MINUTE),
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
    sched.add_job(
        _gc_reap_tick,
        IntervalTrigger(minutes=60),
        id="gc_reap", max_instances=1, misfire_grace_time=300,
    )
    if heartbeat_job is not None:
        sched.add_job(
            heartbeat_job,
            IntervalTrigger(seconds=60),
            id="heartbeat", max_instances=1, misfire_grace_time=15,
        )
    sched.start()
    return sched


def _gc_reap_tick() -> None:
    """Hourly full GC pass: reap per-thread library state stranded by dead
    worker threads.

    LangChain's ToolNode runs each analysis's parallel tool batches on
    short-lived executor threads, and yfinance keeps per-thread state in
    thread-locals — a peewee sqlite connection to its tz cache (two fds)
    plus curl_cffi session connections (CLOSE_WAIT sockets once Yahoo
    idle-closes them). When those threads die the state is unreachable but
    holds its fds until a generation-2 collection, and a long-lived, mostly
    idle daemon reaches gen-2 too rarely on its own: observed live as
    errno-24 fd exhaustion after ~6h of market-hours analyses
    (2026-07-09/10, ~+45 fds per analyzed name). A full collect in an idle
    process costs milliseconds; the raised plist NumberOfFiles limit stays
    as defense-in-depth."""
    import gc

    gc.collect()


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
