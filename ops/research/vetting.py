"""Graph vetting of brain-authored memos (funnel stage 2).

The brain researches; the graph decides. Each pending_vetting memo (a
brain-buy by construction) is distilled into a deterministic brief,
injected into the multi-agent graph as ``research_memo_context``, and
adjudicated from the graph's NATIVE 5-tier rating — no extra LLM call and
no agent-prompt change decide the verdict:

    Buy        -> confirm, conviction high
    Overweight -> confirm, conviction medium
    anything else (Hold/Underweight/Sell/unparseable) -> reject

The mapping lives here, in code — no agent ever learns the
starter/medium/high taxonomy. One additional bounded structured call
extracts machine-checkable falsifiers from the risk debate; each candidate
must pass the same mechanical validity gate brain falsifiers face
(metric+operator+threshold), so the debate can only ADD monitorable exit
conditions. Extraction is additive: its failure never blocks a confirm and
never stores garbage.

Mirrors ops/research/drain.py's deadline/shutdown-boxed loop: stop
conditions are checked BEFORE each memo so a graph run in flight always
finishes; a per-memo failure leaves the memo pending_vetting (retried next
night) and never raises out of the loop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ops.research.memo_brief import build_research_brief
from ops.research.memo_validation import (
    drawdown_convention_problem,
    is_machine_checkable,
)
from tradingagents.agents.utils.structured import bind_structured
from tradingagents.memos.schema import (
    ConvictionTier,
    Falsifier,
    Memo,
    VettingResult,
)

# Native graph rating -> conviction tier on confirm. Ratings absent from
# this map reject. THE strictness knob: a stricter policy would drop
# "Overweight" (spec default is the two-row table).
CONFIRM_TIERS: dict[str, ConvictionTier] = {"Buy": "high", "Overweight": "medium"}

# Inverted map for the short sleeve: the graph rates the STOCK (its brief is
# neutral, no prompt changes) — a bearish rating is what confirms a short.
SHORT_CONFIRM_TIERS: dict[str, ConvictionTier] = {"Sell": "high", "Underweight": "medium"}

MAX_DEBATE_CHARS = 12000
MAX_RATIONALE_CHARS = 2000


class FalsifierBatch(BaseModel):
    items: list[Falsifier] = Field(default_factory=list)


FALSIFIER_PROMPT = """\
You just watched a risk-management debate about buying {ticker}. Extract the
risk team's concerns as MACHINE-CHECKABLE exit conditions (falsifiers) for
the position. Rules:
- Each item MUST set metric, operator, AND threshold (metric examples:
  gross_margin_pct, revenue_yoy_pct, net_debt_to_ebitda,
  drawdown_from_cost_pct). Items without all three are discarded.
- drawdown_from_cost_pct is a POSITIVE percent below cost: use > or >=
  with a threshold like 25 (= down 25% from entry). Other forms are
  discarded.
- check_type: "fundamental" for quarterly financials, "price" for market
  data, "event" for filing/deal state.
- Only conditions actually argued in the debate below — no inventions.
- At most 3 items; return an empty list if the debate raised nothing
  machine-checkable.

Risk debate:
{history}

Risk judge's decision:
{judge}
"""


@dataclass
class VetOutcome:
    ticker: str
    memo_id: str
    verdict: str  # "confirm" | "reject"
    rating: str
    added_falsifiers: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VettingSummary:
    vetted: int
    confirmed: int
    rejected: int
    failed: int
    still_pending: int
    hit_deadline: bool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def extract_risk_falsifiers(
    falsifier_llm, final_state: dict, *, ticker: str,
) -> tuple[list[Falsifier], list[str]]:
    """One bounded structured pass over the risk debate -> validated falsifiers.

    Returns (kept, notes). Never raises: any failure returns ([], [note]) so
    the caller confirms with the brain's falsifiers alone.
    """
    try:
        structured = bind_structured(falsifier_llm, FalsifierBatch, "research-vetting-falsifiers")
    except Exception as exc:  # noqa: BLE001 - enrichment must never block a confirm
        return [], [f"falsifier extraction failed: {type(exc).__name__}: {exc}"]
    if structured is None:
        return [], ["falsifier extraction skipped: no structured-output support"]
    risk = final_state.get("risk_debate_state") or {}
    history = (risk.get("history") or "")[:MAX_DEBATE_CHARS]
    judge = (risk.get("judge_decision") or "")[:MAX_RATIONALE_CHARS]
    if not history and not judge:
        return [], ["falsifier extraction skipped: empty risk debate"]
    try:
        batch = structured.invoke(
            FALSIFIER_PROMPT.format(ticker=ticker, history=history, judge=judge)
        )
    except Exception as exc:  # noqa: BLE001 - enrichment must never block a confirm
        return [], [f"falsifier extraction failed: {type(exc).__name__}: {exc}"]
    if batch is None:
        return [], ["falsifier extraction returned no structured output"]
    kept = [
        f for f in batch.items
        if is_machine_checkable(f) and drawdown_convention_problem(f) is None
    ]
    notes = []
    dropped = len(batch.items) - len(kept)
    if dropped:
        notes.append(
            f"dropped {dropped} non-machine-checkable or "
            f"non-canonical falsifier(s)"
        )
    return kept, notes


def vet_memo(
    memo: Memo, *, adapter, falsifier_llm, memo_store, vetted_by_model: str = "",
    confirm_tiers: dict[str, ConvictionTier] = CONFIRM_TIERS,
) -> VetOutcome:
    """Run the graph over one memo and persist the adjudication.

    ``confirm_tiers`` selects the rating->tier map: the default long map, or
    SHORT_CONFIRM_TIERS when adjudicating short memos.
    """
    brief = build_research_brief(memo)
    result = adapter.propagate(memo.ticker, memo.as_of_date, research_context=brief)
    rating = (result.rating or "").strip()
    rationale = str(result.raw.get("final_trade_decision", ""))[:MAX_RATIONALE_CHARS]
    tier = confirm_tiers.get(rating)
    outcome = VetOutcome(
        ticker=memo.ticker, memo_id=memo.memo_id, verdict="reject", rating=rating,
    )

    if tier is None:
        memo.status = "rejected"
        memo.vetting = VettingResult(
            verdict="reject", rating=rating,
            conviction_before=memo.conviction_tier, conviction_after=None,
            rationale=rationale, vetted_by_model=vetted_by_model,
        )
        memo_store.apply_vetting(memo)
        return outcome

    added, notes = extract_risk_falsifiers(
        falsifier_llm, result.raw, ticker=memo.ticker,
    )
    if notes:
        rationale = (rationale + "\n[vetting] " + "; ".join(notes))[:MAX_RATIONALE_CHARS + 500]
    indices = list(range(len(memo.falsifiers), len(memo.falsifiers) + len(added)))
    conviction_before = memo.conviction_tier
    memo.falsifiers = memo.falsifiers + added
    memo.conviction_tier = tier
    memo.status = "open"
    memo.vetting = VettingResult(
        verdict="confirm", rating=rating,
        conviction_before=conviction_before, conviction_after=tier,
        added_falsifier_indices=indices, rationale=rationale,
        vetted_by_model=vetted_by_model,
    )
    memo_store.apply_vetting(memo)
    outcome.verdict = "confirm"
    outcome.added_falsifiers = len(added)
    outcome.notes = notes
    return outcome


def vet_pending(
    *,
    memo_store,
    adapter,
    falsifier_llm,
    vetted_by_model: str,
    deadline: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
    now: Callable[[], datetime] = _utcnow,
    echo: Callable[[str], None] = lambda msg: None,
    confirm_tiers: dict[str, ConvictionTier] = CONFIRM_TIERS,
) -> VettingSummary:
    """Vet the pending_vetting queue oldest-first until deadline/stop/empty."""
    memos = memo_store.pending_vetting_memos()
    vetted = confirmed = rejected = failed = 0
    hit_deadline = False
    for memo in memos:
        if should_stop is not None and should_stop():
            break
        if deadline is not None and now() >= deadline:
            hit_deadline = True
            break
        try:
            outcome = vet_memo(
                memo, adapter=adapter, falsifier_llm=falsifier_llm,
                memo_store=memo_store, vetted_by_model=vetted_by_model,
                confirm_tiers=confirm_tiers,
            )
        except Exception as exc:  # noqa: BLE001 - one bad name must not strand the queue
            failed += 1
            echo(f"{memo.ticker}: FAILED ({type(exc).__name__}: {exc})")
            continue
        vetted += 1
        if outcome.verdict == "confirm":
            confirmed += 1
        else:
            rejected += 1
        echo(
            f"{outcome.ticker}: {outcome.verdict} (rating {outcome.rating}; "
            f"+{outcome.added_falsifiers} falsifiers)"
        )
    return VettingSummary(
        vetted=vetted, confirmed=confirmed, rejected=rejected, failed=failed,
        still_pending=len(memo_store.pending_vetting_memos()),
        hit_deadline=hit_deadline,
    )
