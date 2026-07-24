"""Reconstruction case source: run the PIT screener at sampled historical
dates over today's universe membership. Labeled RECONSTRUCTION_SOURCE_MODE --
survivorship-biased and never rendered as a clean point-in-time screen.
Per-name facts/prices are fetched once and reused across every sampled asof."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from ops.backtest.cases import CaseCandidate, RECONSTRUCTION_SOURCE_MODE
from ops.research.run import screen_inputs_and_results


@dataclass
class ReconstructionScreenerFetcher:
    universe: Any
    facts_fetcher: Callable[[str], Any]
    price_context_fetcher: Callable[[str], Any]
    triggers_finder: Callable[..., Any]
    screen: Callable[..., Any] = screen_inputs_and_results
    source_mode: str = RECONSTRUCTION_SOURCE_MODE
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

    def __call__(self, asof: date) -> tuple[CaseCandidate, ...]:
        pairs = self.screen(
            self.universe, asof=asof,
            facts_fetcher=self._cached_facts,
            triggers_finder=self.triggers_finder,
            price_context_fetcher=self._cached_ctx,
        )
        out: list[CaseCandidate] = []
        for inputs, result in pairs:
            if not result.passed:
                continue
            out.append(CaseCandidate(
                symbol=inputs.symbol,
                asof=asof,
                score=Decimal(len(inputs.triggers) or 1),
                trigger={"kind": "historical_screener_replay",
                         "asof": asof.isoformat()},
                screen_payload={"passed": True, "cheap": result.cheap,
                                "quality": result.quality},
                source_ref=f"reconstruction:{asof.isoformat()}:{inputs.symbol}",
            ))
        return tuple(out)
