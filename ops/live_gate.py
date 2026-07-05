"""First-N live-fills gate helpers: a one-time paper->robinhood marker event
and a counter of live BUY fills since that marker. Used to enforce
LIVE_MAX_POSITION on early live trading (spec Graduation criteria #5)."""
from __future__ import annotations

from datetime import datetime

from ops import events
from ops.journal import Journal

# Kept as a module alias for existing importers; the canonical constant
# lives in ops.events with the other kind contracts.
MARKER_KIND = events.KIND_BROKER_MODE_LIVE


def record_flip_marker(journal: Journal) -> bool:
    if journal.first_event_at(MARKER_KIND) is not None:
        return False
    journal.record_event(MARKER_KIND, events.broker_mode_live_payload())
    return True


def flip_epoch(journal: Journal) -> datetime | None:
    return journal.first_event_at(MARKER_KIND)


def count_live_buy_fills(journal: Journal) -> int:
    """Count BUY fills after the flip marker that occurred in live mode.

    Historical fills (without a ``broker_mode`` key) are excluded —
    fail-safe: the gate stays active rather than lifting early. Runs as a
    single SQL COUNT (L2) — this is evaluated on every BUY through the
    rule chain, and a Python-side scan of the whole events table would
    creep quadratic as the journal grows.
    """
    epoch = flip_epoch(journal)
    if epoch is None:
        return 0
    return journal.count_events(
        events.KIND_FILL, since=epoch,
        payload_equals={"side": "BUY", "broker_mode": "robinhood"},
    )
