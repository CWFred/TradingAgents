# Backtest Learning-Loop Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the already-built-but-uncalled learning machinery into the CLI so a completed backtest run produces thesis post-mortems, distilled lessons, lesson-conditioned memos, and a paired holdout efficacy verdict.

**Architecture:** Four thin production layers over existing tested libraries: (1) a ds4-backed post-mortem adapter satisfying `load_postmortem_adapter`'s contract; (2) a `backtest lessons` CLI that fixes holdout membership *first* then calls `distill_lessons_cached`; (3) a `lessons` seam threaded from sealed manifests through `research_hit` into the thesis prompt; (4) a `backtest experiment` CLI running `run_paired_efficacy` with a replay-based quality evaluator. No new persistence — every table already exists (schema v3).

**Tech Stack:** Python 3, pytest, Click, SQLite (`backtest.sqlite`), ds4 via OpenAI-compatible loopback (`tradingagents.llm_clients.create_llm_client`).

## Global Constraints

- **Local models only.** Every model call path must pass `ops.backtest.generate.validate_local_model_spec` (provider `openai_compatible`/`ollama`, loopback host, no credentials in URL). Fail closed.
- **Holdout before distillation.** `EfficacyPlan.create` + `store.save_experiment` run and commit BEFORE any distiller call. Lessons must source only training cases (`validate_lesson_sources` enforces; do not weaken).
- **Temporal eligibility is strict:** a lesson conditions a case only if `lesson.eligible_from < case.asof` (strict `<`, matching `eligible_lessons`).
- **Assessor output contract** (`_parse_assessment`, `ops/backtest/postmortem.py:190`): mapping with `thesis_correct: bool`, `narrative: non-empty str`, `evidence: list[str]` ⊆ included `source_ref`s. Cited refs outside the cutoff context raise.
- **Default tests make zero network and zero model calls** — injected fakes only, mirroring `tests/ops/backtest/` conventions.
- **Live DBs untouched.** Only `backtest.sqlite` is written.
- **Money/scores are `Decimal`;** paired qualities are `float` (contract of `PairedResult`).
- Run `python -m ruff check` on every touched file before each commit.

---

### Task 1: ds4 post-mortem adapter

**Files:**
- Create: `ops/backtest/postmortem_adapter.py`
- Test: `tests/ops/backtest/test_postmortem_adapter.py`

**Interfaces:**
- Consumes: `ThesisAssessor` / `AdjudicationEvidenceProvider` protocols (`ops/backtest/postmortem.py:22,34`), `ContextItem.create` (`ops/backtest/models.py:243`), `PriceCache.bars` (`ops/backtest/prices.py`), `BacktestCase`, `load_config`.
- Produces: `configured() -> dict` with keys `assessor`, `evidence_provider`, `model_id`, `prompt_version`, `evidence_cutoff` (None) — the shape `load_postmortem_adapter` (`ops/backtest/service.py:120`) accepts. Plus classes `PriceEvidenceProvider(store_path)` and `Ds4ThesisAssessor(model_spec, client_factory=None)` used by Task 4's evaluator too. Prompt version constant `POSTMORTEM_PROMPT_VERSION = "postmortem-v1"`.

`PriceEvidenceProvider.evidence_for(*, case, memo_json, facts_through)` returns one `ContextItem` per cached post-asof session close for `case.symbol` (kind `"price-close"`, `source_ref=f"price:{case.symbol}:{session}"`, `available_at=session`, content = JSON `{"session":…, "adjusted_close":…, "volume":…}`), capped to the last session ≤ `facts_through`. Deterministic, offline, read-only.

`Ds4ThesisAssessor.assess(*, memo_json, facts_json, facts_through)` sends one chat completion (system prompt: adjudicate whether the memo's thesis was RIGHT ABOUT THE MECHANISM using only the provided facts; reply as JSON `{"thesis_correct": bool, "narrative": str, "evidence": [source_refs]}`) and parses `json.loads` of the reply (strip markdown fences). `client_factory` is the injectable seam; default builds via `tradingagents.llm_clients.create_llm_client` after `validate_local_model_spec(model_spec)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/backtest/test_postmortem_adapter.py
import json
from datetime import date
from decimal import Decimal

import pytest

from ops.backtest.postmortem_adapter import (
    Ds4ThesisAssessor, PriceEvidenceProvider, configured,
)


class _FakeChat:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        class R: content = self.reply
        return R()


def test_assessor_parses_json_reply_and_uses_injected_client():
    fake = _FakeChat(json.dumps({
        "thesis_correct": True, "narrative": "margin inflected as claimed",
        "evidence": ["price:ACME:2025-09-02"],
    }))
    assessor = Ds4ThesisAssessor(
        "openai_compatible:deepseek-v4-flash@http://127.0.0.1:8000/v1",
        client_factory=lambda spec: fake,
    )
    raw = assessor.assess(memo_json="{}", facts_json="[]",
                          facts_through=date(2025, 9, 30))
    assert raw["thesis_correct"] is True
    assert raw["narrative"].startswith("margin")
    assert fake.prompts and "2025-09-30" in fake.prompts[0]


def test_assessor_rejects_nonlocal_model_spec():
    with pytest.raises(Exception):
        Ds4ThesisAssessor("openai_compatible:gpt@https://api.example.com/v1")


def test_price_evidence_provider_emits_post_asof_items(tmp_path, seeded_price_store):
    # seeded_price_store: fixture seeding bars for ACME sessions
    # 2025-06-30 (pre-asof), 2025-07-02 and 2025-07-03 (post-asof) — reuse
    # the bar-seeding helper from tests/ops/backtest/test_prices.py.
    store_path, case = seeded_price_store   # case.asof == 2025-07-01
    provider = PriceEvidenceProvider(store_path)
    items = provider.evidence_for(case=case, memo_json="{}",
                                  facts_through=date(2025, 7, 2))
    refs = [item.source_ref for item in items]
    assert refs == ["price:ACME:2025-07-02"]          # pre-asof and post-cutoff excluded
    assert all(item.available_at <= date(2025, 7, 2) for item in items)


def test_configured_shape():
    cfg = configured()
    assert set(cfg) >= {"assessor", "evidence_provider", "model_id", "prompt_version"}
    assert cfg["prompt_version"] == "postmortem-v1"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/ops/backtest/test_postmortem_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError: ops.backtest.postmortem_adapter`.

- [ ] **Step 3: Implement**

```python
# ops/backtest/postmortem_adapter.py
"""Production post-mortem adapter: ds4 assessor + cached-price evidence.

Loaded via ``backtest postmortem --adapter ops.backtest.postmortem_adapter:configured``
(or env OPS_BACKTEST_POSTMORTEM_ADAPTER). Evidence is deterministic and offline —
only the assessor talks to the local model.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Callable, Sequence

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
        return json.loads(text)


def _default_client_factory(model_spec: str) -> Any:
    from tradingagents.llm_clients import create_llm_client
    return create_llm_client(model_spec)


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
```

Before finalizing, confirm `PriceCache.bars(symbol, start=, end=)` is the actual reader signature in `ops/backtest/prices.py` (adjust the call, not the provider contract, if it differs) and confirm `create_llm_client`'s exact name/location in `tradingagents/llm_clients.py` (it is the same helper `ops/backtest/service.py:_execute_generation` imports — mirror that import).

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/ops/backtest/test_postmortem_adapter.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/backtest/postmortem_adapter.py tests/ops/backtest/test_postmortem_adapter.py
git commit -m "feat(backtest): ds4 post-mortem adapter (assessor + price evidence)"
```

---

### Task 2: `backtest lessons` CLI over fixed holdout

**Files:**
- Create: `ops/backtest/distiller.py`
- Modify: `ops/backtest/service.py` (add `lessons_run`), `ops/cli.py` (add `backtest lessons`), `ops/config.py` (add `backtest_holdout_size: int = 10`, `backtest_experiment_seed: int = 1`)
- Test: `tests/ops/backtest/test_distiller.py`, `tests/ops/backtest/test_cli.py` (extend)

**Interfaces:**
- Consumes: `EfficacyPlan.create(sleeve=, case_ids=, holdout_size=, seed=)`, `DistillationRequest.create(...)`, `distill_lessons_cached(distiller, cache, request=)` (`ops/backtest/lessons.py:162`); `store.save_experiment` / `get_experiment` (`store.py:1362,1342`); thesis assessments joined to a run's cases (same join as `postmortem_run`, `service.py:1067`).
- Produces: `Ds4LessonDistiller(model_spec, client_factory=None)` satisfying `LessonDistiller` (`lessons.py:28` — `distill(*, assessments, model_id, prompt_version) -> Sequence[Mapping]`, each mapping `{"text": str, "source_assessment_keys": [str]}`); `lessons_run(*, path, run_id, execute=False, distiller=None, holdout_size=None, seed=None) -> LessonsResult` where `LessonsResult = (experiment_id, training, holdout, assessments, lessons, executed)`; CLI `backtest lessons RUN_ID [--execute] [--holdout-size N] [--seed N]`. Prompt constant `DISTILL_PROMPT_VERSION = "distill-v1"`.

`lessons_run` order of operations (load-bearing): (1) collect the run's distinct case ids; (2) `EfficacyPlan.create` with configured holdout/seed; (3) `store.save_experiment(plan.record(lesson_fingerprint="pending"))` — idempotent if the same experiment_id exists; (4) load thesis assessments for TRAINING cases only; (5) if not `--execute`, report the plan; else `DistillationRequest.create(...)` + `distill_lessons_cached` with the ds4 distiller.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/backtest/test_distiller.py
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
```

```python
# append to tests/ops/backtest/test_cli.py — reuse that module's existing
# seeded-store fixtures and CliRunner patterns (read them first; the store
# fixture used by the postmortem CLI tests already persists a completed run
# with assessments).
def test_lessons_cli_fixes_holdout_before_distillation(seeded_run_store, monkeypatch):
    calls = []

    class _RecordingDistiller:
        def distill(self, *, assessments, model_id, prompt_version):
            calls.append([a.case_id for a in assessments])
            return [{"text": "lesson", "source_assessment_keys":
                     [assessments[0].assessment_key]}]

    # plan-only: writes the experiment record, no distiller call
    result_plan = _invoke_cli(["backtest", "lessons", seeded_run_store.run_id,
                               "--holdout-size", "2", "--seed", "7"])
    assert result_plan.exit_code == 0
    assert "holdout" in result_plan.output
    assert calls == []

    # execute: distills from training cases only
    result = _invoke_cli(["backtest", "lessons", seeded_run_store.run_id,
                          "--holdout-size", "2", "--seed", "7", "--execute"],
                         distiller=_RecordingDistiller())
    assert result.exit_code == 0
    holdout_ids = _experiment_holdout_ids(seeded_run_store)   # helper via store
    assert not (set(calls[0]) & set(holdout_ids))
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/ops/backtest/test_distiller.py tests/ops/backtest/test_cli.py -k "distiller or lessons_cli" -v`
Expected: FAIL — module/command missing.

- [ ] **Step 3: Implement `Ds4LessonDistiller`**

```python
# ops/backtest/distiller.py
"""ds4-backed lesson distiller satisfying the LessonDistiller protocol."""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

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
                model_id: str, prompt_version: str) -> Sequence[Mapping]:
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
        text = str(getattr(reply, "content", reply)).strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```")
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("distiller reply must be a JSON array")
        return parsed


def _default_client_factory(model_spec: str) -> Any:
    from tradingagents.llm_clients import create_llm_client
    return create_llm_client(model_spec)
```

(`distill_lessons_cached` re-validates everything the model returns — unknown keys, non-training sources, missing tags all raise — so the distiller stays thin.)

- [ ] **Step 4: Implement `lessons_run` in `ops/backtest/service.py`**

```python
@dataclass(frozen=True)
class LessonsResult:
    experiment_id: str
    training: tuple[str, ...]
    holdout: tuple[str, ...]
    assessments: int
    lessons: int
    executed: bool


def lessons_run(
    *, path: str | Path, run_id: str, execute: bool = False,
    distiller: Any | None = None, holdout_size: int | None = None,
    seed: int | None = None,
) -> LessonsResult:
    from ops.backtest.lessons import (
        DistillationRequest, EfficacyPlan, distill_lessons_cached,
    )
    config = load_config()
    holdout_n = holdout_size if holdout_size is not None else config.backtest_holdout_size
    seed_n = seed if seed is not None else config.backtest_experiment_seed
    with BacktestStore(path) as store:
        with store.transaction() as conn:
            case_rows = conn.execute(
                "SELECT DISTINCT case_id FROM run_cases WHERE run_id = ? ORDER BY case_id",
                (run_id,),
            ).fetchall()
        if not case_rows:
            raise UnknownBacktestRun(f"unknown or empty backtest run {run_id!r}")
        case_ids = [row["case_id"] for row in case_rows]
        plan = EfficacyPlan.create(
            sleeve="research", case_ids=case_ids,
            holdout_size=holdout_n, seed=seed_n,
        )
        if store.get_experiment(plan.experiment_id) is None:
            store.save_experiment(plan.record(lesson_fingerprint="pending"))
        assessments = _training_assessments(store, run_id, plan.training_case_ids)
        if not execute:
            return LessonsResult(plan.experiment_id, plan.training_case_ids,
                                 plan.holdout_case_ids, len(assessments), 0, False)
        if not assessments:
            raise BacktestServiceError(
                "no thesis assessments for training cases; run `backtest postmortem --execute` first"
            )
        if distiller is None:
            from ops.backtest.distiller import DISTILL_PROMPT_VERSION, Ds4LessonDistiller
            distiller = Ds4LessonDistiller(config.research_thesis_model)
            prompt_version = DISTILL_PROMPT_VERSION
            model_id = config.research_thesis_model
        else:
            prompt_version = getattr(distiller, "prompt_version", "distill-v1")
            model_id = getattr(distiller, "model_spec", "injected")
        request = DistillationRequest.create(
            sleeve="research",
            training_case_ids=plan.training_case_ids,
            holdout_case_ids=plan.holdout_case_ids,
            assessments=assessments,
            model_id=model_id, prompt_version=prompt_version,
        )
        distilled = distill_lessons_cached(distiller, store, request=request)
        return LessonsResult(plan.experiment_id, plan.training_case_ids,
                             plan.holdout_case_ids, len(assessments),
                             len(distilled), True)


def _training_assessments(store, run_id, training_case_ids):
    with store.transaction() as conn:
        rows = conn.execute(
            "SELECT DISTINCT a.assessment_key FROM thesis_assessments AS a "
            "JOIN decisions AS d ON d.memo_key = a.memo_key "
            "WHERE d.run_id = ? ORDER BY a.assessment_key",
            (run_id,),
        ).fetchall()
    keys = [row["assessment_key"] for row in rows]
    out = []
    training = set(training_case_ids)
    for key in keys:
        assessment = store.get_thesis_assessment(key)
        if assessment is not None and assessment.case_id in training:
            out.append(assessment)
    return tuple(out)
```

Confirm the `run_cases` column name (`case_id`) against `store.py`'s CREATE TABLE before running; use the same store transaction/read idioms as `postmortem_run` directly above it.

- [ ] **Step 5: Add config fields + CLI command**

`ops/config.py`: add `backtest_holdout_size: int = 10` and `backtest_experiment_seed: int = 1` beside the other `backtest_*` fields, including env overrides if the neighboring fields have them (mirror exactly).

`ops/cli.py` (beside `backtest_postmortem`):

```python
@backtest.command("lessons")
@click.argument("run_id")
@click.option("--execute", is_flag=True,
              help="Distill lessons from training-case post-mortems via ds4.")
@click.option("--holdout-size", default=None, type=int)
@click.option("--seed", default=None, type=int)
def backtest_lessons(run_id: str, execute: bool,
                     holdout_size: int | None, seed: int | None) -> None:
    """Fix holdout membership, then distill training-case lessons."""
    from ops.backtest.service import lessons_run

    config = load_config()
    try:
        result = lessons_run(
            path=config.backtest_store_path, run_id=run_id, execute=execute,
            holdout_size=holdout_size, seed=seed,
        )
    except Exception as exc:
        raise _backtest_error(exc) from exc
    click.echo(
        f"experiment {result.experiment_id}: {len(result.training)} training, "
        f"{len(result.holdout)} holdout, {result.assessments} assessment(s), "
        f"{result.lessons} lesson(s){'' if result.executed else ' (plan only)'}"
    )
```

- [ ] **Step 6: Run tests, verify pass**

Run: `python -m pytest tests/ops/backtest/test_distiller.py tests/ops/backtest/test_cli.py tests/ops/backtest/test_config.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add ops/backtest/distiller.py ops/backtest/service.py ops/cli.py ops/config.py tests/ops/backtest/
git commit -m "feat(backtest): lessons CLI with pre-fixed holdout + ds4 distiller"
```

---

### Task 3: Lesson conditioning through memo generation

**Files:**
- Modify: `ops/research/brain.py` (`research_hit` gains `lessons` kwarg), `ops/backtest/generate.py` (`generate_research_memo` + `GenerationRequest` threading), `ops/backtest/service.py` (`_generation_requests` loads eligible lessons)
- Test: `tests/ops/research/test_brain.py` (extend), `tests/ops/backtest/test_generate.py` (extend)

**Interfaces:**
- Consumes: `eligible_lessons(lessons, asof=)` and `lesson_set_hash` (`ops/backtest/lessons.py:232,239`); existing `GenerationRequest.lesson_fingerprint` (`generate.py:102` — already in the memo cache key, so conditioned memos are distinct frozen artifacts).
- Produces: `research_hit(hit, *, …, lessons: Sequence[str] = ())` — lesson texts appended to the thesis/memo prompt as a clearly delimited `PROCESS LESSONS` block; `generate_research_memo(request, *, …, lessons: Sequence[str] = ())`; `_generation_requests(store, cases, *, config, brain_version, prompt_version, lessons: Sequence[Lesson] = ())` computing per-case eligible texts + `lesson_fingerprint = lesson_set_hash(eligible)` (or `"none"` when empty — the existing default).

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/research/test_brain.py — reuse that module's existing fake-LLM
# fixtures that already drive research_hit end-to-end (read them first).
def test_research_hit_injects_lessons_block_into_memo_prompt(fake_brain_env):
    outcome = research_hit(
        fake_brain_env.hit, **fake_brain_env.kwargs,
        lessons=("Demand a named mechanism for margin expansion.",),
    )
    memo_prompts = [p for p in fake_brain_env.thesis_llm.prompts if "PROCESS LESSONS" in p]
    assert memo_prompts, "lessons block missing from thesis prompt"
    assert "named mechanism" in memo_prompts[0]


def test_research_hit_without_lessons_has_no_lessons_block(fake_brain_env):
    research_hit(fake_brain_env.hit, **fake_brain_env.kwargs)
    assert all("PROCESS LESSONS" not in p for p in fake_brain_env.thesis_llm.prompts)
```

```python
# tests/ops/backtest/test_generate.py — reuse the existing fake research_fn
# pattern already used to test generate_research_memo offline.
def test_generation_request_fingerprints_eligible_lessons(seeded_case_store):
    from ops.backtest.lessons import lesson_set_hash
    lesson = _lesson(eligible_from=date(2025, 6, 1))       # helper via models.Lesson
    late = _lesson(eligible_from=date(2026, 1, 1))
    requests = _generation_requests(
        seeded_case_store.store, seeded_case_store.cases,   # case asof 2025-07-01
        config=seeded_case_store.config,
        brain_version="b", prompt_version="p", lessons=(lesson, late),
    )
    assert requests[0].lesson_fingerprint == lesson_set_hash([lesson])  # late excluded


def test_generate_research_memo_passes_lesson_texts():
    captured = {}
    def fake_research(hit, **kwargs):
        captured["lessons"] = kwargs.get("lessons")
        …  # return the module's standard fake researched outcome
    generate_research_memo(request_with_fingerprint, research_fn=fake_research,
                           evidence_llm=object(), thesis_llm=object(),
                           lessons=("only sealed lesson text",))
    assert captured["lessons"] == ("only sealed lesson text",)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/ops/research/test_brain.py tests/ops/backtest/test_generate.py -k lesson -v`
Expected: FAIL — unexpected `lessons` kwarg.

- [ ] **Step 3: Implement the brain seam**

In `ops/research/brain.py`, add `lessons: Sequence[str] = ()` to `research_hit`'s keyword-only params and build the block once:

```python
def _lessons_section(lessons: Sequence[str]) -> str:
    if not lessons:
        return ""
    numbered = "\n".join(f"{i+1}. {text}" for i, text in enumerate(lessons))
    return (
        "\n\nPROCESS LESSONS (distilled from prior graded memos; apply where "
        f"relevant, never cite as evidence):\n{numbered}"
    )
```

Append `_lessons_section(lessons)` to the memo-stage prompt where `MEMO_PROMPT.format(...)` is built (`ops/research/brain.py:386`): `prompt = MEMO_PROMPT.format(...) + _lessons_section(lessons)`. Do NOT touch the evidence-stage prompt (`brain.py:289`) — lessons guide thesis writing, not fact extraction.

- [ ] **Step 4: Thread through generation**

In `ops/backtest/generate.py`: `generate_research_memo(..., lessons: Sequence[str] = ())` adds `"lessons": tuple(lessons)` to the `kwargs` dict passed to `research_fn` only when non-empty (keeps old fakes working). In `ops/backtest/service.py::_generation_requests`, accept `lessons: Sequence[Lesson] = ()`, compute per case `eligible = eligible_lessons(lessons, asof=case.asof)`, set `lesson_fingerprint=lesson_set_hash(eligible) if eligible else "none"`, and carry the texts on the request (add `lesson_texts: tuple[str, ...] = ()` to `GenerationRequest`, excluded from any hash — the fingerprint already covers identity). `_execute_generation` passes `lessons=request.lesson_texts` to `generate_research_memo`.

- [ ] **Step 5: Run the full generate + brain suites**

Run: `python -m pytest tests/ops/backtest/test_generate.py tests/ops/research/test_brain.py tests/ops/backtest/test_cli.py -v`
Expected: PASS, including all pre-existing tests (the no-lessons default must change zero cache keys — `lesson_fingerprint` defaults are untouched).

- [ ] **Step 6: Commit**

```bash
git add ops/research/brain.py ops/backtest/generate.py ops/backtest/service.py tests/
git commit -m "feat(backtest): thread eligible lessons into memo generation"
```

---

### Task 4: `backtest experiment` CLI (paired holdout efficacy)

**Files:**
- Create: `ops/backtest/experiment.py`
- Modify: `ops/backtest/service.py` (add `experiment_run`), `ops/cli.py` (add command)
- Test: `tests/ops/backtest/test_experiment.py`, `tests/ops/backtest/test_cli.py` (extend)

**Interfaces:**
- Consumes: `EfficacyPlan`, `run_paired_efficacy`, `paired_experiment_summary`, `PairedCaseInput`, `PairedEvaluator` (`lessons.py:255-372`); saved experiment + distilled lessons from Task 2; lesson-conditioned generation from Task 3; `replay_case` + `evaluate_replay` machinery already used by `run_cached_backtest`.
- Produces: `ReplayPairedEvaluator` implementing `PairedEvaluator.evaluate(*, case_input, variant, lesson_fingerprint) -> float` — quality = float of the primary-horizon `utility` from the variant memo's replayed case result (control = existing frozen memo with `lesson_fingerprint="none"`; treated = frozen memo variant with the experiment's fingerprint; a missing treated memo raises with the exact `backtest generate` command to create it). `experiment_run(*, path, experiment_id, execute=False, evaluator=None) -> dict` returning `paired_experiment_summary` plus identity fields; CLI `backtest experiment EXPERIMENT_ID [--execute]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/ops/backtest/test_experiment.py
def test_experiment_pairs_control_and_treated_over_holdout(seeded_experiment_store):
    # fixture: experiment with holdout {case-a, case-b}, lessons saved,
    # pinned inputs for both cases — built from existing test_lessons fixtures.
    class _FakeEvaluator:
        def evaluate(self, *, case_input, variant, lesson_fingerprint):
            return 1.0 if variant == "treated" else 0.0

    summary = experiment_run(
        path=seeded_experiment_store.path,
        experiment_id=seeded_experiment_store.experiment_id,
        execute=True, evaluator=_FakeEvaluator(),
    )
    assert summary["pairs"] == 2
    assert summary["mean_delta"] == 1.0
    assert summary["claim"] == "paired descriptive result; no significance claim"
```

- [ ] **Step 2: Run, verify fail** — `python -m pytest tests/ops/backtest/test_experiment.py -v` → `experiment_run` missing.

- [ ] **Step 3: Implement**

`ops/backtest/experiment.py` holds `ReplayPairedEvaluator` (loads the variant's frozen memo by `(case, lesson_fingerprint)`, replays it with the run's stored settings via the same helpers `run_cached_backtest` uses, returns `float(case_result.primary_utility)`; raises `MissingBacktestArtifacts` naming the exact `backtest generate` invocation when the treated memo variant is absent). `service.experiment_run` loads the `ExperimentRecord`, rebuilds `EfficacyPlan` membership from it, loads distilled lessons by the experiment's distillation tags, builds `PairedCaseInput(case_id, asof, pinned_input_hash=<case context manifest hash>)` per holdout case, and calls `run_paired_efficacy` + `paired_experiment_summary`. Plan-only mode reports holdout size and which treated memo variants are still missing (the generation to-do list). CLI command mirrors `backtest lessons` (argument, `--execute`, echo the summary dict as aligned lines, exit nonzero via `_backtest_error` on missing artifacts).

The implementer must read `run_cached_backtest` (`service.py:310`) first and reuse its case-replay internals rather than duplicating replay wiring; if a private helper needs extracting to be callable per-case, extract it in this task with its own test run to keep `backtest run` behavior byte-identical (`python -m pytest tests/ops/backtest/test_cli.py -k run -v` must stay green).

- [ ] **Step 4: Run, verify pass** — full `python -m pytest tests/ops/backtest -q`.

- [ ] **Step 5: Commit**

```bash
git add ops/backtest/experiment.py ops/backtest/service.py ops/cli.py tests/ops/backtest/
git commit -m "feat(backtest): paired holdout efficacy experiment CLI"
```

---

### Task 5: End-to-end validation on the real corpus

**Files:** none (operational; requires Phase 0's matured corpus + completed run).

- [ ] **Step 1:** `python -m pytest tests/ops/backtest tests/ops/research -q` — everything green.
- [ ] **Step 2:** With ds4 up: `python -m ops.cli backtest postmortem <run_id> --execute --adapter ops.backtest.postmortem_adapter:configured` → assessments > 0, quadrants populated (`SELECT quadrant, count(*) FROM case_results GROUP BY quadrant;`).
- [ ] **Step 3:** `python -m ops.cli backtest lessons <run_id>` (plan), then `--execute` → ≥1 lesson with training-only sources.
- [ ] **Step 4:** Generate treated holdout memos (command printed by the experiment plan), then `python -m ops.cli backtest experiment <experiment_id> --execute` → paired summary prints. Capture the summary as the Gate G1 evidence.
- [ ] **Step 5:** Commit any validation-support tweaks; open PR.

## Self-review notes

- Spec coverage: adapter (Task 1), lessons CLI + fixed holdout (Task 2), injection (Task 3), paired efficacy (Task 4), real-data gate (Task 5) — matches roadmap Phase 1 scope. Live-import, sleeves, tournaments are explicitly out of scope (separate plans).
- Type consistency: `Ds4ThesisAssessor`/`Ds4LessonDistiller` share the `client_factory: Callable[[str], Any]` seam; `lessons_run`/`experiment_run` both return via `service.py` and are loaded lazily in `ops/cli.py` like every other backtest command.
- Known verification points called out inline (PriceCache.bars signature, create_llm_client import path, run_cases column name, MEMO_PROMPT append point) — confirm against source before implementing, adjust call sites not contracts.
