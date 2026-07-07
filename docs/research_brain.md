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
