"""The daily memo monitor (Phase C, build-order step 6).

Positions and memos are watched MECHANICALLY; humans get exceptions:

  - machine-checkable falsifiers evaluated against fresh prices/facts
    (ops/research/metrics.py — stateless, journal is the only memory);
  - a -30% drawdown escalates even when no falsifier trips;
  - lapsed hard-dated catalysts surface for event-sleeve memos;
  - due_for_resolution memos push the memo's exit checklist;
  - every escalation queues a re-research hit for the Phase B brain
    (ops research run picks it up) — the monitor NEVER invokes an LLM.

Notifications dedupe against the journal itself (count_events over a
RENOTIFY_DAYS window) so a tripped falsifier nags weekly, not daily.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ops import events
from ops.research.metrics import MetricContext, drawdown_pct, evaluate_falsifier

DRAWDOWN_ESCALATION_PCT = -30.0
RENOTIFY_DAYS = 7


@dataclass
class MonitorOutcome:
    asof: str
    memos_checked: int = 0
    falsifiers_evaluated: int = 0
    tripped: int = 0
    unevaluable: int = 0
    escalations: int = 0
    resolution_due: int = 0
    catalyst_due: int = 0
    errors: list[str] = field(default_factory=list)


def _escalation_payload(symbol: str, asof: date, reason: str) -> dict:
    """A ScreenResult-shaped payload for a monitoring escalation hit.

    The brain's _screen_summary bracket-indexes these exact keys — keep in
    lockstep with ops/research/screener.py's ScreenResult serialization.
    """
    return {
        "symbol": symbol, "asof": asof.isoformat(),
        "passed": True, "cheap": False, "quality": False,
        "valuation_bars": [], "quality_bars": [],
        "triggers": [{
            "kind": "monitor_escalation", "description": reason,
            "date": asof.isoformat(), "source": "monitor",
        }],
        "market_cap": None, "ev_ebit": None,
    }


def _recently_notified(journal, kind: str, *, now: datetime, **payload_keys: str) -> bool:
    """Already journaled within the re-notify window? The journal IS the
    dedupe state — no counters to lose on restart."""
    since = now - timedelta(days=RENOTIFY_DAYS)
    return journal.count_events(kind, since=since, payload_equals=payload_keys) > 0


def _checklist(memo) -> str:
    lines = [f"falsifier: {f.description}" for f in memo.falsifiers]
    lines.append(f"targets: low {memo.price_target_low} / high {memo.price_target_high}")
    lines.extend(f"must-be-true: {item}" for item in memo.must_be_true)
    return "\n".join(lines)


def _build_context(memo, *, today, price_fetcher, facts_fetcher, errors) -> MetricContext:
    """Fetch what this memo's falsifiers actually need — facts only when a
    machine-checkable fundamental falsifier exists, and never fatally."""
    price_ctx = price_fetcher(memo.ticker)
    fundamentals = None
    facts = None
    needs_facts = any(
        f.check_type == "fundamental" and f.metric and f.operator is not None
        and f.threshold is not None
        for f in memo.falsifiers
    )
    if needs_facts:
        try:
            from tradingagents.dataflows.fundamentals import compute_fundamentals

            facts = facts_fetcher(memo.ticker)
            fundamentals = compute_fundamentals(memo.ticker, facts, asof=today)
        except Exception as exc:  # degrade: fundamental checks go unevaluable
            errors.append(f"{memo.ticker}: facts unavailable ({exc})")
    return MetricContext(
        entry_price_ref=memo.entry_price_ref, asof=today,
        entry_era=memo.as_of_date, price_ctx=price_ctx,
        fundamentals=fundamentals, facts=facts,
    )


def _check_memo(memo, ctx, *, journal, screen_store, today, now, outcome) -> None:
    escalation_reasons: list[str] = []
    for i, falsifier in enumerate(memo.falsifiers):
        check = evaluate_falsifier(falsifier, ctx)
        outcome.falsifiers_evaluated += 1
        if check.status == "unevaluable":
            outcome.unevaluable += 1
            continue
        if check.status != "tripped":
            continue
        outcome.tripped += 1
        escalation_reasons.append(f"falsifier tripped: {check.detail}")
        if not _recently_notified(
            journal, events.KIND_FALSIFIER_TRIPPED, now=now,
            memo_id=memo.memo_id, falsifier_index=str(i),
        ):
            journal.record_event(
                events.KIND_FALSIFIER_TRIPPED,
                events.falsifier_tripped_payload(
                    memo_id=memo.memo_id, ticker=memo.ticker,
                    falsifier_index=str(i), description=falsifier.description,
                    metric=falsifier.metric or "",
                    observed=str(check.observed), threshold=str(falsifier.threshold),
                    consecutive_periods=falsifier.consecutive_periods,
                ),
                at=now,
            )

    dd = drawdown_pct(ctx)
    if dd is not None and dd <= DRAWDOWN_ESCALATION_PCT:
        escalation_reasons.append(f"drawdown {dd:.1f}% <= {DRAWDOWN_ESCALATION_PCT}%")

    if escalation_reasons:
        reason = "; ".join(escalation_reasons)
        hit_id = screen_store.enqueue_hit(
            memo.ticker, asof=today,
            payload=_escalation_payload(memo.ticker, today, reason),
        )
        if hit_id is not None:  # enqueue-dedupe doubles as notify-dedupe
            outcome.escalations += 1
            journal.record_event(
                events.KIND_RESEARCH_ESCALATION,
                events.research_escalation_payload(
                    ticker=memo.ticker, memo_id=memo.memo_id,
                    reason=reason, hit_id=hit_id,
                ),
                at=now,
            )

    if memo.thesis_type == "event":
        catalysts = list(memo.catalysts)
        if memo.event_block is not None:
            catalysts += list(memo.event_block.key_dates)
        for i, catalyst in enumerate(catalysts):
            if not (catalyst.hard_date and catalyst.expected_date
                    and catalyst.expected_date <= today):
                continue
            if _recently_notified(
                journal, events.KIND_CATALYST_DUE, now=now,
                memo_id=memo.memo_id, catalyst_index=str(i),
            ):
                continue
            outcome.catalyst_due += 1
            journal.record_event(
                events.KIND_CATALYST_DUE,
                events.catalyst_due_payload(
                    memo_id=memo.memo_id, ticker=memo.ticker,
                    catalyst_index=str(i), description=catalyst.description,
                    expected_date=catalyst.expected_date.isoformat(),
                ),
                at=now,
            )


def monitor_memos(
    *,
    memo_store,
    screen_store,
    journal,
    price_fetcher=None,
    facts_fetcher=None,
    today: date | None = None,
    now: datetime | None = None,
) -> MonitorOutcome:
    """One post-close pass over the open-memo book. Per-memo failures are
    recorded and skipped — one bad ticker must never blind the whole watch."""
    if price_fetcher is None:
        from ops.research.prices import fetch_price_context

        price_fetcher = fetch_price_context
    if facts_fetcher is None:
        from tradingagents.dataflows.edgar_facts import get_company_facts

        facts_fetcher = get_company_facts
    today = today or date.today()
    now = now or datetime.now(timezone.utc)
    outcome = MonitorOutcome(asof=today.isoformat())

    for memo in memo_store.open_memos():
        outcome.memos_checked += 1
        try:
            ctx = _build_context(
                memo, today=today, price_fetcher=price_fetcher,
                facts_fetcher=facts_fetcher, errors=outcome.errors,
            )
            _check_memo(memo, ctx, journal=journal, screen_store=screen_store,
                        today=today, now=now, outcome=outcome)
        except Exception as exc:  # noqa: BLE001 — one name never kills the loop
            outcome.errors.append(f"{memo.ticker}: {type(exc).__name__}: {exc}")

    for memo in memo_store.due_for_resolution(as_of=now):
        if _recently_notified(journal, events.KIND_RESOLUTION_DUE, now=now,
                              memo_id=memo.memo_id):
            continue
        outcome.resolution_due += 1
        elapsed = (now - memo.created_at).days
        journal.record_event(
            events.KIND_RESOLUTION_DUE,
            events.resolution_due_payload(
                memo_id=memo.memo_id, ticker=memo.ticker,
                thesis_type=memo.thesis_type, status=memo.status,
                expected_holding_months=memo.expected_holding_months,
                elapsed_days=elapsed, checklist=_checklist(memo),
            ),
            at=now,
        )

    journal.record_event(
        events.KIND_RESEARCH_MONITOR_RUN,
        events.research_monitor_run_payload(
            asof=outcome.asof, memos_checked=outcome.memos_checked,
            falsifiers_evaluated=outcome.falsifiers_evaluated,
            tripped=outcome.tripped, unevaluable=outcome.unevaluable,
            escalations=outcome.escalations, resolution_due=outcome.resolution_due,
            catalyst_due=outcome.catalyst_due, errors=outcome.errors,
        ),
        at=now,
    )
    return outcome
