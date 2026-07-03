"""Transport protocol and a disabled no-op fallback."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger("ops.notify")


@dataclass(frozen=True)
class NotifyMessage:
    title: str
    body: str
    urgency: str = "normal"  # "normal" | "high"


@runtime_checkable
class Transport(Protocol):
    @property
    def enabled(self) -> bool: ...

    def send(self, message: NotifyMessage) -> None: ...


class DisabledTransport:
    """Stands in for a transport whose credentials are missing. send() is a
    no-op so the dispatcher can treat a missing channel as 'nothing to do'
    rather than a delivery failure."""

    enabled = False

    def __init__(self, reason: str):
        self._reason = reason
        logger.info("notify transport disabled: %s", reason)

    def send(self, message: NotifyMessage) -> None:
        return None
