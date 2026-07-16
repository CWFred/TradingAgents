# Conviction-Weighted Momentum Sleeve (v2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The momentum sleeve acts on the full 5-tier PM rating (Overweight → starter-size buy, Underweight → half-trim), sizes entries by conviction tier, and can displace starter positions to fund high-conviction buys.

**Architecture:** The rating→action mapping widens in `ops/pipeline_adapter.py` (pure function + enum). Sizing moves from one flat notional to a two-rung ladder in `ops/strategy/post_earnings_momentum.py`. A new pure planner `ops/strategy/displacement.py` decides trims/skips; `ops/scheduler/orchestrator.py` executes the plan against the guarded broker and journals every action. Spec: `docs/superpowers/specs/2026-07-14-conviction-weighted-momentum-design.md`.

**Tech Stack:** Python 3.11+, dataclasses, Decimal money math, sqlite journal, pytest.

## Global Constraints

- Momentum sleeve only. Do not touch `ops/research/`, `ops/insider/`, short-sleeve modules, guardrail rules, or the live gate.
- All money math uses `Decimal`; never floats. Journal payloads stringify Decimals (`str(x)`), matching every existing payload builder in `ops/events.py`.
- Tier vocabulary is exactly `"high"` and `"starter"` (constants in `ops/pipeline_adapter.py`); empty string `""` means "no tier".
- Positions with no recorded tier (opened pre-v2, e.g. DAL) are treated as `high` and are never displaceable.
- Tests must not hit the network or an LLM — use `StubPipelineAdapter` / `MagicMock` / `Journal(":memory:")`.
- Run tests with `python -m pytest <path> -v` from the worktree root. 11 pre-existing failures in `tests/ops/test_main.py` on main are known noise — never "fix" them; scope test runs to the files named in each task.
- Work on branch `feat/conviction-v2` cut from `main`, in an isolated git worktree (another agent is active in the primary checkout).
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: Worktree + docs bootstrap

**Files:**
- Create: worktree at `.worktrees/conviction-v2` (or `git worktree add` fallback path), branch `feat/conviction-v2` off `main`
- Create (in worktree): `docs/superpowers/specs/2026-07-14-conviction-weighted-momentum-design.md`, `docs/superpowers/plans/2026-07-14-conviction-weighted-momentum.md`

**Interfaces:**
- Produces: an isolated checkout every later task runs in; spec + plan committed on the feature branch.

- [ ] **Step 1: Create the worktree**

```bash
cd /Users/frednick/Code/TradingAgents
git worktree add .worktrees/conviction-v2 -b feat/conviction-v2 main
```

- [ ] **Step 2: Copy spec + plan onto the branch** (they were committed on `feat/opsdash-react-ui`, not `main`)

```bash
mkdir -p .worktrees/conviction-v2/docs/superpowers/specs .worktrees/conviction-v2/docs/superpowers/plans
cp docs/superpowers/specs/2026-07-14-conviction-weighted-momentum-design.md .worktrees/conviction-v2/docs/superpowers/specs/
cp docs/superpowers/plans/2026-07-14-conviction-weighted-momentum.md .worktrees/conviction-v2/docs/superpowers/plans/
```

- [ ] **Step 3: Sanity-check the test baseline in the worktree**

Run: `cd .worktrees/conviction-v2 && python -m pytest tests/ops/test_pipeline_adapter.py tests/ops/strategy tests/ops/scheduler/test_orchestrator.py tests/ops/test_config.py -q`
Expected: all PASS (these files are green on main).

- [ ] **Step 4: Commit**

```bash
cd .worktrees/conviction-v2
git add docs/superpowers
git commit -m "docs: spec + plan for conviction-weighted momentum v2

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Full rating→(decision, tier) mapping in the pipeline adapter

**Files:**
- Modify: `ops/pipeline_adapter.py`
- Test: `tests/ops/test_pipeline_adapter.py` (extend)

**Interfaces:**
- Consumes: nothing new.
- Produces (later tasks rely on these exact names):
  - `PipelineDecision.TRIM` (new enum member, value `"TRIM"`)
  - `TIER_HIGH = "high"`, `TIER_STARTER = "starter"` module constants
  - `parse_rating_action(text: str) -> tuple[PipelineDecision, str]`
  - `PipelineResult.tier: str` (new field, default `""`)
  - `StubPipelineAdapter(decisions=None, ratings=None, tiers=None)` — `tiers: dict[str, str]`; default tier is `TIER_HIGH` when the stub decision is BUY, else `""`
  - `parse_decision(text) -> PipelineDecision` kept as a thin wrapper (existing tests import it).

- [ ] **Step 1: Write the failing tests** — append to `tests/ops/test_pipeline_adapter.py`:

```python
from ops.pipeline_adapter import (
    TIER_HIGH,
    TIER_STARTER,
    parse_rating_action,
)


def test_parse_rating_action_full_scale():
    assert parse_rating_action("Buy") == (PipelineDecision.BUY, TIER_HIGH)
    assert parse_rating_action("Overweight") == (PipelineDecision.BUY, TIER_STARTER)
    assert parse_rating_action("Hold") == (PipelineDecision.HOLD, "")
    assert parse_rating_action("Underweight") == (PipelineDecision.TRIM, "")
    assert parse_rating_action("Sell") == (PipelineDecision.SELL, "")


def test_parse_rating_action_unknown_and_empty_default_hold():
    assert parse_rating_action("") == (PipelineDecision.HOLD, "")
    assert parse_rating_action("Banana") == (PipelineDecision.HOLD, "")


def test_parse_rating_action_strips_legacy_wrapper_and_punctuation():
    assert parse_rating_action("FINAL TRANSACTION PROPOSAL: Overweight.") == (
        PipelineDecision.BUY, TIER_STARTER,
    )


def test_parse_decision_wrapper_still_collapses_to_decision_only():
    assert parse_decision("Overweight") == PipelineDecision.BUY
    assert parse_decision("Underweight") == PipelineDecision.TRIM


def test_stub_adapter_tier_defaults_high_for_buy():
    stub = StubPipelineAdapter({"AAPL": PipelineDecision.BUY})
    result = stub.propagate("AAPL", date(2026, 7, 14))
    assert result.tier == TIER_HIGH


def test_stub_adapter_tier_explicit_and_default_empty_for_hold():
    stub = StubPipelineAdapter(
        {"AAPL": PipelineDecision.BUY, "MSFT": PipelineDecision.HOLD},
        tiers={"AAPL": TIER_STARTER},
    )
    assert stub.propagate("AAPL", date(2026, 7, 14)).tier == TIER_STARTER
    assert stub.propagate("MSFT", date(2026, 7, 14)).tier == ""
```

(Reuse the file's existing imports of `PipelineDecision`, `StubPipelineAdapter`, `parse_decision`, `date` — add any that are missing at the top.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/test_pipeline_adapter.py -v`
Expected: FAIL — `ImportError: cannot import name 'TIER_HIGH'`.

- [ ] **Step 3: Implement.** In `ops/pipeline_adapter.py`:

Add `TRIM` to the enum:

```python
class PipelineDecision(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    TRIM = "TRIM"
```

Replace the `_HIGH_CONVICTION_BUY` / `_HIGH_CONVICTION_SELL` / `_UPSTREAM_RATINGS` block (lines 54-59) and `parse_decision` (lines 62-84) with:

```python
# Conviction tiers carried on PipelineResult.tier. TIER_HIGH sizes at
# per_position_cap_pct and is what displacement funds; TIER_STARTER sizes
# at starter_position_pct and is what displacement trims. "" = no tier.
TIER_HIGH = "high"
TIER_STARTER = "starter"

# Upstream ratings are one of: Buy, Overweight, Hold, Underweight, Sell.
# v2 posture (spec 2026-07-14): the full scale acts. Overweight enters at
# starter size; Underweight trims half of a held position (the TRIM
# decision is a no-op for unheld symbols — enforced by the orchestrator,
# which is the only layer that knows holdings). Unknown text still
# defaults to HOLD.
_RATING_ACTIONS: dict[str, tuple[PipelineDecision, str]] = {
    "BUY": (PipelineDecision.BUY, TIER_HIGH),
    "OVERWEIGHT": (PipelineDecision.BUY, TIER_STARTER),
    "HOLD": (PipelineDecision.HOLD, ""),
    "UNDERWEIGHT": (PipelineDecision.TRIM, ""),
    "SELL": (PipelineDecision.SELL, ""),
}


def parse_rating_action(text: str) -> tuple[PipelineDecision, str]:
    """Map the upstream rating word to (decision, conviction tier).

    Accepts a leading 'FINAL TRANSACTION PROPOSAL: <X>' wrapper for
    defensive matching against older upstream formats."""
    if not text:
        return (PipelineDecision.HOLD, "")
    m = re.search(r"FINAL TRANSACTION PROPOSAL:\s*(\S+)", text, re.IGNORECASE)
    candidate = m.group(1) if m else text.strip().split()[0] if text.strip() else ""
    candidate = candidate.strip().rstrip(".,").upper()
    return _RATING_ACTIONS.get(candidate, (PipelineDecision.HOLD, ""))


def parse_decision(text: str) -> PipelineDecision:
    """Decision-only view of parse_rating_action (kept for existing callers)."""
    return parse_rating_action(text)[0]
```

Add the field to `PipelineResult` (after `rating: str = ""`):

```python
    # Conviction tier implied by the rating: TIER_HIGH, TIER_STARTER, or "".
    tier: str = ""
```

In `TradingAgentsPipelineAdapter.propagate`, replace `decision = parse_decision(decision_text or "")` with:

```python
        decision, tier = parse_rating_action(decision_text or "")
```

and add `tier=tier,` to the returned `PipelineResult(...)`.

In `StubPipelineAdapter`, extend `__init__` and `propagate`:

```python
    def __init__(
        self,
        decisions: dict[str, PipelineDecision] | None = None,
        ratings: dict[str, str] | None = None,
        tiers: dict[str, str] | None = None,
    ):
        self._decisions = decisions or {}
        self._ratings = ratings or {}
        self._tiers = tiers or {}
```

and in `propagate`, before the `return`:

```python
        default_tier = TIER_HIGH if decision is PipelineDecision.BUY else ""
        tier = self._tiers.get(symbol, default_tier)
```

adding `tier=tier,` to the returned `PipelineResult(...)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ops/test_pipeline_adapter.py -v`
Expected: all PASS (old tests too — the wrapper preserves `parse_decision`; note `parse_decision("Overweight")` changed from HOLD to BUY, so if an old test asserts the v1 collapse, update that assertion to the v2 mapping and cite the spec in the test comment).

- [ ] **Step 5: Commit**

```bash
git add ops/pipeline_adapter.py tests/ops/test_pipeline_adapter.py
git commit -m "feat(ops): act on full 5-tier rating — parse_rating_action + PipelineResult.tier

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Config knobs and capacity raises

**Files:**
- Modify: `ops/config.py`
- Test: `tests/ops/test_config.py` (extend)

**Interfaces:**
- Produces (exact names later tasks use):
  - `OpsConfig.starter_position_pct: Decimal` (default `Decimal("0.05")`)
  - `OpsConfig.displacement_max_trims_per_day: int` (default `2`)
  - `OpsConfig.displacement_min_holding_age_days: int` (default `3`)
  - `max_open_positions` default `12`, `daily_analysis_budget` default `12`
  - Env overrides `OPS_STARTER_POSITION_PCT`, `OPS_DISPLACEMENT_MAX_TRIMS_PER_DAY`, `OPS_DISPLACEMENT_MIN_HOLDING_AGE_DAYS`

- [ ] **Step 1: Write the failing tests** — append to `tests/ops/test_config.py`:

```python
def test_v2_posture_defaults():
    cfg = OpsConfig()
    assert cfg.starter_position_pct == Decimal("0.05")
    assert cfg.displacement_max_trims_per_day == 2
    assert cfg.displacement_min_holding_age_days == 3
    assert cfg.max_open_positions == 12
    assert cfg.daily_analysis_budget == 12


def test_starter_pct_must_be_positive_and_at_most_full_size():
    with pytest.raises(ValueError):
        OpsConfig(starter_position_pct=Decimal("0"))
    with pytest.raises(ValueError):
        OpsConfig(starter_position_pct=Decimal("0.13"))  # > per_position_cap_pct


def test_displacement_counts_must_be_positive():
    with pytest.raises(ValueError):
        OpsConfig(displacement_max_trims_per_day=0)
    with pytest.raises(ValueError):
        OpsConfig(displacement_min_holding_age_days=0)


def test_displacement_env_overrides(monkeypatch):
    monkeypatch.setenv("OPS_STARTER_POSITION_PCT", "0.03")
    monkeypatch.setenv("OPS_DISPLACEMENT_MAX_TRIMS_PER_DAY", "1")
    monkeypatch.setenv("OPS_DISPLACEMENT_MIN_HOLDING_AGE_DAYS", "5")
    cfg = load_config()
    assert cfg.starter_position_pct == Decimal("0.03")
    assert cfg.displacement_max_trims_per_day == 1
    assert cfg.displacement_min_holding_age_days == 5
```

(Match the file's existing imports: `OpsConfig`, `load_config`, `Decimal`, `pytest`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/test_config.py -v`
Expected: new tests FAIL (`unexpected keyword argument 'starter_position_pct'`); existing default-value tests for `max_open_positions == 7` / `daily_analysis_budget == 8`, if present, also fail — update those assertions to 12/12 as part of Step 3 (cite the spec).

- [ ] **Step 3: Implement.** In `OpsConfig`:

- Change `max_open_positions: int = 7` → `max_open_positions: int = 12`
- Change `daily_analysis_budget: int = 8` → `daily_analysis_budget: int = 12`
- After `per_trade_dollar_floor` add:

```python
    # v2 posture (spec 2026-07-14): Overweight enters at starter size.
    starter_position_pct: Decimal = Decimal("0.05")
    # Displacement guards: at most this many funding trims per trading day,
    # and a starter must be held this many TRADING days before it can be
    # trimmed to fund a high-conviction entry.
    displacement_max_trims_per_day: int = 2
    displacement_min_holding_age_days: int = 3
```

In `__post_init__`, after the existing cap/reserve fraction checks:

```python
        if not (Decimal("0") < self.starter_position_pct <= self.per_position_cap_pct):
            raise ValueError(
                "starter_position_pct must be in (0, per_position_cap_pct], got "
                f"{self.starter_position_pct}"
            )
        for fname in ("displacement_max_trims_per_day", "displacement_min_holding_age_days"):
            if getattr(self, fname) <= 0:
                raise ValueError(f"{fname} must be > 0, got {getattr(self, fname)}")
```

In `load_config()`, next to the `per_position_cap_pct` block:

```python
    starter_position_pct = _env_decimal("OPS_STARTER_POSITION_PCT")
    if starter_position_pct is not None:
        kwargs["starter_position_pct"] = starter_position_pct

    disp_trims = _env_int("OPS_DISPLACEMENT_MAX_TRIMS_PER_DAY")
    if disp_trims is not None:
        kwargs["displacement_max_trims_per_day"] = disp_trims

    disp_age = _env_int("OPS_DISPLACEMENT_MIN_HOLDING_AGE_DAYS")
    if disp_age is not None:
        kwargs["displacement_min_holding_age_days"] = disp_age
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ops/test_config.py -v`
Expected: all PASS. Also run `python -m pytest tests/ops/guardrails tests/ops/universe -q` — MaxOpenPositions/budget defaults feed these; update any test asserting the old 7/8 defaults (prefer constructing `OpsConfig(max_open_positions=7)` explicitly in such tests over changing expectations, so the tests express intent, not defaults).

- [ ] **Step 5: Commit**

```bash
git add ops/config.py tests/ops/test_config.py
git commit -m "feat(ops): v2 posture config — starter sizing, displacement guards, capacity raises

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Journal events — rating/tier payloads + three new kinds

**Files:**
- Modify: `ops/events.py`
- Test: `tests/ops/test_events_conviction.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces (exact names Task 6 uses):
  - `analysis_decision_payload(..., rating: str = "")` — `rating` key present only when non-empty
  - `position_opened_payload(..., tier: str | None = None)` — `tier` key present only when set
  - `KIND_DISPLACEMENT_TRIM = "displacement_trim"`, `displacement_trim_payload(*, symbol, tier, notional, funded_symbol, client_order_id)`
  - `KIND_ENTRY_SKIPPED_UNFUNDED = "entry_skipped_unfunded"`, `entry_skipped_unfunded_payload(*, symbol, shortfall, reason)`
  - `KIND_UNDERWEIGHT_TRIM = "underweight_trim"`, `underweight_trim_payload(*, symbol, rating, notional, client_order_id)`

- [ ] **Step 1: Write the failing tests** — create `tests/ops/test_events_conviction.py`:

```python
"""v2 conviction posture events: rating/tier payload fields + new kinds."""
from decimal import Decimal

from ops import events


def test_analysis_decision_payload_includes_rating_when_given():
    p = events.analysis_decision_payload(
        symbol="AAPL", decision="BUY", source="MOMENTUM", asof="2026-07-14",
        rank=3, rating="Overweight",
    )
    assert p["rating"] == "Overweight"


def test_analysis_decision_payload_omits_empty_rating():
    p = events.analysis_decision_payload(
        symbol="AAPL", decision="HOLD", source="MOMENTUM", asof="2026-07-14",
    )
    assert "rating" not in p


def test_position_opened_payload_includes_tier_when_given():
    from datetime import date
    p = events.position_opened_payload(
        symbol="AAPL", source="MOMENTUM", entry_date=date(2026, 7, 14),
        client_order_id="x", tier="starter",
    )
    assert p["tier"] == "starter"


def test_position_opened_payload_omits_missing_tier():
    from datetime import date
    p = events.position_opened_payload(
        symbol="AAPL", source="MOMENTUM", entry_date=date(2026, 7, 14),
        client_order_id="x",
    )
    assert "tier" not in p


def test_new_kinds_have_payload_builders_and_are_quiet():
    for kind in (
        events.KIND_DISPLACEMENT_TRIM,
        events.KIND_ENTRY_SKIPPED_UNFUNDED,
        events.KIND_UNDERWEIGHT_TRIM,
    ):
        assert kind in events.PAYLOAD_BUILDERS
        assert kind in events.QUIET_KINDS


def test_displacement_trim_payload_shape():
    p = events.displacement_trim_payload(
        symbol="OLDN", tier="starter", notional=Decimal("312.50"),
        funded_symbol="NEWB", client_order_id="disp-1",
    )
    assert p == {
        "symbol": "OLDN", "tier": "starter", "notional": "312.50",
        "funded_symbol": "NEWB", "client_order_id": "disp-1",
    }


def test_entry_skipped_unfunded_payload_shape():
    p = events.entry_skipped_unfunded_payload(
        symbol="NEWB", shortfall=Decimal("87.10"), reason="guards exhausted",
    )
    assert p == {"symbol": "NEWB", "shortfall": "87.10", "reason": "guards exhausted"}


def test_underweight_trim_payload_shape():
    p = events.underweight_trim_payload(
        symbol="AAPL", rating="Underweight", notional=Decimal("600.00"),
        client_order_id="uwt-1",
    )
    assert p == {
        "symbol": "AAPL", "rating": "Underweight", "notional": "600.00",
        "client_order_id": "uwt-1",
    }
```

**Note:** before writing the quiet-kinds assertion, open `ops/events.py` and confirm the actual name of the audit-only kind collection (the big tuple/frozenset around lines 175-250 that holds `KIND_ANALYSIS_DECISION`) and of the builder registry dict around line 956 (holds `KIND_ANALYSIS_DECISION: analysis_decision_payload`). Use the real names in the test — the names above (`QUIET_KINDS`, `PAYLOAD_BUILDERS`) are placeholders for whatever the module actually calls them.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/test_events_conviction.py -v`
Expected: FAIL — `AttributeError: module 'ops.events' has no attribute 'KIND_DISPLACEMENT_TRIM'` (and TypeErrors on the new kwargs).

- [ ] **Step 3: Implement.** In `ops/events.py`:

Near `KIND_ANALYSIS_DECISION` (line ~147):

```python
KIND_DISPLACEMENT_TRIM = "displacement_trim"
KIND_ENTRY_SKIPPED_UNFUNDED = "entry_skipped_unfunded"
KIND_UNDERWEIGHT_TRIM = "underweight_trim"
```

In the audit-only/quiet collection (next to `KIND_ANALYSIS_DECISION`, line ~234):

```python
    # v2 conviction posture: displacement/skip/trim breadcrumbs — the sells
    # and buys themselves already notify via KIND_FILL.
    KIND_DISPLACEMENT_TRIM,
    KIND_ENTRY_SKIPPED_UNFUNDED,
    KIND_UNDERWEIGHT_TRIM,
```

Extend `analysis_decision_payload` (line ~816) — add `rating: str = ""` keyword and, before the `rank` block:

```python
    if rating:
        payload["rating"] = rating
```

(also update its docstring: `decision` is now one of "BUY"/"HOLD"/"SELL"/"TRIM").

Extend `position_opened_payload` (line ~533) — add `tier: str | None = None` keyword and, after the `entry_rank` block:

```python
    if tier:
        payload["tier"] = tier
```

Add the three builders next to `analysis_decision_payload`:

```python
def displacement_trim_payload(
    *, symbol: str, tier: str, notional: Decimal, funded_symbol: str,
    client_order_id: str,
) -> dict[str, Any]:
    """One starter trimmed to fund a high-conviction entry (spec 2026-07-14).
    The one-line story: 'trimmed {symbol} ({tier}) to fund {funded_symbol}'."""
    return {
        "symbol": symbol, "tier": tier, "notional": str(notional),
        "funded_symbol": funded_symbol, "client_order_id": client_order_id,
    }


def entry_skipped_unfunded_payload(
    *, symbol: str, shortfall: Decimal, reason: str,
) -> dict[str, Any]:
    """A proposed entry that could not be funded even after displacement."""
    return {"symbol": symbol, "shortfall": str(shortfall), "reason": reason}


def underweight_trim_payload(
    *, symbol: str, rating: str, notional: Decimal, client_order_id: str,
) -> dict[str, Any]:
    """Half-trim of a held position on an Underweight re-analysis."""
    return {
        "symbol": symbol, "rating": rating, "notional": str(notional),
        "client_order_id": client_order_id,
    }
```

Register all three in the payload-builder registry (line ~956), alongside `KIND_ANALYSIS_DECISION: analysis_decision_payload`. Import `Decimal` at the top if the module doesn't already.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ops/test_events_conviction.py tests/ops/test_events_activity.py tests/ops/test_exit_events.py -v`
Expected: all PASS (the two existing files guard the registry/quiet-list invariants).

- [ ] **Step 5: Commit**

```bash
git add ops/events.py tests/ops/test_events_conviction.py
git commit -m "feat(ops): journal rating/tier + displacement, unfunded-skip, underweight-trim events

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Entry ladder in the momentum strategy

**Files:**
- Modify: `ops/strategy/post_earnings_momentum.py`
- Test: `tests/ops/strategy/test_post_earnings_momentum.py` (extend)

**Interfaces:**
- Consumes: `PipelineResult.tier`, `TIER_STARTER` (Task 2); `OpsConfig.starter_position_pct` (Task 3).
- Produces: `propose_orders` sizes each order by `result.tier` — `TIER_STARTER` → `starter_position_pct`, anything else → `per_position_cap_pct`. `StrategyOrder.pipeline.tier` is what the orchestrator journals; no StrategyOrder shape change.

- [ ] **Step 1: Write the failing tests** — append to `tests/ops/strategy/test_post_earnings_momentum.py` (reuse `_candidate` and the existing imports; add `TIER_STARTER` to the `ops.pipeline_adapter` import line):

```python
def test_overweight_starter_gets_starter_notional():
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter(
        {"AAPL": PipelineDecision.BUY}, tiers={"AAPL": TIER_STARTER},
    )
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("10000"), asof_date=date(2026, 7, 14),
    )
    assert len(orders) == 1
    # starter_position_pct = 5% of 10_000
    assert orders[0].order.notional_dollars == Decimal("500.00")
    assert orders[0].pipeline.tier == TIER_STARTER
    assert "starter" in orders[0].reason


def test_high_tier_still_gets_full_notional():
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"AAPL": PipelineDecision.BUY})
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("10000"), asof_date=date(2026, 7, 14),
    )
    assert orders[0].order.notional_dollars == Decimal("1200.00")


def test_starter_below_dollar_floor_is_skipped_not_upsized():
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter(
        {"AAPL": PipelineDecision.BUY}, tiers={"AAPL": TIER_STARTER},
    )
    # equity 80 -> starter notional 4.00 < per_trade_dollar_floor 5
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("80"), asof_date=date(2026, 7, 14),
    )
    assert orders == []


def test_live_cap_applies_to_starter_notional_too():
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter(
        {"AAPL": PipelineDecision.BUY}, tiers={"AAPL": TIER_STARTER},
    )
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("10000"), asof_date=date(2026, 7, 14),
        live_max_position_cap=Decimal("10"),
    )
    assert orders[0].order.notional_dollars == Decimal("10")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/strategy/test_post_earnings_momentum.py -v`
Expected: new tests FAIL (starter order sized `1200.00`, reason lacks "starter").

- [ ] **Step 3: Implement.** In `ops/strategy/post_earnings_momentum.py`:

Import `TIER_STARTER` alongside the existing pipeline imports:

```python
from ops.pipeline_adapter import PipelineAdapter, PipelineDecision, PipelineResult, TIER_STARTER
```

Update `_reason_for` to take the result and tag starters:

```python
def _reason_for(cand: Candidate, result: PipelineResult) -> str:
    suffix = (
        "pipeline BUY (Overweight starter)"
        if result.tier == TIER_STARTER
        else "pipeline BUY"
    )
    if cand.source is CandidateSource.EARNINGS:
        return (
            f"post-earnings beat (EPS {cand.earnings.eps_actual} vs "
            f"est {cand.earnings.eps_estimate}); {suffix}"
        )
    return (
        f"6-mo momentum leader (ret {cand.momentum.trailing_return_6m}, "
        f"> 200d MA); {suffix}"
    )
```

Replace the body of `propose_orders` from the `notional = ...` line down:

```python
        full_notional = _quantize_money(current_equity * self._cfg.per_position_cap_pct)
        starter_notional = _quantize_money(current_equity * self._cfg.starter_position_pct)
        if live_max_position_cap is not None:
            full_notional = min(full_notional, live_max_position_cap)
            starter_notional = min(starter_notional, live_max_position_cap)
        # Even the full-size rung under the floor means no order can ever
        # clear it — bail before spending any LLM budget (v1 behavior kept).
        if full_notional < self._cfg.per_trade_dollar_floor:
            return []
        out: list[StrategyOrder] = []
        for cand in candidates:
            result = pipeline.propagate(cand.symbol, asof_date)
            if decision_sink is not None:
                decision_sink.append(AnalyzedDecision(candidate=cand, pipeline=result))
            if result.decision != PipelineDecision.BUY:
                continue
            notional = starter_notional if result.tier == TIER_STARTER else full_notional
            if notional < self._cfg.per_trade_dollar_floor:
                continue
            order = Order(
                client_order_id=_client_order_id(cand.symbol, asof_date),
                symbol=cand.symbol,
                side=Side.BUY,
                notional_dollars=notional,
                order_type=OrderType.MARKET,
                stop_pct=self._cfg.per_position_stop_pct,
            )
            out.append(StrategyOrder(
                order=order,
                reason=_reason_for(cand, result),
                candidate=cand,
                pipeline=result,
            ))
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ops/strategy/test_post_earnings_momentum.py -v`
Expected: all PASS. One existing test asserts equity 250 → notional `30.00`; that still holds (high tier). If any existing test asserted the old single-notional early-return behavior for sub-floor equity, it still holds via the `full_notional` guard.

- [ ] **Step 5: Commit**

```bash
git add ops/strategy/post_earnings_momentum.py tests/ops/strategy/test_post_earnings_momentum.py
git commit -m "feat(ops): two-rung entry ladder — starter sizing for Overweight buys

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Displacement planner (pure module)

**Files:**
- Create: `ops/strategy/displacement.py`
- Test: `tests/ops/strategy/test_displacement.py` (create)

**Interfaces:**
- Consumes: `TIER_HIGH`/`TIER_STARTER` (Task 2), `OpsConfig` displacement knobs (Task 3), `StrategyOrder` (existing), `Position.market_value(price)` (existing), `ops.trading_time.trading_days_between(start: date, end: date) -> int` (existing).
- Produces (exact names Task 7 uses):

```python
@dataclass(frozen=True)
class PlannedTrim:
    symbol: str
    tier: str
    notional: Decimal      # dollars to sell
    funded_symbol: str

@dataclass(frozen=True)
class UnfundedSkip:
    symbol: str
    shortfall: Decimal
    reason: str

@dataclass(frozen=True)
class DisplacementPlan:
    trims: list[PlannedTrim]
    funded_client_order_ids: frozenset[str]
    skips: list[UnfundedSkip]

def plan_displacement(
    *, proposals: list[StrategyOrder], positions: list[Position],
    provenance: dict[str, dict], quote: Callable[[str], Decimal],
    cash: Decimal, equity: Decimal, trims_used_today: int,
    asof_date: date, config: OpsConfig,
) -> DisplacementPlan: ...
```

- [ ] **Step 1: Write the failing tests** — create `tests/ops/strategy/test_displacement.py`:

```python
"""Displacement planner: trims starters, oldest first, to fund high-tier buys.

Pure-function tests — no broker, no journal. `provenance` is the
position_opened payload per symbol (entry_date + tier), exactly what
Journal.latest_event_payload_by_symbol returns.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

from ops.broker.base import BrokerError
from ops.broker.types import Order, OrderType, Position, Side
from ops.config import OpsConfig
from ops.pipeline_adapter import TIER_HIGH, TIER_STARTER
from ops.strategy.base import StrategyOrder
from ops.strategy.displacement import plan_displacement

ASOF = date(2026, 7, 14)


def _proposal(symbol, notional, tier=TIER_HIGH):
    order = Order(
        client_order_id=f"pem-{symbol}", symbol=symbol, side=Side.BUY,
        notional_dollars=Decimal(notional), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    pipeline = MagicMock()
    pipeline.tier = tier
    return StrategyOrder(order=order, reason="t", candidate=MagicMock(), pipeline=pipeline)


def _position(symbol, qty="10", price="10"):
    return Position(symbol=symbol, quantity=Decimal(qty), avg_entry_price=Decimal(price))


def _prov(entry_date, tier="starter"):
    return {"entry_date": entry_date, "tier": tier}


def _plan(**overrides):
    defaults = dict(
        proposals=[], positions=[], provenance={},
        quote=lambda s: Decimal("10"),
        cash=Decimal("0"), equity=Decimal("10000"), trims_used_today=0,
        asof_date=ASOF, config=OpsConfig(),
    )
    defaults.update(overrides)
    return plan_displacement(**defaults)


def test_funds_from_free_cash_without_trimming():
    # reserve floor = 16% of 10_000 = 1600; cash 3000 leaves 1400 available
    plan = _plan(proposals=[_proposal("NEWB", "1200")], cash=Decimal("3000"))
    assert plan.trims == []
    assert plan.funded_client_order_ids == frozenset({"pem-NEWB"})
    assert plan.skips == []


def test_high_tier_shortfall_trims_oldest_starter_first():
    plan = _plan(
        proposals=[_proposal("NEWB", "1200")],
        cash=Decimal("2400"),  # available = 800, shortfall = 400
        positions=[_position("OLD1", qty="30"), _position("OLD2", qty="30")],
        provenance={"OLD1": _prov("2026-07-06"), "OLD2": _prov("2026-07-01")},
    )
    # OLD2 is older -> trimmed first; 400 shortfall < 300 value? no: value 300
    # each -> takes OLD2 fully (300) then 100 from OLD1.
    assert [(t.symbol, t.notional) for t in plan.trims] == [
        ("OLD2", Decimal("300.00")), ("OLD1", Decimal("100.00")),
    ]
    assert all(t.funded_symbol == "NEWB" for t in plan.trims)
    assert plan.funded_client_order_ids == frozenset({"pem-NEWB"})


def test_starter_proposal_never_triggers_trims():
    plan = _plan(
        proposals=[_proposal("NEWB", "500", tier=TIER_STARTER)],
        cash=Decimal("1600"),  # available = 0
        positions=[_position("OLD1", qty="100")],
        provenance={"OLD1": _prov("2026-07-01")},
    )
    assert plan.trims == []
    assert plan.funded_client_order_ids == frozenset()
    assert len(plan.skips) == 1 and plan.skips[0].symbol == "NEWB"


def test_min_holding_age_gate():
    plan = _plan(
        proposals=[_proposal("NEWB", "100")],
        cash=Decimal("1600"),
        # entered Friday 07-10; Tue 07-14 is only 2 trading days later -> immune
        positions=[_position("OLD1", qty="100")],
        provenance={"OLD1": _prov("2026-07-10")},
    )
    assert plan.trims == []
    assert plan.skips[0].symbol == "NEWB"


def test_max_trims_per_day_budget_respected():
    cfg = OpsConfig()  # displacement_max_trims_per_day = 2
    plan = _plan(
        proposals=[_proposal("NEWB", "1000")],
        cash=Decimal("1600"),  # available 0, shortfall 1000
        positions=[
            _position("OLD1", qty="30"), _position("OLD2", qty="30"),
            _position("OLD3", qty="30"),
        ],
        provenance={
            "OLD1": _prov("2026-07-01"), "OLD2": _prov("2026-07-02"),
            "OLD3": _prov("2026-07-03"),
        },
        config=cfg,
    )
    # needs 1000 but 2 trims x 300 = 600 max -> nothing trimmed, buy skipped
    assert plan.trims == []
    assert plan.funded_client_order_ids == frozenset()
    assert "displacement guards" in plan.skips[0].reason


def test_trims_used_today_counts_against_budget():
    plan = _plan(
        proposals=[_proposal("NEWB", "400")],
        cash=Decimal("1600"),
        positions=[_position("OLD1", qty="30"), _position("OLD2", qty="30")],
        provenance={"OLD1": _prov("2026-07-01"), "OLD2": _prov("2026-07-02")},
        trims_used_today=1,  # only 1 trim left; 400 needs two 300-value starters
    )
    assert plan.trims == []
    assert plan.funded_client_order_ids == frozenset()


def test_untiered_legacy_position_is_immune():
    plan = _plan(
        proposals=[_proposal("NEWB", "100")],
        cash=Decimal("1600"),
        positions=[_position("DAL", qty="100")],
        provenance={"DAL": {"entry_date": "2026-07-01"}},  # no tier key (pre-v2)
    )
    assert plan.trims == []


def test_high_tier_position_is_never_trimmed():
    plan = _plan(
        proposals=[_proposal("NEWB", "100")],
        cash=Decimal("1600"),
        positions=[_position("BIGW", qty="100")],
        provenance={"BIGW": _prov("2026-07-01", tier="high")},
    )
    assert plan.trims == []


def test_quote_failure_skips_that_starter_and_continues():
    def quote(sym):
        if sym == "OLD1":
            raise BrokerError("no quote")
        return Decimal("10")
    plan = _plan(
        proposals=[_proposal("NEWB", "200")],
        cash=Decimal("1600"),
        positions=[_position("OLD1", qty="100"), _position("OLD2", qty="30")],
        provenance={"OLD1": _prov("2026-07-01"), "OLD2": _prov("2026-07-02")},
        quote=quote,
    )
    assert [t.symbol for t in plan.trims] == ["OLD2"]
    assert plan.funded_client_order_ids == frozenset({"pem-NEWB"})


def test_high_before_starter_ordering_and_partial_funding():
    # available 1400: high (1200) funds first, starter (500) then falls short.
    plan = _plan(
        proposals=[
            _proposal("STRT", "500", tier=TIER_STARTER),
            _proposal("HIGH", "1200"),
        ],
        cash=Decimal("3000"),
    )
    assert plan.funded_client_order_ids == frozenset({"pem-HIGH"})
    assert plan.skips[0].symbol == "STRT"


def test_two_high_proposals_share_starters_without_double_spending():
    plan = _plan(
        proposals=[_proposal("NEW1", "300"), _proposal("NEW2", "300")],
        cash=Decimal("1600"),  # available 0
        positions=[_position("OLD1", qty="60")],  # value 600, one position
        provenance={"OLD1": _prov("2026-07-01")},
    )
    # One starter can fund both via two partial trims of the same symbol —
    # but that is TWO trims against the daily budget of 2.
    assert [(t.symbol, t.notional, t.funded_symbol) for t in plan.trims] == [
        ("OLD1", Decimal("300.00"), "NEW1"),
        ("OLD1", Decimal("300.00"), "NEW2"),
    ]
    assert plan.funded_client_order_ids == frozenset({"pem-NEW1", "pem-NEW2"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/strategy/test_displacement.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.strategy.displacement'`.

- [ ] **Step 3: Implement** — create `ops/strategy/displacement.py`:

```python
"""Displacement planner: trim starter positions to fund high-conviction buys.

Pure planning — no broker calls except the injected quote function, no
journal writes. The orchestrator executes the returned plan (spec
2026-07-14, "Displacement engine"). Guards, all from OpsConfig:

- starters only (tier from position_opened provenance; missing tier =
  pre-v2 position = immune),
- oldest entry_date first, partial trims allowed,
- at most displacement_max_trims_per_day trims per trading day (planned
  trims here + trims_used_today already journaled),
- a starter must be >= displacement_min_holding_age_days TRADING days old,
- up-ladder only: trims fund TIER_HIGH proposals exclusively; a starter
  proposal that lacks cash is skipped, never funded by displacement.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ops.broker.base import BrokerError
from ops.broker.types import Position
from ops.config import OpsConfig
from ops.pipeline_adapter import TIER_HIGH, TIER_STARTER
from ops.strategy.base import StrategyOrder
from ops.trading_time import trading_days_between


@dataclass(frozen=True)
class PlannedTrim:
    symbol: str
    tier: str
    notional: Decimal
    funded_symbol: str


@dataclass(frozen=True)
class UnfundedSkip:
    symbol: str
    shortfall: Decimal
    reason: str


@dataclass(frozen=True)
class DisplacementPlan:
    trims: list[PlannedTrim]
    funded_client_order_ids: frozenset[str]
    skips: list[UnfundedSkip]


def _quantize_money(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"))


def _proposal_tier(p: StrategyOrder) -> str:
    return TIER_STARTER if getattr(p.pipeline, "tier", "") == TIER_STARTER else TIER_HIGH


def _trimmable_starters(
    positions: list[Position],
    provenance: dict[str, dict],
    quote: Callable[[str], Decimal],
    asof_date: date,
    min_age_days: int,
) -> tuple[list[str], dict[str, Decimal]]:
    """Starter symbols oldest-first plus their remaining trimmable value."""
    aged: list[tuple[str, Position]] = []
    for pos in positions:
        payload = provenance.get(pos.symbol)
        if not payload or payload.get("tier") != TIER_STARTER:
            continue  # untiered (pre-v2) and high positions are immune
        entry = payload.get("entry_date")
        if not entry:
            continue
        if trading_days_between(date.fromisoformat(entry), asof_date) < min_age_days:
            continue
        aged.append((entry, pos))
    aged.sort(key=lambda t: t[0])
    ordered: list[str] = []
    value: dict[str, Decimal] = {}
    for _, pos in aged:
        try:
            px = quote(pos.symbol)
        except BrokerError:
            continue  # unquotable starter: skip it, never block the plan
        ordered.append(pos.symbol)
        value[pos.symbol] = pos.market_value(px)
    return ordered, value


def plan_displacement(
    *,
    proposals: list[StrategyOrder],
    positions: list[Position],
    provenance: dict[str, dict],
    quote: Callable[[str], Decimal],
    cash: Decimal,
    equity: Decimal,
    trims_used_today: int,
    asof_date: date,
    config: OpsConfig,
) -> DisplacementPlan:
    # Spendable cash above the reserve floor. Trims convert position value
    # to cash without changing equity, so the floor is constant all plan.
    available = cash - equity * config.cash_reserve_pct
    trim_budget = max(0, config.displacement_max_trims_per_day - trims_used_today)

    ordered_starters, remaining_value = _trimmable_starters(
        positions, provenance, quote, asof_date,
        config.displacement_min_holding_age_days,
    )

    trims: list[PlannedTrim] = []
    skips: list[UnfundedSkip] = []
    funded: set[str] = set()

    # High-conviction proposals get first claim on cash AND on trims.
    ordered = sorted(proposals, key=lambda p: 0 if _proposal_tier(p) == TIER_HIGH else 1)
    for p in ordered:
        need = p.order.notional_dollars
        if available >= need:
            available -= need
            funded.add(p.order.client_order_id)
            continue
        if _proposal_tier(p) != TIER_HIGH:
            skips.append(UnfundedSkip(
                symbol=p.order.symbol,
                shortfall=need - available,
                reason="insufficient cash; starter entries never displace",
            ))
            continue
        shortfall = need - available
        planned_here: list[PlannedTrim] = []
        for sym in ordered_starters:
            if len(trims) + len(planned_here) >= trim_budget:
                break
            value = remaining_value.get(sym, Decimal("0"))
            if value <= 0:
                continue
            take = min(value, shortfall)
            planned_here.append(PlannedTrim(
                symbol=sym, tier=TIER_STARTER,
                notional=_quantize_money(take), funded_symbol=p.order.symbol,
            ))
            shortfall -= take
            if shortfall <= 0:
                break
        if shortfall > 0:
            # All-or-nothing per proposal: never trim for a buy that still
            # cannot be placed afterward.
            skips.append(UnfundedSkip(
                symbol=p.order.symbol,
                shortfall=shortfall,
                reason="shortfall remains after displacement guards",
            ))
            continue
        for t in planned_here:
            remaining_value[t.symbol] -= t.notional
        trims.extend(planned_here)
        available = Decimal("0")  # cash was fully consumed; trims covered the rest
        funded.add(p.order.client_order_id)

    return DisplacementPlan(
        trims=trims,
        funded_client_order_ids=frozenset(funded),
        skips=skips,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ops/strategy/test_displacement.py -v`
Expected: all PASS. If `test_min_holding_age_gate` disagrees with `trading_days_between` semantics, check `ops/trading_time.py:70` and adjust the *test's* entry date (not the `<` comparison) so the scenario is 2 trading days old.

- [ ] **Step 5: Commit**

```bash
git add ops/strategy/displacement.py tests/ops/strategy/test_displacement.py
git commit -m "feat(ops): displacement planner — starters fund high-conviction entries

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Orchestrator wiring — execute the plan, journal everything

**Files:**
- Modify: `ops/scheduler/orchestrator.py` (the proposals loop inside `_tick_impl`, plus two new private methods)
- Test: `tests/ops/scheduler/test_orchestrator.py` (extend)

**Interfaces:**
- Consumes: `plan_displacement`/`DisplacementPlan` (Task 6), event kinds + payload builders (Task 4), `PipelineDecision.TRIM` (Task 2), existing `Journal.count_events(kind, since=)`, `Journal.latest_event_payload_by_symbol(kind)`, `trading_day_start(now)`.
- Produces: journal streams `displacement_trim`, `entry_skipped_unfunded`, `underweight_trim`; `position_opened` payloads carry `tier`; `analysis_decision` payloads carry `rating`.

- [ ] **Step 1: Write the failing tests** — append to `tests/ops/scheduler/test_orchestrator.py`. Add these helpers next to `_strategy_order` (note: `_fake_strategy` must now populate the `decision_sink` kwarg, so add a sink-aware variant):

```python
from datetime import timedelta

from ops import events
from ops.pipeline_adapter import PipelineDecision, PipelineResult, TIER_STARTER


def _pipeline_result(symbol, decision=PipelineDecision.BUY, tier="high", rating="Buy"):
    return PipelineResult(
        symbol=symbol, date=date(2026, 7, 14), decision=decision,
        rating=rating, tier=tier,
    )


def _strategy_order_tiered(symbol, notional="50", tier="high", rating="Buy"):
    order = Order(
        client_order_id=f"b-{symbol}", symbol=symbol, side=Side.BUY,
        notional_dollars=Decimal(notional), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    )
    return StrategyOrder(
        order=order, reason="test", candidate=MagicMock(symbol=symbol),
        pipeline=_pipeline_result(symbol, tier=tier, rating=rating),
    )


def _fake_strategy_with_sink(proposals, decisions):
    """Strategy fake that also fills the decision_sink like the real one."""
    strat = MagicMock()

    def side_effect(*, decision_sink=None, **kwargs):
        if decision_sink is not None:
            decision_sink.extend(decisions)
        return proposals

    strat.propose_orders.side_effect = side_effect
    return strat


def _decision(symbol, decision, rating, tier=""):
    from ops.strategy.base import AnalyzedDecision
    return AnalyzedDecision(
        candidate=MagicMock(symbol=symbol, momentum=None),
        pipeline=_pipeline_result(symbol, decision=decision, tier=tier, rating=rating),
    )


def _events_of(journal, kind):
    import json
    rows = journal._conn.execute(
        "SELECT payload FROM events WHERE kind = ?", (kind,),
    ).fetchall()
    return [json.loads(r[0]) for r in rows]
```

Then the tests:

```python
def test_analysis_decision_journals_native_rating(tmp_path):
    journal = _make_journal()
    strat = _fake_strategy_with_sink(
        [], [_decision("AAPL", PipelineDecision.HOLD, rating="Overweight")],
    )
    orch = _make_orchestrator(journal=journal, strategy=strat)
    orch.tick()
    payloads = _events_of(journal, events.KIND_ANALYSIS_DECISION)
    assert payloads and payloads[0]["rating"] == "Overweight"


def test_position_opened_carries_tier(tmp_path):
    journal = _make_journal()
    so = _strategy_order_tiered("AAPL", tier=TIER_STARTER, rating="Overweight")
    strat = _fake_strategy_with_sink([so], [])
    orch = _make_orchestrator(journal=journal, strategy=strat)
    orch.tick()
    payloads = _events_of(journal, events.KIND_POSITION_OPENED)
    assert payloads and payloads[0]["tier"] == TIER_STARTER


def test_displacement_trim_executed_and_journaled(tmp_path):
    from ops.config import OpsConfig
    journal = _make_journal()
    # Aged starter on the books: provenance via a prior position_opened event.
    journal.record_event(
        events.KIND_POSITION_OPENED,
        events.position_opened_payload(
            symbol="OLDS", source="MOMENTUM", entry_date=date(2026, 7, 1),
            client_order_id="old", tier="starter",
        ),
    )
    starter_pos = MagicMock(symbol="OLDS")
    starter_pos.market_value.return_value = Decimal("600")
    broker = _fake_broker(
        positions=[starter_pos], equity=Decimal("10000"), cash=Decimal("1600"),
    )  # available = 0 -> full shortfall
    broker.get_quote.return_value = Decimal("10")
    so = _strategy_order_tiered("NEWB", notional="500", tier="high")
    strat = _fake_strategy_with_sink([so], [])
    orch = _make_orchestrator(
        journal=journal, strategy=strat, broker=broker, config=OpsConfig(),
    )
    orch.tick()
    trims = _events_of(journal, events.KIND_DISPLACEMENT_TRIM)
    assert len(trims) == 1
    assert trims[0]["symbol"] == "OLDS"
    assert trims[0]["funded_symbol"] == "NEWB"
    # SELL for the trim and BUY for the entry both placed
    sides = [c.args[0].side for c in broker.place_order.call_args_list]
    assert Side.SELL in sides and Side.BUY in sides


def test_unfunded_buy_is_skipped_and_journaled(tmp_path):
    from ops.config import OpsConfig
    journal = _make_journal()
    broker = _fake_broker(equity=Decimal("10000"), cash=Decimal("1600"))
    so = _strategy_order_tiered("NEWB", notional="500", tier="high")
    strat = _fake_strategy_with_sink([so], [])
    orch = _make_orchestrator(
        journal=journal, strategy=strat, broker=broker, config=OpsConfig(),
    )
    orch.tick()
    skips = _events_of(journal, events.KIND_ENTRY_SKIPPED_UNFUNDED)
    assert skips and skips[0]["symbol"] == "NEWB"
    assert broker.place_order.call_count == 0
    assert _events_of(journal, events.KIND_POSITION_OPENED) == []


def test_underweight_trim_sells_half_of_held_position(tmp_path):
    journal = _make_journal()
    pos = MagicMock(symbol="AAPL")
    pos.market_value.return_value = Decimal("1200")
    broker = _fake_broker(
        positions=[pos], equity=Decimal("10000"), cash=Decimal("5000"),
    )
    broker.get_quote.return_value = Decimal("120")
    strat = _fake_strategy_with_sink(
        [], [_decision("AAPL", PipelineDecision.TRIM, rating="Underweight")],
    )
    orch = _make_orchestrator(journal=journal, strategy=strat, broker=broker)
    orch.tick()
    trims = _events_of(journal, events.KIND_UNDERWEIGHT_TRIM)
    assert trims and trims[0]["symbol"] == "AAPL"
    assert trims[0]["notional"] == "600.00"
    sell = broker.place_order.call_args_list[0].args[0]
    assert sell.side == Side.SELL and sell.notional_dollars == Decimal("600.00")


def test_underweight_trim_ignored_when_not_held(tmp_path):
    journal = _make_journal()
    broker = _fake_broker(positions=[])
    strat = _fake_strategy_with_sink(
        [], [_decision("GHOST", PipelineDecision.TRIM, rating="Underweight")],
    )
    orch = _make_orchestrator(journal=journal, strategy=strat, broker=broker)
    orch.tick()
    assert _events_of(journal, events.KIND_UNDERWEIGHT_TRIM) == []
    assert broker.place_order.call_count == 0
```

**Note on `_fake_broker` positions:** `MagicMock(symbol=s)` positions work for the displacement path because the planner only touches `pos.symbol` and `pos.market_value(px)`. Confirm `_make_orchestrator`'s default `momentum_finder`/`members_loader` keep the exit engine quiet (they return `[]` — they do).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/scheduler/test_orchestrator.py -v`
Expected: new tests FAIL — no `rating` key, no `tier` key, no displacement/underweight events, `place_order` called for the unfunded buy.

- [ ] **Step 3: Implement.** In `ops/scheduler/orchestrator.py`:

Add imports:

```python
from uuid import uuid4

from ops.broker.types import Order, OrderType, Side
from ops.pipeline_adapter import PipelineDecision
from ops.strategy.displacement import plan_displacement
```

Replace the `with self._pipeline_adapter.session():` block's body (the `proposals` placement loop and the `decisions` journaling loop) with:

```python
        with self._pipeline_adapter.session():
            decisions: list = []
            proposals = self._strategy.propose_orders(
                candidates=fresh_candidates,
                pipeline=self._pipeline_adapter,
                current_equity=current_equity,
                asof_date=asof_date,
                live_max_position_cap=live_cap,
                decision_sink=decisions,
            )
            self._place_entries(proposals, asof_date, now)
            self._apply_underweight_trims(decisions, asof_date)
            for decision in decisions:
                cand = decision.candidate
                self._journal.record_event(
                    events.KIND_ANALYSIS_DECISION,
                    events.analysis_decision_payload(
                        symbol=cand.symbol,
                        decision=decision.pipeline.decision.value,
                        source=cand.source.value,
                        asof=asof_date.isoformat(),
                        rank=cand.momentum.rank if cand.momentum else None,
                        rating=decision.pipeline.rating,
                    ),
                )
```

Add the two methods after `_tick_impl` (before `_run_exits`):

```python
    def _place_entries(self, proposals, asof_date, now) -> None:
        """Fund and place proposed BUYs, displacing starters when a
        high-conviction entry lacks cash (spec 2026-07-14). All-or-nothing
        per proposal: a buy the plan could not fund is skipped (and
        journaled), not fired into a CashReserveRule rejection."""
        if not proposals:
            return
        plan = plan_displacement(
            proposals=proposals,
            positions=list(self._broker.get_positions()),
            provenance=self._journal.latest_event_payload_by_symbol(
                events.KIND_POSITION_OPENED,
            ),
            quote=self._broker.get_quote,
            cash=self._broker.get_cash(),
            equity=self._broker.get_equity(),
            trims_used_today=self._journal.count_events(
                events.KIND_DISPLACEMENT_TRIM, since=trading_day_start(now),
            ),
            asof_date=asof_date,
            config=self._config,
        )
        for trim in plan.trims:
            order = Order(
                client_order_id=(
                    f"disp-{asof_date.isoformat()}-{trim.symbol}-{uuid4().hex[:8]}"
                ),
                symbol=trim.symbol,
                side=Side.SELL,
                notional_dollars=trim.notional,
                order_type=OrderType.MARKET,
            )
            try:
                self._broker.place_order(order)
            except OrderRejected:
                # Funded buy may now bounce off CashReserveRule — that
                # existing rejection path is the safety net, not an error.
                continue
            except BrokerError:
                return
            self._journal.record_event(
                events.KIND_DISPLACEMENT_TRIM,
                events.displacement_trim_payload(
                    symbol=trim.symbol,
                    tier=trim.tier,
                    notional=trim.notional,
                    funded_symbol=trim.funded_symbol,
                    client_order_id=order.client_order_id,
                ),
            )
        for skip in plan.skips:
            self._journal.record_event(
                events.KIND_ENTRY_SKIPPED_UNFUNDED,
                events.entry_skipped_unfunded_payload(
                    symbol=skip.symbol,
                    shortfall=skip.shortfall,
                    reason=skip.reason,
                ),
            )
        for proposal in proposals:
            if proposal.order.client_order_id not in plan.funded_client_order_ids:
                continue
            try:
                self._broker.place_order(proposal.order)
            except OrderRejected:
                continue
            except BrokerError:
                break
            cand = proposal.candidate
            self._journal.record_event(
                events.KIND_POSITION_OPENED,
                events.position_opened_payload(
                    symbol=cand.symbol,
                    source=cand.source.value,
                    entry_date=asof_date,
                    client_order_id=proposal.order.client_order_id,
                    entry_rank=cand.momentum.rank if cand.momentum else None,
                    tier=proposal.pipeline.tier or None,
                ),
            )

    def _apply_underweight_trims(self, decisions, asof_date) -> None:
        """Sell half of any held position the pipeline rated Underweight.
        Dormant today (fresh_candidates excludes held names) but wired so
        the TRIM signal acts the moment held names are re-analyzed."""
        trim_decisions = [
            d for d in decisions
            if d.pipeline.decision is PipelineDecision.TRIM
        ]
        if not trim_decisions:
            return
        held = {p.symbol: p for p in self._broker.get_positions()}
        for d in trim_decisions:
            pos = held.get(d.candidate.symbol)
            if pos is None:
                continue
            try:
                px = self._broker.get_quote(pos.symbol)
            except BrokerError:
                self._journal.record_event(
                    events.KIND_EXIT_SKIPPED_MISSING_DATA,
                    events.exit_skipped_missing_data_payload(
                        symbol=pos.symbol,
                        reason="underweight trim skipped: no quote",
                    ),
                )
                continue
            notional = (pos.market_value(px) * Decimal("0.5")).quantize(Decimal("0.01"))
            if notional <= 0:
                continue
            order = Order(
                client_order_id=(
                    f"uwt-{asof_date.isoformat()}-{pos.symbol}-{uuid4().hex[:8]}"
                ),
                symbol=pos.symbol,
                side=Side.SELL,
                notional_dollars=notional,
                order_type=OrderType.MARKET,
            )
            try:
                self._broker.place_order(order)
            except OrderRejected:
                continue
            except BrokerError:
                return
            self._journal.record_event(
                events.KIND_UNDERWEIGHT_TRIM,
                events.underweight_trim_payload(
                    symbol=pos.symbol,
                    rating=d.pipeline.rating,
                    notional=notional,
                    client_order_id=order.client_order_id,
                ),
            )
```

Note: `cand.source.value` in `_place_entries` — the tiered test helper's `MagicMock(symbol=symbol)` candidate returns a Mock for `source.value`; JSON serialization of a Mock raises. Give the helper's candidate a real source: in `_strategy_order_tiered`, use `MagicMock(symbol=symbol, momentum=None, source=MagicMock(value="MOMENTUM"))` — adjust the helper when writing it if this bites.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ops/scheduler/test_orchestrator.py -v`
Expected: all PASS, including the pre-existing placement tests — `test_tick_places_buy_when_strategy_proposes_order` uses `_fake_broker()` (equity 1000 / cash 500, reserve floor 160 → $50 order is funded from free cash) and `_strategy_order` whose `pipeline=MagicMock()`; `_place_entries` reads `proposal.pipeline.tier or None` — a Mock `tier` is truthy and not JSON-serializable, so `position_opened_payload` would choke. Fix the OLD helper `_strategy_order` to use `pipeline=MagicMock(tier="", rating="Buy")`... no — simpler and honest: update `_strategy_order` to build a real `PipelineResult` via `_pipeline_result(symbol)`. Do that in Step 1 while adding the helpers.

- [ ] **Step 5: Run the wider ops suite**

Run: `python -m pytest tests/ops -q --deselect tests/ops/test_main.py`
Expected: PASS except any of the 11 known `test_main.py` failures (deselected) and the overnight-window research tests if running outside 00:00–08:00 local — both are pre-existing noise per Global Constraints. Investigate anything else.

- [ ] **Step 6: Commit**

```bash
git add ops/scheduler/orchestrator.py tests/ops/scheduler/test_orchestrator.py
git commit -m "feat(ops): orchestrator executes displacement plan + underweight trims, journals rating/tier

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Full verification + branch wrap-up

**Files:**
- No new files; verification only.

- [ ] **Step 1: Full ops + adapter test run**

Run: `python -m pytest tests/ops tests/ops/test_pipeline_adapter.py -q`
Expected: green except the known noise (11 `test_main.py` failures; overnight-window research tests outside 00:00–08:00). Record the exact failure list and diff it against main's (`git stash` not needed — run the same command on the `main` worktree if unsure) to prove no regressions.

- [ ] **Step 2: Spec-conformance sweep**

Re-read `docs/superpowers/specs/2026-07-14-conviction-weighted-momentum-design.md` section by section and check each against the code: mapping table, ladder numbers (12%/5%), guards (2/day, 3 trading days, up-ladder only), config table values, journaling shapes, "explicitly unchanged" list (grep that `ops/research/`, `ops/guardrails/` rule files, and `ops/live_gate.py` have no diff: `git diff main --stat`).

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin feat/conviction-v2
gh pr create --title "feat(ops): conviction-weighted momentum sleeve (v2 posture)" --body "$(cat <<'EOF'
Implements docs/superpowers/specs/2026-07-14-conviction-weighted-momentum-design.md.

- Act on the full 5-tier rating: Overweight -> starter-size BUY, Underweight -> half TRIM of held names; native rating now journaled on analysis_decision.
- Two-rung entry ladder: Buy 12% / Overweight 5% (starter_position_pct).
- Displacement engine: starters (oldest first, partial OK) fund high-conviction entries; max 2 trims/day, 3-trading-day min age, up-ladder only, all-or-nothing per proposal.
- Capacity: max_open_positions 7->12, daily_analysis_budget 8->12.
- Unchanged: drawdown kill switches, stops, deny list, live gate, research/short/insider sleeves.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Report** — summarize test results, any deviations from the plan, and the deploy reminder: after merge, deploy to the `TradingAgents-live` checkout and restart the daemon gracefully (never `launchctl kickstart -k`); watch the first week's `analysis_decision.rating` distribution.

---

## Self-Review Notes (done at plan time)

- **Spec coverage:** mapping table → Task 2; observability `rating` → Tasks 4+7; ladder → Tasks 3+5; displacement + guards → Tasks 6+7; capacity raises → Task 3; error handling (quote failures, unquotable TRIM, validation) → Tasks 6 (planner skip), 7 (`exit_skipped_missing_data` reuse), 3 (`__post_init__`); testing section → each task's tests; rollout → Task 8 Step 4.
- **Type consistency:** `parse_rating_action -> tuple[PipelineDecision, str]`; `PipelineResult.tier: str`; `plan_displacement` signature matches Task 7's call site; payload builder kwargs match Task 7's calls.
- **Known risk:** exact texts of pre-existing tests (old default assertions, `_strategy_order` Mock pipeline) may need the small updates called out inline in Tasks 3, 5, and 7 — each is flagged where it bites.
