"""Unit tests for `ops digest` (overview build faked)."""
from datetime import date

import pytest
from click.testing import CliRunner

import ops.cli as cli_mod

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "journal.sqlite"))
    monkeypatch.setenv("OPS_BASELINE_JOURNAL_PATH", str(tmp_path / "baseline.sqlite"))
    monkeypatch.setenv("OPS_RESEARCH_JOURNAL_PATH", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    return tmp_path


def _canned_report() -> dict:
    """A quiet-day-shaped report — matches build_daily_overview's exact
    output shape (see ops/notify/overview.py) so the CLI's REAL
    format_daily_overview/overview_headline render it without a KeyError."""
    return {
        "date": date(2026, 7, 8),
        "generated_at": None,
        "quiet": True,
        "header": {
            "date": date(2026, 7, 8), "momentum": None, "research": None, "baseline": None,
        },
        "momentum": {
            "cycle_ran": False, "universe": None, "universe_blind": False,
            "analyzed_decided": {"total": 0, "by_verdict": {"BUY": [], "HOLD": [], "SELL": []}},
            "buys_filled": [], "rejected": [], "exits": [],
            "day_equity": None, "day_equity_at": None, "day_pnl_pct": None,
        },
        "research": {
            "memos": [],
            "monitor": {
                "counts": None, "tripped": [], "escalations": [],
                "resolution_due": [], "catalyst_due": [],
            },
            "trades": None, "positions_opened": [], "positions_closed": [],
        },
        "baseline": {"screen": None, "exits": [], "writeoffs": []},
        "anomalies": [],
    }


def test_digest_prints_formatted_markdown(env, monkeypatch):
    monkeypatch.setattr(
        "ops.notify.overview.build_daily_overview", lambda **kw: _canned_report(),
    )
    result = CliRunner().invoke(cli_mod.cli, ["digest"])
    assert result.exit_code == 0, result.output
    assert "# Daily overview -- 2026-07-08" in result.output
    assert "Quiet day -- no activity." in result.output
    assert "## Anomalies" in result.output


def test_digest_output_writes_file(env, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "ops.notify.overview.build_daily_overview", lambda **kw: _canned_report(),
    )
    out_file = tmp_path / "out.md"
    result = CliRunner().invoke(cli_mod.cli, ["digest", "--output", str(out_file)])
    assert result.exit_code == 0, result.output
    assert result.output == ""  # nothing on stdout when --output is given
    contents = out_file.read_text()
    assert "# Daily overview -- 2026-07-08" in contents


def test_digest_push_calls_transport_once(env, monkeypatch):
    monkeypatch.setattr(
        "ops.notify.overview.build_daily_overview", lambda **kw: _canned_report(),
    )
    sent = []

    class _FakeTransport:
        def send(self, message):
            sent.append(message)

    monkeypatch.setattr(
        "ops.notify.push.build_push_transport", lambda cfg: _FakeTransport(),
    )
    result = CliRunner().invoke(cli_mod.cli, ["digest", "--push"])
    assert result.exit_code == 0, result.output
    assert len(sent) == 1
    assert sent[0].title == "Daily overview"
    assert "2026-07-08" in sent[0].body


def test_digest_no_push_by_default(env, monkeypatch):
    monkeypatch.setattr(
        "ops.notify.overview.build_daily_overview", lambda **kw: _canned_report(),
    )
    calls = []
    monkeypatch.setattr(
        "ops.notify.push.build_push_transport",
        lambda cfg: calls.append(1) or None,
    )
    result = CliRunner().invoke(cli_mod.cli, ["digest"])
    assert result.exit_code == 0, result.output
    assert calls == []


def test_digest_empty_real_stores_clean_exit(env):
    # Real (empty) journals + memo store, no fakes: must be a quiet no-op
    # that renders "Quiet day" without raising.
    result = CliRunner().invoke(cli_mod.cli, ["digest"])
    assert result.exit_code == 0, result.output
    assert "Quiet day -- no activity." in result.output


def test_digest_does_not_record_gate_event(env, tmp_path):
    """Manual/debug CLI: must never record KIND_DAILY_OVERVIEW, or a
    developer running `ops digest` for debugging would silently suppress the
    daemon's own scheduled run for the rest of the day."""
    from ops import events
    from ops.journal import Journal

    result = CliRunner().invoke(cli_mod.cli, ["digest"])
    assert result.exit_code == 0, result.output

    journal = Journal(str(env / "journal.sqlite"))
    try:
        kinds = [e["kind"] for e in journal.read_events()]
        assert events.KIND_DAILY_OVERVIEW not in kinds
        assert events.KIND_DAILY_OVERVIEW_ERROR not in kinds
    finally:
        journal.close()
