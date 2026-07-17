"""Backward-compatible pause leases for background model work.

Historically ``research.paused`` was an empty sentinel file and therefore an
indefinite pause.  Timed pauses use a small JSON payload in the same file so
old deployments, dashboards, and operator habits continue to work.  Readers
must treat malformed or empty files as indefinite (fail closed).
"""
from __future__ import annotations

import json
import os
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
    """Create an indefinite pause or a timed lease atomically."""
    if duration is not None and duration.total_seconds() <= 0:
        raise ValueError("pause duration must be positive")
    current = now or _utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("pause clock must be timezone-aware")
    flag = Path(path)
    flag.parent.mkdir(parents=True, exist_ok=True)
    if duration is None:
        content = ""
        state = PauseState(True, reason=reason)
    else:
        until = current.astimezone(timezone.utc) + duration
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
