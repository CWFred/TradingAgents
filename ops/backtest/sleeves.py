"""Adaptable four-piece sleeve contracts and the research-sleeve binding."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from ops.backtest.models import BacktestCase, DecisionAction, PriceBar
from ops.backtest.replay import ExitEvaluation, FalsifierEvaluation, InitialDecision
from ops.research.metrics import MetricContext, evaluate_falsifier
from ops.research.policy import evaluate_research_exit
from ops.research.prices import PriceContext
from ops.research.sizing import SizingDecision, size_entry


class CaseSource(Protocol):
    def cases(self, *, start: date, end: date, target: int) -> Sequence[BacktestCase]: ...


class ContextBuilder(Protocol):
    def build(self, case: BacktestCase) -> object: ...


class Decider(Protocol):
    def decide(self, case: BacktestCase, context: object) -> InitialDecision: ...


class ExitPolicy(Protocol):
    def evaluate(self, bar: PriceBar, entry_price: Decimal) -> str | None: ...


@dataclass(frozen=True)
class BacktestSleeve:
    name: str
    case_source: CaseSource
    context_builder: ContextBuilder
    decider: Decider
    exit_policy: ExitPolicy


def decide_research_memo(
    *,
    memo: Any | None,
    recommendation: str | None,
    generation_status: str = "complete",
    guardrail_reasons: Sequence[str] = (),
    memo_key: str | None = None,
) -> InitialDecision:
    """Map a frozen generation artifact to BUY/PASS without an LLM call."""
    if generation_status != "complete" or memo is None:
        detail = "; ".join(guardrail_reasons) or generation_status
        return InitialDecision(
            DecisionAction.PASS, f"generation {generation_status}: {detail}",
            memo_key=memo_key,
        )
    if recommendation == "buy" and getattr(memo, "status", "") not in {
        "passed", "resolved", "rejected",
    }:
        return InitialDecision(
            DecisionAction.BUY, "frozen memo recommends buy",
            conviction=str(getattr(memo, "conviction_tier", "")),
            memo_key=memo_key,
        )
    return InitialDecision(
        DecisionAction.PASS, "frozen memo recommends pass",
        conviction=str(getattr(memo, "conviction_tier", "")),
        memo_key=memo_key,
    )


def size_research_case(
    *,
    tier: str,
    fixed_equity: Decimal,
    symbol: str,
    sector: str = "UNKNOWN",
    adv_20d: Decimal = Decimal("1000000000"),
) -> SizingDecision:
    """Apply the live sizing function to an isolated, fixed-equity case."""
    return size_entry(
        tier=tier, equity=fixed_equity, cash=fixed_equity,
        cost_by_symbol={}, symbol=symbol, sector=sector,
        cost_by_sector={}, adv_20d=adv_20d,
    )


class ResearchExitPolicy:
    """Stateful per-case adapter that records every falsifier evaluation."""

    def __init__(
        self,
        *,
        memo: Any,
        falsifier_tripped: Callable[[PriceBar], bool] | None = None,
    ) -> None:
        self.memo = memo
        self._external_check = falsifier_tripped or (lambda bar: False)
        self._closes: dict[date, Decimal] = {}

    @staticmethod
    def _name(falsifier: Any, index: int) -> str:
        return str(
            getattr(falsifier, "description", None)
            or getattr(falsifier, "metric", None)
            or f"falsifier-{index}"
        )

    def evaluate(self, bar: PriceBar, entry_price: Decimal) -> ExitEvaluation:
        self._closes[bar.session] = bar.adjusted_close
        price_context = PriceContext(closes=dict(self._closes))
        context = MetricContext(
            entry_price_ref=float(entry_price),
            asof=bar.session,
            entry_era=getattr(self.memo, "as_of_date", bar.session),
            price_ctx=price_context,
            direction=(
                "short" if getattr(self.memo, "thesis_type", "long") == "short"
                else "long"
            ),
        )
        tripped = self._external_check(bar)
        observations: list[FalsifierEvaluation] = []
        for index, falsifier in enumerate(getattr(self.memo, "falsifiers", ())):
            check = evaluate_falsifier(falsifier, context)
            observations.append(FalsifierEvaluation(
                falsifier_index=index,
                name=self._name(falsifier, index),
                status=check.status,
                observed=(
                    Decimal(str(check.observed))
                    if check.observed is not None else None
                ),
                detail=check.detail,
            ))
            tripped = tripped or check.status == "tripped"
        decision = evaluate_research_exit(
            memo=self.memo,
            current_price=bar.adjusted_close,
            falsifier_tripped=tripped,
        )
        return ExitEvaluation(
            reason=decision.reason if decision is not None else None,
            observations=tuple(observations),
        )

    def __call__(self, bar: PriceBar, entry_price: Decimal) -> str | None:
        """Retain the original simple callback API for callers outside replay."""
        return self.evaluate(bar, entry_price).reason


def make_research_exit_policy(
    *,
    memo: Any,
    falsifier_tripped: Callable[[PriceBar], bool] | None = None,
) -> ResearchExitPolicy:
    """Adapt live policy rules and expose replay observation detail."""
    return ResearchExitPolicy(memo=memo, falsifier_tripped=falsifier_tripped)


def resolved_settings(
    defaults: Mapping[str, object], overrides: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Shallow, explicit override merge used before settings fingerprinting."""
    result = dict(defaults)
    unknown = set(overrides or ()) - set(defaults)
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}")
    result.update(overrides or {})
    return result
