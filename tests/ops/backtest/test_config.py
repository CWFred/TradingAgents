from datetime import date
from decimal import Decimal

import pytest

from ops.backtest.models import MIN_BACKTEST_CUTOFF
from ops.config import OpsConfig, load_config


def test_backtest_defaults_are_xdg_aware_and_match_approved_semantics(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = OpsConfig()

    assert cfg.backtest_store_path == str(
        tmp_path / "state" / "tradingagents" / "backtest.sqlite"
    )
    assert cfg.backtest_cutoff == date(2025, 6, 1)
    assert cfg.backtest_cutoff == MIN_BACKTEST_CUTOFF
    assert cfg.backtest_benchmark == "SPY"
    assert cfg.backtest_horizons == (5, 21, 63, 126)
    assert cfg.backtest_primary_horizon == 63
    assert cfg.backtest_wash_band == Decimal("0.03")
    assert cfg.backtest_case_count == 40
    assert cfg.backtest_case_notional == Decimal("10000")
    assert cfg.backtest_min_mature_cases == 20


def test_load_config_reads_all_backtest_env_overrides(monkeypatch, tmp_path):
    path = tmp_path / "isolated.sqlite"
    monkeypatch.setenv("OPS_BACKTEST_STORE_PATH", str(path))
    monkeypatch.setenv("OPS_BACKTEST_CUTOFF", "2025-07-01")
    monkeypatch.setenv("OPS_BACKTEST_BENCHMARK", "qqq")
    monkeypatch.setenv("OPS_BACKTEST_HORIZONS", "10, 20, 40")
    monkeypatch.setenv("OPS_BACKTEST_PRIMARY_HORIZON", "20")
    monkeypatch.setenv("OPS_BACKTEST_WASH_BAND", "0.025")
    monkeypatch.setenv("OPS_BACKTEST_CASE_COUNT", "50")
    monkeypatch.setenv("OPS_BACKTEST_CASE_NOTIONAL", "25000")
    monkeypatch.setenv("OPS_BACKTEST_MIN_MATURE_CASES", "25")
    monkeypatch.setenv("OPS_BACKTEST_PROMISING_MIN_HIT_RATE", "0.60")
    monkeypatch.setenv("OPS_BACKTEST_PROMISING_MIN_MEAN_EXCESS", "0.04")
    monkeypatch.setenv("OPS_BACKTEST_DEAD_MAX_HIT_RATE", "0.35")
    monkeypatch.setenv("OPS_BACKTEST_DEAD_MAX_MEAN_EXCESS", "-0.01")

    cfg = load_config()

    assert cfg.backtest_store_path == str(path)
    assert cfg.backtest_cutoff == date(2025, 7, 1)
    assert cfg.backtest_benchmark == "QQQ"
    assert cfg.backtest_horizons == (10, 20, 40)
    assert cfg.backtest_primary_horizon == 20
    assert cfg.backtest_wash_band == Decimal("0.025")
    assert cfg.backtest_case_count == 50
    assert cfg.backtest_case_notional == Decimal("25000")
    assert cfg.backtest_min_mature_cases == 25
    assert cfg.backtest_promising_min_hit_rate == Decimal("0.60")
    assert cfg.backtest_promising_min_mean_excess == Decimal("0.04")
    assert cfg.backtest_dead_max_hit_rate == Decimal("0.35")
    assert cfg.backtest_dead_max_mean_excess == Decimal("-0.01")


def test_cutoff_can_advance_but_never_move_before_hard_minimum():
    assert OpsConfig(backtest_cutoff=date(2025, 6, 2)).backtest_cutoff == date(2025, 6, 2)
    with pytest.raises(ValueError, match="only advance"):
        OpsConfig(backtest_cutoff=date(2025, 5, 31))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"backtest_horizons": ()},
        {"backtest_horizons": (5, 5, 63)},
        {"backtest_horizons": (21, 5, 63)},
        {"backtest_horizons": (5, 21), "backtest_primary_horizon": 63},
        {"backtest_wash_band": Decimal("-0.01")},
        {"backtest_wash_band": Decimal("1")},
        {"backtest_case_count": 29},
        {"backtest_case_count": 51},
        {"backtest_case_notional": Decimal("0")},
        {"backtest_min_mature_cases": 0},
        {"backtest_min_mature_cases": 41},
        {"backtest_promising_min_hit_rate": Decimal("0.30")},
        {"backtest_promising_min_mean_excess": Decimal("-0.01")},
    ],
)
def test_backtest_config_fails_closed_on_invalid_values(kwargs):
    with pytest.raises(ValueError):
        OpsConfig(**kwargs)


def test_invalid_backtest_env_values_name_the_setting(monkeypatch):
    monkeypatch.setenv("OPS_BACKTEST_CUTOFF", "not-a-date")
    with pytest.raises(ValueError, match="OPS_BACKTEST_CUTOFF"):
        load_config()

    monkeypatch.delenv("OPS_BACKTEST_CUTOFF")
    monkeypatch.setenv("OPS_BACKTEST_HORIZONS", "5,banana,63")
    with pytest.raises(ValueError, match="OPS_BACKTEST_HORIZONS"):
        load_config()

