import os
from decimal import Decimal

import pytest

from ops.config import OpsConfig, load_config


def test_default_config_matches_spec():
    cfg = OpsConfig()
    # Spec section 3 defaults
    assert cfg.deny_list == {
        "SPOT",
        "TQQQ", "SQQQ", "UPRO", "SPXU", "UVXY", "SVXY",
        "SOXL", "SOXS", "LABU", "LABD", "TNA", "TZA",
        "TMF", "TMV", "QLD", "QID",
    }
    assert cfg.per_position_cap_pct == Decimal("0.12")
    assert cfg.per_trade_dollar_floor == Decimal("5")
    assert cfg.max_open_positions == 12
    assert cfg.cash_reserve_pct == Decimal("0.16")
    assert cfg.daily_drawdown_pct == Decimal("-0.07")
    assert cfg.weekly_drawdown_pct == Decimal("-0.15")
    assert cfg.per_position_stop_pct == Decimal("-0.08")
    assert cfg.broker_mode == "paper"
    # Full contractual blackout (buy AND sell rejected) is a strict subset
    # of deny_list — see DenyListRule (M5).
    assert cfg.full_blackout_symbols == {"SPOT"}
    assert cfg.full_blackout_symbols <= cfg.deny_list

def test_full_blackout_symbols_must_be_subset_of_deny_list():
    with pytest.raises(ValueError):
        OpsConfig(full_blackout_symbols=frozenset({"NOTDENIED"}))

def test_load_config_reads_env_overrides(monkeypatch):
    monkeypatch.setenv("OPS_BROKER_MODE", "robinhood")
    monkeypatch.setenv("OPS_PER_POSITION_CAP_PCT", "0.05")
    cfg = load_config()
    assert cfg.broker_mode == "robinhood"
    assert cfg.per_position_cap_pct == Decimal("0.05")


def test_load_config_uses_dataclass_defaults_for_unset_fields(monkeypatch):
    # Make sure no OPS_* vars leak from the test environment
    for key in list(os.environ):
        if key.startswith("OPS_"):
            monkeypatch.delenv(key)
    cfg = load_config()
    # Should match the dataclass defaults exactly
    assert cfg == OpsConfig()


def test_load_config_raises_attributed_error_on_bad_decimal(monkeypatch):
    monkeypatch.setenv("OPS_PER_POSITION_CAP_PCT", "banana")
    with pytest.raises(ValueError, match="OPS_PER_POSITION_CAP_PCT"):
        load_config()


def test_load_config_raises_attributed_error_on_bad_int(monkeypatch):
    monkeypatch.setenv("OPS_MAX_OPEN_POSITIONS", "five")
    with pytest.raises(ValueError, match="OPS_MAX_OPEN_POSITIONS"):
        load_config()


def test_starting_cash_default_and_env(monkeypatch):
    from decimal import Decimal

    from ops.config import OpsConfig, load_config
    assert OpsConfig().starting_cash == Decimal("250")
    monkeypatch.setenv("OPS_STARTING_CASH", "500")
    assert load_config().starting_cash == Decimal("500")


def test_journal_path_defaults_to_xdg_state_home_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("OPS_JOURNAL_PATH", raising=False)
    cfg = OpsConfig()
    assert cfg.journal_path == str(tmp_path / "state" / "tradingagents" / "ops_journal.sqlite")


def test_journal_path_defaults_to_local_state_home_when_xdg_unset(monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    cfg = OpsConfig()
    assert cfg.journal_path == os.path.expanduser(
        "~/.local/state/tradingagents/ops_journal.sqlite"
    )


def test_journal_path_env_override_still_wins(monkeypatch, tmp_path):
    override = str(tmp_path / "custom.sqlite")
    monkeypatch.setenv("OPS_JOURNAL_PATH", override)
    cfg = load_config()
    assert cfg.journal_path == override


def test_starting_cash_must_be_positive():
    from decimal import Decimal

    import pytest

    from ops.config import OpsConfig
    with pytest.raises(ValueError):
        OpsConfig(starting_cash=Decimal("0"))


def test_live_gate_defaults():
    c = OpsConfig()
    assert c.live_max_position == Decimal("10")
    assert c.live_fill_gate_count == 20


def test_live_gate_from_env(monkeypatch):
    monkeypatch.setenv("OPS_LIVE_MAX_POSITION", "8")
    monkeypatch.setenv("OPS_LIVE_FILL_GATE_COUNT", "30")
    c = load_config()
    assert c.live_max_position == Decimal("8") and c.live_fill_gate_count == 30


def test_baseline_config_fields_and_env_overrides(monkeypatch):
    cfg = OpsConfig()
    assert cfg.baseline_starting_cash == Decimal("100000")
    assert cfg.baseline_journal_path.endswith("baseline_journal.sqlite")
    assert cfg.screen_store_path.endswith("research_screen.sqlite")

    monkeypatch.setenv("OPS_BASELINE_JOURNAL_PATH", "/tmp/x.sqlite")
    monkeypatch.setenv("OPS_BASELINE_STARTING_CASH", "50000")
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", "/tmp/y.sqlite")
    cfg = load_config()
    assert cfg.baseline_journal_path == "/tmp/x.sqlite"
    assert cfg.baseline_starting_cash == Decimal("50000")
    assert cfg.screen_store_path == "/tmp/y.sqlite"

    with pytest.raises(ValueError):
        OpsConfig(baseline_starting_cash=Decimal("0"))


def test_envelope_rev2_defaults_and_derived_concurrency():
    cfg = OpsConfig()
    assert cfg.max_open_positions == 12
    assert cfg.per_position_cap_pct == Decimal("0.12")
    assert cfg.cash_reserve_pct == Decimal("0.16")
    # Neither dial is cosmetic: derived effective concurrency equals the cap.
    deployable = Decimal("1") - cfg.cash_reserve_pct
    assert min(cfg.max_open_positions,
               int(deployable / cfg.per_position_cap_pct)) == 7
    # Safety rails unchanged.
    assert cfg.per_position_stop_pct == Decimal("-0.08")
    assert cfg.daily_drawdown_pct == Decimal("-0.07")
    assert cfg.weekly_drawdown_pct == Decimal("-0.15")


def test_daily_analysis_budget_default_env_and_validation(monkeypatch):
    assert OpsConfig().daily_analysis_budget == 12
    monkeypatch.setenv("OPS_DAILY_ANALYSIS_BUDGET", "3")
    assert load_config().daily_analysis_budget == 3
    with pytest.raises(ValueError):
        OpsConfig(daily_analysis_budget=0)


def test_exit_defaults_and_env(monkeypatch):
    cfg = OpsConfig()
    assert cfg.momentum_exit_rank == 25
    assert cfg.earnings_max_hold_days == 40
    assert cfg.stopout_reentry_cooldown_days == 10
    monkeypatch.setenv("OPS_MOMENTUM_EXIT_RANK", "30")
    monkeypatch.setenv("OPS_EARNINGS_MAX_HOLD_DAYS", "50")
    monkeypatch.setenv("OPS_STOPOUT_REENTRY_COOLDOWN_DAYS", "5")
    loaded = load_config()
    assert (loaded.momentum_exit_rank, loaded.earnings_max_hold_days,
            loaded.stopout_reentry_cooldown_days) == (30, 50, 5)


def test_exit_rank_must_exceed_analysis_budget():
    # Exit rank at or below the entry budget removes the hysteresis band
    # and guarantees churn at the boundary.
    with pytest.raises(ValueError):
        OpsConfig(momentum_exit_rank=12)  # <= default budget of 12
    with pytest.raises(ValueError):
        OpsConfig(daily_analysis_budget=8, momentum_exit_rank=8)
    OpsConfig(momentum_exit_rank=13)  # boundary: default budget+1 is valid


def test_exit_day_counts_must_be_positive():
    with pytest.raises(ValueError):
        OpsConfig(earnings_max_hold_days=0)
    with pytest.raises(ValueError):
        OpsConfig(stopout_reentry_cooldown_days=0)


def test_research_model_env_overrides(monkeypatch):
    monkeypatch.setenv("OPS_RESEARCH_EVIDENCE_MODEL", "anthropic:claude-haiku-4-5")
    monkeypatch.setenv("OPS_RESEARCH_THESIS_MODEL", "anthropic:claude-sonnet-5")
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", "/tmp/m.sqlite")
    config = load_config()
    assert config.research_evidence_model == "anthropic:claude-haiku-4-5"
    assert config.research_thesis_model == "anthropic:claude-sonnet-5"
    assert config.memo_store_path == "/tmp/m.sqlite"


def test_malformed_research_model_rejected():
    with pytest.raises(ValueError):
        OpsConfig(research_thesis_model="not-a-spec")


def test_research_journal_env_overrides(monkeypatch):
    monkeypatch.setenv("OPS_RESEARCH_JOURNAL_PATH", "/tmp/research.sqlite")
    monkeypatch.setenv("OPS_RESEARCH_STARTING_CASH", "50000")
    config = load_config()
    assert config.research_journal_path == "/tmp/research.sqlite"
    assert config.research_starting_cash == Decimal("50000")


def test_research_journal_defaults(monkeypatch):
    monkeypatch.delenv("OPS_RESEARCH_JOURNAL_PATH", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/state")
    config = load_config()
    assert config.research_journal_path == "/tmp/state/tradingagents/research_journal.sqlite"
    assert config.research_starting_cash == Decimal("100000")


def test_nonpositive_research_cash_rejected():
    with pytest.raises(ValueError):
        OpsConfig(research_starting_cash=Decimal("0"))


def test_research_cadence_defaults():
    from ops.config import OpsConfig
    cfg = OpsConfig()
    assert cfg.research_screen_interval_days == 3
    assert cfg.research_drain_deadline_hour == 8
    assert cfg.research_screen_ttl_days == 7
    assert cfg.research_drain_nightly_cap == 15


def test_research_cadence_env_overrides(monkeypatch):
    from ops.config import load_config
    monkeypatch.setenv("OPS_RESEARCH_SCREEN_INTERVAL_DAYS", "2")
    monkeypatch.setenv("OPS_RESEARCH_DRAIN_DEADLINE_HOUR", "7")
    monkeypatch.setenv("OPS_RESEARCH_SCREEN_TTL_DAYS", "5")
    monkeypatch.setenv("OPS_RESEARCH_DRAIN_NIGHTLY_CAP", "8")
    cfg = load_config()
    assert cfg.research_screen_interval_days == 2
    assert cfg.research_drain_deadline_hour == 7
    assert cfg.research_screen_ttl_days == 5
    assert cfg.research_drain_nightly_cap == 8


def test_research_cadence_validation():
    import pytest
    from ops.config import OpsConfig
    with pytest.raises(ValueError):
        OpsConfig(research_screen_interval_days=0)
    with pytest.raises(ValueError):
        OpsConfig(research_screen_ttl_days=0)
    with pytest.raises(ValueError):
        OpsConfig(research_drain_nightly_cap=0)
    with pytest.raises(ValueError):
        OpsConfig(research_drain_deadline_hour=24)
    with pytest.raises(ValueError):
        OpsConfig(research_drain_deadline_hour=-1)
    # The deadline must land strictly before the 09:00 first momentum tick —
    # 9 (and later) would let the drain bleed into market-open ds4 usage.
    with pytest.raises(ValueError):
        OpsConfig(research_drain_deadline_hour=9)
    OpsConfig(research_drain_deadline_hour=8)  # boundary: still valid
    OpsConfig(research_drain_deadline_hour=0)  # boundary: still valid


def test_sleeve_path_defaults_and_env_overrides(monkeypatch):
    cfg = OpsConfig()
    assert cfg.short_journal_path.endswith("short_journal.sqlite")
    assert cfg.short_memo_store_path.endswith("short_memos.sqlite")
    assert cfg.short_screen_store_path.endswith("short_screen.sqlite")
    assert cfg.insider_journal_path.endswith("insider_journal.sqlite")
    assert cfg.insider_memo_store_path.endswith("insider_memos.sqlite")
    assert cfg.insider_signal_store_path.endswith("insider_signals.sqlite")
    assert cfg.short_starting_cash == Decimal("10000")
    assert cfg.insider_starting_cash == Decimal("10000")

    monkeypatch.setenv("OPS_SHORT_JOURNAL_PATH", "/tmp/s.sqlite")
    monkeypatch.setenv("OPS_SHORT_MEMO_STORE_PATH", "/tmp/sm.sqlite")
    monkeypatch.setenv("OPS_SHORT_SCREEN_STORE_PATH", "/tmp/ss.sqlite")
    monkeypatch.setenv("OPS_INSIDER_JOURNAL_PATH", "/tmp/i.sqlite")
    monkeypatch.setenv("OPS_INSIDER_MEMO_STORE_PATH", "/tmp/im.sqlite")
    monkeypatch.setenv("OPS_INSIDER_SIGNAL_STORE_PATH", "/tmp/is.sqlite")
    monkeypatch.setenv("OPS_SHORT_STARTING_CASH", "5000")
    monkeypatch.setenv("OPS_INSIDER_STARTING_CASH", "7000")
    cfg = load_config()
    assert cfg.short_journal_path == "/tmp/s.sqlite"
    assert cfg.short_memo_store_path == "/tmp/sm.sqlite"
    assert cfg.short_screen_store_path == "/tmp/ss.sqlite"
    assert cfg.insider_journal_path == "/tmp/i.sqlite"
    assert cfg.insider_memo_store_path == "/tmp/im.sqlite"
    assert cfg.insider_signal_store_path == "/tmp/is.sqlite"
    assert cfg.short_starting_cash == Decimal("5000")
    assert cfg.insider_starting_cash == Decimal("7000")


def test_sleeve_starting_cash_must_be_positive():
    with pytest.raises(ValueError, match="short_starting_cash"):
        OpsConfig(short_starting_cash=Decimal("0"))
    with pytest.raises(ValueError, match="insider_starting_cash"):
        OpsConfig(insider_starting_cash=Decimal("-1"))


def test_v2_posture_defaults():
    cfg = OpsConfig()
    assert cfg.starter_position_pct == Decimal("0.05")
    assert cfg.displacement_max_trims_per_day == 2
    assert cfg.displacement_min_holding_age_days == 3
    assert cfg.max_open_positions == 12
    assert cfg.daily_analysis_budget == 12


def test_starter_pct_must_be_positive_and_at_most_full_size():
    with pytest.raises(ValueError):
        OpsConfig(starter_position_pct=Decimal("0"))
    with pytest.raises(ValueError):
        OpsConfig(starter_position_pct=Decimal("0.13"))  # > per_position_cap_pct


def test_displacement_counts_must_be_positive():
    with pytest.raises(ValueError):
        OpsConfig(displacement_max_trims_per_day=0)
    with pytest.raises(ValueError):
        OpsConfig(displacement_min_holding_age_days=0)


def test_displacement_env_overrides(monkeypatch):
    monkeypatch.setenv("OPS_STARTER_POSITION_PCT", "0.03")
    monkeypatch.setenv("OPS_DISPLACEMENT_MAX_TRIMS_PER_DAY", "1")
    monkeypatch.setenv("OPS_DISPLACEMENT_MIN_HOLDING_AGE_DAYS", "5")
    cfg = load_config()
    assert cfg.starter_position_pct == Decimal("0.03")
    assert cfg.displacement_max_trims_per_day == 1
    assert cfg.displacement_min_holding_age_days == 5
