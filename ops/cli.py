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
from ops.universe import build_universe


@click.group()
def cli() -> None:
    """ops — operational live-trading layer."""


@cli.command("decide-once")
@click.option("--date", "as_of", required=True,
              type=click.DateTime(formats=["%Y-%m-%d"]),
              help="Date to run for, YYYY-MM-DD")
@click.option("--journal", "journal_path", default="ops_journal.sqlite",
              type=click.Path(dir_okay=False), help="SQLite journal path")
@click.option("--starting-cash", default="250",
              help="Paper-broker starting cash (Decimal string)")
@click.option("--stub-pipeline", is_flag=True,
              help="Use a stub pipeline (no LLM calls) — defaults to HOLD")
@click.option("--stub-pipeline-buy", multiple=True,
              help="Symbol(s) the stub pipeline should label BUY. Implies --stub-pipeline.")
def decide_once(
    as_of: datetime,
    journal_path: str,
    starting_cash: str,
    stub_pipeline: bool,
    stub_pipeline_buy: tuple[str, ...],
) -> None:
    """Run a single decision/fill/stop-check pass."""
    asof_date = as_of.date()
    cfg = load_config()
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

    if not candidates:
        click.echo("0 candidates → 0 BUY orders. Guardian: skipped.")
        return

    # Pipeline
    if stub_pipeline or stub_pipeline_buy:
        decisions = {s: PipelineDecision.BUY for s in stub_pipeline_buy}
        pipeline = StubPipelineAdapter(decisions)
    else:
        pipeline = TradingAgentsPipelineAdapter()

    # Quote source — uses yfinance with 60s TTL
    quote_source = make_yfinance_quote_source()

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
                   f"stop ${p.order.stop_loss_price}")
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
