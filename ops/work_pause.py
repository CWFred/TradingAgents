"""Backward-compatible pause leases for background model work.

Historically ``research.paused`` was an empty sentinel file and therefore an
indefinite pause.  Timed pauses use a small JSON payload in the same file so
old deployments, dashboards, and operator habits continue to work.  Readers
must treat malformed or empty files as indefinite (fail closed).
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class PauseState:
    paused: bool
    until: datetime | None = None
    reason: str | None = None

    @property
    def indefinite(self) -> bool:
        return self.paused and self.until is None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_pause_path() -> str:
    return os.path.join(
        os.path.expanduser(os.environ.get("XDG_STATE_HOME") or "~/.local/state"),
        "tradingagents", "research.paused",
    )


def pause_state(
    path: str | Path,
    *,
    now: datetime | None = None,
    cleanup_expired: bool = False,
) -> PauseState:
    """Return the effective pause state.

    Empty, legacy, and malformed files are indefinite pauses.  A valid timed
    lease becomes inactive at ``until``; the active worker may remove expired
    leases, while read-only status/dashboard callers leave the filesystem alone.
    """
    flag = Path(path)
    if not flag.exists():
        return PauseState(False)
    current = now or _utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("pause clock must be timezone-aware")
    try:
        raw = flag.read_text().strip()
        if not raw:
            return PauseState(True)
        payload = json.loads(raw)
        until_raw = payload.get("until")
        if not isinstance(until_raw, str):
            return PauseState(True)
        until = datetime.fromisoformat(until_raw)
        if until.tzinfo is None or until.utcoffset() is None:
            return PauseState(True)
        until = until.astimezone(timezone.utc)
        if current.astimezone(timezone.utc) < until:
            reason = payload.get("reason")
            return PauseState(
                True,
                until=until,
                reason=reason if isinstance(reason, str) else None,
            )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return PauseState(True)
    if cleanup_expired:
        with suppress(FileNotFoundError):
            flag.unlink()
    return PauseState(False)


def set_pause(
    path: str | Path,
    *,
    duration: timedelta | None = None,
    reason: str = "operator",
    now: datetime | None = None,
) -> PauseState:
    """Create or extend a pause atomically; never shorten an active lease."""
    if duration is not None and duration.total_seconds() <= 0:
        raise ValueError("pause duration must be positive")
    current = now or _utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("pause clock must be timezone-aware")
    flag = Path(path)
    flag.parent.mkdir(parents=True, exist_ok=True)
    existing = pause_state(flag, now=current)
    if existing.indefinite:
        return existing
    if duration is None:
        content = ""
        state = PauseState(True, reason=reason)
    else:
        until = current.astimezone(timezone.utc) + duration
        if existing.until is not None and existing.until >= until:
            return existing
        content = json.dumps(
            {
                "version": 1,
                "created_at": current.astimezone(timezone.utc).isoformat(),
                "until": until.isoformat(),
                "reason": reason,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        state = PauseState(True, until=until, reason=reason)
    temporary = flag.with_name(f".{flag.name}.{os.getpid()}.tmp")
    temporary.write_text(content)
    os.replace(temporary, flag)
    return state


def clear_pause(path: str | Path) -> bool:
    """Remove a pause lease, returning whether one existed."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return False
    return True


def request_immediate_pause(journal_path: str | Path) -> bool:
    """Ask a running ops daemon to interrupt all active inference now.

    The durable journal identifies the current service PID.  SIGURG is used
    because its default disposition is ignore: sending this from upgraded CLI
    code to an older daemon is therefore backward-compatible rather than
    process-terminating.  The daemon independently verifies that a pause lease
    exists before acting.
    """
    pause_signal = getattr(signal, "SIGURG", None)
    if pause_signal is None:
        return False

    from ops import events
    from ops.journal import Journal

    try:
        with Journal(str(journal_path), readonly=True) as journal:
            started = journal.last_event(events.KIND_SERVICE_STARTED)
            stopped = journal.last_event(events.KIND_SERVICE_STOPPING)
    except (OSError, ValueError, sqlite3.Error):
        return False
    if started is None:
        return False
    if stopped is not None and stopped["at"] > started["at"]:
        return False
    pid = started["payload"].get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, pause_signal)
    except (OSError, ValueError):
        return False
    return True
