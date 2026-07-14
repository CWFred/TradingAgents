"""Short-thesis memo authoring — the bear authors, the bull defends.

Mirror of ops/research/brain.py's two-stage pipeline with inverted roles:
the counter-argument stage is a BULL defending the stock against the short
case (brain.py's bear attacks a long), and the memo prompt encodes the
short sleeve's inverted semantics — price_target_low is the cover target
(profit), price_target_high the thesis-wrong level, and falsifiers describe
IMPROVEMENT (the thesis breaking). Shared machinery (reading plan, evidence
stage, validation) is imported from brain.py, not duplicated. Memos land in
the SHORT memo store as pending_vetting; the graph adjudicates them with
the inverted confirm map (ops/research/vetting.SHORT_CONFIRM_TIERS).
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from ops.research.brain import (
    MIN_EVIDENCE_ITEMS,
    ResearchError,
    ResearchOutcome,
    _build_reading_plan,
    _evidence_bullets,
    _run_evidence_stage,
)
from ops.research.memo_validation import resolve_evidence, validate_memo
from ops.research.prices import fetch_price_context
from tradingagents.agents.utils.structured import bind_structured
from tradingagents.memos.schema import (
    Catalyst,
    ConvictionTier,
    Falsifier,
    Memo,
    ReturnScenario,
    ShortThesis,
)
from tradingagents.memos.store import MemoStore


class ShortMemoDraft(BaseModel):
    """What the model authors for a short. Code owns identity/evidence/pricing."""

    company_name: str = ""
    thesis_type: Literal["short"] = "short"
    thesis: str
    short_block: ShortThesis
    conviction_tier: ConvictionTier
    price_target_low: float
    price_target_high: float
    expected_holding_months: int = Field(ge=1)
    scenarios: list[ReturnScenario] = Field(default_factory=list)
    must_be_true: list[str] = Field(min_length=1)
    falsifiers: list[Falsifier] = Field(min_length=1)
    catalysts: list[Catalyst] = Field(default_factory=list)
    precedent_memo_ids: list[str] = Field(default_factory=list)
    recommendation: Literal["short", "pass"]


DEFENSE_PROMPT = """\
You are the BULL defending {ticker} against a short thesis. It tripped an \
expensive/deteriorating screen with a red-flag disclosure; the screen result \
and cited filing evidence are below.

First: state the single strongest SPECIFIC reason the shorts are wrong — \
name the actual support (backlog turning, one-time charge behind the optics, \
sticky customer base, balance-sheet cushion, an acquirer's floor).

Then: the 2-3 strongest defenses, each grounded in the evidence below. \
Squeeze mechanics count: note float, crowding, and what forces buyers in.

Screen result:
{screen_summary}

Evidence:
{evidence_bullets}
"""

SHORT_MEMO_PROMPT = """\
Write the SHORT memo for {ticker} as of {asof}. Reference price: {price}.

Rules:
- thesis_type is "short": fill short_block ONLY. overvaluation_mechanism \
MUST answer the bull defense below with a specific named reason, and every \
red_flag must be backed by a cited evidence item.
- PRICE TARGETS INVERT: price_target_low is the COVER target (the profit \
exit, below the current price); price_target_high is the thesis-wrong level \
above it.
- falsifiers describe IMPROVEMENT — the thesis breaking — and at least one \
MUST be machine-checkable (metric, operator, AND threshold; metric examples: \
gross_margin_pct, revenue_yoy_pct, net_debt_to_ebitda, \
drawdown_from_cost_pct). drawdown_from_cost_pct measures the ADVERSE move \
as a POSITIVE percent (the price rising against the short): use > or >= \
with e.g. threshold 20 (= squeezed 20% above entry). Pre-commit now; these \
are the cover rules.
- must_be_true: the load-bearing assumptions, one sentence each.
- precedent_memo_ids: ONLY ids from the past-memos list; empty if none \
apply — "none found" is an explicit, acceptable finding. Never invent ids.
- scenarios: probability-weighted branches; calibration data only, never \
sizing inputs.
- recommendation: "short" if you would open the position now, else "pass". \
Passed memos are shadow-tracked and scored later, so pass honestly. Shorts \
bleed carry — a right-but-early short is a wrong short.

Screen result:
{screen_summary}

Bull defense:
{bull_case}

Evidence (already validated; cite-able):
{evidence_bullets}

Past memos for {ticker}:
{past_memos}
{retry_feedback}
"""


def _short_screen_summary(payload: dict) -> str:
    """Short-screen payload (asdict(ShortScreenResult)) -> prompt text.
    The long _screen_summary reads cheap/quality/valuation_bars keys the
    short payload does not have."""
    lines = [f"{payload['symbol']} short-screened {payload['asof']}: "
             f"market_cap={payload['market_cap']} ev_ebit={payload['ev_ebit']}"]
    for bar in payload.get("bars", []):
        mark = "EXPENSIVE/DETERIORATING" if bar["passed"] else "ok"
        lines.append(f"  [{mark}] {bar['name']}: {bar['detail']}")
    for flag in payload.get("red_flags", []):
        lines.append(f"  red flag {flag['kind']} ({flag['date']}): {flag['description']}")
    return "\n".join(lines)


def research_short_hit(
    hit: dict,
    *,
    evidence_llm,
    thesis_llm,
    memo_store: MemoStore,
    list_filings=None,
    fetch_text=None,
    price_fetcher=None,
    today: date | None = None,
    thesis_model_spec: str | None = None,
) -> ResearchOutcome:
    """Run the full two-stage short pipeline for one pending short-screen hit."""
    from tradingagents.agents.utils.filing_reader_tools import summarize_memo
    from tradingagents.dataflows import edgar

    list_filings = list_filings or edgar.list_filings
    fetch_text = fetch_text or edgar.fetch_filing_text
    price_fetcher = price_fetcher or fetch_price_context
    today = today or date.today()
    symbol = hit["symbol"]
    payload = hit["payload"]
    outcome = ResearchOutcome(symbol=symbol, hit_id=hit["id"], status="failed")

    ctx = price_fetcher(symbol)
    price = ctx.close_on_or_before(today) if ctx is not None else None
    if price is None:
        outcome.errors.append(f"no reference price for {symbol} at {today}")
        return outcome

    # _build_reading_plan pulls trigger accessions from payload["triggers"];
    # the short payload calls them red_flags — same Trigger dict shape.
    reading_payload = {**payload, "triggers": payload.get("red_flags", [])}
    sections = _build_reading_plan(
        symbol, reading_payload, list_filings=list_filings, fetch_text=fetch_text,
    )
    if not sections:
        outcome.errors.append("no readable filings")
        return outcome

    raw_items, allowed_refs, notes = _run_evidence_stage(
        evidence_llm, sections, symbol=symbol,
    )
    outcome.errors.extend(notes)
    kept, dropped = resolve_evidence(raw_items, allowed_refs)
    outcome.evidence_kept, outcome.evidence_dropped = len(kept), len(dropped)
    if len(kept) < MIN_EVIDENCE_ITEMS:
        outcome.errors.append(
            f"insufficient cited evidence: {len(kept)} kept "
            f"(need {MIN_EVIDENCE_ITEMS}), {len(dropped)} dropped"
        )
        return outcome

    past = memo_store.list(ticker=symbol)
    known_precedents = {m.memo_id for m in past}
    past_memos_text = (
        "\n".join(summarize_memo(m) for m in past)
        if past else f"No past memos for {symbol}: none found."
    )
    screen_summary = _short_screen_summary(payload)
    evidence_bullets = _evidence_bullets(kept)

    bull = thesis_llm.invoke(DEFENSE_PROMPT.format(
        ticker=symbol, screen_summary=screen_summary,
        evidence_bullets=evidence_bullets,
    )).content

    structured = bind_structured(thesis_llm, ShortMemoDraft, "research-short-memo")
    if structured is None:
        raise ResearchError(
            "thesis model does not support structured output; "
            "set OPS_RESEARCH_THESIS_MODEL to a provider that does"
        )

    retry_feedback = ""
    for attempt in range(2):
        prompt = SHORT_MEMO_PROMPT.format(
            ticker=symbol, asof=today.isoformat(), price=price,
            screen_summary=screen_summary, bull_case=bull,
            evidence_bullets=evidence_bullets, past_memos=past_memos_text,
            retry_feedback=retry_feedback,
        )
        try:
            draft = structured.invoke(prompt)
        except Exception as exc:
            outcome.errors.append(f"memo emission failed (attempt {attempt + 1}): {exc}")
            draft = None
        if draft is None:
            retry_feedback = (
                "\nYour previous answer was not valid structured output. "
                "Emit the memo again, matching the schema exactly."
            )
            continue
        memo = Memo(
            ticker=symbol, as_of_date=today, entry_price_ref=float(price),
            evidence=kept, status="pending_vetting",
            authored_by_model=thesis_model_spec or "",
            **draft.model_dump(exclude={"recommendation"}),
        )
        errors = validate_memo(
            memo, allowed_refs=allowed_refs, known_precedents=known_precedents,
        )
        # Short-specific target sanity (review finding P3): an inverted band
        # would make the trade step cover as "target hit" on its first run.
        if memo.price_target_low >= memo.entry_price_ref:
            errors.append(
                f"price_target_low ({memo.price_target_low}) is the COVER "
                f"target and must sit BELOW the entry reference price "
                f"({memo.entry_price_ref}) for a short"
            )
        if memo.price_target_high <= memo.entry_price_ref:
            errors.append(
                f"price_target_high ({memo.price_target_high}) is the "
                f"thesis-wrong level and must sit ABOVE the entry reference "
                f"price ({memo.entry_price_ref}) for a short"
            )
        if not errors:
            memo_store.save(memo)
            if draft.recommendation == "pass":
                memo_store.mark_passed(memo.memo_id)
            outcome.status = "researched"
            outcome.memo_id = memo.memo_id
            outcome.recommendation = draft.recommendation
            return outcome
        outcome.errors.extend(errors)
        retry_feedback = (
            "\nYour previous memo was REJECTED for these reasons — fix each "
            "one exactly:\n" + "\n".join(f"- {e}" for e in errors)
        )
    return outcome
