"""Structured investment-memo schema for long-horizon research.

The memo is the spine of the long-horizon fundamental strategy: every deep
research pass produces one, every position links back to one, and every sell
rule and monitoring check reads from one. Memos are also the system's training
signal — a memo written at entry with explicit falsifiable predictions, then
*resolved* against the realized outcome months later, is a clean supervised
example of good or bad reasoning.

Design decisions encoded here:

- **One schema, two thesis types.** A shared spine plus a ``thesis_type``
  discriminator with a type-specific block. A *value* thesis is anchored on
  earning power and monitored against quarterly fundamentals; an *event*
  thesis is anchored on a forced-seller mechanism and monitored against hard
  calendar dates. Everything downstream (monitoring cadence, sizing caps,
  outcome analysis) dispatches on ``thesis_type``, so it lives in one queryable
  corpus rather than two schemas.

- **Falsifiers are machine-checkable where possible.** A falsifier with
  ``metric``/``operator``/``threshold`` set can be evaluated mechanically by
  the monitoring loop; prose-only falsifiers require an LLM pass. Writing them
  down at entry is what makes the "thesis violation" sell rule enforceable.

- **Scenario probabilities are for calibration, NOT sizing.** LLM-stated
  probabilities are uncalibrated; sizing uses conviction tiers. Scenarios are
  recorded so that, once enough memos have resolved, stated probabilities can
  be compared against realized frequencies — only then may they inform sizing.

These are Pydantic models so agents can emit a memo directly through the
``with_structured_output`` path in ``agents/utils/structured.py``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

ThesisType = Literal["value", "event"]
ConvictionTier = Literal["starter", "medium", "high"]
# Lifecycle: the brain writes a buy thesis as ``pending_vetting``; the graph
# vetting stage adjudicates it to ``open`` (tradeable) or ``rejected`` (never
# traded, kept as corpus). ``passed`` = the brain itself declined to buy
# (shadow-tracked). Only ``open`` memos trade — graph confirmation is a
# required gate purely by construction.
MemoStatus = Literal["pending_vetting", "open", "rejected", "passed", "resolved"]

VettingVerdict = Literal["confirm", "reject"]

# Right/wrong process crossed with good/bad outcome. The off-diagonal labels
# are the ones that teach the most: "thesis_wrong_made_money" is luck, not
# skill, and must not be rewarded when the corpus is used as training signal.
OutcomeLabel = Literal[
    "thesis_right_made_money",
    "thesis_right_lost_money",
    "thesis_wrong_made_money",
    "thesis_wrong_lost_money",
]

EventType = Literal[
    "spinoff",
    "activist_13d",
    "insider_cluster",
    "tender_offer",
    "index_deletion",
    "post_bankruptcy",
    "merger_arb",
    "forced_sale_other",
]


class EvidenceItem(BaseModel):
    """A single claim grounded in a specific source document.

    Claims that cannot cite a source do not belong in a memo — the citation
    requirement is the structural defense against debate-stage confabulation.
    """

    claim: str = Field(description="One factual claim supporting or opposing the thesis.")
    source_type: Literal["filing", "transcript", "news", "price_data", "memo"] = Field(
        description="What kind of document the claim is grounded in."
    )
    source_ref: str = Field(
        description=(
            "Locator for the source: EDGAR accession number and section for filings, "
            "URL for news, memo_id for precedent memos."
        )
    )
    quote: str | None = Field(
        default=None, description="Short verbatim excerpt backing the claim, when available."
    )


class Falsifier(BaseModel):
    """A pre-committed condition under which the thesis is wrong.

    If ``metric``, ``operator`` and ``threshold`` are all set the condition is
    machine-checkable and the monitoring loop evaluates it without an LLM.
    """

    description: str = Field(description="Plain-language statement of the falsifying condition.")
    check_type: Literal["fundamental", "event", "price"] = Field(
        description=(
            "fundamental: checked against quarterly financials; event: checked against "
            "filing/deal state; price: checked against market data."
        )
    )
    metric: str | None = Field(
        default=None,
        description="Machine-checkable metric name, e.g. 'gross_margin_pct' or 'drawdown_from_cost_pct'.",
    )
    operator: Literal["<", "<=", ">", ">="] | None = Field(
        default=None, description="Comparison applied as: metric OPERATOR threshold."
    )
    threshold: float | None = Field(default=None, description="Trip threshold for the metric.")
    consecutive_periods: int = Field(
        default=1,
        ge=1,
        description="How many consecutive observation periods the condition must hold to trip.",
    )


class Catalyst(BaseModel):
    """An expected value-unlocking development, with timing when known."""

    description: str
    expected_date: date | None = Field(
        default=None, description="Best estimate of when the catalyst lands."
    )
    hard_date: bool = Field(
        default=False,
        description=(
            "True when the date is a committed calendar event (distribution date, tender "
            "expiration) rather than an estimate. Hard dates drive event-sleeve monitoring."
        ),
    )


class ReturnScenario(BaseModel):
    """A probability-weighted outcome branch. Calibration data only — see module docstring."""

    probability: float = Field(ge=0.0, le=1.0)
    return_pct: float = Field(description="Total return over the holding period, e.g. 0.8 = +80%.")
    description: str


class ValueThesis(BaseModel):
    """Type-specific block for a value + change-trigger thesis."""

    why_cheap: str = Field(
        description=(
            "The bear's explicit answer to why the market prices it here. A specific, named "
            "reason — 'risks include competition' is not an answer. If this cannot be "
            "articulated, the research is not done."
        )
    )
    change_trigger: str = Field(
        description="The reason to look NOW: CEO change, insider cluster, guidance-cut selloff, etc."
    )
    normalized_earnings_view: str = Field(
        description="Estimate of through-cycle earning power and how it differs from screen optics."
    )
    quality_assessment: str = Field(
        description="Moat, returns on capital, balance sheet — why this is not a value trap."
    )


class EventThesis(BaseModel):
    """Type-specific block for an event / forced-seller thesis."""

    event_type: EventType
    seller_identity: str = Field(
        description="Who is selling (index funds, spinoff recipients, estate, fund liquidation)."
    )
    why_non_economic: str = Field(
        description="Why the seller is not price-sensitive — the mispricing mechanism itself."
    )
    pressure_end_estimate: date | None = Field(
        default=None,
        description="When the forced selling is expected to exhaust (e.g. distribution + 2 quarters).",
    )
    key_dates: list[Catalyst] = Field(
        default_factory=list,
        description="Committed calendar events for the situation (record date, expiration, close).",
    )


class Resolution(BaseModel):
    """Filled when the memo's position is closed (or its shadow window lapses)."""

    resolved_at: datetime
    exit_price: float | None = Field(
        default=None, description="Realized exit; None for shadow-tracked 'passed' memos."
    )
    realized_return_pct: float = Field(
        description="Total return from entry reference to resolution, dividends included."
    )
    benchmark_return_pct: float = Field(
        description="Benchmark (e.g. Russell 2000 Value) return over the identical window."
    )
    holding_days: int
    outcome_label: OutcomeLabel
    falsifiers_tripped: list[int] = Field(
        default_factory=list, description="Indices into Memo.falsifiers that tripped."
    )
    catalysts_realized: list[int] = Field(
        default_factory=list, description="Indices into Memo.catalysts that played out."
    )
    narrative: str = Field(
        description="What actually happened, and whether the reasoning (not the outcome) was sound."
    )


class VettingResult(BaseModel):
    """Provenance of the graph-vetting adjudication (funnel stage 2).

    Report-time provenance only — never a sizing input (mirrors
    ``authored_by_model``). ``rating`` is the graph's native 5-tier word
    (Buy/Overweight/Hold/Underweight/Sell); the verdict/conviction mapping
    from that rating lives in ``ops/research/vetting.py``, not here.
    """

    verdict: VettingVerdict
    rating: str = Field(
        description="The graph's native 5-tier rating word that drove the verdict."
    )
    conviction_before: ConvictionTier = Field(
        description="The brain's conviction tier at vetting time."
    )
    conviction_after: ConvictionTier | None = Field(
        default=None,
        description="Graph-mapped conviction applied on confirm; None on reject.",
    )
    added_falsifier_indices: list[int] = Field(
        default_factory=list,
        description="Indices into Memo.falsifiers appended by the risk-debate extraction.",
    )
    rationale: str = Field(
        default="", description="Short judge-decision summary explaining the verdict."
    )
    vetted_by_model: str = Field(
        default="", description="Model spec of the graph that vetted this memo."
    )
    vetted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Memo(BaseModel):
    """A structured investment memo — the unit of research output and of learning."""

    memo_id: str = Field(default_factory=lambda: uuid4().hex)
    schema_version: int = 1
    ticker: str
    company_name: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    as_of_date: date = Field(
        description="Analysis date: only information available on/before this date may be used."
    )

    thesis_type: ThesisType
    thesis: str = Field(description="The core thesis in one paragraph.")
    evidence: list[EvidenceItem] = Field(min_length=1)
    value_block: ValueThesis | None = None
    event_block: EventThesis | None = None

    conviction_tier: ConvictionTier
    entry_price_ref: float = Field(description="Reference price at analysis time.")
    price_target_low: float
    price_target_high: float
    expected_holding_months: int = Field(ge=1)
    scenarios: list[ReturnScenario] = Field(
        default_factory=list,
        description="Probability-weighted outcomes. Calibration data only — never a sizing input.",
    )

    must_be_true: list[str] = Field(
        min_length=1, description="Things that need to be true for the thesis to work."
    )
    falsifiers: list[Falsifier] = Field(min_length=1)
    catalysts: list[Catalyst] = Field(default_factory=list)
    precedent_memo_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Resolved memos for similar past situations, consulted before this one was "
            "written. Empty means 'none found', which must be an explicit finding."
        ),
    )
    authored_by_model: str = Field(
        default="",
        description=(
            "Model spec (provider:model[@base_url]) of the thesis stage that "
            "authored this memo; empty for memos predating attribution. "
            "Report-time attribution only — never a sizing or monitoring input."
        ),
    )

    vetting: VettingResult | None = Field(
        default=None,
        description=(
            "Graph-vetting adjudication provenance; None for memos that have "
            "not been vetted (including pre-funnel memos). Never a sizing input."
        ),
    )

    status: MemoStatus = "open"
    resolution: Resolution | None = None

    def block_matches_type(self) -> bool:
        """True when the type-specific block matches ``thesis_type``."""
        if self.thesis_type == "value":
            return self.value_block is not None and self.event_block is None
        return self.event_block is not None and self.value_block is None
