"""Production post-mortem adapter: ds4 assessor + cached-price evidence.

Loaded via ``backtest postmortem --adapter ops.backtest.postmortem_adapter:configured``
(or env OPS_BACKTEST_POSTMORTEM_ADAPTER). Evidence is deterministic and offline —
only the assessor talks to the local model.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

from ops.backtest.generate import validate_local_model_spec
from ops.backtest.models import BacktestCase, ContextItem
from ops.backtest.prices import PriceCache
from ops.config import load_config

POSTMORTEM_PROMPT_VERSION = "postmortem-v1"

_SYSTEM = (
    "You adjudicate an investment memo after the fact. Using ONLY the provided "
    "facts (nothing you remember), decide whether the memo's causal thesis was "
    "right about the mechanism — not whether the trade made money. Reply with "
    "exactly one JSON object: {\"thesis_correct\": bool, \"narrative\": str, "
    "\"evidence\": [source_ref, ...]} where evidence cites only provided source_refs."
)


class Ds4ThesisAssessor:
    def __init__(self, model_spec: str,
                 client_factory: Callable[[str], Any] | None = None) -> None:
        validate_local_model_spec(model_spec)
        self.model_spec = model_spec
        self._factory = client_factory or _default_client_factory

    def assess(self, *, memo_json: str, facts_json: str,
               facts_through: date) -> dict:
        client = self._factory(self.model_spec)
        prompt = (
            f"{_SYSTEM}\n\nFACTS THROUGH {facts_through.isoformat()}:\n"
            f"{facts_json}\n\nMEMO:\n{memo_json}\n\nJSON verdict:"
        )
        reply = client.invoke(prompt)
        text = getattr(reply, "content", reply)
        text = str(text).strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        raw = json.loads(text)
        # The model reliably cites the memo's own filing accessions alongside
        # the provided facts; the postmortem validator rejects any ref outside
        # the facts set, so intersect here (dropping a citation is safe --
        # provenance only, never content).
        try:
            allowed = {row.get("source_ref") for row in json.loads(facts_json)}
        except (ValueError, TypeError, AttributeError):
            allowed = set()
        cited = raw.get("evidence")
        if isinstance(cited, (list, tuple)):
            raw["evidence"] = [ref for ref in cited if ref in allowed]
        return raw


def _default_client_factory(model_spec: str) -> Any:
    from tradingagents.llm_clients import create_llm_client

    spec = validate_local_model_spec(model_spec)
    return create_llm_client(
        provider=spec.provider, model=spec.model, base_url=spec.base_url,
    ).get_llm()


class PriceEvidenceProvider:
    def __init__(self, store_path: str) -> None:
        self._store_path = store_path

    def evidence_for(self, *, case: BacktestCase, memo_json: str,
                     facts_through: date) -> Sequence[ContextItem]:
        del memo_json
        cache = PriceCache(self._store_path)
        items: list[ContextItem] = []
        for bar in cache.bars(case.symbol, start=case.asof, end=facts_through):
            if bar.session <= case.asof or bar.session > facts_through:
                continue
            items.append(ContextItem.create(
                kind="price-close",
                source_ref=f"price:{case.symbol}:{bar.session.isoformat()}",
                available_at=bar.session,
                content=json.dumps({
                    "session": bar.session.isoformat(),
                    "adjusted_close": str(bar.adjusted_close),
                    "volume": str(bar.volume),
                }, sort_keys=True),
            ))
        return tuple(items)


def configured() -> dict:
    config = load_config()
    model_spec = config.research_thesis_model
    return {
        "assessor": Ds4ThesisAssessor(model_spec),
        "evidence_provider": PriceEvidenceProvider(config.backtest_store_path),
        "model_id": model_spec,
        "prompt_version": POSTMORTEM_PROMPT_VERSION,
        "evidence_cutoff": None,
    }
