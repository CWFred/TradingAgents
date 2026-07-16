"""Memo-lite: one-shot memos for insider-sleeve entries, and their
mechanical resolution at exit.

The memo is a PASSENGER — it never gates or sizes a trade. Authoring runs
the following night inside the shared overnight ds4 bracket (never from the
post-close tick); a per-entry failure journals a note and moves on, leaving
the entry queued for the next night. Memos land in the INSIDER memo store
(spec decision 1: per-sleeve stores) with thesis_type="event",
event_type="insider_cluster" (already in the schema taxonomy), status
"open", vetting=None. Evidence cites the cluster's Form 4 accessions; the
falsifier restates the mechanical stop in the PR#31 drawdown convention.

This corpus is the control arm of the "what does the LLM funnel add?"
experiment: conviction language authored here is never consulted by
anything — calibration reporting can later compare it against the research
sleeve's vetted corpus.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from math import ceil

from pydantic import BaseModel, Field

from ops.activity import NullReporter
from ops.insider.clusters import CLUSTER_WINDOW_DAYS
from ops.insider.store import SignalStore
from ops.insider.trading import MAX_HOLD_CALENDAR_DAYS, STOP_PCT, TARGET_PCT
from ops.research.prices import fetch_price_context
from tradingagents.agents.utils.structured import bind_structured
from tradingagents.memos.schema import (
    EventThesis,
    EvidenceItem,
    Falsifier,
    Memo,
    Resolution,
    ReturnScenario,
)
from tradingagents.memos.store import MemoStore

BENCHMARK_SYMBOL = "IWM"  # Russell 2000 ETF: the small-cap benchmark


class MemoLiteDraft(BaseModel):
    """The one structured call's output. Everything else is code-owned."""

    thesis: str = Field(description="One paragraph: why this cluster matters here.")
    must_be_true: list[str] = Field(min_length=1)
    scenarios: list[ReturnScenario] = Field(default_factory=list)


MEMO_LITE_PROMPT = """\
{symbol} was bought by the insider sleeve on {asof} at ~{price} because \
{n_buyers} insiders ({buyers}) made open-market, non-10b5-1 purchases \
totalling ${agg_dollars} in the last {window} days.

Write a COMPACT memo annotation (the trade already happened; nothing you \
write changes it):
- thesis: one paragraph on why this specific cluster is informative here.
- must_be_true: the load-bearing assumptions, one sentence each.
- scenarios: 2-3 probability-weighted return branches over ~6 months. \
Calibration data only.
"""


def author_pending_memos(
    *,
    signal_store: SignalStore,
    memo_store: MemoStore,
    thesis_llm,
    thesis_model_spec: str = "",
    deadline: datetime | None = None,
    should_stop=None,
    now=None,
    price_fetcher=None,
    echo=lambda msg: None,
    reporter=None,
) -> int:
    """Author memos for entries that don't have one yet. Returns memos
    written. Deadline/stop-boxed like vet_pending: conditions are checked
    BEFORE each entry. Failures leave the entry queued and never raise."""
    price_fetcher = price_fetcher or fetch_price_context
    now_fn = now or (lambda: datetime.now(timezone.utc))
    reporter = reporter or NullReporter()
    structured = bind_structured(thesis_llm, MemoLiteDraft, "insider-memo-lite")
    if structured is None:
        echo("memo-lite skipped: no structured-output support")
        return 0

    written = 0
    for entry in signal_store.entries_without_memo():
        if should_stop is not None and should_stop():
            break
        if deadline is not None and now_fn() >= deadline:
            break
        symbol, asof = entry["symbol"], entry["asof"]
        try:
            with reporter.item("overnight", stage="authoring_memo",
                               symbol=symbol):
                memo = _author_one(
                    symbol=symbol, asof=asof, signal_store=signal_store,
                    structured=structured, price_fetcher=price_fetcher,
                    thesis_model_spec=thesis_model_spec,
                )
                memo_store.save(memo)
                signal_store.set_entry_memo(symbol, asof, memo.memo_id)
            written += 1
            echo(f"{symbol}: memo {memo.memo_id}")
        except Exception as exc:  # noqa: BLE001 - one bad entry must not strand the queue
            echo(f"{symbol}: memo-lite FAILED ({type(exc).__name__}: {exc})")
    return written


def _author_one(*, symbol, asof, signal_store, structured, price_fetcher,
                thesis_model_spec) -> Memo:
    buys = signal_store.buys_in_window(
        symbol, since=asof - timedelta(days=CLUSTER_WINDOW_DAYS), until=asof,
    )
    if not buys:
        raise ValueError("no cluster buys found for entry window")
    buyers = sorted({b["insider_name"] for b in buys})
    agg = sum((b["shares"] * b["price"] for b in buys
               if b["shares"] is not None and b["price"] is not None), Decimal("0"))
    ctx = price_fetcher(symbol)
    price = ctx.close_on_or_before(asof) if ctx is not None else None
    if price is None:
        raise ValueError(f"no reference price for {symbol} at {asof}")

    draft = structured.invoke(MEMO_LITE_PROMPT.format(
        symbol=symbol, asof=asof.isoformat(), price=price,
        n_buyers=len(buyers), buyers=", ".join(buyers),
        agg_dollars=agg, window=CLUSTER_WINDOW_DAYS,
    ))
    if draft is None:
        raise ValueError("structured call returned nothing")

    entry = float(price)
    strength_tier = "medium" if len(buyers) >= 3 else "starter"
    evidence = [
        EvidenceItem(
            claim=f"{b['insider_name']} bought "
                  f"{b['shares']} shares at {b['price']} on {b['transaction_date']}",
            source_type="filing", source_ref=b["accession"],
        )
        for b in buys
    ]
    return Memo(
        ticker=symbol, as_of_date=asof, thesis_type="event",
        thesis=draft.thesis,
        evidence=evidence,
        event_block=EventThesis(
            event_type="insider_cluster",
            seller_identity="open-market counterparties (buy-side signal; no forced seller)",
            why_non_economic=(
                "insiders bought with personal cash outside 10b5-1 plans — "
                "informed accumulation, not forced flow"
            ),
        ),
        conviction_tier=strength_tier,
        entry_price_ref=entry,
        # Mechanical band restating the sleeve's fixed exits — the sleeve's
        # trade step, not these numbers, is the real rulebook.
        price_target_low=entry * (1 + float(STOP_PCT)),
        price_target_high=entry * (1 + float(TARGET_PCT)),
        expected_holding_months=ceil(MAX_HOLD_CALENDAR_DAYS / 30),
        scenarios=draft.scenarios,
        must_be_true=draft.must_be_true,
        falsifiers=[Falsifier(
            description="mechanical stop: price 20% below entry",
            check_type="price", metric="drawdown_from_cost_pct",
            operator=">=", threshold=20.0,
        )],
        authored_by_model=thesis_model_spec,
        status="open",
    )


def resolve_on_exit(
    *,
    memo_store: MemoStore,
    memo_id: str,
    entry_price: Decimal,
    exit_price: Decimal,
    entry_date: date,
    exit_date: date,
    reason: str,
    benchmark_fetcher=None,
) -> None:
    """Mechanically resolve an insider memo when its position exits.

    outcome_label mapping: target -> thesis_right_made_money; stop ->
    thesis_wrong_lost_money (the pre-committed falsifier tripped); time ->
    by sign of the realized return (a pure signal sleeve has no process/
    outcome distinction to judge). Benchmark is IWM over the same window;
    unavailability degrades to 0.0 with a note, never a failure."""
    realized = float(exit_price / entry_price - 1)
    benchmark = 0.0
    note = ""
    try:
        fetcher = benchmark_fetcher or fetch_price_context
        ctx = fetcher(BENCHMARK_SYMBOL)
        b_entry = ctx.close_on_or_before(entry_date) if ctx is not None else None
        b_exit = ctx.close_on_or_before(exit_date) if ctx is not None else None
        if b_entry and b_exit:
            benchmark = float(b_exit / b_entry - 1)
        else:
            note = " (benchmark unavailable; recorded 0.0)"
    except Exception:  # noqa: BLE001 - benchmark is decoration, not a gate
        note = " (benchmark fetch failed; recorded 0.0)"

    if reason == "target":
        label = "thesis_right_made_money"
    elif reason == "stop":
        label = "thesis_wrong_lost_money"
    else:  # "time"
        label = "thesis_right_made_money" if realized > 0 else "thesis_wrong_lost_money"

    memo_store.resolve(memo_id, Resolution(
        resolved_at=datetime.now(timezone.utc),
        exit_price=float(exit_price),
        realized_return_pct=realized,
        benchmark_return_pct=benchmark,
        holding_days=(exit_date - entry_date).days,
        outcome_label=label,
        falsifiers_tripped=[0] if reason == "stop" else [],
        narrative=(
            f"mechanical exit: {reason}; realized {realized:+.1%} vs "
            f"{BENCHMARK_SYMBOL} {benchmark:+.1%}{note}"
        ),
    ))
