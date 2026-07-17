"""Sealed post-cutoff knowledge probe for local memo-generation models."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any


@dataclass(frozen=True)
class ProbeQuestion:
    question_id: str
    event_date: date
    prompt: str
    accepted_answers: tuple[str, ...]


@dataclass(frozen=True)
class ProbeAnswer:
    question_id: str
    response: str
    matched_answer: str | None

    @property
    def contaminated(self) -> bool:
        return self.matched_answer is not None


@dataclass(frozen=True)
class ProbeResult:
    model_id: str
    tested_cutoff: date
    answers: tuple[ProbeAnswer, ...]
    recommended_cutoff: date

    @property
    def contaminated(self) -> bool:
        return any(answer.contaminated for answer in self.answers)


# Prompts do not name a candidate answer. Each event occurred strictly after
# the initial 2025-06-01 case cutoff, and the answer keys are stable outcomes.
DEFAULT_QUESTIONS = (
    ProbeQuestion(
        "nba-finals-2025", date(2025, 6, 22),
        "Which team won the 2025 NBA Finals? Answer with the team name only.",
        ("oklahoma city thunder", "okc thunder"),
    ),
    ProbeQuestion(
        "stanley-cup-2025", date(2025, 6, 17),
        "Which team won the 2025 Stanley Cup? Answer with the team name only.",
        ("florida panthers",),
    ),
    ProbeQuestion(
        "club-world-cup-2025", date(2025, 7, 13),
        "Which club won the 2025 FIFA Club World Cup? Answer with the club name only.",
        ("chelsea", "chelsea fc"),
    ),
)


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def evaluate_probe(
    *,
    model_id: str,
    tested_cutoff: date,
    responses: dict[str, str],
    questions: tuple[ProbeQuestion, ...] = DEFAULT_QUESTIONS,
) -> ProbeResult:
    answers: list[ProbeAnswer] = []
    latest_known = tested_cutoff - timedelta(days=1)
    for question in questions:
        response = str(responses.get(question.question_id, ""))
        normalized = _normalized(response)
        matched = next(
            (
                candidate for candidate in question.accepted_answers
                if _normalized(candidate) in normalized
            ),
            None,
        )
        answers.append(ProbeAnswer(question.question_id, response, matched))
        if matched is not None:
            latest_known = max(latest_known, question.event_date)
    recommended = max(tested_cutoff, latest_known + timedelta(days=1))
    return ProbeResult(
        model_id=model_id, tested_cutoff=tested_cutoff,
        answers=tuple(answers), recommended_cutoff=recommended,
    )


def invoke_probe(llm: Any, questions: tuple[ProbeQuestion, ...] = DEFAULT_QUESTIONS) -> dict[str, str]:
    """Invoke a supplied local chat model; the caller owns backend lifecycle."""
    responses: dict[str, str] = {}
    for question in questions:
        raw = llm.invoke(question.prompt)
        content = getattr(raw, "content", raw)
        responses[question.question_id] = str(content)
    return responses
