# PIT-universe feasibility spike: findings

**Status:** research spike, complete. No production code changed.
**Task:** Task 6 of `2026-07-28-backtest-corpus-expansion`.
**Scratch scripts:** kept out of the repo, at
`/private/tmp/claude-501/-Users-frednick-Code-TradingAgents/97e9117c-fbe8-40a5-b750-6812ecfd4ea5/scratchpad/pit-spike/`
(not committed; not reachable from this checkout).

## Motivating problem

The backtest's reconstruction corpus screens *today's* small-cap universe at
2025 dates. Companies that have since been delisted are invisible to that
screen, so the corpus never sees them — survivorship bias. US delistings run
roughly 7-10%/yr, higher in small caps, and a cheapness screen preferentially
catches the distressed slice of them, so the missing names are
disproportionately losers. This spike asks: can we build a *true* historical
universe (EDGAR-derived membership, not "whatever is still listed today")
that a backtest could actually price?

## Method

16 real US small/micro-cap delistings between 2025-06-01 and 2026-07 were
assembled from `stockanalysis.com`'s bankruptcy/delisting trackers, SEC
`8-K`/tender-offer filings, and news, cross-checked so every entry is a real,
named, dated event (nothing guessed). For each:

1. **yfinance**: called the exact production path,
   `yf.Ticker(t).history(start=.., end=.., auto_adjust=False, actions=True)`
   (mirrors `ops/backtest/price_fetch.py::_default_history_fn`), over the 12
   months ending at the delisting month, against both the original ticker
   and, since Nasdaq/NYSE-bankruptcy names often continue trading OTC under
   a `Q`-suffixed pink-sheet symbol (e.g. `IRBT` → `IRBTQ`), that variant too.
   Whichever returned more rows is reported.
2. **EDGAR**: resolved a CIK (first via `company_tickers.json` — which turns
   out to list *only currently active* filers — then via EDGAR full-text
   search matching the ticker in a filing's cover-page `display_names`,
   which does retain dead filers), then probed
   `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` (alive?) and
   `data.sec.gov/submissions/CIK##########.json` (filings listable?).

`SEC_EDGAR_USER_AGENT="Frednick Piard pfrednick@gmail.com"` was set for all
EDGAR requests; EDGAR calls stayed well under the 10 req/s courtesy limit
(≈2-3 req/s with sleeps).

## Coverage table

| Ticker | Company | Delisted | Reason | yfinance (12mo pre-delisting) | EDGAR CIK | companyfacts | filings listable |
|---|---|---|---|---|---|---|---|
| MRIN | Marin Software Inc | 2025-06 | bankruptcy | **absent** (`MRIN`, `MRINQ`) | resolved | alive (200) | yes (601) |
| PET | Wag! Group Co | 2025-07 | bankruptcy | **absent** (`PET`, `PETQ`) | resolved | alive (200) | yes (600) |
| OTRK | Ontrak Inc | 2025-08 | bankruptcy | **full**, 251 rows, via `OTRKQ` | resolved | alive (200) | yes (1000) |
| MODV | Modivcare Inc | 2025-08 | bankruptcy | **absent** (`MODV`, `MODVQ`) | resolved | alive (200) | yes (1002) |
| CRGX | CARGO Therapeutics Inc | 2025-08 | take-private M&A | **absent** (`CRGX`, `CRGXQ`) | resolved | alive (200) | yes (192) |
| FLYY | Spirit Aviation Holdings Inc | 2025-09 | bankruptcy | **truncated**, 104 rows (2025-04-30→09-26 only), via `FLYYQ` | resolved | alive (200) | yes (1002) |
| YMAB | Y-mAbs Therapeutics Inc | 2025-09 | take-private M&A | **absent** (`YMAB`, `YMABQ`) | resolved | alive (200) | yes (634) |
| FPAY | FlexShopper Inc | 2025-10 | bankruptcy | **full**, 251 rows, via `FPAYQ` | resolved | alive (200) | yes (724) |
| AMBI | Ambipar Emergency Response | 2025-10 | bankruptcy | **full**, 251 rows, via `AMBIQ` | resolved | alive (200) | yes (118) |
| SOND | Sonder Holdings Inc | 2025-11 | bankruptcy | **full**, 250 rows, via `SONDQ` | resolved | alive (200) | yes (417) |
| CLSD | Clearside Biomedical Inc | 2025-11 | bankruptcy | **absent** (`CLSD`, `CLSDQ`) | resolved | alive (200) | yes (552) |
| IRBT | iRobot Corp | 2025-12 | bankruptcy | **absent** (`IRBT`, `IRBTQ`) | resolved | alive (200) | yes (1003) |
| LAZR | Luminar Technologies Inc | 2025-12 | bankruptcy | **absent** (`LAZR`, `LAZRQ`) | resolved | alive (200) | yes (551) |
| ZYXI | Zynex Inc | 2025-12 | bankruptcy | **absent** (`ZYXI`, `ZYXIQ`) | resolved | alive (200) | yes (768) |
| LGMK | LogicMark Inc | 2025-07 | compliance | **full**, 250 rows, plain `LGMK` (still trades) | resolved (active-filer map) | alive (200) | yes (825) |
| FORA | Forian Inc | 2026-05 | take-private M&A | **absent** (`FORA`, `FORAQ`) | resolved | alive (200) | yes (300) |

No request errored (all misses were genuine empty responses, not
exceptions); this is called out explicitly because "yfinance returned
nothing" and "request errored" are different failure modes and only the
former was observed.

## Results

**EDGAR: 16/16 (100%) usable.** CIK resolved, `companyfacts` alive, filings
listable for every single name, including six-months-dead shells. EDGAR
retaining delisted filers is confirmed, not assumed — but the naive path
(`company_tickers.json`, which is what a quick implementation would reach
for) only covers **currently active** filers and would have silently failed
on all 15 delisted names. The full-text-search-by-ticker fallback is
required and needs its own defensiveness: a plain company-name substring
search against `browse-edgar` collided with an unrelated 1990s shell company
that also happened to be named "Ontrak" (`Ontrak Systems Inc`, CIK
`0000946732` vs. the real `Ontrak Inc`, CIK `0001136174`) — ticker-anchored
matching in the full-text search index was necessary to avoid silently
wiring the wrong company into a backtest.

**yfinance: 6/16 (37.5%) usable at all (full or truncated); 5/16 (31%)
fully usable.** By category:

- **Bankruptcy (12 names): 5/12 (42%) usable** — 4 full (`OTRK`, `FPAY`,
  `AMBI`, `SOND`) + 1 truncated (`FLYY`, missing ~7 of 12 months). All 5
  hits required the `Q`-suffix OTC pink-sheet ticker, not the original
  exchange-listed symbol — **querying the pre-delisting ticker alone
  (`ticker.history(...)` exactly as `price_fetch.py` calls it today) got
  0/12 hits.** The other 7 bankruptcy names (`MRIN`, `PET`, `MODV`, `CLSD`,
  `IRBT`, `LAZR`, `ZYXI`) are genuinely absent from Yahoo under either form —
  Yahoo appears to drop names entirely when equity is cancelled in
  reorganization rather than continuing to trade OTC.
- **Take-private M&A (3 names): 0/3 (0%) usable.** `CRGX`, `YMAB`, `FORA`
  all show zero rows under either ticker form. This matches the mechanism:
  acquired shares simply stop trading and get converted to cash — there's no
  OTC continuation to pick up, so Yahoo has nothing to serve even for dates
  well before the deal closed.
- **Compliance (1 name, `LGMK`): 1/1 (100%) usable**, and notably under the
  *plain* ticker — Nasdaq-delisted-for-compliance names often keep trading
  OTC under the same symbol, so this is the one category where the
  production code's existing ticker-only call would actually work
  unmodified. n=1 here, so treat this as a plausible pattern, not a
  measured rate — a fuller compliance-delisting sample would need a
  systematic search this spike didn't have time to run (see Limitations).

**Combined (prices AND facts usable): 5/16 (31%) fully usable, 6/16
(37.5%) at least partially usable.** EDGAR is never the bottleneck; yfinance
coverage is, and it depends heavily on *why* the company delisted and on
querying the right ticker variant.

## Implied corpus fix

The reconstruction corpus itself would need to be **EDGAR-derived, not
today's screen backdated**: pull historical filer membership from EDGAR
(company facts + filings, which are retained indefinitely) rather than
`company_tickers.json` (active-only) or any live screen. That gets true
point-in-time membership for free — EDGAR coverage was 100% in this sample.

Pricing that membership is the actual gap, and the fix has three tiers:

1. **yfinance where covered** — try the original ticker, then the
   `Q`-suffix variant, for any name whose EDGAR filings show a bankruptcy
   event; this recovers ~42% of bankruptcy names for free with the existing
   `price_fetch.py` machinery plus a symbol-variant retry.
2. **`PriceCache.mark_state(TERMINAL)`** (see `ops/backtest/prices.py`,
   `PriceSeriesStatus.TERMINAL`) for names EDGAR confirms delisted but
   yfinance has nothing for (58% of this sample: all 3 take-privates, most
   Chapter-7/11-liquidation bankruptcies, and any compliance delisting not
   individually verified). `mark_state` already short-circuits pricing
   lookups for `TERMINAL`/`UNPRICEABLE`/`PENDING` series
   (`prices.py:397-402`), so the plumbing to represent "known dead, no
   price data available" already exists — it just isn't being driven by an
   EDGAR-derived corpus today.
3. **A third-party paid vendor** (e.g. Polygon, Tiingo, EOD Historical
   Data) for the un-covered 58%+ is the only way to close the gap with
   real prices rather than a `TERMINAL` stub, and was out of scope to test
   here (no API key available in this environment).

Marking the uncovered names `TERMINAL` rather than silently omitting them is
the actual survivorship-bias fix even without new price data: it makes the
corpus's *membership* honest (dead companies are visible entries with a
known-unpriceable status) even where daily bars aren't available, which is
a meaningfully different — and more honest — failure mode than the current
one (dead companies never appear at all).

## Effort estimate

- **EDGAR-derived membership puller** (walk EDGAR submissions/full-text
  search for filer status changes, SIC/market-cap filters, ticker
  resolution with the collision-guard shown above): **~3-5 days.** The
  `company_tickers.json`-only trap and the ticker-collision risk in
  name-based lookup are the two things that will bite a first pass; both
  are now documented above with concrete repro (Ontrak).
- **yfinance symbol-variant retry (`ticker`, then `ticker` + `Q`) wired into
  `price_fetch.py`**: **~0.5-1 day.** Small, mechanical addition to the
  existing fetcher.
- **`TERMINAL`-marking pipeline driven by EDGAR delisting evidence**
  (detect "company stopped filing" / "8-K reports merger closed" /
  "bankruptcy 8-K" and call `mark_state`): **~2-3 days**, mostly in
  classifying *why* a filer went dark from its filing history, since that
  determines whether a `Q`-suffix retry is even worth attempting.
- **Total for a usable-but-partial v1** (EDGAR membership + yfinance
  variant retry + TERMINAL fallback, no paid vendor): **~1.5-2 weeks.**
- Getting to genuinely high price coverage (not just honest membership)
  requires a paid vendor integration, unscoped here — budget a separate
  spike once a vendor is chosen.

## Recommendation: **build later, not now, not never**

- **Not never**: EDGAR-derived membership is real, retained, and cheap to
  pull — the survivorship bias this fixes is structural and will keep
  compounding as more of today's screened universe survivor-bias-selects
  against itself over time. The `TERMINAL`-marking plumbing already exists
  in `ops/backtest/prices.py`; this is a matter of driving it from the
  right data source, not building new infrastructure.
- **Not now**: at 31-37.5% yfinance coverage even with the symbol-variant
  retry, a v1 `HistoricalCaseSource` would produce a corpus where roughly
  two-thirds of the true historical population is present-but-unpriced
  (`TERMINAL`) rather than priced. That's still strictly better than today
  (0% of delisted names visible at all) but it's a lot of engineering
  effort for a corpus that still can't run most of its distressed-name
  cases end-to-end. Given the accelerated-learning-loop priority (see
  `project-backtest-learning-loop` memory: ds4 cutoff May 2025,
  sleeve-triage CLI already the active workstream), this doesn't clear the
  bar to preempt current work.
- **Build later** once either (a) a paid price vendor is budgeted — this
  spike's yfinance ceiling (~42% even for the best-covered category,
  bankruptcy) is a real ceiling, not a bug to fix in our code — or (b) the
  learning loop's current sleeve-triage work surfaces a concrete need for
  point-in-time cases badly enough to justify shipping a
  membership-only/mostly-`TERMINAL` v1 anyway. Re-run this spike's
  yfinance probe against a larger sample (n=50+) before committing engineering
  time, since n=16 (and n=1 for the compliance category) is a screening
  sample, not a sizing estimate.

## Limitations of this spike

- n=16 is enough to see the *shape* of the problem (EDGAR good, yfinance
  bankruptcy-only-and-partial, M&A near-zero) but too small to treat the
  42%/0%/100% category rates as precise; especially the compliance category
  (n=1) should be re-sampled before being used to size engineering work.
- Only `stockanalysis.com`'s public trackers plus targeted web searches were
  used to source delistings; no attempt was made to systematically enumerate
  compliance-only delistings (no bankruptcy involved) via Nasdaq's
  non-compliant-company list or 25-NSE filings, so that category is
  under-sampled by construction, not because compliance delistings are rare.
- yfinance's `Q`-suffix behavior is an empirical pattern observed here, not
  a documented Yahoo convention — it should not be hard-coded as a
  guaranteed rule without a larger sample.
- No paid price vendor was tested; the true "usable price" ceiling with a
  vendor in the mix is unknown and could change the recommendation.
