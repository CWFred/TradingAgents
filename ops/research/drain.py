"""Deadline- and shutdown-boxed drain of the pending research queue.

Shared by `ops research run` (name-capped, manual) and the overnight
scheduler tick (deadline-boxed, unattended). Pure of backend lifecycle:
the caller brings ds4 up and tears it down around this call.

Stop conditions are checked before each name.  If an in-flight model call is
interrupted by the operator pause control, the stop condition is checked again
and that name remains pending for resume:
  1. should_stop() is true  (graceful shutdown requested)
  2. now() >= deadline       (08:00 wall-clock reached)
  3. the pending queue is empty
A ResearchError is a configuration problem and aborts the whole batch
(re-raised, never retried — retrying can't fix a config problem). Any other
exception from a single name is retried up to MAX_ATTEMPTS_PER_HIT times
with a short backoff (transient/resource errors, like the fd-exhaustion
incident, are exactly what this catches); once exhausted, or if a name
comes back with a clean "failed" ResearchOutcome (a deterministic
conclusion, never retried), that hit is marked failed and the drain
continues — one bad name must not strand the queue.
"""
from __future__ import annotations

import gc
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from ops.activity import NullReporter
from ops.research.brain import ResearchError, research_hit


@dataclass(frozen=True)
class DrainSummary:
    researched: int
    failed: int
    still_pending: int
    hit_deadline: bool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


MAX_ATTEMPTS_PER_HIT = 3
RETRY_BACKOFF_SECONDS = (2, 5)  # delay before attempt 2 and attempt 3


class _NameFailed(Exception):
    """Internal: routes a failed ResearchOutcome through the item context
    so the breadcrumb records ok=False without changing drain semantics."""

    def __init__(self, outcome):
        self.outcome = outcome


def _research_with_retries(
    research_fn, hit, *, evidence_llm, thesis_llm, memo_store,
    thesis_model_spec, should_stop, deadline, now, echo,
):
    """Try one hit up to MAX_ATTEMPTS_PER_HIT times, retrying only on a
    raised exception (transient/resource errors) with a short backoff
    between attempts. A ResearchError propagates immediately, unretried —
    it's a config problem shared by every name. A clean "failed"
    ResearchOutcome also returns immediately, unretried — it's a
    deterministic conclusion (e.g. insufficient evidence) that will not
    change on retry. should_stop() and the deadline are both re-checked
    before each backoff so an interruption mid-retry leaves the hit
    pending, same as an interruption between hits."""
    last_exc: Exception | None = None
    for attempt in range(MAX_ATTEMPTS_PER_HIT):
        if attempt > 0:
            if should_stop is not None and should_stop():
                break
            if deadline is not None and now() >= deadline:
                break
            delay = RETRY_BACKOFF_SECONDS[attempt - 1]
            echo(
                f"{hit['symbol']}: attempt {attempt} failed "
                f"({last_exc}); retrying in {delay}s..."
            )
            time.sleep(delay)
        try:
            outcome = research_fn(
                hit, evidence_llm=evidence_llm, thesis_llm=thesis_llm,
                memo_store=memo_store, thesis_model_spec=thesis_model_spec,
            )
        except ResearchError:
            raise
        except Exception as exc:  # noqa: BLE001 - retried, then reported by the caller
            last_exc = exc
            continue
        return outcome  # success OR a clean "failed" outcome — not retried
    raise last_exc


def drain_pending(
    *,
    store,
    memo_store,
    evidence_llm,
    thesis_llm,
    thesis_model_spec: str,
    max_names: int | None = None,
    deadline: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
    now: Callable[[], datetime] = _utcnow,
    echo: Callable[[str], None] = lambda msg: None,
    research_fn: Callable | None = None,
    reporter=None,
    activity_job: str = "overnight",
) -> DrainSummary:
    """``research_fn`` selects the memo author: the default long-thesis
    research_hit (resolved at call time, so tests patching the module
    attribute still take effect), or short_brain.research_short_hit when
    draining the short screen's queue (same contract, same ResearchOutcome)."""
    if research_fn is None:
        research_fn = research_hit
    reporter = reporter or NullReporter()
    hits = store.pending_hits()
    if max_names is not None:
        hits = hits[:max_names]

    researched = failed = 0
    hit_deadline = False
    total = len(hits)
    for i, hit in enumerate(hits):
        if should_stop is not None and should_stop():
            break
        if deadline is not None and now() >= deadline:
            hit_deadline = True
            break
        try:
            with reporter.item(activity_job, stage="researching",
                               symbol=hit["symbol"], seq=f"{i + 1}/{total}"):
                outcome = _research_with_retries(
                    research_fn, hit, evidence_llm=evidence_llm,
                    thesis_llm=thesis_llm, memo_store=memo_store,
                    thesis_model_spec=thesis_model_spec,
                    should_stop=should_stop, deadline=deadline, now=now,
                    echo=echo,
                )
                if outcome.status != "researched":
                    raise _NameFailed(outcome)
        except ResearchError:
            raise  # configuration problem: abort the whole batch
        except _NameFailed as nf:
            if should_stop is not None and should_stop():
                break
            if deadline is not None and now() >= deadline:
                hit_deadline = True
                break
            store.mark_failed(hit["id"])
            failed += 1
            echo(f"{nf.outcome.symbol}: FAILED — " + "; ".join(nf.outcome.errors))
            continue
        except Exception as exc:  # noqa: BLE001 - one bad name must not strand the queue
            if should_stop is not None and should_stop():
                break
            if deadline is not None and now() >= deadline:
                hit_deadline = True
                break
            store.mark_failed(hit["id"])
            failed += 1
            echo(f"{hit['symbol']}: FAILED ({type(exc).__name__}: {exc})")
            continue
        finally:
            # Each item may create library thread-local HTTP/cache state.  A
            # full reap at the item boundary prevents an uninterrupted weekend
            # drain from accumulating descriptors faster than the hourly
            # daemon-level collector can release them.
            gc.collect()
        store.mark_researched(hit["id"])
        researched += 1
        echo(
            f"{outcome.symbol}: memo {outcome.memo_id} "
            f"({outcome.recommendation}; evidence {outcome.evidence_kept} kept"
            f"/{outcome.evidence_dropped} dropped)"
        )

    return DrainSummary(
        researched=researched, failed=failed,
        still_pending=len(store.pending_hits()), hit_deadline=hit_deadline,
    )
