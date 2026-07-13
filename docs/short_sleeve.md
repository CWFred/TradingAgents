# Short Sleeve — Runbook

The fourth paper sleeve: shorts EDGAR-red-flag small/mid-caps. Own journal
(`short_journal.sqlite`), own memo store (`short_memos.sqlite`), own screen
queue (`short_screen.sqlite`), $10k paper bucket
(`OPS_SHORT_STARTING_CASH`). Design:
`docs/superpowers/specs/2026-07-13-short-and-insider-sleeves-design.md`.

Glossary: a *short* sells borrowed shares now and buys them back ("covers")
later — profit if the price fell. *Covering* is the buy-to-close.

## What runs when

| job | where | when | what |
|---|---|---|---|
| short overnight pass | inside `_research_overnight_tick` (00:00–deadline window, half-hourly trigger) | AFTER the research stages each night | short screen if due (every `research_screen_interval_days`), then alternate graph-vetting (inverted map) and brain drain over the short stores; shares the tick's single ds4 bracket |
| short_trade | ops daemon (APScheduler) | 16:27 ET mon-fri | exits then entries on the short ledger; pushes a `short_trade_run` summary |
| research_monitor | (existing job, 16:20) | mon-fri | short memos ride the same monitor: falsifiers + direction-aware drawdown escalation |

The overnight ordering is deliberate: research (the proven sleeve) drains
and vets first; the short sleeve gets whatever window remains. Both share
one managed-backend bracket — the sole guard against two models holding ds4.

## The funnel

1. **Screen** (`ops/research/short_screen.py`): expensive/deteriorating on
   ≥ 2 bars (EV/EBIT > 20× or EBIT ≤ 0 with cap > $500M; net debt/EBITDA
   > 4×; gross margin −3pp YoY) AND ≥ 1 red flag
   (`ops/research/short_triggers.py`): 8-K items 4.02/1.03/3.01, CFO
   departure, insider sell cluster (≥3 non-10b5-1 sellers), going-concern
   full-text hit.
2. **Brain** (`ops/research/short_brain.py`): the bear authors, a bull
   defense stage attacks, memo lands `pending_vetting` with
   `thesis_type="short"`. Price targets invert: `price_target_low` is the
   cover target (profit).
3. **Vetting** (`vet_pending(confirm_tiers=SHORT_CONFIRM_TIERS)`): the
   graph rates the stock normally; **Sell → confirm/high, Underweight →
   confirm/medium**, anything else rejects.
4. **Trading** (`ops/research/short_trading.py`): mechanical.

## Fences (`ops/research/short_sizing.py`)

| fence | value | note |
|---|---|---|
| tiers | 1% / 2% / 3% | half the research sleeve — loss is unbounded |
| name cap | 5% of equity | LIVE exposure (qty × price), not cost |
| sector cap | 15% | live |
| gross short book | ≤ 50% of equity | the margin discipline; shorting adds cash, so there is no cash clamp |
| ADV cap | 2% of 20-day dollar ADV | must be coverable in a squeeze |
| price floor | ≥ $5 | universe filter |
| deny-list | `config.deny_list` | checked at entry |

## Exit reasons (first match wins)

1. memo missing → cover
2. memo resolved → cover
3. falsifier tripped (monitor journals on MAIN journal) → cover
4. **hard stop**: quote ≥ entry × 1.25 → cover
5. **target hit**: quote ≤ `price_target_low` → cover
6. **time stop**: held > min(expected_holding_months, 9) × 30 days → cover

Forced covers always succeed — cash may go negative on a blown-up short;
the damage shows in equity, never as a refused exit.

## Drawdown convention (direction-aware)

`drawdown_from_cost_pct` keeps the invariant **positive = adverse move vs
cost in both directions**: for a short, a 25% squeeze above entry reads
+25 and a 20% profit reads −20. Memo falsifiers use `> N` percent-form
(PR #31 convention); the −30% monitor escalation applies unchanged.

## Paper-fidelity caveats

No borrow cost, no locate, no hard-to-borrow list, no squeeze dynamics —
results flatter the sleeve. The goal is exercising the machinery and
growing the corpus, not proving live shortability. Live shorting is out of
scope without a separate design.

## Manual commands

```bash
# journal peek
sqlite3 ~/.local/state/tradingagents/short_journal.sqlite \
  "SELECT at, kind FROM events ORDER BY id DESC LIMIT 10;"
# memo queue
sqlite3 ~/.local/state/tradingagents/short_memos.sqlite \
  "SELECT ticker, status, conviction_tier FROM memos ORDER BY created_at DESC LIMIT 10;"
```
