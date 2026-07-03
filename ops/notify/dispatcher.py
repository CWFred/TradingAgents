"""Pull-based journal-event dispatcher. Reads events since a durable cursor
and routes each to the configured transports per the policy table. At-least-
once: on a transport failure the cursor is not advanced past the failed
event, so it is retried on the next call."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from ops.journal import Journal
from ops.notify.policy import POLICY, PolicyEntry, render
from ops.notify.transport import Transport

logger = logging.getLogger("ops.notify")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NotifyDispatcher:
    def __init__(
        self,
        journal: Journal,
        transports: dict[str, Transport],
        *,
        consumer: str = "notify",
        policy: dict[str, PolicyEntry] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self._journal = journal
        self._transports = transports
        self._consumer = consumer
        self._policy = policy if policy is not None else POLICY
        self._now = now if now is not None else _utcnow
        self._last_sent: dict[str, datetime] = {}

    def dispatch_once(self) -> int:
        cursor = self._journal.get_cursor(self._consumer)
        sent = 0
        for ev in self._journal.read_events_since(cursor):
            try:
                sent += self._handle(ev)
            except Exception as exc:  # transport failure — hold the cursor
                # Do NOT journal str(exc): transport exceptions from
                # smtplib/requests can embed credentials and hostnames. Only
                # the exception type name (plus event id/kind) is safe to
                # persist in the durable, potentially-shared journal.
                self._journal.record_event(
                    "notify_dispatch_error",
                    {"event_id": ev["id"], "kind": ev["kind"],
                     "error_type": type(exc).__name__},
                )
                logger.warning(
                    "notify dispatch failed at event %s (%s): %s",
                    ev["id"], type(exc).__name__, exc,
                )
                break
            self._journal.set_cursor(self._consumer, ev["id"])
        return sent

    def _handle(self, ev: dict) -> int:
        entry = self._policy.get(ev["kind"])
        if entry is None:
            return 0  # not notified; cursor still advances
        now = None
        if entry.cooldown_seconds is not None:
            last = self._last_sent.get(ev["kind"])
            now = self._now()
            if last is not None and (now - last).total_seconds() < entry.cooldown_seconds:
                return 0  # suppressed; cursor still advances
        message = render(ev["kind"], ev["payload"])
        sent = 0
        for channel in entry.channels:
            transport = self._transports.get(channel)
            if transport is None or not transport.enabled:
                continue
            transport.send(message)  # may raise -> caught by dispatch_once
            sent += 1
        # Arm the cooldown only after every send in the loop above completed
        # without raising. Arming it before sending would suppress the retry
        # of a FAILED send on the next dispatch_once (still inside the
        # cooldown window the failed attempt just armed), silently dropping
        # a throttled alert — an at-least-once violation.
        if entry.cooldown_seconds is not None:
            self._last_sent[ev["kind"]] = now
        return sent
