"""ds4-backed lesson distiller satisfying the LessonDistiller protocol.

Mirrors ``ops/backtest/postmortem_adapter.py``'s ``Ds4ThesisAssessor`` client
idiom -- only the distillation call talks to the local model; validation of
its output (unknown keys, non-training sources, missing provenance tags) is
all handled by ``ops.backtest.lessons.distill_lessons_cached``, so this class
stays thin.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ops.backtest.generate import validate_local_model_spec
from ops.backtest.models import ThesisAssessment

DISTILL_PROMPT_VERSION = "distill-v1"

_SYSTEM = (
    "You turn post-mortem verdicts on investment memos into short, general, "
    "actionable lessons for future memo writing. Each lesson must cite the "
    "assessment_key(s) it came from. Reply with exactly one JSON array: "
    "[{\"text\": str, \"source_assessment_keys\": [str]}]. 0-5 lessons; prefer "
    "fewer, sharper lessons over many vague ones."
)


class Ds4LessonDistiller:
    def __init__(self, model_spec: str,
                 client_factory: Callable[[str], Any] | None = None) -> None:
        validate_local_model_spec(model_spec)
        self.model_spec = model_spec
        self._factory = client_factory or _default_client_factory

    def distill(self, *, assessments: Sequence[ThesisAssessment],
                model_id: str, prompt_version: str) -> Sequence[Mapping[str, Any]]:
        client = self._factory(self.model_spec)
        rows = [{
            "assessment_key": a.assessment_key,
            "case_id": a.case_id,
            "correctness": a.correctness.value,
            "rationale": a.rationale,
        } for a in assessments]
        prompt = (
            f"{_SYSTEM}\n\nPROMPT VERSION {prompt_version} MODEL {model_id}\n"
            f"ASSESSMENTS:\n{json.dumps(rows, sort_keys=True)}\n\nJSON lessons:"
        )
        reply = client.invoke(prompt)
        text = getattr(reply, "content", reply)
        text = str(text).strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("distiller reply must be a JSON array")
        return parsed


def _default_client_factory(model_spec: str) -> Any:
    from tradingagents.llm_clients import create_llm_client

    spec = validate_local_model_spec(model_spec)
    return create_llm_client(
        provider=spec.provider, model=spec.model, base_url=spec.base_url,
    ).get_llm()
