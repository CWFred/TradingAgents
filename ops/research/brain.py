"""The research brain: pending screen hit -> validated structured memo.

Two-stage design for small context windows (spec Phase B):

  Stage 1 (evidence): one bounded structured-output call per filing section;
  every item must cite the section it came from; uncited/unresolvable items
  are stripped MECHANICALLY (resolve_evidence), not by prompt hope.

  Stage 2 (thesis): bear-case-first pass (why is it cheap — a specific,
  named reason), then memo emission through bind_structured into MemoDraft;
  code assembles the Memo and validate_memo gates storage. One retry with
  the validation errors fed back, then the hit is marked failed.

Deliberately NOT an agentic tool loop: local models loop on tools (see
docs/ds4-backend.md gotchas). The LLM only ever answers bounded prompts;
Python decides what gets read.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from ops.research.memo_validation import resolve_evidence, validate_memo
from ops.research.prices import fetch_price_context
from tradingagents.agents.utils.structured import bind_structured
from tradingagents.dataflows.edgar_sections import (
    FilingSection,
    SectionNotFound,
    diff_filing_sections,
    extract_section,
)
from tradingagents.memos.schema import (
    Catalyst,
    ConvictionTier,
    EventThesis,
    EvidenceItem,
    Falsifier,
    Memo,
    ReturnScenario,
    ThesisType,
    ValueThesis,
)
from tradingagents.memos.store import MemoStore

logger = logging.getLogger(__name__)

MIN_EVIDENCE_ITEMS = 3
MAX_TRIGGER_DOCS = 1
SECTION_MAX_CHARS = 12000
MAX_EVIDENCE_ITEMS_PER_SECTION = 8


class ResearchError(RuntimeError):
    """Configuration-level failure (not a per-name data problem)."""


class EvidenceBatch(BaseModel):
    items: list[EvidenceItem] = Field(default_factory=list)


class MemoDraft(BaseModel):
    """What the model authors. Code owns identity, evidence, and pricing."""

    company_name: str = ""
    thesis_type: ThesisType
    thesis: str
    value_block: ValueThesis | None = None
    event_block: EventThesis | None = None
    conviction_tier: ConvictionTier
    price_target_low: float
    price_target_high: float
    expected_holding_months: int = Field(ge=1)
    scenarios: list[ReturnScenario] = Field(default_factory=list)
    must_be_true: list[str] = Field(min_length=1)
    falsifiers: list[Falsifier] = Field(min_length=1)
    catalysts: list[Catalyst] = Field(default_factory=list)
    precedent_memo_ids: list[str] = Field(default_factory=list)
    recommendation: Literal["buy", "pass"]


EVIDENCE_PROMPT = """\
You are an equity research analyst reading ONE section of an SEC filing \
for {ticker}.

Section: {section} of accession {accession} (form {form}).
On EVERY item set source_type="filing" and source_ref="{source_ref}" \
exactly — items citing anything else are discarded.

Extract up to {max_items} evidence items bearing on:
- why the stock might be cheap, or why the cheapness is deserved,
- business quality: margins, returns on capital, balance sheet,
- what changed vs the prior year: new risks, changed numbers, dropped language,
- anything a bear would seize on.

Each item: ONE factual claim, with a short verbatim quote from the text. \
Only what this text supports — no opinions, no outside knowledge.

--- SECTION TEXT ---
{text}
"""

BEAR_PROMPT = """\
You are the bear on {ticker}. It passed a cheapness+quality screen; the \
screen result and cited filing evidence are below.

First: state the single most likely SPECIFIC reason the market prices \
{ticker} where it does. "Risks include competition" is not an answer — name \
the actual concern (segment in decline, customer concentration, fading \
one-time earnings, leverage, litigation, secular threat, value trap).

Then: the 2-3 strongest bear arguments, each grounded in the evidence below.

Screen result:
{screen_summary}

Evidence:
{evidence_bullets}
"""

MEMO_PROMPT = """\
Write the investment memo for {ticker} as of {asof}. Reference price: {price}.

Rules:
- thesis_type "value" (mispriced earning power) fills value_block ONLY; \
"event" (forced/non-economic seller) fills event_block ONLY. In a value \
memo, why_cheap MUST answer the bear case below with a specific named reason.
- falsifiers: at least one MUST be machine-checkable — metric, operator, \
AND threshold all set (metric examples: gross_margin_pct, revenue_yoy_pct, \
net_debt_to_ebitda, drawdown_from_cost_pct). Pre-commit now; these are the \
sell rules.
- must_be_true: the load-bearing assumptions, one sentence each.
- precedent_memo_ids: ONLY ids from the past-memos list; empty if none \
apply — "none found" is an explicit, acceptable finding. Never invent ids.
- scenarios: probability-weighted branches; calibration data only, never \
sizing inputs.
- recommendation: "buy" if you would open the position now, else "pass". \
Passed memos are shadow-tracked and scored later, so pass honestly.

Screen result:
{screen_summary}

Bear case:
{bear_case}

Evidence (already validated; cite-able):
{evidence_bullets}

Past memos for {ticker}:
{past_memos}
{retry_feedback}
"""


@dataclass
class ResearchOutcome:
    symbol: str
    hit_id: int
    status: str  # "researched" | "failed"
    memo_id: str | None = None
    recommendation: str | None = None
    errors: list[str] = field(default_factory=list)
    evidence_kept: int = 0
    evidence_dropped: int = 0


def _screen_summary(payload: dict) -> str:
    lines = [f"{payload['symbol']} screened {payload['asof']}: "
             f"cheap={payload['cheap']} quality={payload['quality']} "
             f"market_cap={payload['market_cap']} ev_ebit={payload['ev_ebit']}"]
    for bar in (*payload.get("valuation_bars", []), *payload.get("quality_bars", [])):
        mark = "PASS" if bar["passed"] else "fail"
        lines.append(f"  [{mark}] {bar['name']}: {bar['detail']}")
    for trig in payload.get("triggers", []):
        lines.append(f"  trigger {trig['kind']} ({trig['date']}): {trig['description']}")
    return "\n".join(lines)


def _evidence_bullets(items: list[EvidenceItem]) -> str:
    return "\n".join(
        f"- {i.claim} [{i.source_ref}]" + (f' "{i.quote}"' if i.quote else "")
        for i in items
    )


def _build_reading_plan(
    symbol: str, payload: dict, *, list_filings, fetch_text,
) -> list[FilingSection]:
    """Fetch each needed accession once; extract sections locally."""
    filings = list_filings(symbol, limit=200)
    by_accession = {f.accession_number: f for f in filings}
    ten_ks = [f for f in filings if f.form.startswith("10-K")]
    ten_qs = [f for f in filings if f.form.startswith("10-Q")]
    wanted: list[tuple[object, str]] = []  # (filing, section)
    if ten_ks:
        wanted += [(ten_ks[0], s) for s in ("mdna", "risk_factors", "business")]
    if ten_qs:
        wanted.append((ten_qs[0], "mdna"))
    trigger_accessions = [
        t["source"] for t in payload.get("triggers", []) if t["source"] != "price"
    ]
    for acc in trigger_accessions[:MAX_TRIGGER_DOCS]:
        if acc in by_accession:
            wanted.append((by_accession[acc], "full"))

    texts: dict[str, str] = {}
    sections: list[FilingSection] = []
    for filing, section in wanted:
        acc = filing.accession_number
        if acc not in texts:
            try:
                texts[acc] = fetch_text(filing)
            except Exception as exc:
                print(f"[research] {symbol}: fetch {acc} failed: {exc}", file=sys.stderr)
                texts[acc] = ""
        if not texts[acc]:
            continue
        try:
            body = extract_section(
                texts[acc], form=filing.form, section=section,
                max_chars=SECTION_MAX_CHARS,
            )
        except SectionNotFound as exc:
            print(f"[research] {symbol}: {exc}", file=sys.stderr)
            continue
        sections.append(FilingSection(
            ticker=symbol, accession=acc, section=section,
            form=filing.form, text=body,
        ))
    if len(ten_ks) >= 2:
        try:
            diff = diff_filing_sections(
                symbol, "mdna",
                (ten_ks[1].report_date or ten_ks[1].filing_date).year,
                (ten_ks[0].report_date or ten_ks[0].filing_date).year,
                max_chars=SECTION_MAX_CHARS,
                # Must honor the forms filter diff_filing_sections passes —
                # a raw `filings` passthrough would let 10-Qs into by_year.
                list_filings=lambda t, forms=None, **kw: [
                    f for f in filings if forms is None or f.form in forms
                ],
                fetch_text=lambda f, **kw: texts.get(f.accession_number) or fetch_text(f),
            )
            sections.append(FilingSection(
                ticker=symbol, accession=diff.source_ref.split(":")[0],
                section="mdna_diff", form="10-K", text=diff.text,
            ))
        except (SectionNotFound, KeyError) as exc:
            print(f"[research] {symbol}: mdna diff skipped: {exc}", file=sys.stderr)
    return sections


def _run_evidence_stage(
    evidence_llm, sections: list[FilingSection], *, symbol: str,
) -> tuple[list[EvidenceItem], set[str], list[str]]:
    structured = bind_structured(evidence_llm, EvidenceBatch, "research-evidence")
    if structured is None:
        raise ResearchError(
            "evidence model does not support structured output; "
            "set OPS_RESEARCH_EVIDENCE_MODEL to a provider that does"
        )
    items: list[EvidenceItem] = []
    notes: list[str] = []
    allowed_refs = {s.source_ref for s in sections}
    for section in sections:
        prompt = EVIDENCE_PROMPT.format(
            ticker=symbol, section=section.section, accession=section.accession,
            form=section.form, source_ref=section.source_ref,
            max_items=MAX_EVIDENCE_ITEMS_PER_SECTION, text=section.text,
        )
        try:
            batch = structured.invoke(prompt)
        except Exception as exc:
            notes.append(f"evidence call failed for {section.source_ref}: {exc}")
            continue
        if batch is None:
            notes.append(f"evidence call returned nothing for {section.source_ref}")
            continue
        items.extend(batch.items)
    return items, allowed_refs, notes


def research_hit(
    hit: dict,
    *,
    evidence_llm,
    thesis_llm,
    memo_store: MemoStore,
    list_filings=None,
    fetch_text=None,
    price_fetcher=None,
    today: date | None = None,
) -> ResearchOutcome:
    """Run the full two-stage pipeline for one pending screen hit."""
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

    sections = _build_reading_plan(
        symbol, payload, list_filings=list_filings, fetch_text=fetch_text,
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
    screen_summary = _screen_summary(payload)
    evidence_bullets = _evidence_bullets(kept)

    bear = thesis_llm.invoke(BEAR_PROMPT.format(
        ticker=symbol, screen_summary=screen_summary,
        evidence_bullets=evidence_bullets,
    )).content

    structured = bind_structured(thesis_llm, MemoDraft, "research-memo")
    if structured is None:
        raise ResearchError(
            "thesis model does not support structured output; "
            "set OPS_RESEARCH_THESIS_MODEL to a provider that does"
        )

    retry_feedback = ""
    for attempt in range(2):
        prompt = MEMO_PROMPT.format(
            ticker=symbol, asof=today.isoformat(), price=price,
            screen_summary=screen_summary, bear_case=bear,
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
            evidence=kept, status="open",
            **draft.model_dump(exclude={"recommendation"}),
        )
        errors = validate_memo(
            memo, allowed_refs=allowed_refs, known_precedents=known_precedents,
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
