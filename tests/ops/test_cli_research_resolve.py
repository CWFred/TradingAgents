"""Unit tests for `ops research resolve` (resolution arithmetic faked; real
MemoStore so the status flip and label round-trip are actually exercised)."""

from datetime import date, datetime, timezone

import pytest
from click.testing import CliRunner

import ops.cli as cli_mod
from tradingagents.memos.schema import EvidenceItem, Falsifier, Memo, ValueThesis
from tradingagents.memos.store import MemoStore

pytestmark = pytest.mark.unit

NUMBERS = {
    "resolved_at": datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc),
    "exit_price": 14.5,
    "realized_return_pct": 0.45,
    "benchmark_return_pct": 0.08,
    "holding_days": 183,
}


def _memo(ticker="WIDG"):
    return Memo(
        ticker=ticker, as_of_date=date(2026, 1, 5), thesis_type="value",
        thesis="Mispriced.",
        evidence=[EvidenceItem(claim="c", source_type="filing", source_ref="a:mdna")],
        value_block=ValueThesis(
            why_cheap="x", change_trigger="y",
            normalized_earnings_view="z", quality_assessment="q",
        ),
        conviction_tier="medium",
        entry_price_ref=10.0, price_target_low=15.0, price_target_high=20.0,
        expected_holding_months=6, must_be_true=["m"],
        falsifiers=[Falsifier(description="d", check_type="price",
                              metric="drawdown_from_cost_pct", operator="<",
                              threshold=-30.0)],
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("OPS_RESEARCH_JOURNAL_PATH", str(tmp_path / "research.sqlite"))
    return tmp_path


def test_resolve_flips_status_and_label_roundtrips(env, monkeypatch):
    memo_store = MemoStore(env / "memos.sqlite")
    memo = _memo()
    memo_store.save(memo)

    monkeypatch.setattr(
        "ops.research.resolution.compute_resolution_numbers", lambda *a, **kw: dict(NUMBERS)
    )
    result = CliRunner().invoke(cli_mod.cli, [
        "research", "resolve", memo.memo_id,
        "--label", "thesis_right_made_money",
        "--narrative", "Rerated on the CEO change as expected.",
    ])
    assert result.exit_code == 0, result.output
    assert "thesis_right_made_money" in result.output

    resolved = memo_store.get(memo.memo_id)
    assert resolved.status == "resolved"
    assert resolved.resolution.outcome_label == "thesis_right_made_money"
    assert resolved.resolution.narrative == "Rerated on the CEO change as expected."
    assert resolved.resolution.exit_price == 14.5


def test_resolve_already_resolved_is_clean_error(env, monkeypatch):
    memo_store = MemoStore(env / "memos.sqlite")
    memo = _memo()
    memo_store.save(memo)
    monkeypatch.setattr(
        "ops.research.resolution.compute_resolution_numbers", lambda *a, **kw: dict(NUMBERS)
    )
    args = [
        "research", "resolve", memo.memo_id,
        "--label", "thesis_right_made_money", "--narrative", "n",
    ]
    first = CliRunner().invoke(cli_mod.cli, args)
    assert first.exit_code == 0, first.output

    second = CliRunner().invoke(cli_mod.cli, args)
    assert second.exit_code != 0
    assert "already resolved" in second.output


def test_resolve_unknown_memo_is_clean_error(env):
    result = CliRunner().invoke(cli_mod.cli, [
        "research", "resolve", "nope-not-a-memo",
        "--label", "thesis_right_made_money", "--narrative", "n",
    ])
    assert result.exit_code != 0
    assert "no memo" in result.output


def test_resolve_rejects_unknown_label_choice(env):
    memo_store = MemoStore(env / "memos.sqlite")
    memo = _memo()
    memo_store.save(memo)
    result = CliRunner().invoke(cli_mod.cli, [
        "research", "resolve", memo.memo_id,
        "--label", "bogus_label", "--narrative", "n",
    ])
    assert result.exit_code != 0
    assert "Invalid value" in result.output or "invalid choice" in result.output.lower()
