"""Command-line entry points for the ops layer.

`decide-once` runs a single end-to-end pass: universe → pipeline → orders →
fills → stop check. Designed for ad-hoc invocation and as the basic
building block for Plan 3's orchestrator."""
from __future__ import annotations

from datetime import date as date_cls, datetime
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
from ops.universe import Candidate, build_universe
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
    output = Path(os.path.abspath(os.path.expanduser(output_path)))
    output.parent.mkdir(parents=True, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)
    output.write_text(rendered)
    click.echo(f"Wrote {output}")
    click.echo(f"Logs will go to {log_dir}/")
    click.echo("To load the service (not done automatically), run:")
    click.echo(f"  launchctl bootstrap gui/$(id -u) {output}")
    click.echo("To unload it later:")
    click.echo(f"  launchctl bootout gui/$(id -u) {output}")
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

    # Universe
    candidates = build_universe(asof_date=asof_date, config=cfg)
    click.echo(f"## Universe ({len(candidates)})")
    if not candidates:
        click.echo("(no candidates — nothing to do today)")
    for c in candidates:
        click.echo(f"  - {c.symbol}: price=${c.last_price} "
                   f"earnings beat (EPS {c.earnings.eps_actual}/"
                   f"{c.earnings.eps_estimate})")
    click.echo("")

    # Quote source — uses yfinance with 60s TTL
    quote_source = make_yfinance_quote_source()

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

    # Broker
    guarded = build_guarded_paper_broker(
        config=cfg, journal=journal,
        quote_source=quote_source,
        starting_cash=cash,
        start_of_day_equity=lambda: cash,    # naive — Plan 3 reads from journal
        start_of_week_equity=lambda: cash,
    )

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
