"""`ops research kick`: screen -> drain-all -> trade, one shot."""
import pytest
from click.testing import CliRunner

import ops.cli as cli_mod

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.setenv("OPS_RESEARCH_JOURNAL_PATH", str(tmp_path / "rj.sqlite"))
    monkeypatch.setenv("OPS_JOURNAL_PATH", str(tmp_path / "journal.sqlite"))
    monkeypatch.delenv("OPS_LLM_MANAGED_BACKEND", raising=False)
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "Test Suite test@example.com")
    monkeypatch.setattr("ops.research.models.build_stage_llm", lambda spec: f"llm:{spec}")
    return tmp_path


def test_kick_runs_screen_drain_trade_in_order(env, monkeypatch):
    calls = []
    monkeypatch.setattr("ops.research.run.run_screen",
                        lambda **kw: calls.append("screen"))

    from ops.research.drain import DrainSummary
    monkeypatch.setattr(
        "ops.research.drain.drain_pending",
        lambda **kw: (calls.append("drain"),
                      DrainSummary(2, 0, 0, False))[1],
    )
    monkeypatch.setattr("ops.research.trading.trade_research_sleeve",
                        lambda **kw: calls.append("trade"))
    # Neutralize the managed backend.
    class _NoBackend:
        def ensure_up(self): pass
        def shutdown(self): pass
    monkeypatch.setattr("ops.llm_backend.build_managed_backend", lambda cfg: _NoBackend())

    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["research", "kick"])
    assert result.exit_code == 0, result.output
    assert calls == ["screen", "drain", "trade"]
