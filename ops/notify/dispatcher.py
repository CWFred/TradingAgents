"""Pull-based journal-event dispatcher. Reads events since a durable cursor
and routes each to the configured transports per the policy table. At-least-
once: on a transport failure the cursor is not advanced past the failed
event, so it is retried on the next call."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from ops import events
from ops.journal import Journal
from ops.notify.policy import POLICY, PolicyEntry, render
from ops.notify.transport import Transport

logger = logging.getLogger("ops.notify")

# Maximum consecutive transport-failure retries per event before giving up.
_MAX_TRANSPORT_RETRIES = 10

# First-enable fast-forward (M2): on a consumer's FIRST-ever dispatch,
# events older than this are skipped instead of notified. A journal that
# predates the dispatcher deployment may hold weeks of history, and
# replaying it as pushes is a storm — but recent events (a startup_halted
# journaled moments before the first tick) must still be delivered, so AGE
# is the discriminator, never event count.
_FAST_FORWARD_MAX_AGE_S = 3600.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RenderError(Exception):
    """Raised when render() fails (KeyError, TypeError, etc.). The original
    exception is chained as __cause__."""


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
        # Consecutive transport-failure count per event id.
        self._fail_counts: dict[int, int] = {}

    def dispatch_once(self) -> int:
        cursor = self._journal.get_cursor(self._consumer)
        # First-enable fast-forward (M2): no cursor row yet means this
        # consumer has never dispatched. Skip past everything older than
        # the age cutoff; anything newer is dispatched normally below in
        # this same tick.
        if not self._journal.has_cursor(self._consumer):
            cutoff = self._now() - timedelta(seconds=_FAST_FORWARD_MAX_AGE_S)
            stale_id = self._journal.last_event_id_before(cutoff)
            if stale_id is not None and stale_id > cursor:
                self._journal.set_cursor(self._consumer, stale_id)
                self._journal.record_event(
                    events.KIND_NOTIFY_CURSOR_INITIALIZED,
                    events.notify_cursor_initialized_payload(
                        consumer=self._consumer, skipped_through=stale_id,
                    ),
                )
                cursor = stale_id
        sent = 0
        for ev in self._journal.read_events_since(cursor):
            try:
                sent += self._handle(ev)
            except RenderError as exc:
                # Compute-time failure: advance cursor, journal once. The
                # original exception rides on __cause__ (see _handle).
                cause_name = type(exc.__cause__).__name__ if exc.__cause__ else "unknown"
                self._journal.record_event(
                    events.KIND_NOTIFY_RENDER_ERROR,
                    events.notify_render_error_payload(
                        event_id=ev["id"], kind=ev["kind"],
                        error_type=cause_name,
                    ),
                )
                logger.warning(
                    "notify render error at event %s (%s): %s",
                    ev["id"], ev["kind"], cause_name,
                )
                self._journal.set_cursor(self._consumer, ev["id"])
            except Exception as exc:  # transport failure — hold the cursor
                self._journal.record_event(
                    events.KIND_NOTIFY_DISPATCH_ERROR,
                    events.notify_dispatch_error_payload(
                        event_id=ev["id"], kind=ev["kind"],
                        error_type=type(exc).__name__,
                    ),
                )
                logger.warning(
                    "notify dispatch failed at event %s (%s): %s",
                    ev["id"], type(exc).__name__, exc,
                )
                # Bounded retry: after N consecutive failures, skip.
                self._fail_counts[ev["id"]] = self._fail_counts.get(ev["id"], 0) + 1
                if self._fail_counts[ev["id"]] >= _MAX_TRANSPORT_RETRIES:
                    self._journal.record_event(
                        events.KIND_NOTIFY_EVENT_SKIPPED,
                        events.notify_event_skipped_payload(
                            event_id=ev["id"], kind=ev["kind"],
                            consecutive_failures=self._fail_counts[ev["id"]],
                        ),
                    )
                    logger.warning(
                        "notify event %s (%s) skipped after %d consecutive failures",
                        ev["id"], ev["kind"], self._fail_counts[ev["id"]],
                    )
                    del self._fail_counts[ev["id"]]
                    self._journal.set_cursor(self._consumer, ev["id"])
                break
            self._journal.set_cursor(self._consumer, ev["id"])
            # Reset fail count on success.
            self._fail_counts.pop(ev["id"], None)
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
        try:
            message = render(ev["kind"], ev["payload"])
        except Exception as exc:
            raise RenderError() from exc
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
