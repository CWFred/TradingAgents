"""Reconstruction case source: run the PIT screener at sampled historical
dates over today's universe membership. Labeled RECONSTRUCTION_SOURCE_MODE --
survivorship-biased and never rendered as a clean point-in-time screen.
Per-name facts/prices are fetched once and reused across every sampled asof."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from ops.backtest.cases import RECONSTRUCTION_SOURCE_MODE, CaseCandidate
from ops.research.prices import PriceContext
from ops.research.run import screen_inputs_and_results
from ops.research.triggers import fetch_trigger_sources, triggers_from_sources

# Cached artifacts (trigger sources, company facts, price context) drift --
# filings/facts get amended, price history gets adjusted for later splits --
# so even within one sweep era they get a modest TTL rather than caching
# forever. Cheap safety, not a correctness requirement (Global Constraints:
# caches hold immutable-once-published data; TTL covers the rare revision).
_CACHE_MAX_AGE = timedelta(days=7)


def _result_payload(result: Any, symbol: str, asof: date) -> dict:
    """Full live-shaped hit payload: the ScreenResult dump (mirroring the
    screen store's ``json.dumps(asdict(result), default=str)``) with the
    symbol/asof keys the research brain's screen summary requires."""
    raw = asdict(result) if is_dataclass(result) else dict(result)
    payload = json.loads(json.dumps(raw, default=str))
    payload.setdefault("symbol", symbol)
    payload.setdefault("asof", asof.isoformat())
    return payload


def _price_context_to_json(ctx: PriceContext | None) -> dict[str, Any] | None:
    if ctx is None:
        return None
    return {
        "closes": {d.isoformat(): str(v) for d, v in ctx.closes.items()},
        "splits": {d.isoformat(): str(v) for d, v in ctx.splits.items()},
    }


def _price_context_from_json(raw: Mapping[str, Any] | None) -> PriceContext | None:
    if raw is None:
        return None
    return PriceContext(
        closes={date.fromisoformat(k): Decimal(v) for k, v in raw["closes"].items()},
        splits={date.fromisoformat(k): Decimal(v) for k, v in raw.get("splits", {}).items()},
    )


@dataclass
class ReconstructionScreenerFetcher:
    """Reconstruction screening at sampled historical asofs, per-symbol facts
    and price context memoized in-process across the whole sweep.

    ``triggers_finder``, when explicitly supplied, is used verbatim and
    uncached -- the pre-caching behavior, kept for tests and any caller that
    wants to inject its own trigger logic. Leave it unset (the default
    construction in ``service.py::_reconstruction_prepare_cases`` does) to
    get the disk-backed cached path instead: trigger sources, company facts,
    and price context are each fetched at most once per symbol per
    ``fetch_cache`` TTL window (``_CACHE_MAX_AGE``), shared across every
    asof in the sweep and across separate ``ReconstructionScreenerFetcher``
    instances/processes that point at the same cache file -- this is what
    makes repeat sweeps run from disk instead of re-hitting EDGAR/yfinance.
    The cached path requires ``fetch_cache``; without it, facts/prices are
    fetched directly (no disk cache) and trigger sources use the default
    ``ops.research.triggers.fetch_trigger_sources`` also uncached.
    """

    universe: Any
    facts_fetcher: Callable[[str], Any]
    price_context_fetcher: Callable[[str], Any]
    triggers_finder: Callable[..., Any] | None = None
    screen: Callable[..., Any] = screen_inputs_and_results
    source_mode: str = RECONSTRUCTION_SOURCE_MODE
    include_near_misses: bool = False
    fetch_cache: Any | None = None
    trigger_sources_fetcher: Callable[[str], Mapping[str, Any]] | None = None
    _facts: dict[str, Any] = field(default_factory=dict, init=False)
    _ctx: dict[str, Any] = field(default_factory=dict, init=False)
    _sources: dict[str, Mapping[str, Any]] = field(default_factory=dict, init=False)

    def _cached_facts(self, symbol: str) -> Any:
        if symbol not in self._facts:
            if self.fetch_cache is not None:
                self._facts[symbol] = self.fetch_cache.get_or_fetch(
                    "company_facts", symbol,
                    lambda: self.facts_fetcher(symbol),
                    max_age=_CACHE_MAX_AGE,
                )
            else:
                self._facts[symbol] = self.facts_fetcher(symbol)
        return self._facts[symbol]

    def _cached_ctx(self, symbol: str) -> Any:
        if symbol not in self._ctx:
            if self.fetch_cache is not None:
                raw = self.fetch_cache.get_or_fetch(
                    "price_context_6y", symbol,
                    lambda: _price_context_to_json(self.price_context_fetcher(symbol)),
                    max_age=_CACHE_MAX_AGE,
                )
                self._ctx[symbol] = _price_context_from_json(raw)
            else:
                self._ctx[symbol] = self.price_context_fetcher(symbol)
        return self._ctx[symbol]

    def _cached_sources(self, symbol: str) -> Mapping[str, Any]:
        if symbol not in self._sources:
            fetch_sources = self.trigger_sources_fetcher or fetch_trigger_sources
            if self.fetch_cache is not None:
                self._sources[symbol] = self.fetch_cache.get_or_fetch(
                    "trigger_sources", symbol,
                    lambda: fetch_sources(symbol),
                    max_age=_CACHE_MAX_AGE,
                )
            else:
                self._sources[symbol] = fetch_sources(symbol)
        return self._sources[symbol]

    def _effective_triggers_finder(self) -> Callable[..., Any]:
        if self.triggers_finder is not None:
            return self.triggers_finder

        def _cached_triggers_finder(symbol: str, *, asof: date) -> Any:
            sources = self._cached_sources(symbol)
            return triggers_from_sources(
                sources, asof=asof, price_context=self._cached_ctx(symbol),
            )

        return _cached_triggers_finder

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
            triggers_finder=self._effective_triggers_finder(),
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
