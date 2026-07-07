# Phase B: The Brain — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `ops research run` consumes pending screen hits and turns each into a schema-valid, citation-resolving structured memo with machine-checkable falsifiers, using only local models — and rejects garbage instead of storing it.

**Architecture:** Three new deterministic data primitives (filing section extraction, YoY section diff, Form 4 XML parsing) feed a two-stage LLM pipeline in `ops/research/brain.py`: stage 1 makes one bounded structured-output call per filing section to extract *cited* evidence (uncited items are stripped mechanically); stage 2 runs a bear-case pass then emits a `MemoDraft` through the existing `structured.py`/Pydantic path, which code assembles into the existing `Memo` schema and validates mechanically (falsifier machine-checkability, citation resolution, precedent enforcement) with one retry before marking the hit `failed`. There is **no agentic tool loop** — local models loop on tools (see `docs/ds4-backend.md` gotchas), so orchestration is plain Python and the LLM only ever answers bounded prompts. The same primitives are additionally exposed as LangChain `@tool` wrappers (build-order step 4's "agent tools") for the existing graph, and the Form 4 parser unlocks the deferred insider-cluster screen trigger.

**Tech Stack:** Python 3.10+, stdlib (`xml.etree`, `difflib`, `re`), Pydantic v2, LangChain structured output via `tradingagents/agents/utils/structured.py`, the existing `tradingagents/llm_clients` provider registry, SQLite stores already in the repo, pytest (all network and all LLMs mocked).

**Spec:** `docs/superpowers/specs/2026-07-06-finish-research-system-design.md`, section "Phase B — The brain". Companion rationale: `docs/long_horizon_research.md` (build-order steps 4–5). Read both once before starting.

## Global Constraints

- Work happens in `/Users/frednick/Code/TradingAgents` on a new branch `feat/phase-b-brain` cut from **updated** `main` (`git checkout main && git pull` first — local main is behind origin). Never commit to `main` directly.
- **Task 0 runs first** — it is the spec's A6 acceptance gate (the 2026-07-06 calibration failed it) and Phase B's memos are only as good as the screen data feeding them. Tasks 1–9 may proceed while Task 0's live re-run is pending user input, but the PR must not merge with the gate unresolved.
- The working tree has unrelated user-modified files (`main.py` at repo root, `tradingagents/dataflows/reddit.py`) — NEVER `git add` them; always stage explicit file lists, never `git add -A` or `git add .`.
- Lint: `ruff check <files you touched>` must pass (line-length 100, py310+). Pre-existing errors in untouched files are not yours to fix.
- Tests: pytest; new test modules set `pytestmark = pytest.mark.unit`; ALL network and ALL LLM calls mocked/injected. Full suite green before every commit: `pytest tests/ -q` (baseline before this plan: 1332 passed, 13 skipped, 69 subtests).
- Money math in `Decimal`; convert at I/O boundaries with `Decimal(str(x))`. Pydantic memo fields that are `float` stay `float` (they are calibration data, not money).
- Local models only: no code path in this plan may require an API key to import or test. Upgrading a stage to an API model must remain a pure config change (that is what Task 6 builds).
- No new journal event kinds are added in this phase (nothing here touches money). If you find yourself adding one, STOP and re-read the spec.
- Never run `launchctl`. The only live-network step is the optional user-gated smoke in Task 10.
- **Escalation rule for the implementer:** if an instruction contradicts what you find in the code (a function signature differs, a fixture doesn't exist), STOP and report BLOCKED with details. Do not improvise around it.

## File structure (what this plan touches)

| File | Task | Responsibility |
|---|---|---|
| `ops/research/screener.py`, `tradingagents/dataflows/fundamentals.py` | 0 | A6 coverage-gate prerequisite: honest bar diagnostics + XBRL concept fallbacks |
| `tradingagents/dataflows/edgar_sections.py` (new) | 1, 2 | deterministic Item-section extraction from 10-K/10-Q text + aligned YoY diff |
| `tradingagents/dataflows/form4.py` (new) | 3 | Form 4 ownership-XML parser → typed insider transactions |
| `ops/research/triggers.py`, `ops/research/run.py` | 4 | insider-cluster change trigger, wired into the weekly screen |
| `tradingagents/memos/store.py`, `tradingagents/agents/utils/filing_reader_tools.py` (new) | 5 | default memo-store path + the four `@tool` wrappers (build-order step 4) |
| `ops/config.py`, `ops/research/models.py` (new) | 6 | memo-store path + per-stage model specs (`OPS_RESEARCH_*_MODEL`) → LangChain LLMs |
| `ops/research/memo_validation.py` (new) | 7 | mechanical accept/reject: falsifiers, citations, precedents |
| `ops/research/brain.py` (new) | 8 | reading plan → evidence passes → bear case → memo emission → validated `Memo` |
| `ops/research/store.py`, `ops/cli.py` | 9 | `failed` hit status + `ops research run --max-names N` batch entry point |
| `docs/research_brain.md` (new), `docs/long_horizon_research.md` | 10 | runbook, build-order checkmarks, PR |

Key repo facts the implementer must know (verified 2026-07-06):

- `tradingagents/dataflows/edgar.py` already provides `list_filings(ticker, *, forms, since, limit)` → `list[Filing]`, `fetch_filing_text(filing, *, max_chars)` → flattened plain text (one text chunk per line), `Filing.url`, `Filing.accession_number`, `CHANGE_TRIGGER_FORMS`, and a module-global 8 req/s throttle. All EDGAR I/O in this plan goes through these (or `_throttled_get` for raw Form 4 XML).
- `tradingagents/memos/schema.py` already defines `Memo`, `EvidenceItem`, `Falsifier`, `Catalyst`, `ReturnScenario`, `ValueThesis`, `EventThesis` (Pydantic v2) and `tradingagents/memos/store.py` defines `MemoStore` with `save`, `get`, `list(ticker=…)`, `mark_passed`, `open_memos`. **Do not modify the `Memo` schema.**
- `tradingagents/agents/utils/structured.py` provides `bind_structured(llm, schema, agent_name)` → structured llm or `None`. The brain uses `bind_structured` + direct `.invoke()` and treats `None`/exceptions as **rejection** — it must NOT use `invoke_structured_or_freetext` (free-text fallback would store unvalidated garbage, the exact failure mode this phase exists to prevent).
- `tradingagents/llm_clients.create_llm_client(provider, model, base_url)` → client with `.get_llm()` returning a LangChain chat model. Provider `openai_compatible` (keyless, `require_base_url`) covers LM Studio (`http://localhost:1234/v1`) and ds4 (`http://127.0.0.1:8000/v1`).
- `ops/llm_backend.py` provides `load_managed_backend_config()` (reads `OPS_LLM_MANAGED_BACKEND`) and `build_managed_backend(config)` → object with `.ensure_up()` / `.shutdown()`. `NullManagedBackend` when unmanaged.
- `ops/research/store.py` `ScreenStore.pending_hits()` returns oldest-first dicts: `{"id": int, "run_id": str, "symbol": str, "asof": "YYYY-MM-DD", "status": "pending", "payload": dict}` where `payload` is the JSON-ified `ScreenResult` (keys: `symbol, asof, passed, cheap, quality, valuation_bars, quality_bars, triggers, market_cap, ev_ebit`; bars have `name, passed, detail`; triggers have `kind, description, date, source`).
- `ops/research/prices.py` `fetch_price_context(symbol)` → `PriceContext` with `close_on_or_before(when)`; already paced via `yf_pacing.call_paced`.
- Test layout: `tradingagents`-layer tests live flat in `tests/` (`tests/test_edgar.py` shows the EDGAR mocking pattern: monkeypatch `edgar.requests.get` via `_install_routes`, set `SEC_EDGAR_USER_AGENT`, zero `_MIN_REQUEST_INTERVAL`); ops-layer tests live in `tests/ops/research/`.

---

### Task 0 (PREREQUISITE — spec A6 gate): screen-coverage diagnosis + concept fallbacks

The 2026-07-06 live calibration run (200-name sample, recorded in
`docs/research_screener.md` under "Calibration runs") failed the spec's A6
acceptance gate: `ev_ebit_vs_sector` computed for only 43% of screened names
(gate: ≥60%; `fcf_yield` passed at 71%). The spec requires this tuning task
before Phase B's brain runs on real hits. Two causes, in likely order of size:

1. **The coverage metric conflates "blind" with "seen and judged".**
   `_ev_ebit_bar`'s detail says `missing:` when `_ev_ebit()` returns `None`,
   but that happens for unprofitable names (EBIT ≤ 0) too — a small-cap
   sample is full of them, and for those the screener *saw* the data and
   correctly judged the bar unpassable. Same bug in `_debt_ebitda_bar`
   (EBITDA ≤ 0) and `_pe_history_bar` (negative EPS). Only true data gaps
   may count as `missing:`.
2. **Thin XBRL concept chains.** `EBIT_CONCEPTS = ("OperatingIncomeLoss",)`
   is a single tag; filers that don't use it currently produce no EBIT at
   all. (`_debt_by_year` already treats untagged debt as zero when a balance
   sheet exists, so debt is rarely the blocker.)

**Files:**
- Modify: `ops/research/screener.py`, `tradingagents/dataflows/fundamentals.py`
- Test: extend `tests/ops/research/test_screener.py`, `tests/test_fundamentals.py`
- Docs: `docs/research_screener.md` (re-run results)

**Interfaces:** no signature changes visible to other tasks; `Bar.detail`
gains two new prefixes — `unprofitable:` and `not-meaningful:` — which the
coverage aggregator in `ops/research/run.py` (counts only `missing:`)
automatically treats as computed. Do NOT touch the aggregator.

- [ ] **Step 0a: Failing tests for honest bar details.** Read
`tests/ops/research/test_screener.py` first; reuse its `NameInputs`/
`Fundamentals` helpers. Append (adapt helper names to the file):

```python
def test_negative_ebit_is_unprofitable_not_missing():
    # EBIT tagged and negative: the screener SAW the data; only true gaps
    # count as missing for coverage purposes.
    inputs = _inputs(ebit=Decimal("-5"), total_debt=Decimal("10"))
    result = screen_universe([inputs] * 6, asof=ASOF)[0]
    bar = next(b for b in result.valuation_bars if b.name == "ev_ebit_vs_sector")
    assert bar.detail.startswith("unprofitable:")


def test_untagged_ebit_is_missing():
    inputs = _inputs(ebit=None, total_debt=Decimal("10"))
    result = screen_universe([inputs] * 6, asof=ASOF)[0]
    bar = next(b for b in result.valuation_bars if b.name == "ev_ebit_vs_sector")
    assert bar.detail.startswith("missing:")


def test_negative_ebitda_and_negative_eps_are_unprofitable():
    inputs = _inputs(ebitda=Decimal("-3"), eps_last=Decimal("-1"))
    result = screen_universe([inputs] * 6, asof=ASOF)[0]
    debt_bar = next(b for b in result.quality_bars if b.name == "debt_to_ebitda")
    pe_bar = next(b for b in result.valuation_bars if b.name == "pe_vs_own_history")
    assert debt_bar.detail.startswith("unprofitable:")
    assert pe_bar.detail.startswith("unprofitable:")
```

The assertion contract: negative-but-tagged fundamentals → `unprofitable:`;
absent tags → `missing:`; both still FAIL the bar (`passed=False` —
unchanged behavior, only the detail prefix changes).

- [ ] **Step 0b: Implement the detail split in `ops/research/screener.py`.**
`_ev_ebit` stays as-is; add a blocker-classifier and thread it into the bar:

```python
def _ev_ebit_blocker(inputs: NameInputs) -> str:
    f = inputs.fundamentals
    if f.ebit is None:
        return "missing: EBIT not tagged in XBRL facts"
    if f.ebit <= _ZERO:
        return "unprofitable: EBIT <= 0"
    if f.total_debt is None:
        return "missing: no balance sheet (debt unknown)"
    return "not-meaningful: enterprise value <= 0"
```

`_ev_ebit_bar` gains the blocker string as a parameter and uses it verbatim
when `ev_ebit is None` (replace the current hardcoded `"missing: EV/EBIT
not computable ..."` detail); `screen_universe` passes
`_ev_ebit_blocker(n)` alongside the value. In `_debt_ebitda_bar`, split the
current combined check: `f.ebitda is None or f.total_debt is None` →
`missing:` details naming which; `f.ebitda <= _ZERO` → `"unprofitable:
EBITDA <= 0"`. In `_pe_history_bar`, split `not eps` (`missing: no EPS
history`) from `eps[-1].value <= _ZERO` (`unprofitable: negative current
EPS`).

Run: `pytest tests/ops/research/test_screener.py -v` — Expected: PASS
(including all pre-existing tests; if a pre-existing test asserts one of the
old detail strings verbatim, update that string only — never the pass/fail
logic).

- [ ] **Step 0c: EBIT fallback chain in `fundamentals.py`.** Failing test
first (append to `tests/test_fundamentals.py`, following its facts-dict
fixture style — read the file first):

```python
def test_ebit_falls_back_to_pretax_plus_interest():
    # No OperatingIncomeLoss tagged; EBIT ≈ pretax income + interest expense
    # (the standard reconstruction; ignores other non-operating items).
    facts = _facts({
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
            {2024: "80", 2025: "90"},
        "InterestExpense": {2024: "20", 2025: "10"},
    })
    f = compute_fundamentals("WIDG", facts, asof=ASOF)
    assert f.ebit == Decimal("100")
```

Then implement: add `INTEREST_EXPENSE_CONCEPTS = ("InterestExpense",
"InterestExpenseNonoperating", "InterestAndDebtExpense")` next to the other
chains, and a derivation used only when the direct chain is empty:

```python
def _ebit_by_year(facts: dict, *, asof: date) -> dict[date, Decimal]:
    direct = _by_year(annual_series(facts, EBIT_CONCEPTS, asof=asof))
    if direct:
        return direct
    # Reconstruction for filers that never tag OperatingIncomeLoss:
    # EBIT ≈ pretax income + interest expense (ignores other non-operating
    # items — acceptable for a screen-stage cheapness bar).
    pretax = _by_year(annual_series(facts, PRETAX_CONCEPTS, asof=asof))
    interest = _by_year(annual_series(facts, INTEREST_EXPENSE_CONCEPTS, asof=asof))
    return {y: pretax[y] + interest[y] for y in sorted(set(pretax) & set(interest))}
```

Rewire `compute_fundamentals` to use `_ebit_by_year` (it currently builds
`ebit_pts`/`ebit_by_year` from `annual_series(facts, EBIT_CONCEPTS, ...)`
directly; `ebit = _latest(...)` becomes the latest value of the returned
dict: `ebit = ebit_by_year[max(ebit_by_year)] if ebit_by_year else None`).
`_roic_history` and the EBITDA composition consume `ebit_by_year` and pick
the fallback up for free.

- [ ] **Step 0d: Re-run the calibration and gate.** Live network step
(needs `SEC_EDGAR_USER_AGENT` — ask the user if unset; never commit it):

```bash
set -a; source .env 2>/dev/null; set +a
.venv/bin/python -m ops.cli screen --limit 200 --dry-run
```

Append the new coverage table to `docs/research_screener.md` under
`## Calibration runs` with the date and "after Task 0 tuning". **Gate:**
`ev_ebit_vs_sector` and `fcf_yield` ≥ 60%. If still below after the metric
fix + EBIT fallback, STOP and report BLOCKED with a tally of the remaining
`missing:` details from stderr (the user decides whether to accept the
residual or extend more chains — do not keep adding concepts speculatively).

- [ ] **Step 0e: Full suite, lint, commit**

```bash
pytest tests/ -q
ruff check ops/research/screener.py tradingagents/dataflows/fundamentals.py tests/ops/research/test_screener.py tests/test_fundamentals.py
git add ops/research/screener.py tradingagents/dataflows/fundamentals.py tests/ops/research/test_screener.py tests/test_fundamentals.py docs/research_screener.md
git commit -m "fix(research): honest coverage diagnostics + EBIT concept fallback (A6 gate)"
```

---

### Task 1: Deterministic filing-section extraction

**Files:**
- Create: `tradingagents/dataflows/edgar_sections.py`
- Test: `tests/test_edgar_sections.py`

**Interfaces:**
- Produces (Tasks 2, 5, 8 rely on these exact names):
  - `class SectionNotFound(ValueError)`
  - `@dataclass(frozen=True) FilingSection: ticker: str; accession: str; section: str; form: str; text: str` with property `source_ref -> str` returning `f"{accession}:{section}"`
  - `extract_section(text: str, *, form: str, section: str, max_chars: int = 12000) -> str`
  - `read_filing_section(ticker: str, accession: str, section: str, *, max_chars: int = 12000, list_filings=None, fetch_text=None) -> FilingSection`
  - `SECTION_ITEMS: dict[str, dict[str, str]]` mapping form family → section key → Item number
- Section keys (the only valid `section` values): `"business"`, `"risk_factors"`, `"mdna"`, `"full"`.

**Why the heuristic works:** `fetch_filing_text` flattens HTML to one text chunk per line. A 10-K contains each Item heading at least twice — once in the table of contents (followed immediately by another heading) and once in the body (followed by pages of prose). Collecting every candidate span (a start-heading match up to the next any-Item heading) and keeping the **longest** span deterministically picks the body occurrence and ignores the TOC.

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for deterministic SEC filing section extraction."""

from datetime import date

import pytest

from tradingagents.dataflows import edgar_sections
from tradingagents.dataflows.edgar import Filing
from tradingagents.dataflows.edgar_sections import (
    FilingSection,
    SectionNotFound,
    extract_section,
    read_filing_section,
)

pytestmark = pytest.mark.unit

# A miniature flattened 10-K: TOC lines first (headings back-to-back), then
# the body, where each heading is followed by real content.
TEN_K_TEXT = "\n".join([
    "TABLE OF CONTENTS",
    "Item 1. Business",
    "Item 1A. Risk Factors",
    "Item 7. Management's Discussion and Analysis",
    "Item 8. Financial Statements",
    "PART I",
    "Item 1. Business",
    "We make widgets in three segments.",
    "The widget market is cyclical.",
    "Item 1A. Risk Factors",
    "Customer concentration: one customer is 40% of revenue.",
    "Litigation: patent suit pending in Delaware.",
    "Item 7. Management's Discussion and Analysis",
    "Revenue declined 12% due to the loss of a distributor.",
    "Gross margin fell 300bps on input costs.",
    "Item 8. Financial Statements",
    "See accompanying notes.",
])


def test_extracts_body_section_not_toc():
    text = extract_section(TEN_K_TEXT, form="10-K", section="risk_factors")
    assert "Customer concentration" in text
    assert "Litigation" in text
    # Stops at the next Item heading.
    assert "Revenue declined" not in text


def test_extracts_mdna():
    text = extract_section(TEN_K_TEXT, form="10-K", section="mdna")
    assert "Revenue declined 12%" in text
    assert "See accompanying notes" not in text


def test_unknown_section_raises():
    with pytest.raises(SectionNotFound):
        extract_section(TEN_K_TEXT, form="10-K", section="compensation")


def test_missing_section_raises():
    with pytest.raises(SectionNotFound):
        extract_section("no items here at all", form="10-K", section="mdna")


def test_full_returns_whole_document_bounded():
    text = extract_section(TEN_K_TEXT, form="8-K", section="full", max_chars=40)
    assert text.startswith("TABLE OF CONTENTS")
    assert "[truncated" in text


def test_truncation_marker():
    text = extract_section(TEN_K_TEXT, form="10-K", section="risk_factors", max_chars=30)
    assert len(text) <= 30 + len("\n[truncated at 30 characters]")
    assert text.endswith("[truncated at 30 characters]")


def _filing(accession="0000000001-26-000001", form="10-K"):
    return Filing(
        ticker="WIDG", cik=1234567, accession_number=accession, form=form,
        filing_date=date(2026, 3, 1), report_date=date(2025, 12, 31),
        primary_document="widg-10k.htm",
    )


def test_read_filing_section_resolves_accession():
    filing = _filing()
    section = read_filing_section(
        "WIDG", filing.accession_number, "mdna",
        list_filings=lambda ticker, **kw: [filing],
        fetch_text=lambda f, **kw: TEN_K_TEXT,
    )
    assert isinstance(section, FilingSection)
    assert section.source_ref == f"{filing.accession_number}:mdna"
    assert "Revenue declined 12%" in section.text
    assert section.form == "10-K"


def test_read_filing_section_unknown_accession_raises():
    with pytest.raises(KeyError):
        read_filing_section(
            "WIDG", "0000000001-26-999999", "mdna",
            list_filings=lambda ticker, **kw: [_filing()],
            fetch_text=lambda f, **kw: TEN_K_TEXT,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_edgar_sections.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradingagents.dataflows.edgar_sections'`

- [ ] **Step 3: Write the implementation**

```python
"""Deterministic section extraction from SEC filing text.

Local-model research reads filings section-by-section ("tool-based bounded
reading" — spec decision 3): the evidence stage gets one bounded section per
LLM call instead of a stuffed context. Extraction must therefore be
deterministic and cheap — a regex over the Item-heading taxonomy, never an
LLM. The TOC-vs-body ambiguity is resolved by span length: candidate spans
run from a start-heading match to the next any-Item heading, and the body
occurrence is the longest span (TOC entries collide with their neighbors).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# form family -> section key -> Item number.
# 10-Q MD&A is Part I Item 2; the longest-span rule keeps Part II items from
# winning because they carry little text.
SECTION_ITEMS: dict[str, dict[str, str]] = {
    "10-K": {"business": "1", "risk_factors": "1A", "mdna": "7"},
    "10-Q": {"mdna": "2", "risk_factors": "1A"},
}

_ANY_ITEM = re.compile(r"^\s*item\s+\d+[a-z]?\.?\b", re.IGNORECASE | re.MULTILINE)


class SectionNotFound(ValueError):
    """The requested section key is unknown or absent from the document."""


@dataclass(frozen=True)
class FilingSection:
    ticker: str
    accession: str
    section: str
    form: str
    text: str

    @property
    def source_ref(self) -> str:
        return f"{self.accession}:{self.section}"


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[truncated at {max_chars} characters]"


def _form_family(form: str) -> str | None:
    for family in SECTION_ITEMS:
        if form.upper().startswith(family):
            return family
    return None


def extract_section(
    text: str, *, form: str, section: str, max_chars: int = 12000,
) -> str:
    """Extract one canonical section from flattened filing text, bounded."""
    if section == "full":
        return _truncate(text, max_chars)
    family = _form_family(form)
    items = SECTION_ITEMS.get(family or "", {})
    item = items.get(section)
    if item is None:
        raise SectionNotFound(
            f"section {section!r} not defined for form {form!r} "
            f"(known: {sorted(items) + ['full']})"
        )
    start_re = re.compile(
        rf"^\s*item\s+{re.escape(item)}\.?\b", re.IGNORECASE | re.MULTILINE,
    )
    best: str | None = None
    for m in start_re.finditer(text):
        nxt = _ANY_ITEM.search(text, m.end())
        span = text[m.end(): nxt.start()] if nxt else text[m.end():]
        if best is None or len(span) > len(best):
            best = span
    if best is None or not best.strip():
        raise SectionNotFound(f"Item {item} ({section}) not found in this {form}")
    return _truncate(best.strip(), max_chars)


def read_filing_section(
    ticker: str,
    accession: str,
    section: str,
    *,
    max_chars: int = 12000,
    list_filings=None,
    fetch_text=None,
) -> FilingSection:
    """Resolve an accession for ``ticker`` and extract one section from it."""
    from tradingagents.dataflows import edgar

    list_filings = list_filings or edgar.list_filings
    fetch_text = fetch_text or edgar.fetch_filing_text
    filings = list_filings(ticker, limit=200)
    filing = next((f for f in filings if f.accession_number == accession), None)
    if filing is None:
        raise KeyError(f"no filing with accession {accession!r} for {ticker!r}")
    text = extract_section(
        fetch_text(filing), form=filing.form, section=section, max_chars=max_chars,
    )
    return FilingSection(
        ticker=ticker.upper(), accession=accession, section=section,
        form=filing.form, text=text,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_edgar_sections.py -v` — Expected: 8 passed

- [ ] **Step 5: Full suite, lint, commit**

```bash
pytest tests/ -q
ruff check tradingagents/dataflows/edgar_sections.py tests/test_edgar_sections.py
git add tradingagents/dataflows/edgar_sections.py tests/test_edgar_sections.py
git commit -m "feat(dataflows): deterministic SEC filing section extraction"
```

---

### Task 2: Aligned year-over-year section diff

**Files:**
- Modify: `tradingagents/dataflows/edgar_sections.py`
- Test: extend `tests/test_edgar_sections.py`

**Interfaces:**
- Produces (Tasks 5 and 8 rely on these exact names):
  - `@dataclass(frozen=True) SectionDiff: ticker: str; section: str; accession_new: str; accession_old: str; text: str` with property `source_ref -> str` returning `f"{accession_new}+{accession_old}:{section}_diff"`
  - `diff_filing_sections(ticker: str, section: str, year_a: int, year_b: int, *, max_chars: int = 12000, list_filings=None, fetch_text=None) -> SectionDiff` — `year_a`/`year_b` are fiscal years matched against `Filing.report_date.year` (falling back to `filing_date.year` when `report_date` is None); output is a unified diff, old→new, so `year_b > year_a` reads as "what changed since".

The design doc ranks this above any embedding index: the YoY *language delta* in risk factors and MD&A is the signal snippet retrieval destroys.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_edgar_sections.py`)

```python
TEN_K_TEXT_PRIOR = TEN_K_TEXT.replace(
    "Customer concentration: one customer is 40% of revenue.",
    "Customer concentration: one customer is 25% of revenue.",
).replace(
    "Litigation: patent suit pending in Delaware.",
    "No material litigation.",
)


def _two_ten_ks():
    new = _filing(accession="0000000001-26-000001")
    old = Filing(
        ticker="WIDG", cik=1234567, accession_number="0000000001-25-000001",
        form="10-K", filing_date=date(2025, 3, 1), report_date=date(2024, 12, 31),
        primary_document="widg-10k.htm",
    )
    texts = {new.accession_number: TEN_K_TEXT, old.accession_number: TEN_K_TEXT_PRIOR}
    return new, old, texts


def test_diff_shows_yoy_language_change():
    new, old, texts = _two_ten_ks()
    diff = edgar_sections.diff_filing_sections(
        "WIDG", "risk_factors", 2024, 2025,
        list_filings=lambda ticker, **kw: [new, old],
        fetch_text=lambda f, **kw: texts[f.accession_number],
    )
    assert diff.source_ref == f"{new.accession_number}+{old.accession_number}:risk_factors_diff"
    assert "-Customer concentration: one customer is 25% of revenue." in diff.text
    assert "+Customer concentration: one customer is 40% of revenue." in diff.text
    assert "+Litigation: patent suit pending in Delaware." in diff.text


def test_diff_missing_year_raises():
    new, old, texts = _two_ten_ks()
    with pytest.raises(KeyError):
        edgar_sections.diff_filing_sections(
            "WIDG", "risk_factors", 2019, 2025,
            list_filings=lambda ticker, **kw: [new, old],
            fetch_text=lambda f, **kw: texts[f.accession_number],
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_edgar_sections.py -v` — Expected: 2 new FAIL with `AttributeError: ... no attribute 'diff_filing_sections'`

- [ ] **Step 3: Implement** (append to `edgar_sections.py`; add `import difflib` at top)

```python
@dataclass(frozen=True)
class SectionDiff:
    ticker: str
    section: str
    accession_new: str
    accession_old: str
    text: str

    @property
    def source_ref(self) -> str:
        return f"{self.accession_new}+{self.accession_old}:{self.section}_diff"


def _fiscal_year(filing) -> int:
    when = filing.report_date or filing.filing_date
    return when.year


def diff_filing_sections(
    ticker: str,
    section: str,
    year_a: int,
    year_b: int,
    *,
    max_chars: int = 12000,
    list_filings=None,
    fetch_text=None,
) -> SectionDiff:
    """Unified diff of one section between two fiscal years' 10-Ks (old→new).

    The YoY language delta is the point: new risk factors, changed customer
    concentration numbers, dropped reassurances. Line-level diff works because
    fetch_filing_text flattens HTML to one text chunk per line.
    """
    from tradingagents.dataflows import edgar

    list_filings = list_filings or edgar.list_filings
    fetch_text = fetch_text or edgar.fetch_filing_text
    filings = list_filings(ticker, forms={"10-K", "10-K/A"}, limit=200)
    by_year: dict[int, object] = {}
    for f in filings:  # newest-first; keep the newest filing per fiscal year
        by_year.setdefault(_fiscal_year(f), f)
    old_year, new_year = sorted((year_a, year_b))
    missing = [y for y in (old_year, new_year) if y not in by_year]
    if missing:
        raise KeyError(
            f"no 10-K for fiscal year(s) {missing} for {ticker!r} "
            f"(have: {sorted(by_year)})"
        )
    old_f, new_f = by_year[old_year], by_year[new_year]
    old_text = extract_section(
        fetch_text(old_f), form=old_f.form, section=section, max_chars=max_chars,
    )
    new_text = extract_section(
        fetch_text(new_f), form=new_f.form, section=section, max_chars=max_chars,
    )
    diff_lines = difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        fromfile=f"{old_f.accession_number} (FY{old_year})",
        tofile=f"{new_f.accession_number} (FY{new_year})",
        lineterm="", n=1,
    )
    return SectionDiff(
        ticker=ticker.upper(), section=section,
        accession_new=new_f.accession_number, accession_old=old_f.accession_number,
        text=_truncate("\n".join(diff_lines), max_chars),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_edgar_sections.py -v` — Expected: 10 passed

- [ ] **Step 5: Full suite, lint, commit**

```bash
pytest tests/ -q
ruff check tradingagents/dataflows/edgar_sections.py tests/test_edgar_sections.py
git add tradingagents/dataflows/edgar_sections.py tests/test_edgar_sections.py
git commit -m "feat(dataflows): aligned year-over-year filing section diff"
```

---

### Task 3: Form 4 insider-transaction parser

**Files:**
- Create: `tradingagents/dataflows/form4.py`
- Test: `tests/test_form4.py`

**Interfaces:**
- Produces (Tasks 4 and 5 rely on these exact names):
  - `@dataclass(frozen=True) InsiderTransaction: insider_name: str; insider_title: str; is_director: bool; is_officer: bool; is_ten_pct_owner: bool; transaction_date: date | None; code: str; shares: Decimal | None; price: Decimal | None; acquired: bool; ten_b5_1: bool; accession: str; filed_date: date` with property `kind -> str` — `"open_market_buy"` (code `P`), `"open_market_sale"` (code `S`), `"grant"` (code `A`), else `"other"`.
  - `parse_form4_xml(xml_text: str, *, accession: str, filed_date: date) -> list[InsiderTransaction]` — pure; non-derivative transactions only (derivative table ignored in v1).
  - `get_insider_transactions(ticker: str, *, since: date, max_filings: int = 10, list_filings=None, fetch_raw=None) -> list[InsiderTransaction]` — lists form `"4"` filings since `since` (newest first, capped at `max_filings`), fetches each raw ownership XML, parses, flattens newest-first.
  - `raw_xml_url(filing) -> str` — the SEC serves Form 4 primary documents as an XSL-rendered view (`primary_document` like `"xslF345X05/wk-form4_123.xml"`); the raw ownership XML is the same filename with the `xslF345X0N/` directory prefix stripped.

**Form 4 XML facts** (ownershipDocument schema): insider identity at `reportingOwner/reportingOwnerId/rptOwnerName`; roles at `reportingOwner/reportingOwnerRelationship/{isDirector,isOfficer,isTenPercentOwner,officerTitle}` (values `"1"`/`"true"`/`"0"`/`"false"` — treat `"1"` and `"true"` as true); transactions at `nonDerivativeTable/nonDerivativeTransaction` with `transactionDate/value`, `transactionCoding/transactionCode`, `transactionAmounts/transactionShares/value`, `transactionAmounts/transactionPricePerShare/value` (may be absent), `transactionAmounts/transactionAcquiredDisposedCode/value` (`A`/`D`); the Rule 10b5-1 checkbox (mandatory since 2023) is the document-level element `aff10b5One`.

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the Form 4 ownership-XML parser."""

from datetime import date
from decimal import Decimal

import pytest

from tradingagents.dataflows import form4
from tradingagents.dataflows.edgar import Filing

pytestmark = pytest.mark.unit

FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
    <aff10b5One>0</aff10b5One>
    <issuer><issuerTradingSymbol>WIDG</issuerTradingSymbol></issuer>
    <reportingOwner>
        <reportingOwnerId><rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>1</isDirector>
            <isOfficer>1</isOfficer>
            <officerTitle>CEO</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionDate><value>2026-06-15</value></transactionDate>
            <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>10000</value></transactionShares>
                <transactionPricePerShare><value>4.25</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
            <transactionDate><value>2026-06-16</value></transactionDate>
            <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>2000</value></transactionShares>
                <transactionPricePerShare><value>4.60</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>
"""

FORM4_10B51_GRANT = FORM4_XML.replace(
    "<aff10b5One>0</aff10b5One>", "<aff10b5One>1</aff10b5One>",
).replace(
    "<transactionCode>P</transactionCode>", "<transactionCode>A</transactionCode>",
)

ACCESSION = "0000000001-26-000042"
FILED = date(2026, 6, 17)


def test_parses_buy_and_sale_with_identity():
    txns = form4.parse_form4_xml(FORM4_XML, accession=ACCESSION, filed_date=FILED)
    assert len(txns) == 2
    buy, sale = txns
    assert buy.insider_name == "DOE JANE"
    assert buy.is_officer and buy.is_director and not buy.is_ten_pct_owner
    assert buy.insider_title == "CEO"
    assert buy.code == "P" and buy.kind == "open_market_buy"
    assert buy.shares == Decimal("10000")
    assert buy.price == Decimal("4.25")
    assert buy.acquired is True
    assert buy.ten_b5_1 is False
    assert buy.transaction_date == date(2026, 6, 15)
    assert buy.accession == ACCESSION
    assert sale.code == "S" and sale.kind == "open_market_sale"
    assert sale.acquired is False


def test_10b51_flag_and_grant_kind():
    txns = form4.parse_form4_xml(FORM4_10B51_GRANT, accession=ACCESSION, filed_date=FILED)
    assert all(t.ten_b5_1 for t in txns)
    assert txns[0].kind == "grant"


def test_malformed_xml_returns_empty():
    assert form4.parse_form4_xml("<not-xml", accession=ACCESSION, filed_date=FILED) == []


def test_raw_xml_url_strips_xsl_prefix():
    f = Filing(
        ticker="WIDG", cik=1234567, accession_number=ACCESSION, form="4",
        filing_date=FILED, report_date=None,
        primary_document="xslF345X05/wk-form4_123.xml",
    )
    url = form4.raw_xml_url(f)
    assert "xslF345X05" not in url
    assert url.endswith("/wk-form4_123.xml")


def test_get_insider_transactions_lists_and_parses():
    f = Filing(
        ticker="WIDG", cik=1234567, accession_number=ACCESSION, form="4",
        filing_date=FILED, report_date=None, primary_document="form4.xml",
    )
    txns = form4.get_insider_transactions(
        "WIDG", since=date(2026, 4, 1),
        list_filings=lambda ticker, **kw: [f],
        fetch_raw=lambda url: FORM4_XML,
    )
    assert len(txns) == 2
    assert txns[0].filed_date == FILED


def test_get_insider_transactions_caps_filings():
    filings = [
        Filing(
            ticker="WIDG", cik=1234567, accession_number=f"000-26-{i:06d}", form="4",
            filing_date=date(2026, 6, 17), report_date=None, primary_document="form4.xml",
        )
        for i in range(20)
    ]
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return FORM4_XML

    form4.get_insider_transactions(
        "WIDG", since=date(2026, 4, 1), max_filings=3,
        list_filings=lambda ticker, **kw: filings, fetch_raw=fake_fetch,
    )
    assert len(fetched) == 3
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_form4.py -v` — Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
"""Form 4 (insider ownership) XML parsing.

Raw Form 4 filing counts are useless as a signal — dominated by routine
10b5-1 sales and equity grants. The parser separates what matters: an
open-market purchase (code P) outside a 10b5-1 plan is an insider spending
their own cash at market. Clusters of those are the strongest single trigger
in the screener taxonomy (and were deferred from build-order step 3 until
this parser existed).
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true"}
_XSL_PREFIX = re.compile(r"^xslF345X\d+/")


@dataclass(frozen=True)
class InsiderTransaction:
    insider_name: str
    insider_title: str
    is_director: bool
    is_officer: bool
    is_ten_pct_owner: bool
    transaction_date: date | None
    code: str
    shares: Decimal | None
    price: Decimal | None
    acquired: bool
    ten_b5_1: bool
    accession: str
    filed_date: date

    @property
    def kind(self) -> str:
        if self.code == "P":
            return "open_market_buy"
        if self.code == "S":
            return "open_market_sale"
        if self.code == "A":
            return "grant"
        return "other"


def _text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    found = node.find(path)
    return (found.text or "").strip() if found is not None else ""


def _flag(node: ET.Element | None, path: str) -> bool:
    return _text(node, path).lower() in _TRUE_VALUES


def _decimal(raw: str) -> Decimal | None:
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def parse_form4_xml(
    xml_text: str, *, accession: str, filed_date: date,
) -> list[InsiderTransaction]:
    """Parse one ownership document. Malformed XML yields [] (skip, don't die)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("form4 %s: unparseable XML (%s)", accession, exc)
        return []
    ten_b5_1 = (root.findtext("aff10b5One") or "").strip().lower() in _TRUE_VALUES
    owner = root.find("reportingOwner")
    name = _text(owner, "reportingOwnerId/rptOwnerName")
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    out: list[InsiderTransaction] = []
    for txn in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        out.append(InsiderTransaction(
            insider_name=name,
            insider_title=_text(rel, "officerTitle"),
            is_director=_flag(rel, "isDirector"),
            is_officer=_flag(rel, "isOfficer"),
            is_ten_pct_owner=_flag(rel, "isTenPercentOwner"),
            transaction_date=_date(_text(txn, "transactionDate/value")),
            code=_text(txn, "transactionCoding/transactionCode"),
            shares=_decimal(_text(txn, "transactionAmounts/transactionShares/value")),
            price=_decimal(
                _text(txn, "transactionAmounts/transactionPricePerShare/value")
            ),
            acquired=_text(
                txn, "transactionAmounts/transactionAcquiredDisposedCode/value"
            ).upper() == "A",
            ten_b5_1=ten_b5_1,
            accession=accession,
            filed_date=filed_date,
        ))
    return out


def raw_xml_url(filing) -> str:
    """URL of the raw ownership XML (primary_document minus the XSL view prefix)."""
    from tradingagents.dataflows.edgar import ARCHIVES_URL

    document = _XSL_PREFIX.sub("", filing.primary_document)
    return ARCHIVES_URL.format(
        cik=filing.cik,
        accession_nodash=filing.accession_number.replace("-", ""),
        document=document,
    )


def _default_fetch_raw(url: str) -> str:
    from tradingagents.dataflows.edgar import _throttled_get

    return _throttled_get(url).text


def get_insider_transactions(
    ticker: str,
    *,
    since: date,
    max_filings: int = 10,
    list_filings=None,
    fetch_raw=None,
) -> list[InsiderTransaction]:
    """All non-derivative insider transactions from Form 4s filed since ``since``.

    ``max_filings`` caps XML fetches — this runs inside the weekly sweep over
    ~1500 names, so per-name I/O must be bounded. Filings are newest-first.
    """
    from tradingagents.dataflows import edgar

    list_filings = list_filings or edgar.list_filings
    fetch_raw = fetch_raw or _default_fetch_raw
    filings = list_filings(ticker, forms={"4"}, since=since, limit=max_filings)
    out: list[InsiderTransaction] = []
    for filing in filings[:max_filings]:
        try:
            xml_text = fetch_raw(raw_xml_url(filing))
        except Exception as exc:  # one bad document must not kill the sweep
            logger.warning("form4 %s: fetch failed (%s)", filing.accession_number, exc)
            continue
        out.extend(parse_form4_xml(
            xml_text, accession=filing.accession_number,
            filed_date=filing.filing_date,
        ))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_form4.py -v` — Expected: 6 passed

- [ ] **Step 5: Full suite, lint, commit**

```bash
pytest tests/ -q
ruff check tradingagents/dataflows/form4.py tests/test_form4.py
git add tradingagents/dataflows/form4.py tests/test_form4.py
git commit -m "feat(dataflows): Form 4 insider-transaction XML parser"
```

---

### Task 4: Insider-cluster change trigger, wired into the screen

**Files:**
- Modify: `ops/research/triggers.py`, `ops/research/run.py` (default `triggers_finder` only)
- Test: extend `tests/ops/research/test_triggers.py`

**Interfaces:**
- Consumes: `form4.get_insider_transactions` (Task 3).
- Produces (Task 8's reading plan and the screen rely on these):
  - `INSIDER_CLUSTER_MIN_BUYERS = 2`
  - `find_insider_cluster_trigger(ticker: str, *, asof: date, lookback_days: int = TRIGGER_LOOKBACK_DAYS, transactions_fetcher=None) -> Trigger | None` — Trigger kind `"insider_cluster"`, `source` = accession of the newest qualifying buy.
  - `find_triggers(ticker: str, *, asof: date, lookback_days: int = TRIGGER_LOOKBACK_DAYS, list_filings=None, transactions_fetcher=None) -> list[Trigger]` — EDGAR triggers plus the insider cluster; this becomes `run_screen`'s default `triggers_finder`.
- Cluster rule: ≥ `INSIDER_CLUSTER_MIN_BUYERS` **distinct** `insider_name`s with at least one `open_market_buy`, NOT `ten_b5_1`, `transaction_date` within `[asof - lookback_days, asof]`. Routine sales and grants never count.

- [ ] **Step 1: Read `tests/ops/research/test_triggers.py` completely** — reuse its fixture style for fake filings/dates. Do not modify existing tests.

- [ ] **Step 2: Write the failing tests** (append; adapt imports to the file's existing ones)

```python
def _buy(name, day, *, ten_b5_1=False, code="P"):
    from tradingagents.dataflows.form4 import InsiderTransaction

    return InsiderTransaction(
        insider_name=name, insider_title="", is_director=True, is_officer=False,
        is_ten_pct_owner=False, transaction_date=day, code=code,
        shares=Decimal("1000"), price=Decimal("5"), acquired=(code == "P"),
        ten_b5_1=ten_b5_1, accession=f"acc-{name}-{day.isoformat()}",
        filed_date=day,
    )


def test_two_distinct_open_market_buyers_trigger():
    from ops.research.triggers import find_insider_cluster_trigger

    asof = date(2026, 7, 1)
    txns = [_buy("DOE JANE", date(2026, 6, 20)), _buy("ROE RICHARD", date(2026, 6, 25))]
    trig = find_insider_cluster_trigger(
        "WIDG", asof=asof, transactions_fetcher=lambda t, *, since, **kw: txns,
    )
    assert trig is not None
    assert trig.kind == "insider_cluster"
    assert trig.source == "acc-ROE RICHARD-2026-06-25"


def test_single_buyer_sales_grants_and_10b51_do_not_trigger():
    from ops.research.triggers import find_insider_cluster_trigger

    asof = date(2026, 7, 1)
    cases = [
        [_buy("DOE JANE", date(2026, 6, 20))],                                # one buyer
        [_buy("DOE JANE", date(2026, 6, 20)), _buy("DOE JANE", date(2026, 6, 25))],  # same buyer twice
        [_buy("A", date(2026, 6, 20), code="S"), _buy("B", date(2026, 6, 25), code="S")],
        [_buy("A", date(2026, 6, 20), code="A"), _buy("B", date(2026, 6, 25), code="A")],
        [_buy("A", date(2026, 6, 20), ten_b5_1=True), _buy("B", date(2026, 6, 25), ten_b5_1=True)],
        [_buy("A", date(2026, 2, 1)), _buy("B", date(2026, 2, 2))],           # outside lookback
    ]
    for txns in cases:
        trig = find_insider_cluster_trigger(
            "WIDG", asof=asof, transactions_fetcher=lambda t, *, since, **kw: txns,
        )
        assert trig is None, txns


def test_find_triggers_combines_edgar_and_cluster():
    from ops.research.triggers import find_triggers

    asof = date(2026, 7, 1)
    txns = [_buy("DOE JANE", date(2026, 6, 20)), _buy("ROE RICHARD", date(2026, 6, 25))]
    out = find_triggers(
        "WIDG", asof=asof,
        list_filings=lambda ticker, **kw: [],
        transactions_fetcher=lambda t, *, since, **kw: txns,
    )
    assert [t.kind for t in out] == ["insider_cluster"]
```

Run: `pytest tests/ops/research/test_triggers.py -v` — Expected: new tests FAIL (`ImportError`).

- [ ] **Step 3: Implement in `ops/research/triggers.py`**

Update the module docstring's "Form 4 insider clusters are DEFERRED" paragraph — the deferral is over; describe the cluster rule instead. Then append:

```python
INSIDER_CLUSTER_MIN_BUYERS = 2


def find_insider_cluster_trigger(
    ticker: str,
    *,
    asof: date,
    lookback_days: int = TRIGGER_LOOKBACK_DAYS,
    transactions_fetcher: Callable[..., list] | None = None,
) -> Trigger | None:
    """A cluster of distinct insiders buying on the open market, own cash,
    outside 10b5-1 plans — the strongest single trigger in the taxonomy."""
    from tradingagents.dataflows.form4 import get_insider_transactions

    fetch = transactions_fetcher or get_insider_transactions
    since = asof - timedelta(days=lookback_days)
    txns = fetch(ticker, since=since)
    buys = [
        t for t in txns
        if t.kind == "open_market_buy" and not t.ten_b5_1
        and t.transaction_date is not None and since <= t.transaction_date <= asof
    ]
    buyers = {t.insider_name for t in buys}
    if len(buyers) < INSIDER_CLUSTER_MIN_BUYERS:
        return None
    latest = max(buys, key=lambda t: t.transaction_date)
    return Trigger(
        kind="insider_cluster",
        description=(
            f"{len(buyers)} insiders made open-market buys (non-10b5-1) "
            f"in the last {lookback_days} days"
        ),
        date=latest.transaction_date,
        source=latest.accession,
    )


def find_triggers(
    ticker: str,
    *,
    asof: date,
    lookback_days: int = TRIGGER_LOOKBACK_DAYS,
    list_filings: Callable[..., list[edgar.Filing]] | None = None,
    transactions_fetcher: Callable[..., list] | None = None,
) -> list[Trigger]:
    """All change triggers for a name: EDGAR filings + insider cluster.

    (The price-selloff trigger stays separate in run.py — it needs the price
    context, which this module deliberately does not fetch.)
    """
    out = find_edgar_triggers(
        ticker, asof=asof, lookback_days=lookback_days, list_filings=list_filings,
    )
    cluster = find_insider_cluster_trigger(
        ticker, asof=asof, lookback_days=lookback_days,
        transactions_fetcher=transactions_fetcher,
    )
    if cluster is not None:
        out.append(cluster)
    return out
```

- [ ] **Step 4: Wire into the screen.** In `ops/research/run.py`, change the import from `find_edgar_triggers` to `find_triggers` and the default `triggers_finder = triggers_finder or find_edgar_triggers` to `triggers_finder = triggers_finder or find_triggers`. Nothing else changes — `_name_inputs` already calls `triggers_finder(symbol, asof=asof)` and existing tests inject their own finder.

- [ ] **Step 5: Full suite, lint, commit**

```bash
pytest tests/ops/research/ -q && pytest tests/ -q
ruff check ops/research/triggers.py ops/research/run.py tests/ops/research/test_triggers.py
git add ops/research/triggers.py ops/research/run.py tests/ops/research/test_triggers.py
git commit -m "feat(research): insider-cluster change trigger from Form 4 open-market buys"
```

---

### Task 5: Past-memo lookup + the four agent tool wrappers

**Files:**
- Modify: `tradingagents/memos/store.py` (one helper function)
- Create: `tradingagents/agents/utils/filing_reader_tools.py`
- Test: `tests/test_filing_reader_tools.py`; extend `tests/test_memo_store.py`

**Interfaces:**
- Produces:
  - `default_memo_store_path() -> str` in `tradingagents/memos/store.py`: `OPS_MEMO_STORE_PATH` env if set, else `${XDG_STATE_HOME:-~/.local/state}/tradingagents/memos.sqlite` (mirrors `ops/config.py`'s `_default_*_path` helpers; lives here so tradingagents-layer tools never import ops).
  - `summarize_memo(memo: Memo) -> str` in `filing_reader_tools.py` — one-paragraph plain-text summary (id, ticker, type, status, thesis first sentence, tier, outcome label if resolved). Task 8's thesis prompt reuses it.
  - Four LangChain `@tool` functions in `filing_reader_tools.py` (build-order step 4's deliverable, following the exact pattern of `tradingagents/agents/utils/fundamental_data_tools.py`): `read_filing_section(ticker, accession, section) -> str`, `diff_filing_sections(ticker, section, year_a: int, year_b: int) -> str`, `get_insider_transactions(ticker, lookback_days: int = 180) -> str`, `get_past_memos(ticker) -> str`. Each returns bounded plain text (never raises to the agent: catch exceptions and return an `"ERROR: ..."` string — tool loops on weak models die on raised exceptions).

These wrappers are for the existing agent graph; the Phase B brain (Task 8) calls the dataflow primitives directly — deterministic orchestration, no tool loop.

- [ ] **Step 1: Failing tests.**

Append to `tests/test_memo_store.py` (reuse its existing imports/fixtures):

```python
def test_default_memo_store_path_env_override(monkeypatch):
    from tradingagents.memos.store import default_memo_store_path

    monkeypatch.setenv("OPS_MEMO_STORE_PATH", "/tmp/custom-memos.sqlite")
    assert default_memo_store_path() == "/tmp/custom-memos.sqlite"
    monkeypatch.delenv("OPS_MEMO_STORE_PATH")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/state")
    assert default_memo_store_path() == "/tmp/state/tradingagents/memos.sqlite"
```

Create `tests/test_filing_reader_tools.py`:

```python
"""Unit tests for the filing-reader agent tools (all I/O injected/mocked)."""

from datetime import date

import pytest

from tradingagents.agents.utils import filing_reader_tools as frt

pytestmark = pytest.mark.unit


def test_tools_are_langchain_tools():
    for t in (frt.read_filing_section, frt.diff_filing_sections,
              frt.get_insider_transactions, frt.get_past_memos):
        assert hasattr(t, "invoke") and hasattr(t, "name")  # BaseTool interface


def test_read_filing_section_returns_error_string_not_raise(monkeypatch):
    def boom(*a, **kw):
        raise KeyError("no filing with accession 'x'")

    monkeypatch.setattr(frt.edgar_sections, "read_filing_section", boom)
    out = frt.read_filing_section.invoke(
        {"ticker": "WIDG", "accession": "x", "section": "mdna"}
    )
    assert out.startswith("ERROR:")


def test_get_past_memos_reports_none_found(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    out = frt.get_past_memos.invoke({"ticker": "WIDG"})
    assert "none found" in out.lower()


def test_get_past_memos_lists_summaries(tmp_path, monkeypatch):
    from tradingagents.memos.schema import EvidenceItem, Falsifier, Memo, ValueThesis
    from tradingagents.memos.store import MemoStore

    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    memo = Memo(
        ticker="WIDG", as_of_date=date(2026, 1, 1), thesis_type="value",
        thesis="Cheap because of a temporary distributor loss.",
        evidence=[EvidenceItem(claim="c", source_type="filing", source_ref="a:mdna")],
        value_block=ValueThesis(
            why_cheap="distributor loss", change_trigger="selloff",
            normalized_earnings_view="normal", quality_assessment="fine",
        ),
        conviction_tier="starter", entry_price_ref=4.0,
        price_target_low=5.0, price_target_high=8.0, expected_holding_months=12,
        must_be_true=["distributor replaced"],
        falsifiers=[Falsifier(description="revenue keeps falling", check_type="fundamental")],
    )
    MemoStore(tmp_path / "memos.sqlite").save(memo)
    out = frt.get_past_memos.invoke({"ticker": "WIDG"})
    assert memo.memo_id in out
    assert "value" in out
```

Run both files — Expected: FAIL (`ImportError` / `AttributeError`).

- [ ] **Step 2: Implement `default_memo_store_path`** in `tradingagents/memos/store.py` (add `import os` if absent):

```python
def default_memo_store_path() -> str:
    """Default memo DB location, shared by ops config and the agent tools.

    Env override first so the tools and OpsConfig always agree on which
    corpus they are reading.
    """
    override = os.environ.get("OPS_MEMO_STORE_PATH")
    if override:
        return override
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(os.path.expanduser(base), "tradingagents", "memos.sqlite")
```

- [ ] **Step 3: Implement the tools module**

```python
"""Filing-reader agent tools (design-doc build order, step 4).

LangChain @tool wrappers over the deterministic EDGAR/memo primitives, for
use by the existing agent graph. They return plain bounded text and NEVER
raise — a raised exception inside a weak local model's tool loop ends the
run, whereas an "ERROR: ..." string lets the model route around it.

The Phase B memo pipeline (ops/research/brain.py) deliberately does NOT call
these through a tool loop; it calls the underlying primitives directly.
"""

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows import edgar_sections, form4
from tradingagents.memos.schema import Memo
from tradingagents.memos.store import MemoStore, default_memo_store_path

_MAX_TOOL_CHARS = 12000


def summarize_memo(memo: Memo) -> str:
    """One-paragraph summary used in tool output and thesis prompts."""
    first_sentence = memo.thesis.split(". ")[0].strip()
    line = (
        f"[{memo.memo_id}] {memo.ticker} {memo.thesis_type} "
        f"({memo.status}, tier={memo.conviction_tier}, as_of={memo.as_of_date}): "
        f"{first_sentence}."
    )
    if memo.resolution is not None:
        line += (
            f" Resolved: {memo.resolution.outcome_label}, "
            f"{memo.resolution.realized_return_pct:+.0%} vs "
            f"benchmark {memo.resolution.benchmark_return_pct:+.0%}."
        )
    return line


@tool
def read_filing_section(
    ticker: Annotated[str, "ticker symbol"],
    accession: Annotated[str, "EDGAR accession number, e.g. 0001234567-26-000123"],
    section: Annotated[str, "one of: business, risk_factors, mdna, full"],
) -> str:
    """Read one section of a specific SEC filing (bounded plain text).

    Deterministic extraction — same accession+section always returns the
    same text. Cite it as "{accession}:{section}".
    """
    try:
        result = edgar_sections.read_filing_section(
            ticker, accession, section, max_chars=_MAX_TOOL_CHARS,
        )
        return f"[{result.source_ref}] ({result.form})\n{result.text}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


@tool
def diff_filing_sections(
    ticker: Annotated[str, "ticker symbol"],
    section: Annotated[str, "one of: business, risk_factors, mdna"],
    year_a: Annotated[int, "earlier fiscal year, e.g. 2024"],
    year_b: Annotated[int, "later fiscal year, e.g. 2025"],
) -> str:
    """Unified diff of one 10-K section between two fiscal years.

    What changed in the language year-over-year: new risk factors, changed
    concentration numbers, dropped reassurances.
    """
    try:
        result = edgar_sections.diff_filing_sections(
            ticker, section, year_a, year_b, max_chars=_MAX_TOOL_CHARS,
        )
        return f"[{result.source_ref}]\n{result.text}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


@tool
def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol"],
    lookback_days: Annotated[int, "how far back to look"] = 180,
) -> str:
    """Insider (Form 4) transactions: who bought/sold, open-market vs plan.

    Open-market buys (code P, non-10b5-1) are insiders spending their own
    cash; routine sales and grants carry little signal.
    """
    from datetime import date, timedelta

    try:
        txns = form4.get_insider_transactions(
            ticker, since=date.today() - timedelta(days=lookback_days),
        )
        if not txns:
            return f"No Form 4 transactions for {ticker} in the last {lookback_days} days."
        lines = [
            f"{t.transaction_date} {t.insider_name} "
            f"({t.insider_title or ('director' if t.is_director else 'insider')}) "
            f"{t.kind} {t.shares} sh @ {t.price} "
            f"{'[10b5-1 plan]' if t.ten_b5_1 else '[not a plan]'} ({t.accession})"
            for t in txns
        ]
        return "\n".join(lines)[:_MAX_TOOL_CHARS]
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


@tool
def get_past_memos(
    ticker: Annotated[str, "ticker symbol"],
) -> str:
    """Past research memos for this ticker from the memo corpus.

    "None found" is an explicit finding — record it as such, do not invent
    precedents.
    """
    try:
        memos = MemoStore(default_memo_store_path()).list(ticker=ticker)
        if not memos:
            return f"No past memos for {ticker}: none found."
        return "\n".join(summarize_memo(m) for m in memos)[:_MAX_TOOL_CHARS]
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"
```

- [ ] **Step 4: Run tests, then full suite, lint, commit**

```bash
pytest tests/test_filing_reader_tools.py tests/test_memo_store.py -v
pytest tests/ -q
ruff check tradingagents/memos/store.py tradingagents/agents/utils/filing_reader_tools.py tests/test_filing_reader_tools.py tests/test_memo_store.py
git add tradingagents/memos/store.py tradingagents/agents/utils/filing_reader_tools.py tests/test_filing_reader_tools.py tests/test_memo_store.py
git commit -m "feat(agents): filing-reader tools + past-memo lookup (build-order step 4)"
```

---

### Task 6: Per-stage model config

**Files:**
- Create: `ops/research/models.py`
- Modify: `ops/config.py`
- Test: `tests/ops/research/test_models.py`; extend `tests/ops/test_config.py`

**Interfaces:**
- Produces (Task 9 relies on these exact names):
  - `@dataclass(frozen=True) ModelSpec: provider: str; model: str; base_url: str | None`
  - `parse_model_spec(spec: str) -> ModelSpec` — format `"provider:model"` or `"provider:model@base_url"`; raises `ValueError` on anything unparseable.
  - `build_stage_llm(spec: str)` — parses, then `create_llm_client(provider=..., model=..., base_url=...).get_llm()` (import `create_llm_client` lazily inside the function).
  - `OpsConfig` gains: `memo_store_path: str` (default from `tradingagents.memos.store.default_memo_store_path`), `research_evidence_model: str` and `research_thesis_model: str`, both defaulting to `"openai_compatible:deepseek-v4-flash@http://127.0.0.1:8000/v1"` (the ds4 managed-backend default — see `docs/ds4-backend.md`). Env overrides in `load_config()`: `OPS_MEMO_STORE_PATH`, `OPS_RESEARCH_EVIDENCE_MODEL`, `OPS_RESEARCH_THESIS_MODEL`. `__post_init__` validates both specs parse (lazy-import `parse_model_spec` inside `__post_init__` to avoid an import cycle).

This is spec decision 3's escape hatch: pointing the thesis stage at an API model later is exactly `OPS_RESEARCH_THESIS_MODEL=anthropic:claude-sonnet-5` — no code change.

- [ ] **Step 1: Failing tests.** Create `tests/ops/research/test_models.py`:

```python
"""Unit tests for per-stage research model specs."""

import pytest

from ops.research.models import ModelSpec, build_stage_llm, parse_model_spec

pytestmark = pytest.mark.unit


def test_parses_provider_model():
    assert parse_model_spec("anthropic:claude-sonnet-5") == ModelSpec(
        provider="anthropic", model="claude-sonnet-5", base_url=None,
    )


def test_parses_provider_model_url():
    spec = parse_model_spec("openai_compatible:deepseek-v4-flash@http://127.0.0.1:8000/v1")
    assert spec.provider == "openai_compatible"
    assert spec.model == "deepseek-v4-flash"
    assert spec.base_url == "http://127.0.0.1:8000/v1"


@pytest.mark.parametrize("bad", ["", "no-colon", ":model", "provider:", "p:m@"])
def test_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_model_spec(bad)


def test_build_stage_llm_routes_through_registry(monkeypatch):
    captured = {}

    class FakeClient:
        def get_llm(self):
            return "the-llm"

    def fake_create(*, provider, model, base_url=None, **kw):
        captured.update(provider=provider, model=model, base_url=base_url)
        return FakeClient()

    import tradingagents.llm_clients as llm_clients

    monkeypatch.setattr(llm_clients, "create_llm_client", fake_create)
    llm = build_stage_llm("openai_compatible:foo@http://localhost:1234/v1")
    assert llm == "the-llm"
    assert captured == {
        "provider": "openai_compatible", "model": "foo",
        "base_url": "http://localhost:1234/v1",
    }
```

Append to `tests/ops/test_config.py` (follow the file's existing env-override test pattern exactly — read it first):

```python
def test_research_model_env_overrides(monkeypatch):
    monkeypatch.setenv("OPS_RESEARCH_EVIDENCE_MODEL", "anthropic:claude-haiku-4-5")
    monkeypatch.setenv("OPS_RESEARCH_THESIS_MODEL", "anthropic:claude-sonnet-5")
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", "/tmp/m.sqlite")
    config = load_config()
    assert config.research_evidence_model == "anthropic:claude-haiku-4-5"
    assert config.research_thesis_model == "anthropic:claude-sonnet-5"
    assert config.memo_store_path == "/tmp/m.sqlite"


def test_malformed_research_model_rejected():
    with pytest.raises(ValueError):
        OpsConfig(research_thesis_model="not-a-spec")
```

Run both — Expected: FAIL.

- [ ] **Step 2: Implement `ops/research/models.py`**

```python
"""Per-stage LLM construction for the research brain.

Each pipeline stage (evidence, thesis) gets its own model spec so any single
stage can be pointed at a bigger local model — or an API model — by config
change alone (spec decision 3's escape hatch). Spec format:

    provider:model              e.g.  anthropic:claude-sonnet-5
    provider:model@base_url     e.g.  openai_compatible:deepseek-v4-flash@http://127.0.0.1:8000/v1
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    base_url: str | None = None


def parse_model_spec(spec: str) -> ModelSpec:
    head, _, url = spec.partition("@")
    provider, _, model = head.partition(":")
    provider, model, url = provider.strip(), model.strip(), url.strip()
    if not provider or not model or ("@" in spec and not url):
        raise ValueError(
            f"invalid model spec {spec!r}: expected 'provider:model' or "
            "'provider:model@base_url'"
        )
    return ModelSpec(provider=provider, model=model, base_url=url or None)


def build_stage_llm(spec: str):
    """Build a LangChain chat model for one pipeline stage."""
    parsed = parse_model_spec(spec)
    from tradingagents.llm_clients import create_llm_client

    return create_llm_client(
        provider=parsed.provider, model=parsed.model, base_url=parsed.base_url,
    ).get_llm()
```

Note for the `build_stage_llm` test: `build_stage_llm` must import `create_llm_client` FROM the package at call time in a way the monkeypatch intercepts. `from tradingagents.llm_clients import create_llm_client` inside the function body binds at call time from the module namespace, so `monkeypatch.setattr(llm_clients, "create_llm_client", ...)` works. Keep the import inside the function.

- [ ] **Step 3: Extend `ops/config.py`.** Add near the other `_default_*_path` helpers:

```python
def _default_memo_store_path() -> str:
    from tradingagents.memos.store import default_memo_store_path

    return default_memo_store_path()


_DEFAULT_RESEARCH_MODEL = "openai_compatible:deepseek-v4-flash@http://127.0.0.1:8000/v1"
```

Dataclass fields (after `screen_store_path`):

```python
    memo_store_path: str = field(default_factory=_default_memo_store_path)
    research_evidence_model: str = _DEFAULT_RESEARCH_MODEL
    research_thesis_model: str = _DEFAULT_RESEARCH_MODEL
```

In `__post_init__` (at the end):

```python
        from ops.research.models import parse_model_spec

        for fname in ("research_evidence_model", "research_thesis_model"):
            parse_model_spec(getattr(self, fname))  # raises ValueError if malformed
```

In `load_config()` (next to the other string envs):

```python
    memo_store_path = os.environ.get("OPS_MEMO_STORE_PATH")
    if memo_store_path is not None:
        kwargs["memo_store_path"] = memo_store_path

    research_evidence_model = os.environ.get("OPS_RESEARCH_EVIDENCE_MODEL")
    if research_evidence_model is not None:
        kwargs["research_evidence_model"] = research_evidence_model

    research_thesis_model = os.environ.get("OPS_RESEARCH_THESIS_MODEL")
    if research_thesis_model is not None:
        kwargs["research_thesis_model"] = research_thesis_model
```

- [ ] **Step 4: Run tests, full suite, lint, commit**

```bash
pytest tests/ops/research/test_models.py tests/ops/test_config.py -v && pytest tests/ -q
ruff check ops/research/models.py ops/config.py tests/ops/research/test_models.py tests/ops/test_config.py
git add ops/research/models.py ops/config.py tests/ops/research/test_models.py tests/ops/test_config.py
git commit -m "feat(research): per-stage model config (OPS_RESEARCH_*_MODEL) + memo store path"
```

---

### Task 7: Mechanical memo validation

**Files:**
- Create: `ops/research/memo_validation.py`
- Test: `tests/ops/research/test_memo_validation.py`

**Interfaces:**
- Produces (Task 8 relies on these exact names):
  - `resolve_evidence(items: list[EvidenceItem], allowed_refs: set[str]) -> tuple[list[EvidenceItem], list[str]]` — kept items and one human-readable reason per dropped item. Filing-type items whose `source_ref` is not in `allowed_refs` are dropped; non-filing types (`price_data`, `memo`, etc.) are dropped too in v1 — the evidence stage only reads filings, so anything else is confabulated.
  - `validate_memo(memo: Memo, *, allowed_refs: set[str], known_precedents: set[str]) -> list[str]` — empty list = valid. Checks, each yielding one error string:
    1. `memo.block_matches_type()` is False → `"thesis block does not match thesis_type"`.
    2. No machine-checkable falsifier (at least one falsifier needs `metric`, `operator` AND `threshold` all set) → `"no machine-checkable falsifier (need metric+operator+threshold on at least one)"`. Prose-only *additional* falsifiers are fine (design doc: "machine-checkable where possible").
    3. Any evidence item with `source_type == "filing"` whose `source_ref` is not in `allowed_refs` → one error naming the bad ref (belt-and-braces: Task 8 strips these pre-assembly, but validation is the gate that must hold even if assembly changes).
    4. Any `precedent_memo_ids` entry not in `known_precedents` → error naming the invented id ("none found" = empty list is valid and explicit).
    5. `memo.entry_price_ref <= 0` → error.
    6. `memo.price_target_low > memo.price_target_high` → error.
- Consumes: `Memo`, `EvidenceItem`, `Falsifier` from `tradingagents.memos.schema`.

- [ ] **Step 1: Failing tests**

```python
"""Unit tests for mechanical memo validation (the weak-model gate)."""

from datetime import date

import pytest

from ops.research.memo_validation import resolve_evidence, validate_memo
from tradingagents.memos.schema import EvidenceItem, Falsifier, Memo, ValueThesis

pytestmark = pytest.mark.unit

REF = "0000000001-26-000001:mdna"


def _evidence(ref=REF, source_type="filing"):
    return EvidenceItem(claim="revenue fell 12%", source_type=source_type, source_ref=ref)


def _machine_falsifier():
    return Falsifier(
        description="gross margin below 30% for two quarters",
        check_type="fundamental", metric="gross_margin_pct",
        operator="<", threshold=30.0, consecutive_periods=2,
    )


def _memo(**overrides):
    kwargs = dict(
        ticker="WIDG", as_of_date=date(2026, 7, 6), thesis_type="value",
        thesis="Mispriced on a temporary distributor loss.",
        evidence=[_evidence()],
        value_block=ValueThesis(
            why_cheap="lost its largest distributor last quarter",
            change_trigger="insider cluster", normalized_earnings_view="~$1.20 EPS",
            quality_assessment="net cash, 20% ROIC",
        ),
        conviction_tier="starter", entry_price_ref=4.10,
        price_target_low=5.0, price_target_high=8.0, expected_holding_months=12,
        must_be_true=["distributor volume replaced within 3 quarters"],
        falsifiers=[_machine_falsifier()],
    )
    kwargs.update(overrides)
    return Memo(**kwargs)


def test_valid_memo_passes():
    assert validate_memo(_memo(), allowed_refs={REF}, known_precedents=set()) == []


def test_prose_only_falsifiers_rejected():
    memo = _memo(falsifiers=[Falsifier(description="thesis stops working", check_type="fundamental")])
    errors = validate_memo(memo, allowed_refs={REF}, known_precedents=set())
    assert any("machine-checkable" in e for e in errors)


def test_unresolvable_citation_rejected():
    memo = _memo(evidence=[_evidence(ref="9999999999-26-000001:mdna")])
    errors = validate_memo(memo, allowed_refs={REF}, known_precedents=set())
    assert any("9999999999" in e for e in errors)


def test_invented_precedent_rejected():
    memo = _memo(precedent_memo_ids=["deadbeef"])
    errors = validate_memo(memo, allowed_refs={REF}, known_precedents={"cafebabe"})
    assert any("deadbeef" in e for e in errors)


def test_known_precedent_and_empty_precedents_ok():
    assert validate_memo(
        _memo(precedent_memo_ids=["cafebabe"]),
        allowed_refs={REF}, known_precedents={"cafebabe"},
    ) == []


def test_inverted_targets_and_bad_price_rejected():
    memo = _memo(price_target_low=9.0, price_target_high=5.0, entry_price_ref=0.0)
    errors = validate_memo(memo, allowed_refs={REF}, known_precedents=set())
    assert len(errors) == 2


def test_resolve_evidence_strips_unknown_refs_and_non_filing():
    kept, dropped = resolve_evidence(
        [
            _evidence(),
            _evidence(ref="bad-ref:mdna"),
            _evidence(source_type="news"),
        ],
        allowed_refs={REF},
    )
    assert [e.source_ref for e in kept] == [REF]
    assert len(dropped) == 2
```

Run: `pytest tests/ops/research/test_memo_validation.py -v` — Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 2: Implement**

```python
"""Mechanical accept/reject gate for locally-generated memos.

Local models produce plausible-looking garbage; the defense is structural,
not prompt hope (spec decision 3). Every check here is machine-decidable:
a memo that fails is rejected (the hit is marked failed after one retry),
never stored. The monitoring loop (Phase C) depends on check 2: without a
machine-checkable falsifier a memo can never be mechanically monitored.
"""

from __future__ import annotations

from tradingagents.memos.schema import EvidenceItem, Memo


def _is_machine_checkable(falsifier) -> bool:
    return (
        bool(falsifier.metric)
        and falsifier.operator is not None
        and falsifier.threshold is not None
    )


def resolve_evidence(
    items: list[EvidenceItem], allowed_refs: set[str],
) -> tuple[list[EvidenceItem], list[str]]:
    """Keep items citing a section we actually read; explain each drop.

    v1 evidence only ever comes from filings the pipeline fetched, so
    non-filing source types are confabulation by construction.
    """
    kept: list[EvidenceItem] = []
    dropped: list[str] = []
    for item in items:
        if item.source_type != "filing":
            dropped.append(
                f"non-filing evidence ({item.source_type}): {item.claim[:80]!r}"
            )
        elif item.source_ref not in allowed_refs:
            dropped.append(
                f"unresolvable citation {item.source_ref!r}: {item.claim[:80]!r}"
            )
        else:
            kept.append(item)
    return kept, dropped


def validate_memo(
    memo: Memo, *, allowed_refs: set[str], known_precedents: set[str],
) -> list[str]:
    """All reasons this memo must be rejected; empty means store it."""
    errors: list[str] = []
    if not memo.block_matches_type():
        errors.append(
            "thesis block does not match thesis_type (fill exactly the "
            f"{memo.thesis_type}_block)"
        )
    if not any(_is_machine_checkable(f) for f in memo.falsifiers):
        errors.append(
            "no machine-checkable falsifier (need metric+operator+threshold "
            "on at least one)"
        )
    for item in memo.evidence:
        if item.source_type == "filing" and item.source_ref not in allowed_refs:
            errors.append(f"evidence cites unread section {item.source_ref!r}")
    for pid in memo.precedent_memo_ids:
        if pid not in known_precedents:
            errors.append(f"precedent memo id {pid!r} does not exist")
    if memo.entry_price_ref <= 0:
        errors.append(f"entry_price_ref must be positive, got {memo.entry_price_ref}")
    if memo.price_target_low > memo.price_target_high:
        errors.append(
            f"price_target_low {memo.price_target_low} exceeds "
            f"price_target_high {memo.price_target_high}"
        )
    return errors
```

- [ ] **Step 3: Run tests, full suite, lint, commit**

```bash
pytest tests/ops/research/test_memo_validation.py -v && pytest tests/ -q
ruff check ops/research/memo_validation.py tests/ops/research/test_memo_validation.py
git add ops/research/memo_validation.py tests/ops/research/test_memo_validation.py
git commit -m "feat(research): mechanical memo validation gate"
```

---

### Task 8: The two-stage brain

**Files:**
- Create: `ops/research/brain.py`
- Test: `tests/ops/research/test_brain.py`

**Interfaces:**
- Consumes: `FilingSection`, `extract_section`, `SectionNotFound`, `diff_filing_sections` (Tasks 1–2); `resolve_evidence`, `validate_memo` (Task 7); `summarize_memo` (Task 5); `bind_structured` from `tradingagents.agents.utils.structured`; `MemoStore`; `fetch_price_context` from `ops.research.prices`; `edgar.list_filings` / `edgar.fetch_filing_text`.
- Produces (Task 9 relies on these exact names):
  - `class ResearchError(RuntimeError)` — configuration-level failures (e.g. provider without structured output support).
  - Pydantic `EvidenceBatch(BaseModel): items: list[EvidenceItem] = []`
  - Pydantic `MemoDraft(BaseModel)` — everything the model authors; code assembles the `Memo` around it (the model never writes `memo_id`, `created_at`, `status`, `evidence`, `entry_price_ref`, `ticker`, `as_of_date`):

    ```python
    class MemoDraft(BaseModel):
        company_name: str = ""
        thesis_type: ThesisType
        thesis: str
        value_block: ValueThesis | None = None
        event_block: EventThesis | None = None
        conviction_tier: ConvictionTier
        price_target_low: float
        price_target_high: float
        expected_holding_months: int = Field(ge=1)
        scenarios: list[ReturnScenario] = Field(default_factory=list)
        must_be_true: list[str] = Field(min_length=1)
        falsifiers: list[Falsifier] = Field(min_length=1)
        catalysts: list[Catalyst] = Field(default_factory=list)
        precedent_memo_ids: list[str] = Field(default_factory=list)
        recommendation: Literal["buy", "pass"]
    ```
  - `@dataclass ResearchOutcome: symbol: str; hit_id: int; status: str` (`"researched" | "failed"`) `; memo_id: str | None = None; recommendation: str | None = None; errors: list[str] = field(default_factory=list); evidence_kept: int = 0; evidence_dropped: int = 0`
  - `research_hit(hit: dict, *, evidence_llm, thesis_llm, memo_store: MemoStore, list_filings=None, fetch_text=None, price_fetcher=None, today: date | None = None) -> ResearchOutcome` — the single-name pipeline. `hit` is a `ScreenStore.pending_hits()` element.
- Constants: `MIN_EVIDENCE_ITEMS = 3` (fewer surviving cited items than this = research too thin, reject), `MAX_TRIGGER_DOCS = 1`, `SECTION_MAX_CHARS = 12000`, `MAX_EVIDENCE_ITEMS_PER_SECTION = 8`.

**Pipeline shape** (all orchestration is Python; the LLM answers bounded prompts only):

1. **Reading plan** — one `list_filings(ticker, limit=200)` call, then from it: the latest 10-K (`mdna`, `risk_factors`, `business`), the latest 10-Q (`mdna`), a `mdna` diff of the two latest 10-Ks when two exist, and up to `MAX_TRIGGER_DOCS` trigger-source filings from the hit payload (accession match; section `"full"`). Each unique accession's text is fetched once via `fetch_text` and sections are extracted locally with `extract_section` — never one listing call per section. Missing sections (`SectionNotFound`) are skipped with a note, not fatal.
2. **Evidence stage** — per section, `bind_structured(evidence_llm, EvidenceBatch, "evidence")` (raise `ResearchError` if it returns `None` — the provider cannot do structured output at all) and one `.invoke(EVIDENCE_PROMPT...)`. A per-section exception or `None` result is recorded and skipped. All items across sections are then passed through `resolve_evidence` with `allowed_refs` = the source_refs actually read. Fewer than `MIN_EVIDENCE_ITEMS` survivors → outcome `failed` (no thesis stage, no LLM spend on garbage).
3. **Thesis stage** — (a) bear-case pass: plain `thesis_llm.invoke(BEAR_PROMPT...)`, take `.content`; (b) memo emission: `bind_structured(thesis_llm, MemoDraft, "memo")` + `.invoke(MEMO_PROMPT...)`; `None` or exception counts as a failed attempt.
4. **Assembly + validation** — `Memo(ticker=..., as_of_date=today, entry_price_ref=float(price), evidence=kept_items, status="open", **draft.model_dump(exclude={"recommendation"}))`; then `validate_memo`. On errors: ONE retry of the emission call with the errors appended (`retry_feedback`); still invalid → `failed`.
5. **Persist** — `memo_store.save(memo)`; if `draft.recommendation == "pass"` → `memo_store.mark_passed(memo.memo_id)` (shadow-tracking: a pass is data, not discard). Return `researched`.

The reference price comes from `price_fetcher` (default `fetch_price_context`) → `close_on_or_before(today)`; no price → `failed` (a memo without an entry reference can never be resolved).

Past memos: `memo_store.list(ticker=symbol)` → `known_precedents` id set + `summarize_memo` lines in the prompt (or the literal line `"No past memos for {ticker}: none found."` — the explicit finding).

- [ ] **Step 1: Write the prompts and skeleton first (no tests yet — the prompts are data).** Create `ops/research/brain.py` with module docstring, imports, constants, schemas, and the three prompt templates:

```python
"""The research brain: pending screen hit -> validated structured memo.

Two-stage design for small context windows (spec Phase B):

  Stage 1 (evidence): one bounded structured-output call per filing section;
  every item must cite the section it came from; uncited/unresolvable items
  are stripped MECHANICALLY (resolve_evidence), not by prompt hope.

  Stage 2 (thesis): bear-case-first pass (why is it cheap — a specific,
  named reason), then memo emission through bind_structured into MemoDraft;
  code assembles the Memo and validate_memo gates storage. One retry with
  the validation errors fed back, then the hit is marked failed.

Deliberately NOT an agentic tool loop: local models loop on tools (see
docs/ds4-backend.md gotchas). The LLM only ever answers bounded prompts;
Python decides what gets read.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from ops.research.memo_validation import resolve_evidence, validate_memo
from ops.research.prices import fetch_price_context
from tradingagents.agents.utils.structured import bind_structured
from tradingagents.dataflows.edgar_sections import (
    FilingSection,
    SectionNotFound,
    diff_filing_sections,
    extract_section,
)
from tradingagents.memos.schema import (
    Catalyst,
    ConvictionTier,
    EventThesis,
    EvidenceItem,
    Falsifier,
    Memo,
    ReturnScenario,
    ThesisType,
    ValueThesis,
)
from tradingagents.memos.store import MemoStore

logger = logging.getLogger(__name__)

MIN_EVIDENCE_ITEMS = 3
MAX_TRIGGER_DOCS = 1
SECTION_MAX_CHARS = 12000
MAX_EVIDENCE_ITEMS_PER_SECTION = 8


class ResearchError(RuntimeError):
    """Configuration-level failure (not a per-name data problem)."""


class EvidenceBatch(BaseModel):
    items: list[EvidenceItem] = Field(default_factory=list)


class MemoDraft(BaseModel):
    """What the model authors. Code owns identity, evidence, and pricing."""

    company_name: str = ""
    thesis_type: ThesisType
    thesis: str
    value_block: ValueThesis | None = None
    event_block: EventThesis | None = None
    conviction_tier: ConvictionTier
    price_target_low: float
    price_target_high: float
    expected_holding_months: int = Field(ge=1)
    scenarios: list[ReturnScenario] = Field(default_factory=list)
    must_be_true: list[str] = Field(min_length=1)
    falsifiers: list[Falsifier] = Field(min_length=1)
    catalysts: list[Catalyst] = Field(default_factory=list)
    precedent_memo_ids: list[str] = Field(default_factory=list)
    recommendation: Literal["buy", "pass"]


EVIDENCE_PROMPT = """\
You are an equity research analyst reading ONE section of an SEC filing \
for {ticker}.

Section: {section} of accession {accession} (form {form}).
On EVERY item set source_type="filing" and source_ref="{source_ref}" \
exactly — items citing anything else are discarded.

Extract up to {max_items} evidence items bearing on:
- why the stock might be cheap, or why the cheapness is deserved,
- business quality: margins, returns on capital, balance sheet,
- what changed vs the prior year: new risks, changed numbers, dropped language,
- anything a bear would seize on.

Each item: ONE factual claim, with a short verbatim quote from the text. \
Only what this text supports — no opinions, no outside knowledge.

--- SECTION TEXT ---
{text}
"""

BEAR_PROMPT = """\
You are the bear on {ticker}. It passed a cheapness+quality screen; the \
screen result and cited filing evidence are below.

First: state the single most likely SPECIFIC reason the market prices \
{ticker} where it does. "Risks include competition" is not an answer — name \
the actual concern (segment in decline, customer concentration, fading \
one-time earnings, leverage, litigation, secular threat, value trap).

Then: the 2-3 strongest bear arguments, each grounded in the evidence below.

Screen result:
{screen_summary}

Evidence:
{evidence_bullets}
"""

MEMO_PROMPT = """\
Write the investment memo for {ticker} as of {asof}. Reference price: {price}.

Rules:
- thesis_type "value" (mispriced earning power) fills value_block ONLY; \
"event" (forced/non-economic seller) fills event_block ONLY. In a value \
memo, why_cheap MUST answer the bear case below with a specific named reason.
- falsifiers: at least one MUST be machine-checkable — metric, operator, \
AND threshold all set (metric examples: gross_margin_pct, revenue_yoy_pct, \
net_debt_to_ebitda, drawdown_from_cost_pct). Pre-commit now; these are the \
sell rules.
- must_be_true: the load-bearing assumptions, one sentence each.
- precedent_memo_ids: ONLY ids from the past-memos list; empty if none \
apply — "none found" is an explicit, acceptable finding. Never invent ids.
- scenarios: probability-weighted branches; calibration data only, never \
sizing inputs.
- recommendation: "buy" if you would open the position now, else "pass". \
Passed memos are shadow-tracked and scored later, so pass honestly.

Screen result:
{screen_summary}

Bear case:
{bear_case}

Evidence (already validated; cite-able):
{evidence_bullets}

Past memos for {ticker}:
{past_memos}
{retry_feedback}
"""
```

- [ ] **Step 2: Write the failing tests.** Create `tests/ops/research/test_brain.py`:

```python
"""Unit tests for the two-stage research brain (no network, no real LLMs)."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ops.research import brain
from ops.research.brain import EvidenceBatch, MemoDraft, research_hit
from ops.research.prices import PriceContext
from tradingagents.dataflows.edgar import Filing
from tradingagents.memos.schema import EvidenceItem, Falsifier, ValueThesis
from tradingagents.memos.store import MemoStore

pytestmark = pytest.mark.unit

TODAY = date(2026, 7, 6)
ACC_10K = "0000000001-26-000001"
ACC_10K_OLD = "0000000001-25-000001"
ACC_10Q = "0000000001-26-000050"

TEN_K_TEXT = "\n".join([
    "Item 1. Business", "We make widgets.",
    "Item 1A. Risk Factors", "Customer concentration is 40%.",
    "Item 7. Management's Discussion and Analysis", "Revenue fell 12%.",
    "Item 8. Financial Statements", "Notes.",
])
TEN_Q_TEXT = "\n".join([
    "Item 2. Management's Discussion and Analysis", "Q1 revenue stabilized.",
    "Item 3. Quantitative Disclosures", "None.",
])


def _filing(accession, form, filed, report):
    return Filing(
        ticker="WIDG", cik=1, accession_number=accession, form=form,
        filing_date=filed, report_date=report, primary_document="doc.htm",
    )


FILINGS = [
    _filing(ACC_10Q, "10-Q", date(2026, 5, 10), date(2026, 3, 31)),
    _filing(ACC_10K, "10-K", date(2026, 3, 1), date(2025, 12, 31)),
    _filing(ACC_10K_OLD, "10-K", date(2025, 3, 1), date(2024, 12, 31)),
]
TEXTS = {ACC_10K: TEN_K_TEXT, ACC_10K_OLD: TEN_K_TEXT, ACC_10Q: TEN_Q_TEXT}


def _hit():
    return {
        "id": 7, "run_id": "screen-2026-07-04-abcd1234", "symbol": "WIDG",
        "asof": "2026-07-04", "status": "pending",
        "payload": {
            "symbol": "WIDG", "asof": "2026-07-04", "passed": True,
            "cheap": True, "quality": True,
            "valuation_bars": [
                {"name": "fcf_yield", "passed": True, "detail": "FCF yield 9.1% vs 6%"},
            ],
            "quality_bars": [
                {"name": "roic_5y", "passed": True, "detail": "mean ROIC 15.2% vs 12%"},
            ],
            "triggers": [
                {"kind": "selloff", "description": "40% below high",
                 "date": "2026-07-04", "source": "price"},
            ],
            "market_cap": "450000000", "ev_ebit": "6.1",
        },
    }


def _evidence_item(ref):
    return EvidenceItem(
        claim="revenue fell 12%", source_type="filing", source_ref=ref, quote="Revenue fell 12%.",
    )


def _draft(**overrides):
    kwargs = dict(
        company_name="Widget Co", thesis_type="value",
        thesis="Mispriced on distributor loss. Earnings normalize.",
        value_block=ValueThesis(
            why_cheap="lost largest distributor", change_trigger="selloff",
            normalized_earnings_view="$1.20", quality_assessment="net cash",
        ),
        conviction_tier="starter", price_target_low=5.0, price_target_high=8.0,
        expected_holding_months=12, must_be_true=["volume replaced"],
        falsifiers=[Falsifier(
            description="margin collapse", check_type="fundamental",
            metric="gross_margin_pct", operator="<", threshold=30.0,
        )],
        recommendation="buy",
    )
    kwargs.update(overrides)
    return MemoDraft(**kwargs)


class FakeLLM:
    """Covers both bind_structured (returns self) and plain .invoke paths.

    ``responses`` is consumed in order. Pydantic instances are returned as-is
    (structured call results); strings come back as .content objects (plain
    calls); Exceptions are raised.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, str):
            return SimpleNamespace(content=result)
        return result


@pytest.fixture
def memo_store(tmp_path):
    return MemoStore(tmp_path / "memos.sqlite")


def _price_fetcher(symbol):
    return PriceContext(closes={TODAY: Decimal("4.10")})


def _run(evidence_llm, thesis_llm, memo_store, hit=None):
    return research_hit(
        hit or _hit(), evidence_llm=evidence_llm, thesis_llm=thesis_llm,
        memo_store=memo_store,
        list_filings=lambda ticker, **kw: FILINGS,
        fetch_text=lambda f, **kw: TEXTS[f.accession_number],
        price_fetcher=_price_fetcher, today=TODAY,
    )


def _good_evidence_llm():
    # 5 sections read: 10-K mdna/risk_factors/business, 10-Q mdna, 10-K diff.
    return FakeLLM([
        EvidenceBatch(items=[_evidence_item(f"{ACC_10K}:mdna")]),
        EvidenceBatch(items=[_evidence_item(f"{ACC_10K}:risk_factors")]),
        EvidenceBatch(items=[_evidence_item(f"{ACC_10K}:business")]),
        EvidenceBatch(items=[_evidence_item(f"{ACC_10Q}:mdna")]),
        EvidenceBatch(items=[_evidence_item(f"{ACC_10K}+{ACC_10K_OLD}:mdna_diff")]),
    ])


def test_happy_path_saves_open_memo(memo_store):
    thesis_llm = FakeLLM(["bear: distributor loss is permanent", _draft()])
    outcome = _run(_good_evidence_llm(), thesis_llm, memo_store)
    assert outcome.status == "researched"
    assert outcome.recommendation == "buy"
    memo = memo_store.get(outcome.memo_id)
    assert memo.status == "open"
    assert memo.ticker == "WIDG"
    assert memo.entry_price_ref == pytest.approx(4.10)
    assert memo.as_of_date == TODAY
    assert len(memo.evidence) == 5


def test_pass_recommendation_shadow_tracks(memo_store):
    thesis_llm = FakeLLM(["bear case", _draft(recommendation="pass")])
    outcome = _run(_good_evidence_llm(), thesis_llm, memo_store)
    assert outcome.status == "researched"
    assert memo_store.get(outcome.memo_id).status == "passed"


def test_uncited_evidence_stripped_and_thin_research_fails(memo_store):
    # Model cites a section that was never read -> all items dropped -> fail
    # before any thesis-stage spend.
    bad = FakeLLM([EvidenceBatch(items=[_evidence_item("invented:mdna")])] * 5)
    thesis_llm = FakeLLM([])
    outcome = _run(bad, thesis_llm, memo_store)
    assert outcome.status == "failed"
    assert outcome.evidence_dropped == 5
    assert thesis_llm.prompts == []  # thesis stage never ran
    assert any("evidence" in e for e in outcome.errors)


def test_invalid_memo_retries_once_with_feedback_then_fails(memo_store):
    bad_draft = _draft(falsifiers=[
        Falsifier(description="prose only", check_type="fundamental"),
    ])
    thesis_llm = FakeLLM(["bear case", bad_draft, bad_draft])
    outcome = _run(_good_evidence_llm(), thesis_llm, memo_store)
    assert outcome.status == "failed"
    assert any("machine-checkable" in e for e in outcome.errors)
    # 3 thesis calls: bear, emission, retry emission — and the retry prompt
    # carried the validation feedback.
    assert len(thesis_llm.prompts) == 3
    assert "machine-checkable" in thesis_llm.prompts[2]
    assert memo_store.list(ticker="WIDG") == []


def test_retry_success_saves(memo_store):
    bad_draft = _draft(precedent_memo_ids=["invented"])
    thesis_llm = FakeLLM(["bear case", bad_draft, _draft()])
    outcome = _run(_good_evidence_llm(), thesis_llm, memo_store)
    assert outcome.status == "researched"


def test_no_price_fails_fast(memo_store):
    outcome = research_hit(
        _hit(), evidence_llm=FakeLLM([]), thesis_llm=FakeLLM([]),
        memo_store=memo_store,
        list_filings=lambda ticker, **kw: FILINGS,
        fetch_text=lambda f, **kw: TEXTS[f.accession_number],
        price_fetcher=lambda s: None, today=TODAY,
    )
    assert outcome.status == "failed"
    assert any("price" in e for e in outcome.errors)


def test_structured_output_unsupported_raises_research_error(memo_store):
    class NoStructured:
        def with_structured_output(self, schema):
            raise NotImplementedError

    with pytest.raises(brain.ResearchError):
        _run(NoStructured(), FakeLLM([]), memo_store)


def test_past_memos_feed_precedents(memo_store):
    # Seed a prior memo; the new draft may cite its id and validation passes.
    thesis_llm1 = FakeLLM(["bear", _draft()])
    first = _run(_good_evidence_llm(), thesis_llm1, memo_store)
    prior_id = first.memo_id

    thesis_llm2 = FakeLLM(["bear", _draft(precedent_memo_ids=[prior_id])])
    second = _run(_good_evidence_llm(), thesis_llm2, memo_store)
    assert second.status == "researched"
    # The thesis prompt actually contained the precedent summary.
    assert prior_id in thesis_llm2.prompts[1]
```

Run: `pytest tests/ops/research/test_brain.py -v` — Expected: FAIL (`ImportError: cannot import name 'research_hit'`).

- [ ] **Step 3: Implement the pipeline** (append to `brain.py`):

```python
@dataclass
class ResearchOutcome:
    symbol: str
    hit_id: int
    status: str  # "researched" | "failed"
    memo_id: str | None = None
    recommendation: str | None = None
    errors: list[str] = field(default_factory=list)
    evidence_kept: int = 0
    evidence_dropped: int = 0


def _screen_summary(payload: dict) -> str:
    lines = [f"{payload['symbol']} screened {payload['asof']}: "
             f"cheap={payload['cheap']} quality={payload['quality']} "
             f"market_cap={payload['market_cap']} ev_ebit={payload['ev_ebit']}"]
    for bar in (*payload.get("valuation_bars", []), *payload.get("quality_bars", [])):
        mark = "PASS" if bar["passed"] else "fail"
        lines.append(f"  [{mark}] {bar['name']}: {bar['detail']}")
    for trig in payload.get("triggers", []):
        lines.append(f"  trigger {trig['kind']} ({trig['date']}): {trig['description']}")
    return "\n".join(lines)


def _evidence_bullets(items: list[EvidenceItem]) -> str:
    return "\n".join(
        f"- {i.claim} [{i.source_ref}]" + (f' "{i.quote}"' if i.quote else "")
        for i in items
    )


def _build_reading_plan(
    symbol: str, payload: dict, *, list_filings, fetch_text,
) -> list[FilingSection]:
    """Fetch each needed accession once; extract sections locally."""
    filings = list_filings(symbol, limit=200)
    by_accession = {f.accession_number: f for f in filings}
    ten_ks = [f for f in filings if f.form.startswith("10-K")]
    ten_qs = [f for f in filings if f.form.startswith("10-Q")]
    wanted: list[tuple[object, str]] = []  # (filing, section)
    if ten_ks:
        wanted += [(ten_ks[0], s) for s in ("mdna", "risk_factors", "business")]
    if ten_qs:
        wanted.append((ten_qs[0], "mdna"))
    trigger_accessions = [
        t["source"] for t in payload.get("triggers", []) if t["source"] != "price"
    ]
    for acc in trigger_accessions[:MAX_TRIGGER_DOCS]:
        if acc in by_accession:
            wanted.append((by_accession[acc], "full"))

    texts: dict[str, str] = {}
    sections: list[FilingSection] = []
    for filing, section in wanted:
        acc = filing.accession_number
        if acc not in texts:
            try:
                texts[acc] = fetch_text(filing)
            except Exception as exc:
                print(f"[research] {symbol}: fetch {acc} failed: {exc}", file=sys.stderr)
                texts[acc] = ""
        if not texts[acc]:
            continue
        try:
            body = extract_section(
                texts[acc], form=filing.form, section=section,
                max_chars=SECTION_MAX_CHARS,
            )
        except SectionNotFound as exc:
            print(f"[research] {symbol}: {exc}", file=sys.stderr)
            continue
        sections.append(FilingSection(
            ticker=symbol, accession=acc, section=section,
            form=filing.form, text=body,
        ))
    if len(ten_ks) >= 2:
        try:
            diff = diff_filing_sections(
                symbol, "mdna",
                (ten_ks[1].report_date or ten_ks[1].filing_date).year,
                (ten_ks[0].report_date or ten_ks[0].filing_date).year,
                max_chars=SECTION_MAX_CHARS,
                # Must honor the forms filter diff_filing_sections passes —
                # a raw `filings` passthrough would let 10-Qs into by_year.
                list_filings=lambda t, forms=None, **kw: [
                    f for f in filings if forms is None or f.form in forms
                ],
                fetch_text=lambda f, **kw: texts.get(f.accession_number) or fetch_text(f),
            )
            sections.append(FilingSection(
                ticker=symbol, accession=diff.source_ref.split(":")[0],
                section="mdna_diff", form="10-K", text=diff.text,
            ))
        except (SectionNotFound, KeyError) as exc:
            print(f"[research] {symbol}: mdna diff skipped: {exc}", file=sys.stderr)
    return sections


def _run_evidence_stage(
    evidence_llm, sections: list[FilingSection], *, symbol: str,
) -> tuple[list[EvidenceItem], set[str], list[str]]:
    structured = bind_structured(evidence_llm, EvidenceBatch, "research-evidence")
    if structured is None:
        raise ResearchError(
            "evidence model does not support structured output; "
            "set OPS_RESEARCH_EVIDENCE_MODEL to a provider that does"
        )
    items: list[EvidenceItem] = []
    notes: list[str] = []
    allowed_refs = {s.source_ref for s in sections}
    for section in sections:
        prompt = EVIDENCE_PROMPT.format(
            ticker=symbol, section=section.section, accession=section.accession,
            form=section.form, source_ref=section.source_ref,
            max_items=MAX_EVIDENCE_ITEMS_PER_SECTION, text=section.text,
        )
        try:
            batch = structured.invoke(prompt)
        except Exception as exc:
            notes.append(f"evidence call failed for {section.source_ref}: {exc}")
            continue
        if batch is None:
            notes.append(f"evidence call returned nothing for {section.source_ref}")
            continue
        items.extend(batch.items)
    return items, allowed_refs, notes


def research_hit(
    hit: dict,
    *,
    evidence_llm,
    thesis_llm,
    memo_store: MemoStore,
    list_filings=None,
    fetch_text=None,
    price_fetcher=None,
    today: date | None = None,
) -> ResearchOutcome:
    """Run the full two-stage pipeline for one pending screen hit."""
    from tradingagents.agents.utils.filing_reader_tools import summarize_memo
    from tradingagents.dataflows import edgar

    list_filings = list_filings or edgar.list_filings
    fetch_text = fetch_text or edgar.fetch_filing_text
    price_fetcher = price_fetcher or fetch_price_context
    today = today or date.today()
    symbol = hit["symbol"]
    payload = hit["payload"]
    outcome = ResearchOutcome(symbol=symbol, hit_id=hit["id"], status="failed")

    ctx = price_fetcher(symbol)
    price = ctx.close_on_or_before(today) if ctx is not None else None
    if price is None:
        outcome.errors.append(f"no reference price for {symbol} at {today}")
        return outcome

    sections = _build_reading_plan(
        symbol, payload, list_filings=list_filings, fetch_text=fetch_text,
    )
    if not sections:
        outcome.errors.append("no readable filings")
        return outcome

    raw_items, allowed_refs, notes = _run_evidence_stage(
        evidence_llm, sections, symbol=symbol,
    )
    outcome.errors.extend(notes)
    kept, dropped = resolve_evidence(raw_items, allowed_refs)
    outcome.evidence_kept, outcome.evidence_dropped = len(kept), len(dropped)
    if len(kept) < MIN_EVIDENCE_ITEMS:
        outcome.errors.append(
            f"insufficient cited evidence: {len(kept)} kept "
            f"(need {MIN_EVIDENCE_ITEMS}), {len(dropped)} dropped"
        )
        return outcome

    past = memo_store.list(ticker=symbol)
    known_precedents = {m.memo_id for m in past}
    past_memos_text = (
        "\n".join(summarize_memo(m) for m in past)
        if past else f"No past memos for {symbol}: none found."
    )
    screen_summary = _screen_summary(payload)
    evidence_bullets = _evidence_bullets(kept)

    bear = thesis_llm.invoke(BEAR_PROMPT.format(
        ticker=symbol, screen_summary=screen_summary,
        evidence_bullets=evidence_bullets,
    )).content

    structured = bind_structured(thesis_llm, MemoDraft, "research-memo")
    if structured is None:
        raise ResearchError(
            "thesis model does not support structured output; "
            "set OPS_RESEARCH_THESIS_MODEL to a provider that does"
        )

    retry_feedback = ""
    for attempt in range(2):
        prompt = MEMO_PROMPT.format(
            ticker=symbol, asof=today.isoformat(), price=price,
            screen_summary=screen_summary, bear_case=bear,
            evidence_bullets=evidence_bullets, past_memos=past_memos_text,
            retry_feedback=retry_feedback,
        )
        try:
            draft = structured.invoke(prompt)
        except Exception as exc:
            outcome.errors.append(f"memo emission failed (attempt {attempt + 1}): {exc}")
            draft = None
        if draft is None:
            retry_feedback = (
                "\nYour previous answer was not valid structured output. "
                "Emit the memo again, matching the schema exactly."
            )
            continue
        memo = Memo(
            ticker=symbol, as_of_date=today, entry_price_ref=float(price),
            evidence=kept, status="open",
            **draft.model_dump(exclude={"recommendation"}),
        )
        errors = validate_memo(
            memo, allowed_refs=allowed_refs, known_precedents=known_precedents,
        )
        if not errors:
            memo_store.save(memo)
            if draft.recommendation == "pass":
                memo_store.mark_passed(memo.memo_id)
            outcome.status = "researched"
            outcome.memo_id = memo.memo_id
            outcome.recommendation = draft.recommendation
            return outcome
        outcome.errors.extend(errors)
        retry_feedback = (
            "\nYour previous memo was REJECTED for these reasons — fix each "
            "one exactly:\n" + "\n".join(f"- {e}" for e in errors)
        )
    return outcome
```

Implementation notes:
- `Memo(...)` construction passes `value_block`/`event_block` as Pydantic sub-models from `draft.model_dump()` — Pydantic v2 re-validates dicts into models automatically; if it does not in this repo's Pydantic version, use `draft.model_dump(exclude={"recommendation"}, mode="python")` (sub-models stay as instances with the default `model_dump`? NO — `model_dump` converts recursively to dicts, which `Memo` re-validates fine in Pydantic v2. If a test fails on this, construct `Memo` with explicit fields from `draft` attributes instead: `value_block=draft.value_block`, etc.). The tests will tell you immediately.
- The `test_structured_output_unsupported_raises_research_error` test exercises the `bind_structured` → `None` path (bind_structured catches `NotImplementedError`/`AttributeError` and returns None).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ops/research/test_brain.py -v` — Expected: 9 passed

- [ ] **Step 5: Full suite, lint, commit**

```bash
pytest tests/ -q
ruff check ops/research/brain.py tests/ops/research/test_brain.py
git add ops/research/brain.py tests/ops/research/test_brain.py
git commit -m "feat(research): two-stage memo brain — cited evidence, bear-case-first thesis, validated emission"
```

---

### Task 9: `failed` hit status + `ops research run`

**Files:**
- Modify: `ops/research/store.py` (one method + docstring), `ops/cli.py`
- Test: extend `tests/ops/research/test_store.py`; new `tests/ops/test_cli_research_run.py`

**Interfaces:**
- Produces: `ScreenStore.mark_failed(hit_id: int) -> None` (status `"failed"`; a failed symbol is re-queueable by a later screen run exactly like `researched`/`expired` — `record_run` only dedupes against `pending`); CLI `ops research run [--max-names N]` (default 3).
- Consumes: `research_hit`, `ResearchError` (Task 8); `build_stage_llm` (Task 6); `load_managed_backend_config` / `build_managed_backend` from `ops.llm_backend`; `MemoStore`; `ScreenStore`.
- CLI behavior: no pending hits → print and exit 0. Otherwise bring the managed backend up once (honors `OPS_LLM_MANAGED_BACKEND=ds4`; `NullManagedBackend` no-ops for LM Studio/JIT), research up to N oldest hits, `mark_researched` or `mark_failed` per outcome, ALWAYS `backend.shutdown()` in `finally`, print a per-name line and a summary. Exit code 1 when every attempted hit failed (the blind-run analog), else 0. A per-hit unexpected exception marks that hit failed and continues — EXCEPT `ResearchError`, which is configuration and aborts the batch.

- [ ] **Step 1: Failing store test** (append to `tests/ops/research/test_store.py`, following its fixtures):

```python
def test_mark_failed_and_requeue(store):
    run1 = store.record_run(asof=ASOF, universe_size=5, results=[_result("AAA")])
    hit = store.pending_hits()[0]
    store.mark_failed(hit["id"])
    assert store.pending_hits() == []
    # A later run may queue the symbol again.
    store.record_run(asof=ASOF, universe_size=5, results=[_result("AAA")])
    assert [h["symbol"] for h in store.pending_hits()] == ["AAA"]
```

(Adapt `_result`/`ASOF`/fixture names to the file's existing helpers — read it first. The contract: after `mark_failed` the hit is not pending, and the symbol re-queues.)

- [ ] **Step 2: Implement `mark_failed`** in `ops/research/store.py` next to `mark_expired`:

```python
    def mark_failed(self, hit_id: int) -> None:
        """Deep research rejected this hit's memo (weak-model guardrails).

        Surfaced for human review via `ops research run` output; a later
        screen pass may queue the symbol fresh.
        """
        self._set_status(hit_id, "failed")
```

Update the module docstring's lifecycle line to: `pending -> researched (a memo exists) | failed (research rejected) | expired (went stale)`.

Run: `pytest tests/ops/research/test_store.py -v` — Expected: PASS.

- [ ] **Step 3: Failing CLI test.** Create `tests/ops/test_cli_research_run.py`:

```python
"""Unit tests for `ops research run` (LLMs, stores, and backend all faked)."""

from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner

import ops.cli as cli_mod
from ops.research.brain import ResearchOutcome

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_SCREEN_STORE_PATH", str(tmp_path / "screen.sqlite"))
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    monkeypatch.delenv("OPS_LLM_MANAGED_BACKEND", raising=False)
    # The command imports these lazily (repo convention: heavy imports live
    # in command bodies), so patch the SOURCE modules, not ops.cli.
    monkeypatch.setattr("ops.research.models.build_stage_llm", lambda spec: f"llm:{spec}")
    return tmp_path


def _seed_hits(tmp_path, symbols):
    from ops.research.screener import Bar, ScreenResult
    from ops.research.store import ScreenStore

    store = ScreenStore(tmp_path / "screen.sqlite")
    results = [
        ScreenResult(
            symbol=s, asof=date(2026, 7, 4), passed=True, cheap=True, quality=True,
            valuation_bars=(Bar("fcf_yield", True, "ok"),),
            quality_bars=(Bar("roic_5y", True, "ok"),),
            triggers=(), market_cap=Decimal("450000000"), ev_ebit=Decimal("6"),
        )
        for s in symbols
    ]
    store.record_run(asof=date(2026, 7, 4), universe_size=9, results=results)
    return store


def test_no_pending_hits_exits_zero(env):
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["research", "run"])
    assert result.exit_code == 0
    assert "no pending hits" in result.output


def test_researches_marks_and_summarizes(env, monkeypatch):
    store = _seed_hits(env, ["AAA", "BBB", "CCC", "DDD"])

    def fake_research(hit, **kw):
        status = "failed" if hit["symbol"] == "BBB" else "researched"
        return ResearchOutcome(
            symbol=hit["symbol"], hit_id=hit["id"], status=status,
            memo_id="m-" + hit["symbol"] if status == "researched" else None,
            recommendation="buy" if status == "researched" else None,
            errors=["no machine-checkable falsifier"] if status == "failed" else [],
        )

    monkeypatch.setattr("ops.research.brain.research_hit", fake_research)
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["research", "run", "--max-names", "3"])
    assert result.exit_code == 0, result.output
    statuses = {h["symbol"]: h["status"] for h in _all_hits(store)}
    assert statuses == {
        "AAA": "researched", "BBB": "failed", "CCC": "researched", "DDD": "pending",
    }
    assert "2 researched, 1 failed" in result.output


def test_all_failed_exits_one(env, monkeypatch):
    _seed_hits(env, ["AAA"])
    monkeypatch.setattr(
        "ops.research.brain.research_hit",
        lambda hit, **kw: ResearchOutcome(
            symbol=hit["symbol"], hit_id=hit["id"], status="failed",
            errors=["insufficient cited evidence"],
        ),
    )
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["research", "run"])
    assert result.exit_code == 1


def test_unexpected_exception_marks_failed_and_continues(env, monkeypatch):
    store = _seed_hits(env, ["AAA", "BBB"])
    calls = {"n": 0}

    def flaky(hit, **kw):
        calls["n"] += 1
        if hit["symbol"] == "AAA":
            raise RuntimeError("backend hiccup")
        return ResearchOutcome(
            symbol=hit["symbol"], hit_id=hit["id"], status="researched",
            memo_id="m-BBB", recommendation="pass",
        )

    monkeypatch.setattr("ops.research.brain.research_hit", flaky)
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["research", "run"])
    assert result.exit_code == 0, result.output
    statuses = {h["symbol"]: h["status"] for h in _all_hits(store)}
    assert statuses == {"AAA": "failed", "BBB": "researched"}


def _all_hits(store):
    with store._connect() as conn:
        rows = conn.execute("SELECT symbol, status FROM screen_hits ORDER BY id").fetchall()
    return [{"symbol": r["symbol"], "status": r["status"]} for r in rows]
```

Run: `pytest tests/ops/test_cli_research_run.py -v` — Expected: FAIL (no `run` command).

- [ ] **Step 4: Implement the CLI command.** In `ops/cli.py`, add under the existing `research` group. All research imports stay INSIDE the command body (repo convention — the brain pulls in yfinance/pydantic and must not slow `ops --help`); the tests patch the source modules, and the lazy `from ... import` picks the patched attributes up at invocation time:

```python
@research.command("run")
@click.option("--max-names", default=3, show_default=True, type=int,
              help="How many pending hits to research this batch (oldest first).")
def research_run(max_names: int) -> None:
    """Deep-research pending screen hits into structured memos (local models)."""
    from ops.llm_backend import build_managed_backend, load_managed_backend_config
    from ops.research.brain import ResearchError, research_hit
    from ops.research.models import build_stage_llm
    from ops.research.store import ScreenStore
    from tradingagents.memos.store import MemoStore

    config = load_config()
    store = ScreenStore(config.screen_store_path)
    hits = store.pending_hits()[:max_names]
    if not hits:
        click.echo("no pending hits")
        return
    memo_store = MemoStore(config.memo_store_path)
    evidence_llm = build_stage_llm(config.research_evidence_model)
    thesis_llm = build_stage_llm(config.research_thesis_model)
    backend = build_managed_backend(load_managed_backend_config())
    researched = failed = 0
    try:
        backend.ensure_up()
        for hit in hits:
            try:
                outcome = research_hit(
                    hit, evidence_llm=evidence_llm, thesis_llm=thesis_llm,
                    memo_store=memo_store,
                )
            except ResearchError:
                raise  # configuration problem: abort the whole batch
            except Exception as exc:
                store.mark_failed(hit["id"])
                failed += 1
                click.echo(f"{hit['symbol']}: FAILED ({type(exc).__name__}: {exc})")
                continue
            if outcome.status == "researched":
                store.mark_researched(hit["id"])
                researched += 1
                click.echo(
                    f"{outcome.symbol}: memo {outcome.memo_id} "
                    f"({outcome.recommendation}; evidence {outcome.evidence_kept} kept"
                    f"/{outcome.evidence_dropped} dropped)"
                )
            else:
                store.mark_failed(hit["id"])
                failed += 1
                click.echo(f"{outcome.symbol}: FAILED — " + "; ".join(outcome.errors))
    finally:
        backend.shutdown()
    click.echo(f"research run: {researched} researched, {failed} failed, "
               f"{len(store.pending_hits())} still pending")
    if failed == len(hits):
        raise SystemExit(1)
```

- [ ] **Step 5: Run tests, full suite, lint, commit**

```bash
pytest tests/ops/test_cli_research_run.py tests/ops/research/test_store.py -v && pytest tests/ -q
ruff check ops/research/store.py ops/cli.py tests/ops/test_cli_research_run.py tests/ops/research/test_store.py
git add ops/research/store.py ops/cli.py tests/ops/test_cli_research_run.py tests/ops/research/test_store.py
git commit -m "feat(research): ops research run — batch deep-research entry point + failed hit status"
```

---

### Task 10: Docs, PR, optional live smoke

- [ ] **Step 1: Write `docs/research_brain.md`** — the runbook for the brain:

```markdown
# Research Brain Runbook (`ops research run`)

Phase B of docs/superpowers/specs/2026-07-06-finish-research-system-design.md.
Turns pending screen hits (see docs/research_screener.md) into structured
memos in the memo store. Local models only.

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `OPS_RESEARCH_EVIDENCE_MODEL` | `openai_compatible:deepseek-v4-flash@http://127.0.0.1:8000/v1` | stage-1 model (`provider:model[@base_url]`) |
| `OPS_RESEARCH_THESIS_MODEL` | same | stage-2 model |
| `OPS_MEMO_STORE_PATH` | `~/.local/state/tradingagents/memos.sqlite` | memo corpus |
| `OPS_LLM_MANAGED_BACKEND` | unset | `ds4` = auto start/stop the ds4 server around the batch |
| `SEC_EDGAR_USER_AGENT` | — | required (filings are read live) |

LM Studio instead of ds4: leave `OPS_LLM_MANAGED_BACKEND` unset and point the
model specs at `openai_compatible:<model-id>@http://localhost:1234/v1`
(JIT loading brings the model up). Upgrading one stage to an API model is a
pure config change, e.g. `OPS_RESEARCH_THESIS_MODEL=anthropic:claude-sonnet-5`.

## Running

    ops research run --max-names 3

Oldest pending hits first. Each name: bounded section reads (latest 10-K
mdna/risk_factors/business, latest 10-Q mdna, 10-K MD&A YoY diff, one
trigger filing) -> cited evidence extraction -> bear-case pass -> memo
emission -> mechanical validation. On ds4 budget tens of minutes per name
(single-threaded heavy reasoner; watch the ds4 server log for progress).

## Rejection (weak-model guardrails)

A memo is stored ONLY if: >=1 falsifier is machine-checkable
(metric+operator+threshold), every evidence citation resolves to a section
actually read, precedent ids exist in the corpus, the thesis block matches
thesis_type, and price targets are sane. One retry with the errors fed
back, then the hit is marked `failed` (visible in the run summary; the
symbol re-queues on a later screen pass). `recommendation: pass` memos are
stored with status `passed` and shadow-tracked — a pass is data.

## Inspecting output

    sqlite3 ~/.local/state/tradingagents/memos.sqlite \
      "SELECT memo_id, ticker, thesis_type, status, conviction_tier FROM memos"
```

- [ ] **Step 2: Update `docs/long_horizon_research.md`** build order: mark steps 4 and 5 done (`4. ✅ Filing-reader agent tools ...`, `5. ✅ Thesis-type-aware memo pipeline (ops/research/brain.py; two-stage, deterministic orchestration) ...` — adjust step 5's wording to what was actually built: a deterministic two-stage pipeline rather than a graph-node rework, per the Phase B spec).

- [ ] **Step 3: Full suite, lint, commit, push, PR**

```bash
pytest tests/ -q
git add docs/research_brain.md docs/long_horizon_research.md
git commit -m "docs(research): brain runbook + build-order checkmarks"
git push -u origin feat/phase-b-brain
gh pr create --repo CWFred/TradingAgents --base main --head feat/phase-b-brain \
  --title "feat(research): phase B — the brain (filing readers, insider clusters, two-stage memo pipeline)" \
  --body "Implements Phase B of docs/superpowers/specs/2026-07-06-finish-research-system-design.md: filing section extraction + YoY diff, Form 4 parser + insider-cluster trigger, agent tool wrappers, per-stage local-model config, mechanical memo validation, two-stage brain, and ops research run.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Report the PR URL and WAIT for user review.

- [ ] **Step 4 (OPTIONAL, USER GATE — live smoke).** Only with explicit user confirmation, and only if a pending hit exists (`sqlite3 ~/.local/state/tradingagents/research_screen.sqlite "SELECT symbol FROM screen_hits WHERE status='pending' LIMIT 5"`). Requires `SEC_EDGAR_USER_AGENT` and a running/managed local model. Warn the user first: on ds4 this takes tens of minutes for ONE name and loads ~86 GB (LM Studio must have no model loaded — see `docs/ds4-backend.md`).

```bash
export SEC_EDGAR_USER_AGENT="..."           # from the user
export OPS_LLM_MANAGED_BACKEND=ds4          # or point OPS_RESEARCH_*_MODEL at LM Studio
.venv/bin/python -m ops.cli research run --max-names 1
```

Expected: either a `memo <id> (buy|pass; evidence N kept/M dropped)` line and a row in the memo store, or a `FAILED — <reasons>` line and the hit marked `failed`. BOTH are correct behavior; garbage stored silently is the only failure. Record the outcome in `docs/research_brain.md` under a `## Smoke runs` heading.

---

## Verification checklist (after all tasks)

0. A6 gate resolved: post-Task-0 calibration coverage for `ev_ebit_vs_sector` and `fcf_yield` ≥ 60% recorded in `docs/research_screener.md` (or an explicit user decision to accept the residual).
1. `pytest tests/ -q` green on `feat/phase-b-brain` (expect ~40–45 new tests over the 1332 baseline).
2. `ops research run` with no pending hits prints `no pending hits`, exit 0 (safe to run anywhere).
3. Grep discipline: `grep -rn "invoke_structured_or_freetext" ops/research/` returns nothing (the brain must never free-text-fallback).
4. `ops screen --help` unchanged; `tests/ops/research/test_run.py` still green (trigger wiring is signature-compatible).
5. Spec coverage: filing readers (T1–T3), tool wrappers incl. `get_past_memos` (T5), insider-cluster unlock (T4), two-stage graph + guardrails (T7–T8), per-stage model config (T6), batch entry point (T9). Phase C hooks in place: `Memo.falsifiers` machine-checkable by construction, `MemoStore.open_memos`/`due_for_resolution` already exist.
