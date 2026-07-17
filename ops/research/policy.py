"""Pure decision policies shared by live research trading and backtests."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ResearchExitDecision:
    """A mechanical research-sleeve exit, or ``None`` when the thesis holds."""

    reason: str
    rule: str


def evaluate_research_exit(
    *,
    memo,
    current_price: Decimal | None,
    falsifier_tripped: bool = False,
) -> ResearchExitDecision | None:
    """Return the first matching live research exit rule.

    Ordering is contractual and mirrors the historical live behavior: missing
    or resolved provenance wins over a falsifier, which wins over the target.
    A missing price only makes the target rule unevaluable; it never causes a
    liquidation.
    """
    if memo is None:
        return ResearchExitDecision(reason="memo missing", rule="memo_missing")
    if memo.status == "resolved":
        return ResearchExitDecision(reason="resolved", rule="memo_resolved")
    if falsifier_tripped:
        return ResearchExitDecision(reason="falsifier tripped", rule="falsifier")
    if (
        current_price is not None
        and current_price >= Decimal(str(memo.price_target_high))
    ):
        return ResearchExitDecision(reason="target hit", rule="target")
    return None
