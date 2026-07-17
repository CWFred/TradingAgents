from datetime import date
from types import SimpleNamespace

import pytest

from ops.backtest.probe import DEFAULT_QUESTIONS, evaluate_probe, invoke_probe

pytestmark = pytest.mark.unit


def test_unknown_answers_keep_cutoff():
    result = evaluate_probe(
        model_id="local:test", tested_cutoff=date(2025, 6, 1),
        responses={question.question_id: "I do not know" for question in DEFAULT_QUESTIONS},
    )
    assert result.contaminated is False
    assert result.recommended_cutoff == date(2025, 6, 1)


def test_known_later_event_advances_past_latest_match():
    result = evaluate_probe(
        model_id="local:test", tested_cutoff=date(2025, 6, 1),
        responses={
            "nba-finals-2025": "Oklahoma City Thunder",
            "club-world-cup-2025": "Chelsea FC won it.",
        },
    )
    assert result.contaminated is True
    assert result.recommended_cutoff == date(2025, 7, 14)


class _LLM:
    def invoke(self, prompt):
        return SimpleNamespace(content=f"reply to {prompt[:8]}")


def test_invoke_probe_collects_each_raw_response():
    responses = invoke_probe(_LLM())
    assert set(responses) == {question.question_id for question in DEFAULT_QUESTIONS}
