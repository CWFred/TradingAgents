"""Command-line entry points for the ops layer.

`decide-once` runs a single end-to-end pass: universe → pipeline → orders →
fills → stop check. Designed for ad-hoc invocation and as the basic
building block for Plan 3's orchestrator."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import click

from ops import build_guarded_paper_broker
from ops.broker.base import OrderRejected
from ops.config import load_config
from ops.journal import Journal
from ops.pipeline_adapter import (
    PipelineDecision,
    StubPipelineAdapter,
    TradingAgentsPipelineAdapter,
)
from ops.position_guardian import PositionGuardian
from ops.quotes import make_yfinance_quote_source
from ops.strategy.post_earnings_momentum import PostEarningsMomentumStrategy
from ops.universe import Candidate, CandidateSource
from ops.universe.composite import build_composite_universe
from ops.universe.earnings import EarningsHit


@click.group()
def cli() -> None:
    """ops — operational live-trading layer."""


@cli.command()
def run():
    """Start the always-on orchestrator + guardian service."""
    import sys
    from ops.main import run as _run
    sys.exit(_run())


def _install_plist(rendered: str, output_path: str, log_dir: str) -> None:
    """Write a rendered plist and print the (never-run) launchctl commands."""
    import os
    from pathlib import Path

    output = Path(os.path.abspath(os.path.expanduser(output_path)))
    output.parent.mkdir(parents=True, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)
    output.write_text(rendered)
    click.echo(f"Wrote {output}")
    click.echo(f"Logs will go to {log_dir}/")
    click.echo("To load it (not done automatically), run:")
    click.echo(f"  launchctl bootstrap gui/$(id -u) {output}")
    click.echo("To unload it later:")
    click.echo(f"  launchctl bootout gui/$(id -u) {output}")


@cli.command("install-service")
@click.option("--output", "output_path",
              default="~/Library/LaunchAgents/com.tradingagents.ops.plist",
              show_default=True, type=click.Path(dir_okay=False),
              help="Where to write the rendered launchd plist")
@click.option("--log-dir", "log_dir",
              default="~/.local/state/tradingagents/logs", show_default=True,
              help="Directory for the service's stdout/stderr logs")
def install_service(output_path: str, log_dir: str) -> None:
    """Render the launchd agent plist and print the load command.

    Writes the file only — never invokes launchctl. Loading the agent
    stays an explicit user action, so a supervisor is never installed as
    a side effect of running a command."""
    import os
    import sys
    from pathlib import Path

    from ops.deploy import render_launchd_plist

    repo_root = str(Path(__file__).resolve().parents[1])
    log_dir = os.path.abspath(os.path.expanduser(log_dir))
    rendered = render_launchd_plist(
        repo_root=repo_root,
        venv_python=sys.executable,
        log_dir=log_dir,
    )
    _install_plist(rendered, output_path, log_dir)

    from ops.deploy import render_dashboard_plist

    dash_output = str(Path(os.path.abspath(os.path.expanduser(output_path)))
                      .with_name("com.tradingagents.dashboard.plist"))
    rendered_dash = render_dashboard_plist(
        repo_root=repo_root, venv_python=sys.executable, log_dir=log_dir)
    _install_plist(rendered_dash, dash_output, log_dir)
    click.echo(
        "NOTE: launchd cannot start jobs on a sleeping laptop. Consider a "
        "wake schedule (your call to apply):\n"
        "  sudo pmset repeat wakeorpoweron MTWRF 09:20:00"
    )


@cli.command("notify-once")
@click.option("--journal", "journal_path", default=None,
              type=click.Path(dir_okay=False),
              help="SQLite journal path (default: the configured ops journal path)")
def notify_once(journal_path: str | None) -> None:
    """Dispatch any pending journal events to notification transports once."""
    from ops.main import _build_dispatcher

    journal_path = journal_path or load_config().journal_path
    journal = Journal(journal_path)
    try:
        n = _build_dispatcher(journal).dispatch_once()
        click.echo(f"dispatched {n} message(s)")
    finally:
        journal.close()


@cli.command("status")
@click.option("--journal", "journal_path", default=None,
              type=click.Path(dir_okay=False),
              help="SQLite journal path (default: the configured ops journal path)")
def status(journal_path: str | None) -> None:
    """Print a journal-only snapshot of the trading system.

    Reads ONLY the journal (WAL concurrent reads) — no broker, no MCP,
    no quotes — so it is always safe to run beside the live service and
    works when the broker is unreachable. Positions/cash are the journal
    replay ("journal view"); reconciliation is what compares that to
    live truth."""
    from ops.status import build_status, format_status

    journal_path = journal_path or load_config().journal_path
    journal = Journal(journal_path)
    try:
        click.echo(format_status(build_status(journal, load_config())))
    finally:
        journal.close()


@cli.command("dashboard")
@click.option("--port", default=None, type=int,
              help="Port on 127.0.0.1 (default: $OPS_DASHBOARD_PORT or 8321)")
def dashboard(port: int | None) -> None:
    """Serve the read-only local ops dashboard (127.0.0.1 only).

    Observes the journals, memo store, and screen store via mode=ro
    reads — no broker, no writes, no network. Runs in the foreground;
    launchd owns backgrounding (see install-service)."""
    import sys

    from ops.dashboard.server import serve

    sys.exit(serve(port=port))


@cli.command()
@click.option("--asof", "asof_dt", default=None,
              type=click.DateTime(formats=["%Y-%m-%d"]),
              help="Screen as of this date (YYYY-MM-DD); default today. Debug knob, "
                   "not a backtest — see docs/research_screener.md.")
@click.option("--dry-run", is_flag=True,
              help="Screen and print only — no store writes, no baseline trades.")
@click.option("--limit", default=None, type=int,
              help="Screen only the first N universe names (smoke runs).")
@click.option("--notify", "do_notify", is_flag=True,
              help="Send a Pushover summary (or a high-urgency alert on a blind sweep).")
def screen(asof_dt: datetime | None, dry_run: bool, limit: int | None, do_notify: bool = False) -> None:
    """Run the small/mid-cap fundamental screen + null-baseline portfolio."""
    from ops.research.run import run_screen

    config = load_config()
    asof_date = asof_dt.date() if asof_dt else datetime.now().date()
    if asof_date < datetime.now().date() and not dry_run:
        click.echo(
            "warning: backdated --asof screens point-in-time fundamentals but "
            "uses TODAY's universe membership and TODAY's baseline fill prices "
            "— debug knob, not a backtest."
        )
    try:
        summary = run_screen(config=config, asof=asof_date, dry_run=dry_run,
                             limit=limit)
    except Exception as exc:
        # The unattended Saturday job's only signal is this push — a
        # whole-run crash (Nasdaq 403, Edgar misconfig) must not be silent.
        if do_notify:
            from ops.notify.config import load_notify_config
            from ops.notify.push import build_push_transport
            from ops.notify.transport import NotifyMessage

            build_push_transport(load_notify_config()).send(NotifyMessage(
                title="screen FAILED",
                body=f"{type(exc).__name__}: {exc}",
                urgency="high",
            ))
        raise
    click.echo(f"screen run {summary.run_id or '(dry-run)'} asof {summary.asof}")
    click.echo(
        f"universe {summary.universe_size}, screened {summary.screened}, "
        f"passed {len(summary.passed)}, errors {len(summary.errors)}"
    )
    for symbol in summary.passed:
        click.echo(f"  PASS {symbol}")
    if summary.baseline is not None:
        click.echo(
            f"baseline: {len(summary.baseline['buys'])} buys, "
            f"{len(summary.baseline['exits'])} exits, "
            f"{len(summary.baseline['skipped'])} skipped, "
            f"{len(summary.baseline.get('writeoffs', []))} written off"
        )

    for bar_name, counts in sorted(summary.coverage.items()):
        total = counts["computed"] + counts["missing"]
        pct = (100 * counts["computed"] // total) if total else 0
        click.echo(f"  coverage {bar_name}: {counts['computed']}/{total} ({pct}%)")

    # Blind = an empty universe (the 2026-07-06 incident mode: every fetch
    # failed or the cache is poisoned) OR a majority of names erroring.
    blind = (summary.universe_size == 0
             or len(summary.errors) * 2 > summary.universe_size)
    if do_notify:
        from ops.notify.config import load_notify_config
        from ops.notify.push import build_push_transport
        from ops.notify.transport import NotifyMessage

        transport = build_push_transport(load_notify_config())
        if blind:
            if summary.universe_size == 0:
                body = "universe came back EMPTY (fetch failures?); results unusable"
            else:
                body = (f"{len(summary.errors)}/{summary.universe_size} names "
                        "errored; results unusable")
            transport.send(NotifyMessage(
                title="screen BLIND", body=body, urgency="high",
            ))
        else:
            n_writeoffs = (
                len(summary.baseline.get("writeoffs", []))
                if summary.baseline is not None else 0
            )
            body = (f"asof {summary.asof}: {len(summary.passed)} passed / "
                    f"{summary.screened} screened / {len(summary.errors)} errors")
            if n_writeoffs > 0:
                body += f", {n_writeoffs} written off"
            transport.send(NotifyMessage(
                title="screen complete", body=body, urgency="normal",
            ))
    if blind:
        raise SystemExit(2)


@cli.command("digest")
@click.option("--date", "digest_date", default=None,
              type=click.DateTime(formats=["%Y-%m-%d"]),
              help="Overview for this date (YYYY-MM-DD); default today.")
@click.option("--output", "output_path", default=None,
              type=click.Path(dir_okay=False),
              help="Write the markdown to this file instead of stdout.")
@click.option("--push/--no-push", "do_push", default=False,
              help="Also push the headline via Pushover (off by default).")
def digest(digest_date: datetime | None, output_path: str | None, do_push: bool) -> None:
    """Cross-sleeve daily overview: momentum + research + baseline + anomalies.

    Manual/debug companion to the daemon's weekday-16:35/Saturday-18:00
    `daily_overview` job — reads all three journals + the memo store, same as
    the daemon, but deliberately does NOT record the gate event, so running
    this never suppresses the daemon's own scheduled run."""
    from ops.notify.overview import (
        build_daily_overview,
        format_daily_overview,
        overview_headline,
    )
    from ops.trading_time import TRADING_TZ
    from tradingagents.memos.store import MemoStore

    config = load_config()
    # Attach the ET trading-tz directly to the parsed midnight (rather than
    # reinterpreting through UTC) so trading_day_start resolves exactly the
    # requested calendar date, DST-safe.
    now = digest_date.replace(tzinfo=TRADING_TZ) if digest_date is not None else None
    memo_store = MemoStore(config.memo_store_path)
    with (
        Journal(config.journal_path) as main_journal,
        Journal(config.baseline_journal_path) as baseline_journal,
        Journal(config.research_journal_path) as research_journal,
    ):
        report = build_daily_overview(
            main_journal=main_journal, baseline_journal=baseline_journal,
            research_journal=research_journal, memo_store=memo_store,
            config=config, now=now,
        )
    rendered = format_daily_overview(report)
    if output_path is not None:
        with open(output_path, "w") as f:
            f.write(rendered + "\n")
    else:
        click.echo(rendered)
    if do_push:
        from ops.notify.config import load_notify_config
        from ops.notify.push import build_push_transport
        from ops.notify.transport import NotifyMessage

        build_push_transport(load_notify_config()).send(NotifyMessage(
            title="Daily overview", body=overview_headline(report),
        ))


@cli.group()
def research() -> None:
    """Long-horizon research sleeve commands."""


@research.command("write-off")
@click.argument("symbol")
@click.option("--price", required=True,
              help="Settlement price per share (deal price or last trade).")
@click.option("--note", default=None, help="Why (e.g. 'acquired 2026-08-01 at $12.50').")
def research_write_off(symbol: str, price: str, note: str | None) -> None:
    """Resolve a delisted baseline position at a known price."""
    from ops.research.baseline import write_off_position

    config = load_config()
    journal = Journal(config.baseline_journal_path)
    try:
        result = write_off_position(
            journal=journal, symbol=symbol, price=Decimal(price),
            starting_cash=config.baseline_starting_cash, note=note,
        )
    finally:
        journal.close()
    click.echo(
        f"wrote off {result['quantity']} {result['symbol']} at {result['price']} "
        f"(proceeds {result['proceeds']})"
    )


@research.command("run")
@click.option("--max-names", default=3, show_default=True, type=int,
              help="How many pending hits to research this batch (oldest first).")
@click.option("--notify", "do_notify", is_flag=True,
              help="Send a Pushover summary (or a high-urgency alert on a batch abort).")
def research_run(max_names: int, do_notify: bool = False) -> None:
    """Deep-research pending screen hits into structured memos (local models)."""
    from ops.llm_backend import build_managed_backend, load_managed_backend_config
    from ops.research.drain import drain_pending
    from ops.research.models import build_stage_llm
    from ops.research.store import ScreenStore
    from tradingagents.memos.store import MemoStore

    config = load_config()
    store = ScreenStore(config.screen_store_path)
    hits = store.pending_hits()[:max_names]
    if not hits:
        # A quiet week must not push: no pending hits is a no-op, not a
        # failure, so notify is skipped entirely here.
        click.echo("no pending hits")
        return

    # Fail fast: EdgarNotConfiguredError is a ValueError subtype, not a
    # ResearchError, so research_hit's per-hit `except Exception` below
    # would swallow it and mark_failed() every hit in the batch — burning
    # real pending screen hits on a pure configuration error. Mirrors the
    # same guard in ops/research/run.py's run_screen().
    from tradingagents.dataflows import edgar

    researched = failed = 0
    try:
        edgar.get_user_agent()

        memo_store = MemoStore(config.memo_store_path)
        evidence_llm = build_stage_llm(config.research_evidence_model)
        thesis_llm = build_stage_llm(config.research_thesis_model)
        backend = build_managed_backend(load_managed_backend_config())
        try:
            backend.ensure_up()
            summary = drain_pending(
                store=store, memo_store=memo_store,
                evidence_llm=evidence_llm, thesis_llm=thesis_llm,
                thesis_model_spec=config.research_thesis_model,
                max_names=max_names, echo=click.echo,
            )
            researched, failed = summary.researched, summary.failed
        finally:
            backend.shutdown()
    except edgar.EdgarNotConfiguredError as exc:
        # Aborts before the loop even starts — the unattended Saturday job's
        # only signal is this push, so it must fire here too, not just for
        # exceptions raised mid-batch below.
        click.echo(f"research run: aborted — {exc}", err=True)
        if do_notify:
            from ops.notify.config import load_notify_config
            from ops.notify.push import build_push_transport
            from ops.notify.transport import NotifyMessage

            build_push_transport(load_notify_config()).send(NotifyMessage(
                title="research run FAILED",
                body=f"{type(exc).__name__}: {exc}",
                urgency="high",
            ))
        raise SystemExit(1) from exc
    except Exception as exc:
        # Any other batch-aborting exception (ResearchError propagated from
        # the loop, backend.ensure_up() failure, ...) — same signal.
        if do_notify:
            from ops.notify.config import load_notify_config
            from ops.notify.push import build_push_transport
            from ops.notify.transport import NotifyMessage

            build_push_transport(load_notify_config()).send(NotifyMessage(
                title="research run FAILED",
                body=f"{type(exc).__name__}: {exc}",
                urgency="high",
            ))
        raise
    click.echo(f"research run: {researched} researched, {failed} failed, "
               f"{len(store.pending_hits())} still pending")
    if do_notify:
        from ops.notify.config import load_notify_config
        from ops.notify.push import build_push_transport
        from ops.notify.transport import NotifyMessage

        build_push_transport(load_notify_config()).send(NotifyMessage(
            title="research run complete",
            body=(f"{researched} researched, {failed} failed, "
                  f"{len(store.pending_hits())} still pending"),
            urgency="normal",
        ))
    if failed == len(hits):
        raise SystemExit(1)


@research.command("pause")
def research_pause() -> None:
    """Pause the overnight research window (screen/drain/vet) and free ds4.

    Drops the pause flag the daemon checks between names: the in-flight
    name finishes (up to ~30 min), then the window stops and ds4 shuts
    down. Momentum trading is unaffected. Survives daemon restarts; undo
    with `ops research resume`.
    """
    from pathlib import Path

    config = load_config()
    flag = Path(config.research_pause_flag_path)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    click.echo(
        f"research paused (flag: {flag}). The in-flight name finishes, "
        "then ds4 frees — up to ~30 minutes."
    )


@research.command("resume")
def research_resume() -> None:
    """Resume the overnight research window after `ops research pause`.

    Removes the pause flag; the daemon's half-hourly overnight job picks
    the queues back up within 30 minutes (weekends run until Monday
    morning's deadline, weekdays until the pre-market deadline).
    """
    from pathlib import Path

    config = load_config()
    flag = Path(config.research_pause_flag_path)
    if flag.exists():
        flag.unlink()
        click.echo("research resumed; the daemon picks work up within 30 minutes.")
    else:
        click.echo("research was not paused; nothing to do.")


@research.command("kick")
def research_kick() -> None:
    """One-shot demo: screen now (ignore the 3-day gate), drain the whole
    pending queue, then run the research trade step — so paper positions
    appear in a single manual run. Independent of the nightly schedule."""
    from datetime import date

    from ops.llm_backend import build_managed_backend, load_managed_backend_config
    from ops.quotes import make_yfinance_quote_source
    from ops.research.drain import drain_pending
    from ops.research.models import build_stage_llm
    from ops.research.run import run_screen
    from ops.research.store import ScreenStore
    from ops.research.trading import trade_research_sleeve
    from tradingagents.dataflows import edgar
    from tradingagents.memos.store import MemoStore

    config = load_config()
    edgar.get_user_agent()  # fail fast on missing SEC user agent

    click.echo("kick: screening...")
    run_screen(config=config, asof=date.today())

    store = ScreenStore(config.screen_store_path)
    memo_store = MemoStore(config.memo_store_path)
    evidence_llm = build_stage_llm(config.research_evidence_model)
    thesis_llm = build_stage_llm(config.research_thesis_model)
    backend = build_managed_backend(load_managed_backend_config())
    try:
        backend.ensure_up()
        summary = drain_pending(
            store=store, memo_store=memo_store,
            evidence_llm=evidence_llm, thesis_llm=thesis_llm,
            thesis_model_spec=config.research_thesis_model, echo=click.echo,
        )
    finally:
        backend.shutdown()
    click.echo(f"kick: drained {summary.researched} researched, "
               f"{summary.failed} failed")

    with Journal(config.research_journal_path) as research_journal, \
            Journal(config.journal_path) as main_journal:
        trade_research_sleeve(
            memo_store=memo_store, research_journal=research_journal,
            main_journal=main_journal,
            quote_source=make_yfinance_quote_source(),
            starting_cash=config.research_starting_cash, asof=date.today(),
        )
    click.echo("kick: done")


@research.command("monitor")
def research_monitor() -> None:
    """Run the daily memo monitor once (falsifiers, drawdown, resolution due)."""
    from ops.journal import Journal
    from ops.research.monitor import monitor_memos
    from ops.research.store import ScreenStore
    from tradingagents.memos.store import MemoStore

    config = load_config()
    with Journal(config.journal_path) as journal:
        from ops import events as ops_events

        if journal.has_event_today(ops_events.KIND_RESEARCH_MONITOR_RUN):
            click.echo("note: a monitor run was already recorded today; running again")
        outcome = monitor_memos(
            memo_store=MemoStore(config.memo_store_path),
            screen_store=ScreenStore(config.screen_store_path),
            journal=journal,
        )
    click.echo(
        f"monitor {outcome.asof}: {outcome.memos_checked} memos, "
        f"{outcome.falsifiers_evaluated} falsifiers ({outcome.tripped} tripped, "
        f"{outcome.unevaluable} unevaluable), {outcome.escalations} escalations, "
        f"{outcome.resolution_due} due for resolution, {outcome.catalyst_due} catalysts due"
    )
    for err in outcome.errors:
        click.echo(f"  error: {err}")


@research.command("trade")
def research_trade() -> None:
    """Run the daily research-sleeve trade step once (mechanical entries/exits)."""
    from datetime import date

    from ops.journal import Journal
    from ops.quotes import make_yfinance_quote_source
    from ops.research.trading import trade_research_sleeve
    from tradingagents.memos.store import MemoStore

    config = load_config()
    with Journal(config.journal_path) as journal:
        from ops import events as ops_events

        if journal.has_event_today(ops_events.KIND_RESEARCH_TRADE_RUN):
            click.echo("note: a trade run was already recorded today; running again")
        with Journal(config.research_journal_path) as research_journal:
            outcome = trade_research_sleeve(
                memo_store=MemoStore(config.memo_store_path),
                research_journal=research_journal,
                main_journal=journal,
                quote_source=make_yfinance_quote_source(),
                starting_cash=config.research_starting_cash,
                asof=date.today(),
            )
    click.echo(
        f"trade {outcome.asof}: entered {outcome.entered}, exited {outcome.exited}, "
        f"{len(outcome.skipped)} skipped"
    )
    for s in outcome.skipped:
        click.echo(f"  skipped: {s}")
    for err in outcome.errors:
        click.echo(f"  error: {err}")


@research.command("resolve")
@click.argument("memo_id")
@click.option("--label", "outcome_label", required=True,
              type=click.Choice([
                  "thesis_right_made_money", "thesis_right_lost_money",
                  "thesis_wrong_made_money", "thesis_wrong_lost_money",
              ]),
              help="Right/wrong process crossed with made/lost money — human judgment call.")
@click.option("--narrative", required=True,
              help="What actually happened, and whether the reasoning was sound.")
@click.option("--exit-price", "exit_price", default=None, type=float,
              help="Explicit exit price; overrides the sell-fill/current-close ladder.")
def research_resolve(memo_id: str, outcome_label: str, narrative: str,
                      exit_price: float | None) -> None:
    """Resolve a memo: the arithmetic is computed, the human supplies only
    the outcome label and the narrative."""
    from ops.journal import Journal
    from ops.research.resolution import ResolutionError, compute_resolution_numbers
    from tradingagents.memos.schema import Resolution
    from tradingagents.memos.store import MemoStore

    config = load_config()
    memo_store = MemoStore(config.memo_store_path)
    memo = memo_store.get(memo_id)
    if memo is None:
        raise click.ClickException(f"no memo with id {memo_id!r}")
    if memo.status == "resolved":
        raise click.ClickException(f"memo {memo_id!r} is already resolved")

    with Journal(config.research_journal_path) as research_journal:
        try:
            numbers = compute_resolution_numbers(
                memo, research_journal=research_journal, exit_price=exit_price,
            )
        except ResolutionError as exc:
            raise click.ClickException(str(exc)) from exc

    resolution = Resolution(
        **numbers, outcome_label=outcome_label,
        falsifiers_tripped=[], catalysts_realized=[], narrative=narrative,
    )
    try:
        resolved = memo_store.resolve(memo_id, resolution)
    except (KeyError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"resolved {resolved.ticker} ({memo_id}): {outcome_label} — "
        f"exit {numbers['exit_price']}, realized {numbers['realized_return_pct']:+.1%} "
        f"vs benchmark {numbers['benchmark_return_pct']:+.1%} "
        f"over {numbers['holding_days']}d"
    )


@research.command("report")
@click.option("--output", "output_path", default=None,
              type=click.Path(dir_okay=False),
              help="Write the markdown report to this file instead of stdout.")
def research_report(output_path: str | None) -> None:
    """Quarterly calibration report: corpus stats, outcome 2x2, scenario
    calibration, bought-vs-passed, sleeve-vs-baseline, per-model attribution.

    Reads ONLY the memo store and the research/baseline journals — no
    broker, no quotes — so it is safe to run any time (day one included)."""
    from ops.research.report import build_report, format_report
    from tradingagents.memos.store import MemoStore

    config = load_config()
    memo_store = MemoStore(config.memo_store_path)
    with (
        Journal(config.research_journal_path) as research_journal,
        Journal(config.baseline_journal_path) as baseline_journal,
    ):
        report = build_report(
            memo_store=memo_store, research_journal=research_journal,
            baseline_journal=baseline_journal,
        )
    rendered = format_report(report)
    if output_path is not None:
        with open(output_path, "w") as f:
            f.write(rendered + "\n")
    else:
        click.echo(rendered)


@cli.command("decide-once")
@click.option("--date", "as_of", required=True,
              type=click.DateTime(formats=["%Y-%m-%d"]),
              help="Date to run for, YYYY-MM-DD")
@click.option("--journal", "journal_path", default=None,
              type=click.Path(dir_okay=False),
              help="SQLite journal path (default: the configured ops journal path)")
@click.option("--starting-cash", default="250",
              help="Paper-broker starting cash (Decimal string)")
@click.option("--stub-pipeline", is_flag=True,
              help="Use a stub pipeline (no LLM calls) — defaults to HOLD")
@click.option("--stub-pipeline-buy", multiple=True,
              help="Symbol(s) the stub pipeline should label BUY. Implies --stub-pipeline.")
@click.option("--force-candidate", "force_candidates", multiple=True,
              help="Smoke-testing only: inject SYMBOL into the candidate list at its "
                   "real quote, bypassing the earnings/liquidity universe filters. "
                   "Guardrails still apply — a deny-listed symbol is REJECTED at the "
                   "broker boundary, which is the point of forcing it.")
def decide_once(
    as_of: datetime,
    journal_path: str | None,
    starting_cash: str,
    stub_pipeline: bool,
    stub_pipeline_buy: tuple[str, ...],
    force_candidates: tuple[str, ...],
) -> None:
    """Run a single decision/fill/stop-check pass."""
    asof_date = as_of.date()
    cfg = load_config()
    journal_path = journal_path or cfg.journal_path
    journal = Journal(journal_path)
    cash = Decimal(starting_cash)

    click.echo(f"# decide-once — {asof_date.isoformat()}")
    click.echo(f"Cash: ${cash}    Config: {cfg.broker_mode} broker, "
               f"per-position cap {cfg.per_position_cap_pct}, "
               f"stop {cfg.per_position_stop_pct}")
    click.echo("")

    # Quote source — uses yfinance with 60s TTL
    quote_source = make_yfinance_quote_source()

    # Broker (needed before composite universe for held positions)
    guarded = build_guarded_paper_broker(
        config=cfg, journal=journal,
        quote_source=quote_source,
        starting_cash=cash,
        start_of_day_equity=lambda: cash,    # naive — Plan 3 reads from journal
        start_of_week_equity=lambda: cash,
    )

    # Universe — composite (both earnings and momentum sleeves)
    held = {p.symbol for p in guarded.get_positions()}
    candidates = build_composite_universe(
        asof_date=asof_date, config=cfg,
        held_symbols=frozenset(held),
        free_slots=max(0, cfg.max_open_positions - len(held)),
    )
    click.echo(f"## Universe ({len(candidates)})")
    if not candidates:
        click.echo("(no candidates — nothing to do today)")
    for c in candidates:
        if c.earnings is not None:
            why = (f"earnings beat (EPS {c.earnings.eps_actual}/"
                   f"{c.earnings.eps_estimate})")
        else:
            why = (f"momentum leader (rank {c.momentum.rank}, "
                   f"6m {c.momentum.trailing_return_6m:+.0%})")
        click.echo(f"  - {c.symbol}: price=${c.last_price} {why}")
    click.echo("")

    # Forced candidates (smoke testing): bypass the universe filters, never
    # the guardrails — a deny-listed forced symbol must be REJECTED below.
    present = {c.symbol for c in candidates}
    for sym in force_candidates:
        sym = sym.upper()
        if sym in present:
            continue
        price = quote_source(sym)
        candidates.append(Candidate(
            symbol=sym,
            source=CandidateSource.EARNINGS,
            earnings=EarningsHit(
                symbol=sym, report_date=asof_date,
                eps_actual=Decimal("0"), eps_estimate=Decimal("0"),
                revenue_actual=None, revenue_estimate=None,
                eps_beat=False, revenue_beat=None,
            ),
            last_price=price,
            avg_dollar_volume_20d=Decimal("0"),
        ))
        present.add(sym)
        click.echo(f"## Forced candidate (smoke): {sym} @ ${price}")

    if not candidates:
        click.echo("0 candidates → 0 BUY orders. Guardian: skipped.")
        return

    # Pipeline
    if stub_pipeline or stub_pipeline_buy:
        decisions = {s: PipelineDecision.BUY for s in stub_pipeline_buy}
        pipeline = StubPipelineAdapter(decisions)
    else:
        pipeline = TradingAgentsPipelineAdapter()

    # Strategy
    strategy = PostEarningsMomentumStrategy(config=cfg)
    proposals = strategy.propose_orders(
        candidates=candidates, pipeline=pipeline,
        current_equity=guarded.get_equity(),
        asof_date=asof_date,
    )

    click.echo(f"## Pipeline decisions")
    if not proposals:
        click.echo("0 BUY proposals (all HOLD/SELL or below trade floor)")
    for p in proposals:
        click.echo(f"  - {p.order.symbol}: {p.pipeline.decision.value} "
                   f"→ ${p.order.notional_dollars} @ ~${p.candidate.last_price}, "
                   f"stop_pct {p.order.stop_pct}")
    click.echo("")

    # Place orders
    click.echo(f"## Orders")
    for p in proposals:
        try:
            fill = guarded.place_order(p.order)
            click.echo(f"  - {p.order.symbol}: FILLED qty={fill.quantity} @ ${fill.price}")
        except OrderRejected as exc:
            click.echo(f"  - {p.order.symbol}: REJECTED [{exc.rule_name}] {exc.reason}")
    click.echo("")

    # Guardian
    click.echo("## Guardian (one stop-check pass)")
    guardian = PositionGuardian(
        broker=guarded, quote_source=quote_source, config=cfg,
    )
    for action in guardian.check_stops_once():
        verb = "SOLD" if action.sold else "held"
        click.echo(f"  - {action.symbol}: {verb} (current ${action.current}, "
                   f"unrealized {action.pct})")
    click.echo("")

    # Summary
    positions = guarded.get_positions()
    equity = guarded.get_equity()
    click.echo(f"End-of-pass equity: ${equity}")
    click.echo(f"Open positions: {len(positions)}")
    for pos in positions:
        click.echo(f"  - {pos.symbol}: qty={pos.quantity} entry=${pos.avg_entry_price} "
                   f"stop=${pos.stop_loss_price}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
