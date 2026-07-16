"""Deadline- and shutdown-boxed drain of the pending research queue.

Shared by `ops research run` (name-capped, manual) and the overnight
scheduler tick (deadline-boxed, unattended). Pure of backend lifecycle:
the caller brings ds4 up and tears it down around this call.

Stop conditions, checked BEFORE each name so a name already in flight
always finishes:
  1. should_stop() is true  (graceful shutdown requested)
  2. now() >= deadline       (08:00 wall-clock reached)
  3. the pending queue is empty
A ResearchError is a configuration problem and aborts the whole batch
(re-raised); any other per-name exception marks that hit failed and
continues — one bad name must not strand the queue.
"""
from __future__ import annotations

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


class _NameFailed(Exception):
    """Internal: routes a failed ResearchOutcome through the item context
    so the breadcrumb records ok=False without changing drain semantics."""

    def __init__(self, outcome):
        self.outcome = outcome


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
                outcome = research_fn(
                    hit, evidence_llm=evidence_llm, thesis_llm=thesis_llm,
                    memo_store=memo_store, thesis_model_spec=thesis_model_spec,
                )
                if outcome.status != "researched":
                    raise _NameFailed(outcome)
        except ResearchError:
            raise  # configuration problem: abort the whole batch
        except _NameFailed as nf:
            store.mark_failed(hit["id"])
            failed += 1
            echo(f"{nf.outcome.symbol}: FAILED — " + "; ".join(nf.outcome.errors))
            continue
        except Exception as exc:  # noqa: BLE001 - one bad name must not strand the queue
            store.mark_failed(hit["id"])
            failed += 1
            echo(f"{hit['symbol']}: FAILED ({type(exc).__name__}: {exc})")
            continue
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
