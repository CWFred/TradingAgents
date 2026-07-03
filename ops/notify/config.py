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


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def load_notify_config() -> NotifyConfig:
    port_raw = os.environ.get("OPS_SMTP_PORT")
    return NotifyConfig(
        notify_enabled=_env_bool("OPS_NOTIFY_ENABLED"),
        pushover_user_key=os.environ.get("OPS_PUSHOVER_USER_KEY"),
        pushover_app_token=os.environ.get("OPS_PUSHOVER_APP_TOKEN"),
        smtp_host=os.environ.get("OPS_SMTP_HOST"),
        smtp_port=int(port_raw) if port_raw else 587,
        smtp_user=os.environ.get("OPS_SMTP_USER"),
        smtp_password=os.environ.get("OPS_SMTP_PASSWORD"),
        smtp_from=os.environ.get("OPS_SMTP_FROM"),
        smtp_to=os.environ.get("OPS_SMTP_TO"),
    )
