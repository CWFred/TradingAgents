"""Unit tests for `ops research trade` (trade core faked)."""

from decimal import Decimal

import pytest
from click.testing import CliRunner

import ops.cli as cli_mod
from ops.research.trading import TradeOutcome

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "journal.sqlite"))
    monkeypatch.setenv("OPS_RESEARCH_JOURNAL_PATH", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    return tmp_path


def test_trade_echoes_summary(env, monkeypatch):
    outcome = TradeOutcome(
        asof="2026-07-07", entered=["WIDG"], exited=["SPIN"],
        skipped=["AAA: adv cap"], errors=[],
        equity=Decimal("101000"), cash=Decimal("60000"),
    )
    monkeypatch.setattr("ops.research.trading.trade_research_sleeve",
                        lambda **kw: outcome)
    result = CliRunner().invoke(cli_mod.cli, ["research", "trade"])
    assert result.exit_code == 0, result.output
    assert "WIDG" in result.output and "SPIN" in result.output
    assert "adv cap" in result.output


def test_trade_empty_everything_clean_exit(env, monkeypatch):
    # Real stores/journals, but a no-network quote source: no memos -> no quotes needed.
    monkeypatch.setattr(
        "ops.quotes.make_yfinance_quote_source",
        lambda: (lambda s: (_ for _ in ()).throw(AssertionError("no quotes needed"))),
    )
    result = CliRunner().invoke(cli_mod.cli, ["research", "trade"])
    assert result.exit_code == 0, result.output
