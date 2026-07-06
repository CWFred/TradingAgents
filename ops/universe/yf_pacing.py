"""Pacing + retry + failure counting for batch yfinance I/O.

On 2026-07-06 a transient Yahoo degradation (rate-limiting under ~500 rapid
calls at the open) made yfinance raise KeyError for every symbol; the
per-name skip handlers correctly ate the errors and the day's universe was
silently empty. This module is the single choke point that fixes all three
aspects: a process-global minimum interval keeps sweeps under Yahoo's
rate limits, transient failures retry with backoff, and ok/failed counters
feed the universe_diagnostics journal event so blindness becomes a
measurable, alertable condition instead of stderr noise.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

MIN_INTERVAL_SECONDS = 0.15
BACKOFF_SECONDS = (5.0, 25.0)
# Circuit breaker: after this many CONSECUTIVE failed calls the outage is
# systemic, not transient — later calls fail fast (one attempt, no backoff)
# so a dead feed costs ~0.15s/name instead of ~30s/name across a sweep.
# Failures keep counting either way; the blind alarm fires hours sooner.
BREAKER_CONSECUTIVE_FAILURES = 5

_lock = threading.Lock()
_last_call_at = 0.0
_counters: dict[str, list[int]] = {}  # label -> [ok, failed]
_consecutive_failures = 0


def call_paced(
    fn: Callable[[], T],
    *,
    label: str,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> T:
    """Run ``fn`` under the global pace; retry transient failures.

    Re-raises the last exception once retries are exhausted. Counts one
    ok/failed per COMPLETED call — a retry that eventually succeeds is ok.
    """
    global _last_call_at
    with _lock:
        breaker_open = _consecutive_failures >= BREAKER_CONSECUTIVE_FAILURES
    attempts = 1 if breaker_open else len(BACKOFF_SECONDS) + 1
    last_exc: Exception | None = None
    for attempt in range(attempts):
        with _lock:
            wait = MIN_INTERVAL_SECONDS - (monotonic() - _last_call_at)
        if wait > 0:
            sleep(wait)
        with _lock:
            _last_call_at = monotonic()
        try:
            result = fn()
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                sleep(BACKOFF_SECONDS[attempt])
                continue
            _count(label, ok=False)
            raise
        _count(label, ok=True)
        return result
    raise last_exc  # pragma: no cover - loop always returns or raises


def _count(label: str, *, ok: bool) -> None:
    global _consecutive_failures
    with _lock:
        bucket = _counters.setdefault(label, [0, 0])
        bucket[0 if ok else 1] += 1
        _consecutive_failures = 0 if ok else _consecutive_failures + 1


def snapshot_and_reset() -> dict[str, dict[str, int]]:
    """Counters since the last snapshot, then cleared — one cycle's worth.
    Also closes the breaker: each cycle starts with fresh retries."""
    global _consecutive_failures
    with _lock:
        snap = {k: {"ok": v[0], "failed": v[1]} for k, v in _counters.items()}
        _counters.clear()
        _consecutive_failures = 0
    return snap
