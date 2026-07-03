"""Pushover push transport (synchronous, via requests)."""
from __future__ import annotations

import requests

from ops.notify.config import NotifyConfig
from ops.notify.transport import DisabledTransport, NotifyMessage, Transport

_API_URL = "https://api.pushover.net/1/messages.json"
_TIMEOUT = 10


class PushoverTransport:
    enabled = True

    def __init__(self, *, user_key: str, app_token: str):
        self._user_key = user_key
        self._app_token = app_token

    def send(self, message: NotifyMessage) -> None:
        data = {
            "token": self._app_token,
            "user": self._user_key,
            "title": message.title,
            "message": message.body,
            "priority": 1 if message.urgency == "high" else 0,
        }
        resp = requests.post(_API_URL, data=data, timeout=_TIMEOUT)
        resp.raise_for_status()


def build_push_transport(cfg: NotifyConfig) -> Transport:
    if cfg.pushover_user_key and cfg.pushover_app_token:
        return PushoverTransport(
            user_key=cfg.pushover_user_key, app_token=cfg.pushover_app_token,
        )
    return DisabledTransport("pushover: user_key/app_token not configured")
