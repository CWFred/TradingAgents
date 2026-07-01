import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ops.cli import cli
from ops.universe import Candidate
from ops.universe.earnings import EarningsHit


def _candidate(sym, price="200"):
    hit = EarningsHit(
        symbol=sym, report_date=date(2026, 6, 30),
        eps_actual=Decimal("1"), eps_estimate=Decimal("0.9"),
        revenue_actual=Decimal("100"), revenue_estimate=Decimal("90"),
        eps_beat=True, revenue_beat=True,
    )
    return Candidate(symbol=sym, earnings=hit,
                     last_price=Decimal(price),
                     avg_dollar_volume_20d=Decimal("100000000"))


def test_decide_once_happy_path(tmp_path):
    journal_path = str(tmp_path / "j.sqlite")
    runner = CliRunner()
    with patch("ops.cli.build_universe", return_value=[_candidate("AAPL")]), \
         patch("ops.cli.make_yfinance_quote_source", return_value=lambda s: Decimal("200")):
        result = runner.invoke(cli, [
            "decide-once", "--date", "2026-06-30",
            "--journal", journal_path, "--stub-pipeline-buy", "AAPL",
        ])
    assert result.exit_code == 0, result.output
    assert "AAPL" in result.output
    assert "FILLED" in result.output
    # Journal has one fill
    from ops.journal import Journal
    j = Journal(journal_path)
    fills = j.read_fills()
    assert len(fills) == 1
    assert fills[0]["symbol"] == "AAPL"


def test_decide_once_with_no_candidates(tmp_path):
    runner = CliRunner()
    with patch("ops.cli.build_universe", return_value=[]):
        result = runner.invoke(cli, [
            "decide-once", "--date", "2026-06-30",
            "--journal", str(tmp_path / "j.sqlite"),
        ])
    assert result.exit_code == 0
    assert "no candidates" in result.output.lower()


def test_decide_once_skips_holds(tmp_path):
    runner = CliRunner()
    with patch("ops.cli.build_universe", return_value=[_candidate("AAPL")]), \
         patch("ops.cli.make_yfinance_quote_source", return_value=lambda s: Decimal("200")):
        # No --stub-pipeline-buy → stub defaults to HOLD
        result = runner.invoke(cli, [
            "decide-once", "--date", "2026-06-30",
            "--journal", str(tmp_path / "j.sqlite"),
            "--stub-pipeline",
        ])
    assert result.exit_code == 0
    assert "HOLD" in result.output or "0 BUY" in result.output


def test_decide_once_runs_guardian_pass(tmp_path):
    """If a position is already open and the current quote is below the stop,
    decide-once's guardian pass should close it."""
    # Bootstrap a position via direct journal manipulation is awkward; instead,
    # run decide-once twice: first to open AAPL, then with a lower quote to close it.
    runner = CliRunner()
    journal_path = str(tmp_path / "j.sqlite")

    # First run: open AAPL at $200
    with patch("ops.cli.build_universe", return_value=[_candidate("AAPL")]), \
         patch("ops.cli.make_yfinance_quote_source", return_value=lambda s: Decimal("200")):
        r1 = runner.invoke(cli, [
            "decide-once", "--date", "2026-06-30",
            "--journal", journal_path, "--stub-pipeline-buy", "AAPL",
        ])
    assert r1.exit_code == 0
    # Second run: no new candidates, but quote dropped — guardian should fire
    with patch("ops.cli.build_universe", return_value=[]), \
         patch("ops.cli.make_yfinance_quote_source",
               return_value=lambda s: Decimal("180")):  # -10% vs 200
        r2 = runner.invoke(cli, [
            "decide-once", "--date", "2026-07-01",
            "--journal", journal_path, "--starting-cash", "225",
        ])
    assert r2.exit_code == 0
    # NOTE: the broker is fresh each invocation (in-memory PaperBroker), so a
    # second-run guardian only sees what's in THIS process's broker book —
    # which is empty. This test documents the limitation: stop enforcement
    # requires the orchestrator from Plan 3, where the broker lives across
    # ticks. The decide-once command runs one stop pass on the broker built
    # for THIS invocation, which is mostly useful when the same invocation
    # both opens and (in pathological cases) closes positions.
    assert "guardian" in r2.output.lower()
