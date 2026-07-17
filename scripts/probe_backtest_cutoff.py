"""Run and optionally persist the explicit local-model cutoff probe."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone

from ops.backtest.generate import canonical_local_model_id, validate_local_model_spec
from ops.backtest.models import stable_hash
from ops.backtest.probe import DEFAULT_QUESTIONS, evaluate_probe, invoke_probe
from ops.backtest.store import BacktestStore
from tradingagents.llm_clients import create_llm_client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--cutoff", default="2025-06-01")
    parser.add_argument("--store")
    args = parser.parse_args()

    model_spec = validate_local_model_spec(args.model)
    model_id = canonical_local_model_id(args.model)
    tested_cutoff = date.fromisoformat(args.cutoff)
    llm = create_llm_client(
        provider=model_spec.provider, model=model_spec.model,
        base_url=model_spec.base_url,
    ).get_llm()
    responses = invoke_probe(llm)
    result = evaluate_probe(
        model_id=model_id, tested_cutoff=tested_cutoff, responses=responses,
    )
    probe_id = "probe-" + stable_hash({
        "model_id": result.model_id,
        "tested_cutoff": result.tested_cutoff,
        "responses": responses,
    })[:24]
    payload = {
        "probe_id": probe_id,
        "model_id": result.model_id,
        "tested_cutoff": result.tested_cutoff.isoformat(),
        "contaminated": result.contaminated,
        "recommended_cutoff": result.recommended_cutoff.isoformat(),
        "effective_cutoff": max(
            tested_cutoff,
            result.recommended_cutoff if result.contaminated else tested_cutoff,
        ).isoformat(),
        "questions": [
            {
                "question_id": question.question_id,
                "event_date": question.event_date.isoformat(),
                "prompt": question.prompt,
                "response": answer.response,
                "matched_answer": answer.matched_answer,
            }
            for question, answer in zip(DEFAULT_QUESTIONS, result.answers, strict=True)
        ],
    }
    if args.store:
        with BacktestStore(args.store, cutoff=tested_cutoff) as store:
            store.record_cutoff_probe(
                probe_id=probe_id,
                model_id=result.model_id,
                tested_cutoff=result.tested_cutoff,
                prompts={question.question_id: question.prompt for question in DEFAULT_QUESTIONS},
                responses=responses,
                rubric={
                    answer.question_id: answer.matched_answer
                    for answer in result.answers
                },
                contaminated=result.contaminated,
                recommended_cutoff=result.recommended_cutoff,
                created_at=datetime.now(timezone.utc),
            )
            payload["effective_cutoff"] = store.effective_cutoff.isoformat()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 2 if result.contaminated else 0


if __name__ == "__main__":
    raise SystemExit(main())
