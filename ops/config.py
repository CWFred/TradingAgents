"""Operational config for the live-trading layer.

Defaults match docs/superpowers/specs/2026-06-30-tradingagents-live-v1-design.md
section "Guardrail rules". Override at runtime via OPS_* env vars.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_DEFAULT_DENY_LIST = frozenset({
    "SPOT",
    "TQQQ", "SQQQ", "UPRO", "SPXU", "UVXY", "SVXY",
    "SOXL", "SOXS", "LABU", "LABD", "TNA", "TZA",
    "TMF", "TMV", "QLD", "QID",
})

def _default_journal_path() -> str:
    """Default journal location: ${XDG_STATE_HOME:-~/.local/state}/tradingagents/ops_journal.sqlite.

    Computed fresh on every OpsConfig() construction (not a module-level
    constant) so tests can monkeypatch XDG_STATE_HOME and so the resolved
    path always reflects the current environment. A CWD-relative default
    (the old behavior) silently creates a fresh journal — and fresh paper
    account — whenever `ops run` is launched from the wrong directory.
    """
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(os.path.expanduser(base), "tradingagents", "ops_journal.sqlite")


def _default_baseline_journal_path() -> str:
    """Baseline (null-hypothesis) paper portfolio journal — separate DB from
    the trading journal so the control can never contaminate real state."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(os.path.expanduser(base), "tradingagents", "baseline_journal.sqlite")


def _default_screen_store_path() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(os.path.expanduser(base), "tradingagents", "research_screen.sqlite")


# Symbols in this set are a FULL contractual blackout: buy AND sell are
# rejected. This is a strict subset of deny_list. Everything else in
# deny_list (the leveraged ETFs) is BUY-denied but SELL-allowed — selling
# reduces risk, so DenyListRule permits exiting a leveraged-ETF position
# that was somehow acquired manually. SPOT must never be exitable through
# this path (see DenyListRule / RobinhoodBroker._enforce_spot_hard_check).
_FULL_BLACKOUT_SYMBOLS = frozenset({"SPOT"})


@dataclass(frozen=True)
class OpsConfig:
    broker_mode: str = "paper"  # "paper" or "robinhood"
    deny_list: frozenset[str] = field(default_factory=lambda: _DEFAULT_DENY_LIST)  # Not env-overridable; extend via code
    full_blackout_symbols: frozenset[str] = field(default_factory=lambda: _FULL_BLACKOUT_SYMBOLS)  # Not env-overridable; extend via code
    per_position_cap_pct: Decimal = Decimal("0.12")
    per_trade_dollar_floor: Decimal = Decimal("5")
    max_open_positions: int = 7
    cash_reserve_pct: Decimal = Decimal("0.16")
    daily_drawdown_pct: Decimal = Decimal("-0.07")
    weekly_drawdown_pct: Decimal = Decimal("-0.15")
    per_position_stop_pct: Decimal = Decimal("-0.08")
    journal_path: str = field(default_factory=_default_journal_path)
    starting_cash: Decimal = Decimal("250")
    live_max_position: Decimal = Decimal("10")
    live_fill_gate_count: int = 20
    baseline_journal_path: str = field(default_factory=_default_baseline_journal_path)
    baseline_starting_cash: Decimal = Decimal("100000")
    screen_store_path: str = field(default_factory=_default_screen_store_path)
    # Cost dial: max full-pipeline (LLM) analyses per day; risk is capped separately.
    daily_analysis_budget: int = 8
    # Exit engine (spec Component 6). Entry is top-daily_analysis_budget;
    # the gap up to momentum_exit_rank is deliberate hysteresis.
    momentum_exit_rank: int = 25
    earnings_max_hold_days: int = 40
    stopout_reentry_cooldown_days: int = 10

    def __post_init__(self) -> None:
        # Drawdown and per-position-stop percentages must be negative — a
        # positive number here silently disables the kill switch.
        for fname in ("daily_drawdown_pct", "weekly_drawdown_pct", "per_position_stop_pct"):
            val = getattr(self, fname)
            if val >= 0:
                raise ValueError(f"{fname} must be negative, got {val}")
        # Caps and reserves are fractions in [0, 1].
        for fname in ("per_position_cap_pct", "cash_reserve_pct"):
            val = getattr(self, fname)
            if not (Decimal("0") <= val <= Decimal("1")):
                raise ValueError(f"{fname} must be in [0, 1], got {val}")
        if self.per_trade_dollar_floor < 0:
            raise ValueError(
                f"per_trade_dollar_floor must be >= 0, got {self.per_trade_dollar_floor}"
            )
        if self.starting_cash <= 0:
            raise ValueError(
                f"starting_cash must be > 0, got {self.starting_cash}"
            )
        if self.max_open_positions <= 0:
            raise ValueError(
                f"max_open_positions must be > 0, got {self.max_open_positions}"
            )
        if self.daily_analysis_budget <= 0:
            raise ValueError(
                f"daily_analysis_budget must be > 0, got {self.daily_analysis_budget}"
            )
        for fname in ("earnings_max_hold_days", "stopout_reentry_cooldown_days"):
            val = getattr(self, fname)
            if val <= 0:
                raise ValueError(f"{fname} must be > 0, got {val}")
        if self.momentum_exit_rank <= self.daily_analysis_budget:
            raise ValueError(
                "momentum_exit_rank must exceed daily_analysis_budget "
                f"(hysteresis band), got {self.momentum_exit_rank} <= "
                f"{self.daily_analysis_budget}"
            )
        if self.broker_mode not in ("paper", "robinhood"):
            raise ValueError(
                f"broker_mode must be 'paper' or 'robinhood', got {self.broker_mode!r}"
            )
        if not self.full_blackout_symbols <= self.deny_list:
            raise ValueError(
                "full_blackout_symbols must be a subset of deny_list, got "
                f"{self.full_blackout_symbols - self.deny_list}"
            )
        if self.live_max_position <= 0:
            raise ValueError(f"live_max_position must be > 0, got {self.live_max_position}")
        if self.live_fill_gate_count < 0:
            raise ValueError(
                f"live_fill_gate_count must be >= 0, got {self.live_fill_gate_count}"
            )
        if self.baseline_starting_cash <= 0:
            raise ValueError(
                f"baseline_starting_cash must be > 0, got {self.baseline_starting_cash}"
            )


def _env_decimal(name: str) -> Decimal | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid value for {name!r}: {raw!r}") from exc


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid value for {name!r}: {raw!r}") from exc


def load_config() -> OpsConfig:
    kwargs: dict = {}

    broker_mode = os.environ.get("OPS_BROKER_MODE")
    if broker_mode is not None:
        kwargs["broker_mode"] = broker_mode

    per_position_cap_pct = _env_decimal("OPS_PER_POSITION_CAP_PCT")
    if per_position_cap_pct is not None:
        kwargs["per_position_cap_pct"] = per_position_cap_pct

    per_trade_dollar_floor = _env_decimal("OPS_PER_TRADE_DOLLAR_FLOOR")
    if per_trade_dollar_floor is not None:
        kwargs["per_trade_dollar_floor"] = per_trade_dollar_floor

    max_open_positions = _env_int("OPS_MAX_OPEN_POSITIONS")
    if max_open_positions is not None:
        kwargs["max_open_positions"] = max_open_positions

    cash_reserve_pct = _env_decimal("OPS_CASH_RESERVE_PCT")
    if cash_reserve_pct is not None:
        kwargs["cash_reserve_pct"] = cash_reserve_pct

    daily_drawdown_pct = _env_decimal("OPS_DAILY_DRAWDOWN_PCT")
    if daily_drawdown_pct is not None:
        kwargs["daily_drawdown_pct"] = daily_drawdown_pct

    weekly_drawdown_pct = _env_decimal("OPS_WEEKLY_DRAWDOWN_PCT")
    if weekly_drawdown_pct is not None:
        kwargs["weekly_drawdown_pct"] = weekly_drawdown_pct

    per_position_stop_pct = _env_decimal("OPS_PER_POSITION_STOP_PCT")
    if per_position_stop_pct is not None:
        kwargs["per_position_stop_pct"] = per_position_stop_pct

    journal_path = os.environ.get("OPS_JOURNAL_PATH")
    if journal_path is not None:
        kwargs["journal_path"] = journal_path

    starting_cash = _env_decimal("OPS_STARTING_CASH")
    if starting_cash is not None:
        kwargs["starting_cash"] = starting_cash

    live_max_position = _env_decimal("OPS_LIVE_MAX_POSITION")
    if live_max_position is not None:
        kwargs["live_max_position"] = live_max_position

    live_fill_gate_count = _env_int("OPS_LIVE_FILL_GATE_COUNT")
    if live_fill_gate_count is not None:
        kwargs["live_fill_gate_count"] = live_fill_gate_count

    baseline_journal_path = os.environ.get("OPS_BASELINE_JOURNAL_PATH")
    if baseline_journal_path is not None:
        kwargs["baseline_journal_path"] = baseline_journal_path

    baseline_starting_cash = _env_decimal("OPS_BASELINE_STARTING_CASH")
    if baseline_starting_cash is not None:
        kwargs["baseline_starting_cash"] = baseline_starting_cash

    screen_store_path = os.environ.get("OPS_SCREEN_STORE_PATH")
    if screen_store_path is not None:
        kwargs["screen_store_path"] = screen_store_path

    daily_analysis_budget = _env_int("OPS_DAILY_ANALYSIS_BUDGET")
    if daily_analysis_budget is not None:
        kwargs["daily_analysis_budget"] = daily_analysis_budget

    momentum_exit_rank = _env_int("OPS_MOMENTUM_EXIT_RANK")
    if momentum_exit_rank is not None:
        kwargs["momentum_exit_rank"] = momentum_exit_rank

    earnings_max_hold_days = _env_int("OPS_EARNINGS_MAX_HOLD_DAYS")
    if earnings_max_hold_days is not None:
        kwargs["earnings_max_hold_days"] = earnings_max_hold_days

    stopout_reentry_cooldown_days = _env_int("OPS_STOPOUT_REENTRY_COOLDOWN_DAYS")
    if stopout_reentry_cooldown_days is not None:
        kwargs["stopout_reentry_cooldown_days"] = stopout_reentry_cooldown_days

    return OpsConfig(**kwargs)
