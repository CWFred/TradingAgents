# Portfolio-Manager Scheduled Reassessment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Portfolio Manager flag a specific future date ("recheck this ticker around the Starship Flight 13 launch") as a typed field, and have the existing monitor/escalation pipeline automatically requeue that ticker for research when the date arrives — with zero new LLM passes and zero new infrastructure.

**Architecture:** `PortfolioDecision` gains two optional structured fields (`reassess_after: date`, `reassess_trigger: str`), populated by the same single structured-output call the PM already makes. The Portfolio Manager node threads the raw values into graph state (not just the rendered markdown). `ops/research/vetting.py`'s `vet_memo` — which already runs the full graph per memo and already has the memo object in scope — reads those two typed values off `result.raw` (a plain dict, no text parsing) and appends a `Catalyst(hard_date=True)` to the memo. `ops/research/monitor.py`'s existing catalyst-due check, currently restricted to `thesis_type == "event"`, is widened to check every memo's `catalysts` list regardless of type, and — like the existing falsifier-escalation path — pushes a re-research hit onto the queue `ops/research/drain.py` already drains. No new scheduler, no new store table, no new LLM call.

**Tech Stack:** Python, Pydantic v2 (`with_structured_output`), pytest, SQLite-backed `MemoStore`/`ScreenStore`.

## Global Constraints

- No second LLM pass to extract `reassess_after`/`reassess_trigger` — they must come from the same structured call as the rest of `PortfolioDecision`.
- `reassess_after`, if implausible (in the past, or more than 365 days out), must be silently nulled rather than raising — mirrors the existing `price_target` nullish-coercion pattern (`schemas.py:33-36`), since a malformed field must never fail the whole structured call.
- Reuse the existing `Catalyst` model (`tradingagents/memos/schema.py:126-139`) and the existing escalation queue (`ScreenStore.enqueue_hit`, `ops/research/store.py:116`) — no new schema, no new queue.
- Every task must leave `pytest -m unit` green before moving to the next task.

---

## File Structure

- `tradingagents/agents/schemas.py` — add `reassess_after`/`reassess_trigger` fields + validator to `PortfolioDecision`; render them in `render_pm_decision`.
- `tradingagents/agents/utils/structured.py` — add `invoke_structured_with_result`, which also returns the parsed Pydantic object (or `None` on fallback); rewrite `invoke_structured_or_freetext` as a thin wrapper so the other three callers (Trader, Research Manager, Sentiment Analyst) are unaffected.
- `tradingagents/agents/managers/portfolio_manager.py` — use `invoke_structured_with_result`; put `pm_reassess_after`/`pm_reassess_trigger` (ISO date string / description string, `""` when absent) into the returned state dict.
- `tradingagents/agents/utils/agent_states.py` — declare the two new `AgentState` keys.
- `ops/pipeline_adapter.py` — `StubPipelineAdapter` gains an optional `reassess` constructor param so tests can inject the two values without a real LLM.
- `ops/research/vetting.py` — in `vet_memo`'s confirm branch, read the two values off `result.raw` and append a `Catalyst` to `memo.catalysts` before persisting.
- `ops/research/monitor.py` — widen `_check_memo`'s catalyst-due block to run for every `thesis_type` (not just `"event"`), and have a due catalyst also call `screen_store.enqueue_hit` + record `KIND_RESEARCH_ESCALATION`, exactly like the existing falsifier-escalation branch a few lines above it.

---

### Task 1: `PortfolioDecision` gains `reassess_after` / `reassess_trigger`

**Files:**
- Modify: `tradingagents/agents/schemas.py:19` (imports), `tradingagents/agents/schemas.py:188-228` (`PortfolioDecision`), `tradingagents/agents/schemas.py:231-250` (`render_pm_decision`)
- Test: `tests/test_memory_log.py`

**Interfaces:**
- Produces: `PortfolioDecision.reassess_after: date | None`, `PortfolioDecision.reassess_trigger: str | None`. `render_pm_decision` emits `**Reassess After**: YYYY-MM-DD` and `**Reassess Trigger**: ...` lines when set.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_memory_log.py`, right after `test_pm_returns_rendered_markdown_with_rating` (currently ending at line 728):

```python
    def test_pm_reassess_fields_render_when_set(self):
        captured = {}
        decision = PortfolioDecision(
            rating=PortfolioRating.UNDERWEIGHT,
            executive_summary="Trim ahead of the binary catalyst.",
            investment_thesis="Explosion risk is not priced in at this multiple.",
            reassess_after=date(2026, 8, 3),
            reassess_trigger="Starship Flight 13 outcome",
        )
        llm = _structured_pm_llm(captured, decision)
        pm_node = create_portfolio_manager(llm)
        result = pm_node(_make_pm_state())
        md = result["final_trade_decision"]
        assert "**Reassess After**: 2026-08-03" in md
        assert "**Reassess Trigger**: Starship Flight 13 outcome" in md

    def test_pm_reassess_fields_omitted_when_unset(self):
        captured = {}
        llm = _structured_pm_llm(captured)
        pm_node = create_portfolio_manager(llm)
        md = pm_node(_make_pm_state())["final_trade_decision"]
        assert "Reassess After" not in md
        assert "Reassess Trigger" not in md

    def test_pm_reassess_after_in_the_past_is_nulled(self):
        decision = PortfolioDecision(
            rating=PortfolioRating.HOLD,
            executive_summary="s",
            investment_thesis="t",
            reassess_after=date(2020, 1, 1),
        )
        assert decision.reassess_after is None

    def test_pm_reassess_after_too_far_out_is_nulled(self):
        from datetime import timedelta
        far = date.today() + timedelta(days=400)
        decision = PortfolioDecision(
            rating=PortfolioRating.HOLD,
            executive_summary="s",
            investment_thesis="t",
            reassess_after=far,
        )
        assert decision.reassess_after is None
```

`tests/test_memory_log.py` has no `datetime` import yet. Add this line after the `from unittest.mock import MagicMock, patch` import (line 3):

```python
from datetime import date
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_memory_log.py -k reassess -v`
Expected: FAIL — `PortfolioDecision` has no field `reassess_after`.

- [ ] **Step 3: Implement the schema change**

In `tradingagents/agents/schemas.py`, change the import line at the top:

```python
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator
```

Add a constant right after `_NULLISH_FLOAT` (schemas.py:30):

```python
# A reassess date must be a real near-term recheck, not a hallucinated
# far-future or already-past one — null it out rather than fail the call.
_MAX_REASSESS_HORIZON_DAYS = 365
```

In `PortfolioDecision` (schemas.py:188-228), add two fields after `time_horizon` (after line 223) and a second validator after `_nullish_float_to_none` (after line 228):

```python
    reassess_after: date | None = Field(
        default=None,
        description=(
            "If a specific future date or event should trigger automatic "
            "re-analysis of this position (e.g. a known binary catalyst — "
            "earnings, a launch, an FDA date), give that date in YYYY-MM-DD "
            "form. Null if no scheduled recheck is warranted."
        ),
    )
    reassess_trigger: str | None = Field(
        default=None,
        description=(
            "One-line description of what to check for on reassess_after, "
            "e.g. 'Starship Flight 13 outcome'. Null if reassess_after is null."
        ),
    )

    @field_validator("reassess_after", mode="before")
    @classmethod
    def _nullish_reassess_date_to_none(cls, v):
        return _coerce_optional_float(v)

    @field_validator("reassess_after")
    @classmethod
    def _bound_reassess_after(cls, v):
        if v is None:
            return v
        today = datetime.now(timezone.utc).date()
        if v < today or v > today + timedelta(days=_MAX_REASSESS_HORIZON_DAYS):
            return None
        return v
```

Update `render_pm_decision` (schemas.py:231-250) to add the new lines after the `time_horizon` block:

```python
    if decision.time_horizon:
        parts.extend(["", f"**Time Horizon**: {decision.time_horizon}"])
    if decision.reassess_after is not None:
        parts.extend(["", f"**Reassess After**: {decision.reassess_after.isoformat()}"])
        if decision.reassess_trigger:
            parts.extend(["", f"**Reassess Trigger**: {decision.reassess_trigger}"])
    return "\n".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_memory_log.py -k reassess -v`
Expected: PASS (4 tests)

Run the full file to make sure nothing else broke: `pytest tests/test_memory_log.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/agents/schemas.py tests/test_memory_log.py
git commit -m "schemas: add reassess_after/reassess_trigger to PortfolioDecision"
```

---

### Task 2: Thread the typed fields through the Portfolio Manager node into graph state

**Files:**
- Modify: `tradingagents/agents/utils/structured.py` (whole file, 80 lines), `tradingagents/agents/managers/portfolio_manager.py` (whole file, 93 lines), `tradingagents/agents/utils/agent_states.py:75-80`
- Test: `tests/test_memory_log.py`, `tests/test_structured_agents.py`

**Interfaces:**
- Consumes: `PortfolioDecision.reassess_after`/`.reassess_trigger` from Task 1.
- Produces: `invoke_structured_with_result(structured_llm, plain_llm, prompt, render, agent_name) -> tuple[str, T | None]` in `structured.py`. `portfolio_manager_node` return dict gains `"pm_reassess_after": str` (ISO date or `""`) and `"pm_reassess_trigger": str` (or `""`). `AgentState` declares both keys.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_structured_agents.py`, in the `test_invoke_structured_falls_back_when_result_is_none` area (after line 170):

```python
@pytest.mark.unit
def test_invoke_structured_with_result_returns_parsed_object_on_success():
    from tradingagents.agents.utils.structured import invoke_structured_with_result

    class Obj:
        def __init__(self):
            self.rating = "Buy"

    structured = MagicMock()
    structured.invoke.return_value = Obj()
    plain = MagicMock()

    rendered, obj = invoke_structured_with_result(
        structured, plain, "prompt", render=lambda r: r.rating, agent_name="t"
    )
    assert rendered == "Buy"
    assert isinstance(obj, Obj)
    plain.invoke.assert_not_called()


@pytest.mark.unit
def test_invoke_structured_with_result_returns_none_object_on_fallback():
    from tradingagents.agents.utils.structured import invoke_structured_with_result

    structured = MagicMock()
    structured.invoke.side_effect = ValueError("bad JSON")
    plain = MagicMock()
    plain.invoke.return_value = MagicMock(content="FREETEXT")

    rendered, obj = invoke_structured_with_result(
        structured, plain, "prompt", render=lambda r: r.rating, agent_name="t"
    )
    assert rendered == "FREETEXT"
    assert obj is None
```

Add to `tests/test_memory_log.py`, after the reassess-render tests from Task 1:

```python
    def test_pm_state_carries_reassess_fields_when_set(self):
        decision = PortfolioDecision(
            rating=PortfolioRating.UNDERWEIGHT,
            executive_summary="Trim ahead of the binary catalyst.",
            investment_thesis="Explosion risk is not priced in at this multiple.",
            reassess_after=date(2026, 8, 3),
            reassess_trigger="Starship Flight 13 outcome",
        )
        llm = _structured_pm_llm({}, decision)
        pm_node = create_portfolio_manager(llm)
        result = pm_node(_make_pm_state())
        assert result["pm_reassess_after"] == "2026-08-03"
        assert result["pm_reassess_trigger"] == "Starship Flight 13 outcome"

    def test_pm_state_reassess_fields_empty_when_unset(self):
        llm = _structured_pm_llm({})
        pm_node = create_portfolio_manager(llm)
        result = pm_node(_make_pm_state())
        assert result["pm_reassess_after"] == ""
        assert result["pm_reassess_trigger"] == ""

    def test_pm_state_reassess_fields_empty_on_freetext_fallback(self):
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError("provider unsupported")
        llm.invoke.return_value = MagicMock(content="**Rating**: Hold")
        pm_node = create_portfolio_manager(llm)
        result = pm_node(_make_pm_state())
        assert result["pm_reassess_after"] == ""
        assert result["pm_reassess_trigger"] == ""
```

(`MagicMock` is already imported at the top of `tests/test_memory_log.py` for the existing fallback test at line 735.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_structured_agents.py -k invoke_structured_with_result -v`
Expected: FAIL — no such function.

Run: `pytest tests/test_memory_log.py -k pm_state -v`
Expected: FAIL — `KeyError: 'pm_reassess_after'`.

- [ ] **Step 3: Implement**

Replace the body of `tradingagents/agents/utils/structured.py` from `def invoke_structured_or_freetext` (line 49) onward with:

```python
def invoke_structured_with_result(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> tuple[str, T | None]:
    """Like the markdown-only path below, but also returns the parsed object.

    Some agents (the Portfolio Manager) need a typed field that doesn't
    belong in the rendered markdown. The second return value is None
    whenever the free-text fallback fired — there is no structured object
    on that path.
    """
    if structured_llm is not None:
        try:
            result = structured_llm.invoke(prompt)
            if result is None:
                raise ValueError("structured output returned no parsed result")
            return render(result), result
        except Exception as exc:
            logger.warning(
                "%s: structured-output invocation failed (%s); retrying once as free text",
                agent_name, exc,
            )

    response = plain_llm.invoke(prompt)
    return response.content, None


def invoke_structured_or_freetext(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """Run the structured call and render to markdown; fall back to free-text on any failure.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape). The same value is forwarded to the free-text path so the
    fallback sees the same input the structured call did.
    """
    rendered, _ = invoke_structured_with_result(structured_llm, plain_llm, prompt, render, agent_name)
    return rendered
```

In `tradingagents/agents/managers/portfolio_manager.py`, change the import (line 18-21):

```python
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_with_result,
)
```

Replace the invocation and return block (lines 66-90):

```python
        final_trade_decision, decision_obj = invoke_structured_with_result(
            structured_llm,
            llm,
            prompt,
            render_pm_decision,
            "Portfolio Manager",
        )
        reassess_after = (
            decision_obj.reassess_after.isoformat()
            if decision_obj is not None and decision_obj.reassess_after is not None
            else ""
        )
        reassess_trigger = (
            decision_obj.reassess_trigger
            if decision_obj is not None and decision_obj.reassess_trigger
            else ""
        )

        new_risk_debate_state = {
            "judge_decision": final_trade_decision,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
            "pm_reassess_after": reassess_after,
            "pm_reassess_trigger": reassess_trigger,
        }
```

In `tradingagents/agents/utils/agent_states.py`, after `final_trade_decision` (line 75):

```python
    final_trade_decision: Annotated[str, "Final decision made by the Risk Analysts"]
    pm_reassess_after: Annotated[str, "ISO date (YYYY-MM-DD) the Portfolio Manager flagged for scheduled re-analysis, or empty"]
    pm_reassess_trigger: Annotated[str, "One-line description of what to recheck at pm_reassess_after, or empty"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_structured_agents.py tests/test_memory_log.py -v`
Expected: all PASS

- [ ] **Step 5: Run the full unit suite to check for regressions in Trader/Research Manager/Sentiment Analyst**

Run: `pytest -m unit -k "trader or research_manager or sentiment" -v`
Expected: all PASS (they call the unchanged `invoke_structured_or_freetext` wrapper)

- [ ] **Step 6: Commit**

```bash
git add tradingagents/agents/utils/structured.py tradingagents/agents/managers/portfolio_manager.py tradingagents/agents/utils/agent_states.py tests/test_structured_agents.py tests/test_memory_log.py
git commit -m "agents: thread PM reassess_after/reassess_trigger into graph state"
```

---

### Task 3: `StubPipelineAdapter` can inject reassess values for tests

**Files:**
- Modify: `ops/pipeline_adapter.py:167-206` (`StubPipelineAdapter`)
- Test: `tests/ops/test_pipeline_adapter.py`

**Interfaces:**
- Consumes: nothing new from prior tasks (this is test plumbing only — production `TradingAgentsPipelineAdapter` needs no change since `raw = final_state`, which already carries `pm_reassess_after`/`pm_reassess_trigger` once Task 2 lands).
- Produces: `StubPipelineAdapter(reassess={"TICK": ("2026-08-03", "Some catalyst")})`; `result.raw["pm_reassess_after"]` / `result.raw["pm_reassess_trigger"]` are populated for that symbol, `("", "")` for every other symbol.

- [ ] **Step 1: Write the failing test**

Add to `tests/ops/test_pipeline_adapter.py`, near `test_stub_adapter_default_rating_is_hold` (around line 215):

```python
def test_stub_adapter_reassess_defaults_empty():
    result = StubPipelineAdapter().propagate("X", date(2026, 7, 9))
    assert result.raw["pm_reassess_after"] == ""
    assert result.raw["pm_reassess_trigger"] == ""


def test_stub_adapter_reassess_injectable_per_symbol():
    stub = StubPipelineAdapter(reassess={"SPCX": ("2026-08-03", "Starship Flight 13 outcome")})
    result = stub.propagate("SPCX", date(2026, 7, 9))
    assert result.raw["pm_reassess_after"] == "2026-08-03"
    assert result.raw["pm_reassess_trigger"] == "Starship Flight 13 outcome"
    other = stub.propagate("OTHER", date(2026, 7, 9))
    assert other.raw["pm_reassess_after"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ops/test_pipeline_adapter.py -k reassess -v`
Expected: FAIL — `KeyError: 'pm_reassess_after'`.

- [ ] **Step 3: Implement**

In `ops/pipeline_adapter.py`, change `StubPipelineAdapter.__init__` (lines 177-185):

```python
    def __init__(
        self,
        decisions: dict[str, PipelineDecision] | None = None,
        ratings: dict[str, str] | None = None,
        tiers: dict[str, str] | None = None,
        reassess: dict[str, tuple[str, str]] | None = None,
    ):
        self._decisions = decisions or {}
        self._ratings = ratings or {}
        self._tiers = tiers or {}
        self._reassess = reassess or {}
```

Change `propagate` (lines 187-197) to build `raw` with the two new keys:

```python
    def propagate(
        self, symbol: str, asof_date: date, research_context: str = "",
    ) -> PipelineResult:
        decision = self._decisions.get(symbol, PipelineDecision.HOLD)
        reassess_after, reassess_trigger = self._reassess.get(symbol, ("", ""))
        raw = {
            "final_trade_decision": "",
            "pm_reassess_after": reassess_after,
            "pm_reassess_trigger": reassess_trigger,
            "risk_debate_state": {
                "history": f"stub risk debate for {symbol}",
                "judge_decision": "stub judge decision",
            },
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ops/test_pipeline_adapter.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/pipeline_adapter.py tests/ops/test_pipeline_adapter.py
git commit -m "pipeline_adapter: let StubPipelineAdapter inject PM reassess fields"
```

---

### Task 4: `vet_memo` appends a `Catalyst` from the PM's reassess fields

**Files:**
- Modify: `ops/research/vetting.py:27-47` (imports), `ops/research/vetting.py:155-203` (`vet_memo`)
- Test: `tests/ops/research/test_vetting.py`

**Interfaces:**
- Consumes: `result.raw["pm_reassess_after"]` / `result.raw["pm_reassess_trigger"]` (Task 2/3), `Catalyst(description: str, expected_date: date | None, hard_date: bool)` (`tradingagents/memos/schema.py:126-139`).
- Produces: on confirm, `memo.catalysts` gains one more `Catalyst` entry when `pm_reassess_after` is non-empty; unchanged when empty (existing behavior for every current test).

- [ ] **Step 1: Write the failing tests**

Add to `tests/ops/research/test_vetting.py`, after `test_inverted_map_confirms_a_short_on_bearish_ratings` (around line 120):

```python
def test_confirm_appends_catalyst_from_pm_reassess_fields(store):
    memo = _memo()
    store.save(memo)
    adapter = StubPipelineAdapter(
        ratings={"ACME": "Buy"},
        reassess={"ACME": ("2026-08-03", "Starship Flight 13 outcome")},
    )
    vet_memo(memo, adapter=adapter, falsifier_llm=NoFalsifierLLM(), memo_store=store)
    got = store.get(memo.memo_id)
    assert len(got.catalysts) == 1
    cat = got.catalysts[0]
    assert cat.description == "Starship Flight 13 outcome"
    assert cat.expected_date == date(2026, 8, 3)
    assert cat.hard_date is True


def test_confirm_without_pm_reassess_fields_adds_no_catalyst(store):
    memo = _memo()
    store.save(memo)
    adapter = StubPipelineAdapter(ratings={"ACME": "Buy"})
    vet_memo(memo, adapter=adapter, falsifier_llm=NoFalsifierLLM(), memo_store=store)
    got = store.get(memo.memo_id)
    assert got.catalysts == []


def test_reject_ignores_pm_reassess_fields(store):
    memo = _memo()
    store.save(memo)
    adapter = StubPipelineAdapter(
        ratings={"ACME": "Hold"},
        reassess={"ACME": ("2026-08-03", "irrelevant")},
    )
    vet_memo(memo, adapter=adapter, falsifier_llm=NoFalsifierLLM(), memo_store=store)
    got = store.get(memo.memo_id)
    assert got.status == "rejected"
    assert got.catalysts == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ops/research/test_vetting.py -k reassess -v`
Expected: FAIL — `assert len(got.catalysts) == 1` fails (`0 == 1`) since nothing populates it yet.

- [ ] **Step 3: Implement**

In `ops/research/vetting.py`, change the imports (lines 27-47):

```python
from tradingagents.agents.utils.structured import bind_structured
from tradingagents.memos.schema import (
    Catalyst,
    ConvictionTier,
    Falsifier,
    Memo,
    VettingResult,
)
```

In `vet_memo` (lines 155-203), insert the catalyst append right before `memo_store.apply_vetting(memo)` in the confirm branch (after line 191, before line 192's `memo.status = "open"` — precise insertion is right after `memo.conviction_tier = tier`):

```python
    added, notes = extract_risk_falsifiers(
        falsifier_llm, result.raw, ticker=memo.ticker,
    )
    if notes:
        rationale = (rationale + "\n[vetting] " + "; ".join(notes))[:MAX_RATIONALE_CHARS + 500]
    indices = list(range(len(memo.falsifiers), len(memo.falsifiers) + len(added)))
    conviction_before = memo.conviction_tier
    memo.falsifiers = memo.falsifiers + added
    memo.conviction_tier = tier
    reassess_after = str(result.raw.get("pm_reassess_after") or "")
    if reassess_after:
        reassess_trigger = str(result.raw.get("pm_reassess_trigger") or "") or "PM-scheduled reassessment"
        memo.catalysts = memo.catalysts + [
            Catalyst(
                description=reassess_trigger,
                expected_date=date.fromisoformat(reassess_after),
                hard_date=True,
            )
        ]
    memo.status = "open"
```

Add `date` to the existing `datetime` import at the top of the file (line 32):

```python
from datetime import date, datetime, timezone
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ops/research/test_vetting.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add ops/research/vetting.py tests/ops/research/test_vetting.py
git commit -m "vetting: append a hard-dated Catalyst from the PM's reassess fields on confirm"
```

---

### Task 5: Monitor checks catalysts on every thesis type and requeues research when one is due

**Files:**
- Modify: `ops/research/monitor.py:1-16` (module docstring), `ops/research/monitor.py:102-173` (`_check_memo`)
- Test: `tests/ops/research/test_monitor.py`

**Interfaces:**
- Consumes: `memo.catalysts` (all thesis types, `tradingagents/memos/schema.py:315`), `screen_store.enqueue_hit` (`ops/research/store.py:116-135`), `_escalation_payload` (`monitor.py:44-58`, unchanged).
- Produces: a due hard-dated catalyst on ANY thesis-type memo now (a) records `KIND_CATALYST_DUE` (unchanged), and (b) calls `screen_store.enqueue_hit` + records `KIND_RESEARCH_ESCALATION`, incrementing `outcome.escalations` — mirroring the falsifier-escalation branch immediately above it in the same function.

- [ ] **Step 1: Write the failing tests**

The existing test `test_lapsed_hard_catalyst_surfaces_for_event_memo` (`tests/ops/research/test_monitor.py:152-164`) only checks `catalyst_due`; extend it to also assert the escalation, and add a new test proving a **value**-thesis memo's PM-driven catalyst now escalates too (previously it was silently ignored). Replace the existing test and add the new one right after it:

```python
def test_lapsed_hard_catalyst_surfaces_for_event_memo(stores):
    memo_store, screen_store, journal = stores
    memo_store.save(_memo(
        ticker="SPIN", thesis_type="event",
        key_dates=[Catalyst(description="distribution date",
                            expected_date=date(2026, 6, 30), hard_date=True)],
    ))
    outcome = _run(stores)
    assert outcome.catalyst_due == 1
    assert outcome.escalations == 1
    due = _events_of(journal, events.KIND_CATALYST_DUE)
    assert len(due) == 1 and due[0]["payload"]["ticker"] == "SPIN"
    assert [h["symbol"] for h in screen_store.pending_hits()] == ["SPIN"]
    # Soft/future dates never fire: re-run dedupes too (pending hit already queued).
    outcome2 = _run(stores)
    assert outcome2.catalyst_due == 0
    assert outcome2.escalations == 0


def test_pm_reassess_catalyst_on_value_memo_also_escalates(stores):
    """A value-thesis memo can carry a PM-scheduled reassess date (Task 4)
    even though it has no event_block — the monitor must not silently
    ignore top-level catalysts just because thesis_type != 'event'."""
    memo_store, screen_store, journal = stores
    memo_store.save(_memo(
        ticker="SPCX", thesis_type="value",
        catalysts=[Catalyst(description="Starship Flight 13 outcome",
                            expected_date=date(2026, 6, 30), hard_date=True)],
    ))
    outcome = _run(stores)
    assert outcome.catalyst_due == 1
    assert outcome.escalations == 1
    assert [h["symbol"] for h in screen_store.pending_hits()] == ["SPCX"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ops/research/test_monitor.py -k "catalyst" -v`
Expected: `test_pm_reassess_catalyst_on_value_memo_also_escalates` FAILS with `outcome.catalyst_due == 0` (the `thesis_type == "event"` gate skips it); the modified `test_lapsed_hard_catalyst_surfaces_for_event_memo` FAILS on `outcome.escalations == 1` (currently 0, no enqueue happens).

- [ ] **Step 3: Implement**

In `ops/research/monitor.py`, update the module docstring bullet (line 8):

```
  - a lapsed hard-dated catalyst (any thesis type — including one the
    Portfolio Manager scheduled at vetting time) escalates like a tripped
    falsifier;
```

Replace the catalyst block in `_check_memo` (lines 151-173) — remove the `thesis_type == "event"` gate and add the escalation call:

```python
    catalysts = list(memo.catalysts)
    if memo.event_block is not None:
        catalysts += list(memo.event_block.key_dates)
    for i, catalyst in enumerate(catalysts):
        if not (catalyst.hard_date and catalyst.expected_date
                and catalyst.expected_date <= today):
            continue
        if _recently_notified(
            journal, events.KIND_CATALYST_DUE, now=now,
            memo_id=memo.memo_id, catalyst_index=str(i),
        ):
            continue
        outcome.catalyst_due += 1
        journal.record_event(
            events.KIND_CATALYST_DUE,
            events.catalyst_due_payload(
                memo_id=memo.memo_id, ticker=memo.ticker,
                catalyst_index=str(i), description=catalyst.description,
                expected_date=catalyst.expected_date.isoformat(),
            ),
            at=now,
        )
        reason = f"catalyst due: {catalyst.description}"
        hit_id = screen_store.enqueue_hit(
            memo.ticker, asof=today,
            payload=_escalation_payload(memo.ticker, today, reason),
        )
        if hit_id is not None:
            outcome.escalations += 1
            journal.record_event(
                events.KIND_RESEARCH_ESCALATION,
                events.research_escalation_payload(
                    ticker=memo.ticker, memo_id=memo.memo_id,
                    reason=reason, hit_id=hit_id,
                ),
                at=now,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ops/research/test_monitor.py -v`
Expected: all PASS

- [ ] **Step 5: Run the full unit suite**

Run: `pytest -m unit -v`
Expected: all PASS, no regressions in `tests/ops/test_cli_research_monitor.py` or `tests/ops/test_main.py` (both import `monitor_memos`).

- [ ] **Step 6: Commit**

```bash
git add ops/research/monitor.py tests/ops/research/test_monitor.py
git commit -m "monitor: check catalysts on every thesis type; requeue research when one is due"
```

---

## End-to-end result

After all five tasks: the Portfolio Manager can emit `reassess_after`/`reassess_trigger` in its one existing structured call → `vet_memo` (already running in the vetting/confirm path, already holding the memo) writes it onto the memo as a `Catalyst` with zero extra LLM calls or parsing → the existing daily `monitor_memos` tick sees the date arrive and pushes the ticker back onto the same research queue `ops/research/drain.py` already drains with retries. Nothing new to schedule, store, or parse.
