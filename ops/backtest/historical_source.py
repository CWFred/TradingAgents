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
                screen_payload=_result_payload(result, inputs.symbol, asof),
                source_ref=f"reconstruction:{asof.isoformat()}:{inputs.symbol}",
            ))
        return tuple(out)
