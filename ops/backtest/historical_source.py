"""Reconstruction case source: run the PIT screener at sampled historical
dates over today's universe membership. Labeled RECONSTRUCTION_SOURCE_MODE --
survivorship-biased and never rendered as a clean point-in-time screen.
Per-name facts/prices are fetched once and reused across every sampled asof."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from ops.backtest.cases import RECONSTRUCTION_SOURCE_MODE, CaseCandidate
from ops.research.run import screen_inputs_and_results


def _result_payload(result: Any, symbol: str, asof: date) -> dict:
    """Full live-shaped hit payload: the ScreenResult dump (mirroring the
    screen store's ``json.dumps(asdict(result), default=str)``) with the
    symbol/asof keys the research brain's screen summary requires."""
    raw = asdict(result) if is_dataclass(result) else dict(result)
    payload = json.loads(json.dumps(raw, default=str))
    payload.setdefault("symbol", symbol)
    payload.setdefault("asof", asof.isoformat())
    return payload


@dataclass
class ReconstructionScreenerFetcher:
    universe: Any
    facts_fetcher: Callable[[str], Any]
    price_context_fetcher: Callable[[str], Any]
    triggers_finder: Callable[..., Any]
    screen: Callable[..., Any] = screen_inputs_and_results
    source_mode: str = RECONSTRUCTION_SOURCE_MODE
    include_near_misses: bool = False
    _facts: dict[str, Any] = field(default_factory=dict, init=False)
    _ctx: dict[str, Any] = field(default_factory=dict, init=False)

    def _cached_facts(self, symbol: str) -> Any:
        if symbol not in self._facts:
            self._facts[symbol] = self.facts_fetcher(symbol)
        return self._facts[symbol]

    def _cached_ctx(self, symbol: str) -> Any:
        if symbol not in self._ctx:
            self._ctx[symbol] = self.price_context_fetcher(symbol)
        return self._ctx[symbol]

    def _candidate(
        self, inputs: Any, result: Any, asof: date, *, kind: str, failed: str | None = None,
    ) -> CaseCandidate:
        trigger = {"kind": kind, "asof": asof.isoformat()}
        if failed is not None:
            trigger["failed"] = failed
        prefix = "reconstruction" if kind == "historical_screener_replay" else "nearmiss"
        return CaseCandidate(
            symbol=inputs.symbol,
            asof=asof,
            score=Decimal(len(inputs.triggers) or 1),
            trigger=trigger,
            screen_payload=_result_payload(result, inputs.symbol, asof),
            source_ref=f"{prefix}:{asof.isoformat()}:{inputs.symbol}",
        )

    def __call__(self, asof: date) -> tuple[CaseCandidate, ...]:
        pairs = self.screen(
            self.universe, asof=asof,
            facts_fetcher=self._cached_facts,
            triggers_finder=self.triggers_finder,
            price_context_fetcher=self._cached_ctx,
        )
        out: list[CaseCandidate] = []
        for inputs, result in pairs:
            conditions = {
                "cheap": bool(result.cheap),
                "quality": bool(result.quality),
                "trigger": len(inputs.triggers) >= 1,
            }
            if result.passed:
                out.append(self._candidate(
                    inputs, result, asof, kind="historical_screener_replay",
                ))
            elif self.include_near_misses and sum(conditions.values()) == 2:
                failed = next(k for k, ok in conditions.items() if not ok)
                out.append(self._candidate(
                    inputs, result, asof, kind="near_miss_control", failed=failed,
                ))
        return tuple(out)
