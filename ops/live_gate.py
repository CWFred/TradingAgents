"""First-N live-fills gate helpers: a one-time paper->robinhood marker event
and a counter of live BUY fills since that marker. Used to enforce
LIVE_MAX_POSITION on early live trading (spec Graduation criteria #5)."""
from __future__ import annotations

from datetime import datetime

from ops.journal import Journal

MARKER_KIND = "broker_mode_live"


def record_flip_marker(journal: Journal) -> bool:
    if any(e["kind"] == MARKER_KIND for e in journal.read_events()):
        return False
    journal.record_event(MARKER_KIND, {"note": "paper->robinhood flip"})
    return True


def flip_epoch(journal: Journal) -> datetime | None:
    markers = [e for e in journal.read_events() if e["kind"] == MARKER_KIND]
    return markers[0]["at"] if markers else None


def count_live_buy_fills(journal: Journal) -> int:
    epoch = flip_epoch(journal)
    if epoch is None:
        return 0
    return sum(
        1
        for e in journal.read_events()
        if e["kind"] == "fill"
        and e["at"] >= epoch
        and e["payload"].get("side") == "BUY"
    )
