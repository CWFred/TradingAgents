"""Delivery configuration for notifications, kept separate from OpsConfig
so delivery secrets never mix with the risk-parameter object."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class NotifyConfig:
    notify_enabled: bool = False
    pushover_user_key: str | None = None
    pushover_app_token: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None
    # Dead-man's switch (A1.3): a healthchecks.io-style URL pinged while
    # the guardian loop is alive, so the EXTERNAL service alerts when pings
    # stop. None = feature off. Lives here (not OpsConfig) because it is
    # delivery configuration, not a risk parameter.
    heartbeat_url: str | None = None


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_smtp_port(port_raw: str | None) -> int:
    """Parse OPS_SMTP_PORT, matching the named-variable error message
    pattern used by ops/config.py::_env_int, so a non-numeric value fails
    with a clear message instead of a bare int() ValueError."""
    if not port_raw:
        return 587
    try:
        return int(port_raw)
    except ValueError as exc:
        raise ValueError(f"Invalid value for 'OPS_SMTP_PORT': {port_raw!r}") from exc


def load_notify_config() -> NotifyConfig:
    port_raw = os.environ.get("OPS_SMTP_PORT")
    return NotifyConfig(
        notify_enabled=_env_bool("OPS_NOTIFY_ENABLED"),
        pushover_user_key=os.environ.get("OPS_PUSHOVER_USER_KEY"),
        pushover_app_token=os.environ.get("OPS_PUSHOVER_APP_TOKEN"),
        smtp_host=os.environ.get("OPS_SMTP_HOST"),
        smtp_port=_env_smtp_port(port_raw),
        smtp_user=os.environ.get("OPS_SMTP_USER"),
        smtp_password=os.environ.get("OPS_SMTP_PASSWORD"),
        smtp_from=os.environ.get("OPS_SMTP_FROM"),
        smtp_to=os.environ.get("OPS_SMTP_TO"),
        heartbeat_url=os.environ.get("OPS_HEARTBEAT_URL"),
    )
