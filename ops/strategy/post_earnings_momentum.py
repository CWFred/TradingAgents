"""Post-earnings momentum strategy: for each candidate that the pipeline
labels BUY, build a sized order with an entry-relative stop.

The stop is carried as Order.stop_pct (entry-relative, e.g. -0.08) rather
than an absolute price: cand.last_price is a stale previous-close reference
(from the 20-day history call), and a gap between that reference and the
actual fill can put an absolute stop on the wrong side of the fill. The
broker resolves stop_pct to an absolute price from the real fill price at
fill time (see PaperBroker/RobinhoodBroker)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from ops.broker.types import Order, OrderType, Side
from ops.config import OpsConfig
from ops.llm_backend import ManagedBackendPaused
from ops.pipeline_adapter import PipelineAdapter, PipelineDecision, PipelineResult, TIER_STARTER
from ops.strategy.base import AnalyzedDecision, StrategyOrder
from ops.universe import Candidate, CandidateSource


def _client_order_id(symbol: str, asof: date) -> str:
    # uuid4-suffixed rather than positionally indexed: the same symbol at
    # the same universe index recurs every 30-minute tick on the same
    # trading date (e.g. after a CashReserveRule rejection), so an index
    # alone collides. client_order_id is a replay/idempotency key (see
    # ops.journal's UNIQUE index and paper.py::from_journal), so it must be
    # unique per order, not just per tick.
    return f"pem-{asof.isoformat()}-{symbol}-{uuid4().hex[:8]}"


def _quantize_money(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"))


class AnalysisBatchError(RuntimeError):
    """Every candidate failed, indicating a batch-wide/systemic problem."""


def _failed_analysis(cand: Candidate, asof_date: date, exc: Exception) -> PipelineResult:
    """Fail closed for one symbol while retaining an auditable error."""
    return PipelineResult(
        symbol=cand.symbol,
        date=asof_date,
        decision=PipelineDecision.HOLD,
        raw={"analysis_error": f"{type(exc).__name__}: {exc}"},
    )


def _reason_for(cand: Candidate, result: PipelineResult) -> str:
    suffix = (
        "pipeline BUY (Overweight starter)"
        if result.tier == TIER_STARTER
        else "pipeline BUY"
    )
    if cand.source is CandidateSource.EARNINGS:
        return (
            f"post-earnings beat (EPS {cand.earnings.eps_actual} vs "
            f"est {cand.earnings.eps_estimate}); {suffix}"
        )
    return (
        f"6-mo momentum leader (ret {cand.momentum.trailing_return_6m}, "
        f"> 200d MA); {suffix}"
    )


class PostEarningsMomentumStrategy:
    def __init__(self, *, config: OpsConfig):
        self._cfg = config

    def propose_orders(
        self,
        *,
        candidates: list[Candidate],
        pipeline: PipelineAdapter,
        current_equity: Decimal,
        asof_date: date,
        live_max_position_cap: Decimal | None = None,
        decision_sink: list[AnalyzedDecision] | None = None,
    ) -> list[StrategyOrder]:
        full_notional = _quantize_money(current_equity * self._cfg.per_position_cap_pct)
        starter_notional = _quantize_money(current_equity * self._cfg.starter_position_pct)
        if live_max_position_cap is not None:
            full_notional = min(full_notional, live_max_position_cap)
            starter_notional = min(starter_notional, live_max_position_cap)
        # Even the full-size rung under the floor means no order can ever
        # clear it — bail before spending any LLM budget (v1 behavior kept).
        if full_notional < self._cfg.per_trade_dollar_floor:
            return []
        out: list[StrategyOrder] = []
        successful_analyses = 0
        failed_results: list[PipelineResult] = []
        for cand in candidates:
            try:
                result = pipeline.propagate(cand.symbol, asof_date)
            except ManagedBackendPaused:
                # An operator pause is a batch-level instruction, not a bad
                # symbol. Preserve the daily retry by aborting immediately.
                raise
            except Exception as exc:  # noqa: BLE001 - per-symbol fault boundary
                result = _failed_analysis(cand, asof_date, exc)
                failed_results.append(result)
            else:
                successful_analyses += 1
            if decision_sink is not None:
                decision_sink.append(AnalyzedDecision(candidate=cand, pipeline=result))
            if result.raw.get("analysis_error"):
                # No risk-reviewed final verdict exists for this symbol. HOLD
                # it, but retain completed symbols and continue the batch.
                continue
            if result.decision != PipelineDecision.BUY:
                continue
            notional = starter_notional if result.tier == TIER_STARTER else full_notional
            if notional < self._cfg.per_trade_dollar_floor:
                continue
            order = Order(
                client_order_id=_client_order_id(cand.symbol, asof_date),
                symbol=cand.symbol,
                side=Side.BUY,
                notional_dollars=notional,
                order_type=OrderType.MARKET,
                stop_pct=self._cfg.per_position_stop_pct,
            )
            out.append(StrategyOrder(
                order=order,
                reason=_reason_for(cand, result),
                candidate=cand,
                pipeline=result,
            ))
        if failed_results and successful_analyses == 0:
            first_error = failed_results[0].raw["analysis_error"]
            raise AnalysisBatchError(
                f"all {len(failed_results)} candidate analyses failed; "
                f"first error: {first_error}"
            )
        return out
