import json
from datetime import date

from ops.backtest.distiller import Ds4LessonDistiller


def _assessment(key, case_id):
    from ops.backtest.models import ThesisAssessment, ThesisCorrectness
    return ThesisAssessment(
        assessment_key=key, memo_key=f"memo-{key}", case_id=case_id,
        correctness=ThesisCorrectness.WRONG, rationale="thesis missed pricing power",
        evidence_cutoff=date(2025, 10, 1), model_id="m", prompt_version="p",
    )


def test_distiller_maps_reply_to_lesson_mappings():
    class _FakeChat:
        def invoke(self, prompt):
            class R:
                content = json.dumps([{
                    "text": "Demand a named mechanism for margin expansion.",
                    "source_assessment_keys": ["a1"],
                }])
            return R()

    distiller = Ds4LessonDistiller(
        "openai_compatible:deepseek-v4-flash@http://127.0.0.1:8000/v1",
        client_factory=lambda spec: _FakeChat(),
    )
    out = distiller.distill(
        assessments=[_assessment("a1", "case-1")],
        model_id="m", prompt_version="p",
    )
    assert out[0]["text"].startswith("Demand")
    assert out[0]["source_assessment_keys"] == ["a1"]
