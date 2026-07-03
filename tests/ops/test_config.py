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
    assert cfg.per_position_cap_pct == Decimal("0.10")
    assert cfg.per_trade_dollar_floor == Decimal("5")
    assert cfg.max_open_positions == 5
    assert cfg.cash_reserve_pct == Decimal("0.20")
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
