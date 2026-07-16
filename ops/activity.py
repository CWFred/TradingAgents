"""ActivityReporter — journal-backed breadcrumbs for the live dashboard.

Emits activity_started/activity_finished pairs around ds4-holding work.
Deliberately best-effort: a breadcrumb must never break the work it
describes, so emit failures print to stderr and are swallowed; exceptions
from the wrapped body always re-raise after the ok=False finish."""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager

from ops import events


class ActivityHandle:
    """Mutable handle a job/item body can set an outcome summary on."""

    def __init__(self) -> None:
        self.outcome: str | None = None


class ActivityReporter:
    def __init__(self, journal) -> None:
        self._journal = journal

    def _emit(self, kind: str, payload: dict) -> None:
        try:
            self._journal.record_event(kind, payload)
        except Exception as exc:  # noqa: BLE001 - breadcrumbs are best-effort
            print(f"activity emit failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)

    @contextmanager
    def _scope(self, *, scope: str, job: str, stage: str | None = None,
               symbol: str | None = None, seq: str | None = None,
               reason: str | None = None):
        self._emit(events.KIND_ACTIVITY_STARTED, events.activity_started_payload(
            scope=scope, job=job, stage=stage, symbol=symbol, seq=seq,
            reason=reason,
        ))
        handle = ActivityHandle()
        t0 = time.monotonic()

        def finish(ok: bool) -> None:
            self._emit(
                events.KIND_ACTIVITY_FINISHED,
                events.activity_finished_payload(
                    scope=scope, job=job, stage=stage, symbol=symbol, seq=seq,
                    ok=ok, duration_s=round(time.monotonic() - t0, 3),
                    outcome=handle.outcome,
                ))

        try:
            yield handle
        except BaseException:
            finish(ok=False)
            raise
        finish(ok=True)

    def job(self, job: str, *, reason: str | None = None):
        return self._scope(scope="job", job=job, reason=reason)

    def item(self, job: str, *, stage: str, symbol: str | None = None,
             seq: str | None = None):
        return self._scope(scope="item", job=job, stage=stage, symbol=symbol,
                           seq=seq)


class NullReporter:
    """Default reporter: same interface, journals nothing."""

    @contextmanager
    def _noop(self):
        yield ActivityHandle()

    def job(self, job: str, *, reason: str | None = None):
        return self._noop()

    def item(self, job: str, *, stage: str, symbol: str | None = None,
             seq: str | None = None):
        return self._noop()
