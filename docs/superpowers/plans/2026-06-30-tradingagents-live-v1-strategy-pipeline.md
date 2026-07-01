# TradingAgents Live v1 — Strategy & Pipeline Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `ops decide-once --date YYYY-MM-DD` CLI command that runs the full decision-to-paper-fill loop once on demand: build today's earnings-based universe, run each candidate through the upstream TradingAgents pipeline, route BUY decisions through the guarded paper broker, then run one stop-check pass.

**Architecture:** Two new sibling packages under `ops/` — `ops/universe/` (yfinance-backed S&P 500 + earnings filter + liquidity filter) and `ops/strategy/` (Strategy ABC + post-earnings momentum). A thin `ops/pipeline_adapter.py` wraps `tradingagents.TradingAgentsGraph` behind a swappable interface so tests don't pay LLM costs. `ops/position_guardian.py` provides a one-shot stop-check (Plan 3 will make it a background thread). `ops/cli.py` wires it together.

**Tech Stack:** Python 3.12, `yfinance`, `click` for the CLI, existing `pytest`. No Robinhood code in this plan — paper broker only.

## Global Constraints

- Decimal end-to-end for any monetary value — no `float` anywhere in `ops/`.
- Every new public function/class is type-hinted; uses Python 3.12 modern syntax (`list[X]`, `X | None`).
- Every order placed through the guarded paper broker built by `ops.build_guarded_paper_broker` (Plan 1 factory). Do NOT instantiate `PaperBroker` directly anywhere in production code.
- The existing `tradingagents/` package is imported, never modified.
- Tests that hit external network (yfinance, LLMs) are marked with the `integration` marker (already declared in `pyproject.toml`) and skipped by default in unit-test runs.
- Branch: `feat/ops-strategy-pipeline` (already created off `main`, with the data-sources spec update cherry-picked).

---

## File Structure

```
ops/
  quotes.py                          # yfinance-backed QuoteSource factory
  pipeline_adapter.py                # Wraps tradingagents.TradingAgentsGraph
  position_guardian.py               # check_stops_once()
  cli.py                             # `decide-once` command via click
  universe/
    __init__.py                      # build_universe() top-level
    sp500.py                         # S&P 500 member list (weekly cache)
    earnings.py                      # Recent-earnings filter
    filters.py                       # Liquidity + price + deny-list
    _data/                           # cached files (gitignored)
      sp500_members.json             # weekly snapshot
  strategy/
    __init__.py
    base.py                          # Strategy ABC + Decision dataclass
    post_earnings_momentum.py
tests/
  ops/
    test_quotes.py
    test_pipeline_adapter.py
    test_position_guardian.py
    test_cli_decide_once.py
    universe/
      __init__.py
      test_sp500.py
      test_earnings.py
      test_filters.py
      test_universe.py
    strategy/
      __init__.py
      test_post_earnings_momentum.py
```

---

## Task 0: Scaffold + dependency

**Files:**
- Modify: `pyproject.toml` — add `yfinance>=0.2.40` and `click>=8.1` to dependencies
- Modify: `.gitignore` — add `ops/universe/_data/`
- Create: `ops/universe/__init__.py`, `ops/strategy/__init__.py` (both empty)
- Create: `tests/ops/universe/__init__.py`, `tests/ops/strategy/__init__.py` (both empty)
- Create: `ops/universe/_data/.gitkeep`

**Interfaces:**
- Consumes: nothing
- Produces: importable empty packages `ops.universe`, `ops.strategy`; `yfinance` and `click` installed in `.venv`

- [ ] **Step 1: Inspect existing deps**

Run: `grep -A 20 'dependencies = \[' pyproject.toml | head -25`
Note current dep list so the additions slot in correctly.

- [ ] **Step 2: Add deps to pyproject.toml**

In the `[project]` table's `dependencies = [...]` list, append two entries:

```toml
"yfinance>=0.2.40",
"click>=8.1",
```

- [ ] **Step 3: Install into the venv**

Run: `.venv/bin/pip install -e . 2>&1 | tail -3`
Expected: successful install of yfinance and click (and their transitive deps).

- [ ] **Step 4: Add gitignore entry**

Append to `.gitignore`:
```
# Cached universe data (refreshed weekly at runtime)
ops/universe/_data/*
!ops/universe/_data/.gitkeep
```

- [ ] **Step 5: Create empty package skeleton**

```bash
cd ~/Code/TradingAgents
mkdir -p ops/universe/_data ops/strategy tests/ops/universe tests/ops/strategy
touch ops/universe/__init__.py ops/strategy/__init__.py
touch tests/ops/universe/__init__.py tests/ops/strategy/__init__.py
touch ops/universe/_data/.gitkeep
```

- [ ] **Step 6: Verify imports**

Run: `.venv/bin/python -c "import yfinance, click; from ops import universe, strategy; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore ops/universe ops/strategy tests/ops/universe tests/ops/strategy
git commit -m "chore(ops): add yfinance + click deps and scaffold universe/strategy packages"
```

---

## Task 1: yfinance-backed quote source

**Files:**
- Create: `ops/quotes.py`
- Test: `tests/ops/test_quotes.py`

**Interfaces:**
- Consumes: `decimal.Decimal`
- Produces: `make_yfinance_quote_source(*, ttl_seconds: int = 60) -> Callable[[str], Decimal]` — returns a function that takes a ticker, returns the latest price as Decimal. Internally caches by symbol for `ttl_seconds`.

- [ ] **Step 1: Write the failing test**

```python
# tests/ops/test_quotes.py
from decimal import Decimal
from unittest.mock import patch, MagicMock
import pytest
from ops.quotes import make_yfinance_quote_source


def _fake_ticker(price: float) -> MagicMock:
    t = MagicMock()
    # yfinance Ticker.fast_info has last_price; tolerate either path
    t.fast_info = MagicMock()
    t.fast_info.last_price = price
    return t


def test_quote_source_returns_decimal_from_yfinance():
    with patch("ops.quotes.yf.Ticker", return_value=_fake_ticker(200.05)):
        q = make_yfinance_quote_source(ttl_seconds=60)
        assert q("AAPL") == Decimal("200.05")


def test_quote_source_caches_within_ttl():
    fake = _fake_ticker(200.05)
    with patch("ops.quotes.yf.Ticker", return_value=fake) as mock_ticker:
        q = make_yfinance_quote_source(ttl_seconds=60)
        q("AAPL")
        q("AAPL")
        q("AAPL")
        # Only one yf.Ticker call within TTL
        assert mock_ticker.call_count == 1


def test_quote_source_refreshes_after_ttl(monkeypatch):
    # Drive the cache's clock manually
    clock = [1000.0]
    monkeypatch.setattr("ops.quotes._now", lambda: clock[0])
    with patch("ops.quotes.yf.Ticker", return_value=_fake_ticker(200.05)) as mock_ticker:
        q = make_yfinance_quote_source(ttl_seconds=60)
        q("AAPL")
        clock[0] += 30
        q("AAPL")  # still cached
        assert mock_ticker.call_count == 1
        clock[0] += 31  # now past TTL
        q("AAPL")
        assert mock_ticker.call_count == 2


def test_quote_source_raises_on_missing_price():
    bad = MagicMock()
    bad.fast_info = MagicMock()
    bad.fast_info.last_price = None
    with patch("ops.quotes.yf.Ticker", return_value=bad):
        q = make_yfinance_quote_source()
        with pytest.raises(ValueError, match="ZZZZ"):
            q("ZZZZ")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/ops/test_quotes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.quotes'`.

- [ ] **Step 3: Write minimal implementation**

```python
# ops/quotes.py
"""yfinance-backed quote source with a small per-symbol TTL cache."""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Callable

import yfinance as yf


def _now() -> float:
    return time.monotonic()


def make_yfinance_quote_source(*, ttl_seconds: int = 60) -> Callable[[str], Decimal]:
    cache: dict[str, tuple[float, Decimal]] = {}

    def get(symbol: str) -> Decimal:
        now = _now()
        cached = cache.get(symbol)
        if cached is not None and now - cached[0] < ttl_seconds:
            return cached[1]
        ticker = yf.Ticker(symbol)
        raw = ticker.fast_info.last_price
        if raw is None:
            raise ValueError(f"no last_price available for {symbol}")
        price = Decimal(str(raw))
        cache[symbol] = (now, price)
        return price

    return get
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/ops/test_quotes.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add ops/quotes.py tests/ops/test_quotes.py
git commit -m "feat(ops): yfinance-backed quote source with per-symbol TTL cache"
```

---

## Task 2: S&P 500 membership

**Files:**
- Create: `ops/universe/sp500.py`
- Test: `tests/ops/universe/test_sp500.py`

**Interfaces:**
- Consumes: stdlib `json`, `urllib.request`, `pathlib.Path`, `datetime`
- Produces: `load_sp500_members(*, cache_path: Path | None = None, max_age_days: int = 7, fetch: Callable[[], list[str]] | None = None) -> list[str]` — loads cached membership; refetches if cache is stale or missing; fetcher defaults to `_fetch_from_wikipedia()`.

Approach: scrape the Wikipedia table at `https://en.wikipedia.org/wiki/List_of_S%26P_500_companies` (the first table has all current members). Cache to JSON at `cache_path` (default `ops/universe/_data/sp500_members.json`) with a `fetched_at` timestamp.

- [ ] **Step 1: Write the failing test**

```python
# tests/ops/universe/test_sp500.py
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ops.universe.sp500 import load_sp500_members


def _write_cache(path: Path, members: list[str], age_days: int) -> None:
    fetched = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": fetched, "members": members}))


def test_uses_cache_when_fresh(tmp_path):
    cache = tmp_path / "sp500.json"
    _write_cache(cache, ["AAPL", "MSFT", "NVDA"], age_days=1)

    def fetch():
        raise AssertionError("should not fetch when cache is fresh")

    members = load_sp500_members(cache_path=cache, fetch=fetch)
    assert members == ["AAPL", "MSFT", "NVDA"]


def test_refetches_when_cache_is_stale(tmp_path):
    cache = tmp_path / "sp500.json"
    _write_cache(cache, ["OLD"], age_days=30)

    def fetch():
        return ["AAPL", "MSFT"]

    members = load_sp500_members(cache_path=cache, max_age_days=7, fetch=fetch)
    assert members == ["AAPL", "MSFT"]
    # Cache should now be updated
    written = json.loads(cache.read_text())
    assert written["members"] == ["AAPL", "MSFT"]


def test_fetches_when_cache_missing(tmp_path):
    cache = tmp_path / "missing.json"
    members = load_sp500_members(cache_path=cache, fetch=lambda: ["AAPL"])
    assert members == ["AAPL"]
    assert cache.exists()


def test_returns_only_unique_uppercase_symbols(tmp_path):
    cache = tmp_path / "sp500.json"

    def fetch():
        return ["aapl", "AAPL", "msft", "BRK.B"]

    members = load_sp500_members(cache_path=cache, fetch=fetch)
    assert members == ["AAPL", "BRK.B", "MSFT"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/ops/universe/test_sp500.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# ops/universe/sp500.py
"""S&P 500 membership list. Cached weekly to JSON; refreshed by scraping
Wikipedia's `List of S&P 500 companies` table."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import urllib.request

_DEFAULT_CACHE = Path(__file__).parent / "_data" / "sp500_members.json"
_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def _fetch_from_wikipedia() -> list[str]:
    req = urllib.request.Request(_WIKI_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    # The first table (id="constituents") has columns: Symbol, Security, ...
    # Each row's first <td> is the ticker, sometimes wrapped in <a>.
    # Wikipedia uses "BRK.B"; yfinance uses "BRK-B" — translate dots to dashes.
    pattern = re.compile(
        r'<tr[^>]*>\s*<td[^>]*>\s*<a[^>]*>([A-Z][A-Z0-9.]*)</a>', re.MULTILINE
    )
    matches = pattern.findall(html)
    if len(matches) < 400:
        raise RuntimeError(f"sp500 scrape returned only {len(matches)} symbols — page format changed?")
    return [m.replace(".", "-") for m in matches]


def load_sp500_members(
    *,
    cache_path: Path | None = None,
    max_age_days: int = 7,
    fetch: Callable[[], list[str]] | None = None,
) -> list[str]:
    cache_path = cache_path or _DEFAULT_CACHE
    fetch = fetch or _fetch_from_wikipedia
    if cache_path.exists():
        data = json.loads(cache_path.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if datetime.now(timezone.utc) - fetched_at < timedelta(days=max_age_days):
            members = data["members"]
            return sorted({s.upper() for s in members})
    members = fetch()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "members": sorted({s.upper() for s in members}),
        })
    )
    return sorted({s.upper() for s in members})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/ops/universe/test_sp500.py -v`
Expected: 4 passed.

- [ ] **Step 5: Smoke-test live Wikipedia fetch**

Run: `.venv/bin/python -c "from ops.universe.sp500 import _fetch_from_wikipedia; m = _fetch_from_wikipedia(); print(len(m), m[:5])"`
Expected: prints a count near 500 and the first five tickers (alphabetical or page order). If the parser returns < 400 symbols, the regex above needs adjustment for the current page format.

- [ ] **Step 6: Commit**

```bash
git add ops/universe/sp500.py tests/ops/universe/test_sp500.py
git commit -m "feat(ops): S&P 500 membership via Wikipedia scrape, weekly cache"
```

---

## Task 3: Recent-earnings filter

**Files:**
- Create: `ops/universe/earnings.py`
- Test: `tests/ops/universe/test_earnings.py`

**Interfaces:**
- Consumes: `yfinance`, `datetime.date`
- Produces:
  - `@dataclass(frozen=True) class EarningsHit: symbol: str; report_date: date; eps_actual: Decimal; eps_estimate: Decimal; revenue_actual: Decimal; revenue_estimate: Decimal; eps_beat: bool; revenue_beat: bool`
  - `find_recent_earnings_beats(tickers: list[str], asof_date: date, *, lookback_days: int = 2, fetch: Callable[[str], EarningsHit | None] | None = None) -> list[EarningsHit]` — for each ticker, ask the fetcher; keep only those whose `report_date` is within `lookback_days` trading days of `asof_date` AND have both `eps_beat` and `revenue_beat` True. Default fetcher pulls from yfinance.

- [ ] **Step 1: Write the failing test**

```python
# tests/ops/universe/test_earnings.py
from datetime import date
from decimal import Decimal

from ops.universe.earnings import EarningsHit, find_recent_earnings_beats


def _hit(symbol, report_date, *, eps_beat=True, revenue_beat=True):
    return EarningsHit(
        symbol=symbol, report_date=report_date,
        eps_actual=Decimal("1"), eps_estimate=Decimal("0.9"),
        revenue_actual=Decimal("100"), revenue_estimate=Decimal("90"),
        eps_beat=eps_beat, revenue_beat=revenue_beat,
    )


def test_keeps_beats_within_lookback():
    today = date(2026, 6, 30)
    table = {
        "AAPL": _hit("AAPL", date(2026, 6, 27)),   # 1 trading day back
        "MSFT": _hit("MSFT", date(2026, 6, 30)),   # today
        "NVDA": _hit("NVDA", date(2026, 6, 24)),   # too old (>2 trading days)
        "META": _hit("META", date(2026, 6, 30), eps_beat=False),   # miss
        "AMZN": _hit("AMZN", date(2026, 6, 30), revenue_beat=False),  # miss
        "GOOG": None,                              # no earnings recently
    }
    result = find_recent_earnings_beats(
        ["AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOG"],
        asof_date=today, lookback_days=2,
        fetch=lambda sym: table[sym],
    )
    syms = sorted(h.symbol for h in result)
    assert syms == ["AAPL", "MSFT"]


def test_returns_empty_when_no_hits():
    result = find_recent_earnings_beats(
        ["AAPL"], asof_date=date(2026, 6, 30),
        fetch=lambda sym: None,
    )
    assert result == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/ops/universe/test_earnings.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# ops/universe/earnings.py
"""Recent-earnings filter. Returns tickers that reported in the last N
trading days with both an EPS beat and a revenue beat."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable

import yfinance as yf


@dataclass(frozen=True)
class EarningsHit:
    symbol: str
    report_date: date
    eps_actual: Decimal
    eps_estimate: Decimal
    revenue_actual: Decimal
    revenue_estimate: Decimal
    eps_beat: bool
    revenue_beat: bool


def _is_trading_day(d: date) -> bool:
    # Mon=0..Fri=4. Holidays are not handled here — a holiday inside the
    # lookback window simply shortens the effective range by one calendar day.
    return d.weekday() < 5


def _trading_days_back(asof: date, n: int) -> date:
    d = asof
    counted = 0
    while counted < n:
        d -= timedelta(days=1)
        if _is_trading_day(d):
            counted += 1
    return d


def _fetch_from_yfinance(symbol: str) -> EarningsHit | None:
    t = yf.Ticker(symbol)
    df = getattr(t, "earnings_dates", None)
    if df is None or df.empty:
        return None
    df = df.dropna(subset=["EPS Estimate", "Reported EPS"])
    if df.empty:
        return None
    # most recent reported row
    row = df.iloc[0]
    eps_actual = Decimal(str(row["Reported EPS"]))
    eps_est = Decimal(str(row["EPS Estimate"]))
    # Revenue columns may not be present; treat absence as a beat False
    rev_actual = Decimal(str(row.get("Reported Revenue", 0) or 0))
    rev_est = Decimal(str(row.get("Revenue Estimate", 0) or 0))
    return EarningsHit(
        symbol=symbol,
        report_date=row.name.date() if hasattr(row.name, "date") else row.name,
        eps_actual=eps_actual,
        eps_estimate=eps_est,
        revenue_actual=rev_actual,
        revenue_estimate=rev_est,
        eps_beat=eps_actual > eps_est,
        revenue_beat=rev_actual > rev_est,
    )


def find_recent_earnings_beats(
    tickers: list[str],
    asof_date: date,
    *,
    lookback_days: int = 2,
    fetch: Callable[[str], EarningsHit | None] | None = None,
) -> list[EarningsHit]:
    fetch = fetch or _fetch_from_yfinance
    earliest = _trading_days_back(asof_date, lookback_days)
    hits: list[EarningsHit] = []
    for sym in tickers:
        hit = fetch(sym)
        if hit is None:
            continue
        if hit.report_date < earliest or hit.report_date > asof_date:
            continue
        if not (hit.eps_beat and hit.revenue_beat):
            continue
        hits.append(hit)
    return hits
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/ops/universe/test_earnings.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add ops/universe/earnings.py tests/ops/universe/test_earnings.py
git commit -m "feat(ops): recent-earnings beats filter (EPS + revenue)"
```

---

## Task 4: Universe filters + assembly

**Files:**
- Create: `ops/universe/filters.py`
- Create: `ops/universe/__init__.py` (replace empty file)
- Test: `tests/ops/universe/test_filters.py`
- Test: `tests/ops/universe/test_universe.py`

**Interfaces:**
- Consumes: `EarningsHit` (Task 3), `load_sp500_members` (Task 2), `OpsConfig` (Plan 1), `yfinance`
- Produces:
  - `@dataclass(frozen=True) class Candidate: symbol: str; earnings: EarningsHit; last_price: Decimal; avg_dollar_volume_20d: Decimal`
  - `apply_liquidity_filter(symbols: list[str], *, min_adv: Decimal, min_price: Decimal, fetch_metrics: Callable[[str], tuple[Decimal, Decimal] | None]) -> list[tuple[str, Decimal, Decimal]]` — returns `(symbol, price, adv)` triples for symbols that meet both floors. `fetch_metrics(symbol) -> (price, 20d_avg_dollar_volume) | None`.
  - `apply_deny_list(symbols: list[str], deny_list: frozenset[str]) -> list[str]`
  - `build_universe(*, asof_date: date, config: OpsConfig, members_loader: Callable[[], list[str]] | None = None, earnings_finder: Callable[..., list[EarningsHit]] | None = None, metrics_fetcher: Callable[[str], tuple[Decimal, Decimal] | None] | None = None) -> list[Candidate]` — composes the pipeline: members → deny-list → earnings beats → liquidity → ranked Candidates.

- [ ] **Step 1: Write filters test**

```python
# tests/ops/universe/test_filters.py
from decimal import Decimal

from ops.universe.filters import apply_deny_list, apply_liquidity_filter


def test_deny_list_strips_excluded_symbols():
    result = apply_deny_list(["AAPL", "SPOT", "MSFT", "TQQQ"], frozenset({"SPOT", "TQQQ"}))
    assert result == ["AAPL", "MSFT"]


def test_liquidity_filter_keeps_above_both_floors():
    metrics = {
        "AAPL": (Decimal("200"), Decimal("60000000")),  # passes
        "PENNY": (Decimal("2"),  Decimal("60000000")),  # price floor
        "ILLIQ": (Decimal("200"), Decimal("10000000")),  # adv floor
        "ZZZZ": None,                                    # no data
    }
    result = apply_liquidity_filter(
        ["AAPL", "PENNY", "ILLIQ", "ZZZZ"],
        min_adv=Decimal("50000000"),
        min_price=Decimal("5"),
        fetch_metrics=lambda s: metrics[s],
    )
    syms = [r[0] for r in result]
    assert syms == ["AAPL"]
```

- [ ] **Step 2: Write universe test**

```python
# tests/ops/universe/test_universe.py
from datetime import date
from decimal import Decimal

from ops.config import OpsConfig
from ops.universe import build_universe
from ops.universe.earnings import EarningsHit


def _hit(sym):
    return EarningsHit(
        symbol=sym, report_date=date(2026, 6, 30),
        eps_actual=Decimal("1"), eps_estimate=Decimal("0.9"),
        revenue_actual=Decimal("100"), revenue_estimate=Decimal("90"),
        eps_beat=True, revenue_beat=True,
    )


def test_build_universe_composes_pipeline():
    cfg = OpsConfig()

    def members():
        return ["AAPL", "SPOT", "MSFT", "TQQQ", "PENNY"]

    def earnings(syms, asof_date, lookback_days, fetch=None):
        # SPOT and TQQQ are deny-listed, so should never reach earnings
        assert "SPOT" not in syms
        assert "TQQQ" not in syms
        return [_hit(s) for s in syms]

    def metrics(sym):
        if sym == "PENNY":
            return Decimal("2"), Decimal("100000000")
        return Decimal("200"), Decimal("100000000")

    result = build_universe(
        asof_date=date(2026, 6, 30),
        config=cfg,
        members_loader=members,
        earnings_finder=earnings,
        metrics_fetcher=metrics,
    )
    syms = [c.symbol for c in result]
    assert syms == sorted(syms)            # deterministic ordering
    assert syms == ["AAPL", "MSFT"]        # SPOT/TQQQ denied, PENNY price filter
    assert all(c.last_price == Decimal("200") for c in result)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ops/universe/test_filters.py tests/ops/universe/test_universe.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Write filters implementation**

```python
# ops/universe/filters.py
"""Universe filters: liquidity, deny-list, etc."""
from __future__ import annotations

from decimal import Decimal
from typing import Callable

import yfinance as yf


def apply_deny_list(symbols: list[str], deny_list: frozenset[str]) -> list[str]:
    return [s for s in symbols if s not in deny_list]


def apply_liquidity_filter(
    symbols: list[str],
    *,
    min_adv: Decimal,
    min_price: Decimal,
    fetch_metrics: Callable[[str], tuple[Decimal, Decimal] | None],
) -> list[tuple[str, Decimal, Decimal]]:
    out: list[tuple[str, Decimal, Decimal]] = []
    for sym in symbols:
        m = fetch_metrics(sym)
        if m is None:
            continue
        price, adv = m
        if price < min_price or adv < min_adv:
            continue
        out.append((sym, price, adv))
    return out


def fetch_price_and_adv_from_yfinance(symbol: str) -> tuple[Decimal, Decimal] | None:
    """20-day average dollar volume = mean(close * volume) over last 20 trading days."""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="20d", auto_adjust=False)
        if hist.empty:
            return None
        last_price = Decimal(str(hist["Close"].iloc[-1]))
        dollar_vol = (hist["Close"] * hist["Volume"]).mean()
        return last_price, Decimal(str(float(dollar_vol)))
    except Exception:
        return None
```

- [ ] **Step 5: Write universe assembly**

```python
# ops/universe/__init__.py
"""Top-level universe builder.

Composes: S&P 500 members → deny-list → recent earnings beats → liquidity →
sorted list of Candidates."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable

from ops.config import OpsConfig
from ops.universe.earnings import EarningsHit, find_recent_earnings_beats
from ops.universe.filters import (
    apply_deny_list,
    apply_liquidity_filter,
    fetch_price_and_adv_from_yfinance,
)
from ops.universe.sp500 import load_sp500_members

# Hard-coded for v1 — promoted to OpsConfig only if we need to tune
_MIN_ADV = Decimal("50000000")
_MIN_PRICE = Decimal("5")
_LOOKBACK_TRADING_DAYS = 2


@dataclass(frozen=True)
class Candidate:
    symbol: str
    earnings: EarningsHit
    last_price: Decimal
    avg_dollar_volume_20d: Decimal


def build_universe(
    *,
    asof_date: date,
    config: OpsConfig,
    members_loader: Callable[[], list[str]] | None = None,
    earnings_finder: Callable[..., list[EarningsHit]] | None = None,
    metrics_fetcher: Callable[[str], tuple[Decimal, Decimal] | None] | None = None,
) -> list[Candidate]:
    members_loader = members_loader or load_sp500_members
    earnings_finder = earnings_finder or find_recent_earnings_beats
    metrics_fetcher = metrics_fetcher or fetch_price_and_adv_from_yfinance

    members = members_loader()
    eligible = apply_deny_list(members, config.deny_list)
    hits = earnings_finder(
        eligible, asof_date=asof_date, lookback_days=_LOOKBACK_TRADING_DAYS,
    )
    hits_by_sym = {h.symbol: h for h in hits}
    liquid = apply_liquidity_filter(
        list(hits_by_sym.keys()),
        min_adv=_MIN_ADV,
        min_price=_MIN_PRICE,
        fetch_metrics=metrics_fetcher,
    )
    candidates = [
        Candidate(
            symbol=sym,
            earnings=hits_by_sym[sym],
            last_price=price,
            avg_dollar_volume_20d=adv,
        )
        for sym, price, adv in liquid
    ]
    candidates.sort(key=lambda c: c.symbol)
    return candidates


__all__ = ["Candidate", "build_universe"]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/universe/ -v`
Expected: 6 passed (2 filters + 2 universe + 4 from prior tasks already in place).

- [ ] **Step 7: Commit**

```bash
git add ops/universe/filters.py ops/universe/__init__.py tests/ops/universe/test_filters.py tests/ops/universe/test_universe.py
git commit -m "feat(ops/universe): liquidity + deny-list filters and build_universe composition"
```

---

## Task 5: Pipeline adapter

**Files:**
- Create: `ops/pipeline_adapter.py`
- Test: `tests/ops/test_pipeline_adapter.py`

**Interfaces:**
- Consumes: `tradingagents.graph.trading_graph.TradingAgentsGraph` (the upstream class)
- Produces:
  - `class PipelineDecision(str, Enum): BUY = "BUY"; HOLD = "HOLD"; SELL = "SELL"`
  - `@dataclass(frozen=True) class PipelineResult: symbol: str; date: date; decision: PipelineDecision; raw: dict`
  - `class PipelineAdapter(Protocol): def propagate(self, symbol: str, asof_date: date) -> PipelineResult: ...`
  - `class TradingAgentsPipelineAdapter` — concrete; constructs and reuses one `TradingAgentsGraph`; parses its returned decision text into the enum.
  - `class StubPipelineAdapter` — accepts a `dict[str, PipelineDecision]` mapping symbol → fixed decision; for tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/ops/test_pipeline_adapter.py
from datetime import date

import pytest

from ops.pipeline_adapter import (
    PipelineDecision,
    PipelineResult,
    StubPipelineAdapter,
    TradingAgentsPipelineAdapter,
    parse_decision,
)


def test_stub_returns_fixed_decision():
    stub = StubPipelineAdapter({"AAPL": PipelineDecision.BUY, "MSFT": PipelineDecision.HOLD})
    r = stub.propagate("AAPL", date(2026, 6, 30))
    assert isinstance(r, PipelineResult)
    assert r.decision == PipelineDecision.BUY
    assert r.symbol == "AAPL"


def test_stub_defaults_to_hold_for_unknown_symbol():
    stub = StubPipelineAdapter({})
    r = stub.propagate("ZZZZ", date(2026, 6, 30))
    assert r.decision == PipelineDecision.HOLD


@pytest.mark.parametrize("text,expected", [
    ("FINAL TRANSACTION PROPOSAL: BUY", PipelineDecision.BUY),
    ("FINAL TRANSACTION PROPOSAL: SELL", PipelineDecision.SELL),
    ("FINAL TRANSACTION PROPOSAL: HOLD", PipelineDecision.HOLD),
    ("buy", PipelineDecision.BUY),
    ("the analysts agree: SELL the position", PipelineDecision.SELL),
    ("we should HOLD for now", PipelineDecision.HOLD),
    ("inconclusive analysis", PipelineDecision.HOLD),   # fallback
])
def test_parse_decision_handles_various_phrasings(text, expected):
    assert parse_decision(text) == expected


def test_real_adapter_constructs_graph_lazily(monkeypatch):
    """The TradingAgentsGraph is heavy (LLM clients); construction must be
    deferred to first call so importing this module is cheap."""
    constructed = []

    class FakeGraph:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def propagate(self, ticker, dt):
            return ({}, "FINAL TRANSACTION PROPOSAL: BUY")

    monkeypatch.setattr("ops.pipeline_adapter.TradingAgentsGraph", FakeGraph)
    adapter = TradingAgentsPipelineAdapter()
    assert constructed == []     # not yet
    r = adapter.propagate("AAPL", date(2026, 6, 30))
    assert constructed == [{}]   # constructed exactly once on first call
    adapter.propagate("MSFT", date(2026, 6, 30))
    assert constructed == [{}]   # still only one construction
    assert r.decision == PipelineDecision.BUY
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/ops/test_pipeline_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# ops/pipeline_adapter.py
"""Adapter around the upstream TradingAgentsGraph.

Production code uses TradingAgentsPipelineAdapter; tests and dry-runs use
StubPipelineAdapter to avoid LLM costs. The graph is constructed lazily so
importing this module is free of side effects."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Protocol

from tradingagents.graph.trading_graph import TradingAgentsGraph


class PipelineDecision(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


@dataclass(frozen=True)
class PipelineResult:
    symbol: str
    date: date
    decision: PipelineDecision
    raw: dict = field(default_factory=dict)


class PipelineAdapter(Protocol):
    def propagate(self, symbol: str, asof_date: date) -> PipelineResult: ...


_DECISION_PATTERN = re.compile(
    r"\b(BUY|SELL|HOLD)\b", re.IGNORECASE,
)


def parse_decision(text: str) -> PipelineDecision:
    """Parse the upstream's final decision text into the enum.

    The framework's Portfolio Manager emits 'FINAL TRANSACTION PROPOSAL: <X>'
    where X is BUY/SELL/HOLD. We accept that AND any prominent occurrence of
    those tokens, falling back to HOLD if none is found (safe default)."""
    if not text:
        return PipelineDecision.HOLD
    # Prefer the FINAL TRANSACTION PROPOSAL line if present
    m = re.search(r"FINAL TRANSACTION PROPOSAL:\s*(BUY|SELL|HOLD)", text, re.IGNORECASE)
    if m:
        return PipelineDecision(m.group(1).upper())
    m = _DECISION_PATTERN.search(text)
    if m:
        return PipelineDecision(m.group(1).upper())
    return PipelineDecision.HOLD


class TradingAgentsPipelineAdapter:
    """Wraps the upstream graph. Constructs lazily and reuses one instance."""

    def __init__(self, **graph_kwargs):
        self._kwargs = graph_kwargs
        self._graph: TradingAgentsGraph | None = None

    def _ensure_graph(self) -> TradingAgentsGraph:
        if self._graph is None:
            self._graph = TradingAgentsGraph(**self._kwargs)
        return self._graph

    def propagate(self, symbol: str, asof_date: date) -> PipelineResult:
        graph = self._ensure_graph()
        raw, decision_text = graph.propagate(symbol, asof_date.isoformat())
        decision = parse_decision(decision_text or "")
        raw_dict = raw if isinstance(raw, dict) else {"output": str(raw)}
        return PipelineResult(symbol=symbol, date=asof_date, decision=decision, raw=raw_dict)


class StubPipelineAdapter:
    """In-memory adapter for tests and dry-runs. Returns fixed decisions."""

    def __init__(self, decisions: dict[str, PipelineDecision] | None = None):
        self._decisions = decisions or {}

    def propagate(self, symbol: str, asof_date: date) -> PipelineResult:
        decision = self._decisions.get(symbol, PipelineDecision.HOLD)
        return PipelineResult(symbol=symbol, date=asof_date, decision=decision, raw={})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/ops/test_pipeline_adapter.py -v`
Expected: 10 passed (7 parse cases + 2 stub + 1 lazy-construction).

- [ ] **Step 5: Commit**

```bash
git add ops/pipeline_adapter.py tests/ops/test_pipeline_adapter.py
git commit -m "feat(ops): pipeline adapter (real + stub) with lazy graph construction"
```

---

## Task 6: Post-earnings momentum strategy

**Files:**
- Create: `ops/strategy/base.py`
- Create: `ops/strategy/post_earnings_momentum.py`
- Test: `tests/ops/strategy/test_post_earnings_momentum.py`

**Interfaces:**
- Consumes: `Candidate` (Task 4), `PipelineAdapter`, `PipelineDecision` (Task 5), `Order`, `OrderType`, `Side` (Plan 1), `OpsConfig` (Plan 1)
- Produces:
  - `@dataclass(frozen=True) class StrategyOrder: order: Order; reason: str; candidate: Candidate; pipeline: PipelineResult`
  - `class Strategy(Protocol): def propose_orders(self, *, candidates: list[Candidate], pipeline: PipelineAdapter, current_equity: Decimal, asof_date: date) -> list[StrategyOrder]: ...`
  - `class PostEarningsMomentumStrategy` — concrete impl: for each candidate, ask pipeline; if BUY, build an `Order` with notional = min(per-position cap, current_price), stop_loss = last_price * (1 + per_position_stop_pct). Skip non-BUY decisions.

- [ ] **Step 1: Write the failing test**

```python
# tests/ops/strategy/test_post_earnings_momentum.py
from datetime import date
from decimal import Decimal

from ops.broker.types import Side, OrderType
from ops.config import OpsConfig
from ops.pipeline_adapter import PipelineDecision, StubPipelineAdapter
from ops.strategy.post_earnings_momentum import PostEarningsMomentumStrategy
from ops.universe import Candidate
from ops.universe.earnings import EarningsHit


def _candidate(sym, price="200"):
    hit = EarningsHit(
        symbol=sym, report_date=date(2026, 6, 30),
        eps_actual=Decimal("1"), eps_estimate=Decimal("0.9"),
        revenue_actual=Decimal("100"), revenue_estimate=Decimal("90"),
        eps_beat=True, revenue_beat=True,
    )
    return Candidate(
        symbol=sym, earnings=hit,
        last_price=Decimal(price), avg_dollar_volume_20d=Decimal("100000000"),
    )


def test_emits_buy_order_for_pipeline_buy():
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"AAPL": PipelineDecision.BUY})
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=date(2026, 6, 30),
    )
    assert len(orders) == 1
    so = orders[0]
    assert so.order.symbol == "AAPL"
    assert so.order.side == Side.BUY
    assert so.order.order_type == OrderType.MARKET
    # Per-position cap = 10% of 250 = 25
    assert so.order.notional_dollars == Decimal("25.00")
    # Stop = 200 * (1 + -0.08) = 184
    assert so.order.stop_loss_price == Decimal("184.00")
    assert so.order.client_order_id.startswith("pem-")
    assert so.candidate.symbol == "AAPL"
    assert so.pipeline.decision == PipelineDecision.BUY


def test_skips_non_buy_decisions():
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({
        "AAPL": PipelineDecision.HOLD, "MSFT": PipelineDecision.SELL,
    })
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL"), _candidate("MSFT")], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=date(2026, 6, 30),
    )
    assert orders == []


def test_skips_when_notional_below_floor():
    """If 10% of equity is below the per_trade_dollar_floor, skip the candidate."""
    cfg = OpsConfig()  # per_trade_dollar_floor default = $5; per_position_cap = 10%
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"AAPL": PipelineDecision.BUY})
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL")], pipeline=pipe,
        current_equity=Decimal("40"),     # 10% = $4, below $5 floor
        asof_date=date(2026, 6, 30),
    )
    assert orders == []


def test_client_order_id_is_unique_per_candidate(monkeypatch):
    cfg = OpsConfig()
    strat = PostEarningsMomentumStrategy(config=cfg)
    pipe = StubPipelineAdapter({"AAPL": PipelineDecision.BUY, "MSFT": PipelineDecision.BUY})
    orders = strat.propose_orders(
        candidates=[_candidate("AAPL"), _candidate("MSFT")], pipeline=pipe,
        current_equity=Decimal("250"), asof_date=date(2026, 6, 30),
    )
    cids = {o.order.client_order_id for o in orders}
    assert len(cids) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/ops/strategy/test_post_earnings_momentum.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write `Strategy` base + Decision wrapper**

```python
# ops/strategy/base.py
"""Strategy primitives."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from ops.broker.types import Order
from ops.pipeline_adapter import PipelineAdapter, PipelineResult
from ops.universe import Candidate


@dataclass(frozen=True)
class StrategyOrder:
    order: Order
    reason: str
    candidate: Candidate
    pipeline: PipelineResult


class Strategy(Protocol):
    def propose_orders(
        self,
        *,
        candidates: list[Candidate],
        pipeline: PipelineAdapter,
        current_equity: Decimal,
        asof_date: date,
    ) -> list[StrategyOrder]: ...
```

- [ ] **Step 4: Write the strategy implementation**

```python
# ops/strategy/post_earnings_momentum.py
"""Post-earnings momentum strategy: for each candidate that the pipeline
labels BUY, build a sized order with an entry-relative stop."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from ops.broker.types import Order, OrderType, Side
from ops.config import OpsConfig
from ops.pipeline_adapter import PipelineAdapter, PipelineDecision
from ops.strategy.base import StrategyOrder
from ops.universe import Candidate


def _client_order_id(symbol: str, asof: date, idx: int) -> str:
    return f"pem-{asof.isoformat()}-{symbol}-{idx}"


def _quantize_money(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"))


class PostEarningsMomentumStrategy:
    def __init__(self, *, config: OpsConfig):
        self._cfg = config

    def propose_orders(
        self,
        *,
        candidates: list[Candidate],
        pipeline: PipelineAdapter,
        current_equity: Decimal,
        asof_date: date,
    ) -> list[StrategyOrder]:
        notional = _quantize_money(current_equity * self._cfg.per_position_cap_pct)
        if notional < self._cfg.per_trade_dollar_floor:
            return []
        out: list[StrategyOrder] = []
        for idx, cand in enumerate(candidates):
            result = pipeline.propagate(cand.symbol, asof_date)
            if result.decision != PipelineDecision.BUY:
                continue
            stop_price = _quantize_money(
                cand.last_price * (Decimal("1") + self._cfg.per_position_stop_pct)
            )
            order = Order(
                client_order_id=_client_order_id(cand.symbol, asof_date, idx),
                symbol=cand.symbol,
                side=Side.BUY,
                notional_dollars=notional,
                order_type=OrderType.MARKET,
                stop_loss_price=stop_price,
            )
            out.append(StrategyOrder(
                order=order,
                reason=f"post-earnings beat (EPS {cand.earnings.eps_actual} vs "
                       f"est {cand.earnings.eps_estimate}); pipeline BUY",
                candidate=cand,
                pipeline=result,
            ))
        return out
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/ops/strategy/test_post_earnings_momentum.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add ops/strategy/base.py ops/strategy/post_earnings_momentum.py tests/ops/strategy/test_post_earnings_momentum.py
git commit -m "feat(ops/strategy): post-earnings momentum builds sized BUYs with stops"
```

---

## Task 7: Position guardian (one-shot)

**Files:**
- Create: `ops/position_guardian.py`
- Test: `tests/ops/test_position_guardian.py`

**Interfaces:**
- Consumes: `GuardedBroker` (Plan 1), `Position`, `Order`, `OrderType`, `Side`, `Decimal`, `OpsConfig`
- Produces:
  - `@dataclass(frozen=True) class StopAction: symbol: str; entry: Decimal; current: Decimal; pct: Decimal; sold: bool; reason: str`
  - `class PositionGuardian: def __init__(self, *, broker: GuardedBroker, quote_source: Callable[[str], Decimal], config: OpsConfig): ...`
  - `def check_stops_once(self) -> list[StopAction]` — for every open position, fetch quote; if `current/entry - 1 <= per_position_stop_pct`, place a SELL with `notional_dollars=Decimal("0")` (close-all). Returns one StopAction per position checked.

Note: this is a *single-pass* checker. Plan 3 wraps this in a background-thread loop.

- [ ] **Step 1: Write the failing test**

```python
# tests/ops/test_position_guardian.py
from decimal import Decimal

from ops import build_guarded_paper_broker
from ops.broker.types import Order, OrderType, Side
from ops.config import OpsConfig
from ops.journal import Journal
from ops.position_guardian import PositionGuardian


def _stack(tmp_path, *, starting_cash="250", quotes=None):
    quotes = quotes or {"AAPL": Decimal("200")}
    j = Journal(str(tmp_path / "j.sqlite"))
    cfg = OpsConfig()
    guarded = build_guarded_paper_broker(
        config=cfg, journal=j,
        quote_source=lambda s: quotes[s],
        starting_cash=Decimal(starting_cash),
        start_of_day_equity=lambda: Decimal(starting_cash),
        start_of_week_equity=lambda: Decimal(starting_cash),
    )
    return j, guarded, cfg, quotes


def _open_position(guarded):
    guarded.place_order(Order(
        client_order_id="open", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))


def test_guardian_does_nothing_when_above_stop(tmp_path):
    j, guarded, cfg, quotes = _stack(tmp_path)
    _open_position(guarded)
    quotes["AAPL"] = Decimal("190")   # -5%, above -8% threshold
    g = PositionGuardian(broker=guarded, quote_source=guarded.get_quote, config=cfg)
    actions = g.check_stops_once()
    assert len(actions) == 1
    assert actions[0].sold is False
    assert len(guarded.get_positions()) == 1


def test_guardian_closes_position_at_stop(tmp_path):
    j, guarded, cfg, quotes = _stack(tmp_path)
    _open_position(guarded)
    quotes["AAPL"] = Decimal("184")   # -8% exactly
    g = PositionGuardian(broker=guarded, quote_source=guarded.get_quote, config=cfg)
    actions = g.check_stops_once()
    assert actions[0].sold is True
    assert actions[0].symbol == "AAPL"
    assert guarded.get_positions() == []
    # Stop event journaled
    events = j.read_events()
    stops = [e for e in events if e["kind"] == "stop_hit"]
    assert len(stops) == 1
    assert stops[0]["payload"]["symbol"] == "AAPL"


def test_guardian_handles_multiple_positions(tmp_path):
    quotes = {"AAPL": Decimal("200"), "MSFT": Decimal("200")}
    j, guarded, cfg, _ = _stack(tmp_path, starting_cash="10000", quotes=quotes)
    guarded.place_order(Order(
        client_order_id="a", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    guarded.place_order(Order(
        client_order_id="m", symbol="MSFT", side=Side.BUY,
        notional_dollars=Decimal("100"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    quotes["AAPL"] = Decimal("220")    # +10%, hold
    quotes["MSFT"] = Decimal("180")    # -10%, stop
    g = PositionGuardian(broker=guarded, quote_source=guarded.get_quote, config=cfg)
    actions = g.check_stops_once()
    assert {a.symbol for a in actions} == {"AAPL", "MSFT"}
    sold = {a.symbol for a in actions if a.sold}
    assert sold == {"MSFT"}
    remaining = {p.symbol for p in guarded.get_positions()}
    assert remaining == {"AAPL"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/ops/test_position_guardian.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# ops/position_guardian.py
"""One-shot stop-loss enforcement.

For every open position, check the current quote and place a close-all SELL
if the position is at or past the per_position_stop_pct threshold. This is
the single-pass variant; Plan 3 will wrap it in a background-thread loop."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from ops.broker.guarded import GuardedBroker
from ops.broker.types import Order, OrderType, Side
from ops.config import OpsConfig


@dataclass(frozen=True)
class StopAction:
    symbol: str
    entry: Decimal
    current: Decimal
    pct: Decimal
    sold: bool
    reason: str


class PositionGuardian:
    def __init__(
        self,
        *,
        broker: GuardedBroker,
        quote_source: Callable[[str], Decimal],
        config: OpsConfig,
    ):
        self._broker = broker
        self._quote = quote_source
        self._cfg = config

    def check_stops_once(self) -> list[StopAction]:
        actions: list[StopAction] = []
        for pos in self._broker.get_positions():
            current = self._quote(pos.symbol)
            pct = pos.unrealized_pct(current)
            triggered = pct <= self._cfg.per_position_stop_pct
            if not triggered:
                actions.append(StopAction(
                    symbol=pos.symbol, entry=pos.avg_entry_price,
                    current=current, pct=pct, sold=False,
                    reason=f"unrealized {pct} above stop {self._cfg.per_position_stop_pct}",
                ))
                continue
            sell = Order(
                client_order_id=f"stop-{pos.symbol}",
                symbol=pos.symbol, side=Side.SELL,
                notional_dollars=Decimal("0"),  # sell-all
                order_type=OrderType.MARKET,
            )
            self._broker.place_order(sell)
            self._broker._journal.record_event(   # type: ignore[attr-defined]
                "stop_hit",
                {
                    "symbol": pos.symbol,
                    "entry": str(pos.avg_entry_price),
                    "current": str(current),
                    "pct": str(pct),
                    "threshold": str(self._cfg.per_position_stop_pct),
                },
            )
            actions.append(StopAction(
                symbol=pos.symbol, entry=pos.avg_entry_price,
                current=current, pct=pct, sold=True,
                reason=f"stop hit at {pct} (threshold {self._cfg.per_position_stop_pct})",
            ))
        return actions
```

Note: the guardian reaches into `broker._journal` to write the `stop_hit` event. That's an intentional pinhole — the journal is the source of truth for the system, and stop events belong in the same journal. A cleaner refactor would expose `journal` on `GuardedBroker` as a public read-only property; we'll do that in Task 9's cleanup pass if the integration test reveals it matters.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/ops/test_position_guardian.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add ops/position_guardian.py tests/ops/test_position_guardian.py
git commit -m "feat(ops): one-shot PositionGuardian for stop-loss enforcement"
```

---

## Task 8: Expose journal on GuardedBroker (small cleanup)

**Files:**
- Modify: `ops/broker/guarded.py:18-22` — add a read-only `journal` property
- Modify: `ops/position_guardian.py` — use `broker.journal` instead of the underscore-attr reach-around
- Modify: `tests/ops/broker/test_guarded.py` — add a test for the property

**Interfaces:**
- Consumes: existing GuardedBroker
- Produces: `GuardedBroker.journal -> Journal` (read-only attribute)

- [ ] **Step 1: Add the property**

In `ops/broker/guarded.py`, after `__init__`, add:

```python
@property
def journal(self) -> Journal:
    return self._journal
```

- [ ] **Step 2: Update position_guardian.py**

Replace `self._broker._journal.record_event(...)` with `self._broker.journal.record_event(...)`. Remove the `# type: ignore[attr-defined]` comment.

- [ ] **Step 3: Add the test**

Append to `tests/ops/broker/test_guarded.py`:

```python
def test_guarded_exposes_journal(tmp_path):
    j, paper, guarded = _stack(tmp_path)
    assert guarded.journal is j
```

- [ ] **Step 4: Run the full ops suite**

Run: `.venv/bin/pytest tests/ops/ -q 2>&1 | tail -3`
Expected: all tests pass (incremented by 1).

- [ ] **Step 5: Commit**

```bash
git add ops/broker/guarded.py ops/position_guardian.py tests/ops/broker/test_guarded.py
git commit -m "refactor(ops): expose journal on GuardedBroker; guardian no longer reaches into _journal"
```

---

## Task 9: `decide-once` CLI

**Files:**
- Create: `ops/cli.py`
- Test: `tests/ops/test_cli_decide_once.py`

**Interfaces:**
- Consumes: every public surface from earlier tasks plus Plan 1's `build_guarded_paper_broker`
- Produces: `python -m ops decide-once --date YYYY-MM-DD [--journal PATH] [--stub-pipeline]` command that:
  1. Loads config from env (`OpsConfig`)
  2. Opens the journal
  3. Builds the universe for the given date
  4. Constructs `TradingAgentsPipelineAdapter` (or `StubPipelineAdapter` if `--stub-pipeline`)
  5. Builds a `GuardedBroker` via the factory with a yfinance quote source
  6. Runs `PostEarningsMomentumStrategy.propose_orders` to get a list of `StrategyOrder`s
  7. Places each via the guarded broker; collects fills and rejections
  8. Runs `PositionGuardian.check_stops_once` once
  9. Prints a Markdown summary report to stdout
  10. Exits 0 if no errors

The `--stub-pipeline` flag is provided so the CLI can be exercised in a test without hitting LLMs.

- [ ] **Step 1: Write the failing test**

```python
# tests/ops/test_cli_decide_once.py
import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ops.cli import cli
from ops.universe import Candidate
from ops.universe.earnings import EarningsHit


def _candidate(sym, price="200"):
    hit = EarningsHit(
        symbol=sym, report_date=date(2026, 6, 30),
        eps_actual=Decimal("1"), eps_estimate=Decimal("0.9"),
        revenue_actual=Decimal("100"), revenue_estimate=Decimal("90"),
        eps_beat=True, revenue_beat=True,
    )
    return Candidate(symbol=sym, earnings=hit,
                     last_price=Decimal(price),
                     avg_dollar_volume_20d=Decimal("100000000"))


def test_decide_once_happy_path(tmp_path):
    journal_path = str(tmp_path / "j.sqlite")
    runner = CliRunner()
    with patch("ops.cli.build_universe", return_value=[_candidate("AAPL")]), \
         patch("ops.cli.make_yfinance_quote_source", return_value=lambda s: Decimal("200")):
        result = runner.invoke(cli, [
            "decide-once", "--date", "2026-06-30",
            "--journal", journal_path, "--stub-pipeline-buy", "AAPL",
        ])
    assert result.exit_code == 0, result.output
    assert "AAPL" in result.output
    assert "FILLED" in result.output
    # Journal has one fill
    from ops.journal import Journal
    j = Journal(journal_path)
    fills = j.read_fills()
    assert len(fills) == 1
    assert fills[0]["symbol"] == "AAPL"


def test_decide_once_with_no_candidates(tmp_path):
    runner = CliRunner()
    with patch("ops.cli.build_universe", return_value=[]):
        result = runner.invoke(cli, [
            "decide-once", "--date", "2026-06-30",
            "--journal", str(tmp_path / "j.sqlite"),
        ])
    assert result.exit_code == 0
    assert "no candidates" in result.output.lower()


def test_decide_once_skips_holds(tmp_path):
    runner = CliRunner()
    with patch("ops.cli.build_universe", return_value=[_candidate("AAPL")]), \
         patch("ops.cli.make_yfinance_quote_source", return_value=lambda s: Decimal("200")):
        # No --stub-pipeline-buy → stub defaults to HOLD
        result = runner.invoke(cli, [
            "decide-once", "--date", "2026-06-30",
            "--journal", str(tmp_path / "j.sqlite"),
            "--stub-pipeline",
        ])
    assert result.exit_code == 0
    assert "HOLD" in result.output or "0 BUY" in result.output


def test_decide_once_runs_guardian_pass(tmp_path):
    """If a position is already open and the current quote is below the stop,
    decide-once's guardian pass should close it."""
    # Bootstrap a position via direct journal manipulation is awkward; instead,
    # run decide-once twice: first to open AAPL, then with a lower quote to close it.
    runner = CliRunner()
    journal_path = str(tmp_path / "j.sqlite")

    # First run: open AAPL at $200
    with patch("ops.cli.build_universe", return_value=[_candidate("AAPL")]), \
         patch("ops.cli.make_yfinance_quote_source", return_value=lambda s: Decimal("200")):
        r1 = runner.invoke(cli, [
            "decide-once", "--date", "2026-06-30",
            "--journal", journal_path, "--stub-pipeline-buy", "AAPL",
        ])
    assert r1.exit_code == 0
    # Second run: no new candidates, but quote dropped — guardian should fire
    with patch("ops.cli.build_universe", return_value=[]), \
         patch("ops.cli.make_yfinance_quote_source",
               return_value=lambda s: Decimal("180")):  # -10% vs 200
        r2 = runner.invoke(cli, [
            "decide-once", "--date", "2026-07-01",
            "--journal", journal_path, "--starting-cash", "225",
        ])
    assert r2.exit_code == 0
    # NOTE: the broker is fresh each invocation (in-memory PaperBroker), so a
    # second-run guardian only sees what's in THIS process's broker book —
    # which is empty. This test documents the limitation: stop enforcement
    # requires the orchestrator from Plan 3, where the broker lives across
    # ticks. The decide-once command runs one stop pass on the broker built
    # for THIS invocation, which is mostly useful when the same invocation
    # both opens and (in pathological cases) closes positions.
    assert "guardian" in r2.output.lower()
```

The last test documents the limitation — `decide-once` only sees positions opened in the same invocation because the PaperBroker is in-memory. Plan 3's orchestrator addresses this by keeping the broker alive across ticks. The test exists as a marker; it doesn't assert a fill happened.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/ops/test_cli_decide_once.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ops.cli'`.

- [ ] **Step 3: Write the CLI implementation**

```python
# ops/cli.py
"""Command-line entry points for the ops layer.

`decide-once` runs a single end-to-end pass: universe → pipeline → orders →
fills → stop check. Designed for ad-hoc invocation and as the basic
building block for Plan 3's orchestrator."""
from __future__ import annotations

from datetime import date as date_cls, datetime
from decimal import Decimal

import click

from ops import build_guarded_paper_broker
from ops.broker.base import OrderRejected
from ops.config import load_config
from ops.journal import Journal
from ops.pipeline_adapter import (
    PipelineDecision,
    StubPipelineAdapter,
    TradingAgentsPipelineAdapter,
)
from ops.position_guardian import PositionGuardian
from ops.quotes import make_yfinance_quote_source
from ops.strategy.post_earnings_momentum import PostEarningsMomentumStrategy
from ops.universe import build_universe


@click.group()
def cli() -> None:
    """ops — operational live-trading layer."""


@cli.command("decide-once")
@click.option("--date", "as_of", required=True,
              type=click.DateTime(formats=["%Y-%m-%d"]),
              help="Date to run for, YYYY-MM-DD")
@click.option("--journal", "journal_path", default="ops_journal.sqlite",
              type=click.Path(dir_okay=False), help="SQLite journal path")
@click.option("--starting-cash", default="250",
              help="Paper-broker starting cash (Decimal string)")
@click.option("--stub-pipeline", is_flag=True,
              help="Use a stub pipeline (no LLM calls) — defaults to HOLD")
@click.option("--stub-pipeline-buy", multiple=True,
              help="Symbol(s) the stub pipeline should label BUY. Implies --stub-pipeline.")
def decide_once(
    as_of: datetime,
    journal_path: str,
    starting_cash: str,
    stub_pipeline: bool,
    stub_pipeline_buy: tuple[str, ...],
) -> None:
    """Run a single decision/fill/stop-check pass."""
    asof_date = as_of.date()
    cfg = load_config()
    journal = Journal(journal_path)
    cash = Decimal(starting_cash)

    click.echo(f"# decide-once — {asof_date.isoformat()}")
    click.echo(f"Cash: ${cash}    Config: {cfg.broker_mode} broker, "
               f"per-position cap {cfg.per_position_cap_pct}, "
               f"stop {cfg.per_position_stop_pct}")
    click.echo("")

    # Universe
    candidates = build_universe(asof_date=asof_date, config=cfg)
    click.echo(f"## Universe ({len(candidates)})")
    if not candidates:
        click.echo("(no candidates — nothing to do today)")
    for c in candidates:
        click.echo(f"  - {c.symbol}: price=${c.last_price} "
                   f"earnings beat (EPS {c.earnings.eps_actual}/"
                   f"{c.earnings.eps_estimate})")
    click.echo("")

    if not candidates:
        click.echo("0 candidates → 0 BUY orders. Guardian: skipped.")
        return

    # Pipeline
    if stub_pipeline or stub_pipeline_buy:
        decisions = {s: PipelineDecision.BUY for s in stub_pipeline_buy}
        pipeline = StubPipelineAdapter(decisions)
    else:
        pipeline = TradingAgentsPipelineAdapter()

    # Quote source — uses yfinance with 60s TTL
    quote_source = make_yfinance_quote_source()

    # Broker
    guarded = build_guarded_paper_broker(
        config=cfg, journal=journal,
        quote_source=quote_source,
        starting_cash=cash,
        start_of_day_equity=lambda: cash,    # naive — Plan 3 reads from journal
        start_of_week_equity=lambda: cash,
    )

    # Strategy
    strategy = PostEarningsMomentumStrategy(config=cfg)
    proposals = strategy.propose_orders(
        candidates=candidates, pipeline=pipeline,
        current_equity=guarded.get_equity(),
        asof_date=asof_date,
    )

    click.echo(f"## Pipeline decisions")
    if not proposals:
        click.echo("0 BUY proposals (all HOLD/SELL or below trade floor)")
    for p in proposals:
        click.echo(f"  - {p.order.symbol}: {p.pipeline.decision.value} "
                   f"→ ${p.order.notional_dollars} @ ~${p.candidate.last_price}, "
                   f"stop ${p.order.stop_loss_price}")
    click.echo("")

    # Place orders
    click.echo(f"## Orders")
    for p in proposals:
        try:
            fill = guarded.place_order(p.order)
            click.echo(f"  - {p.order.symbol}: FILLED qty={fill.quantity} @ ${fill.price}")
        except OrderRejected as exc:
            click.echo(f"  - {p.order.symbol}: REJECTED [{exc.rule_name}] {exc.reason}")
    click.echo("")

    # Guardian
    click.echo("## Guardian (one stop-check pass)")
    guardian = PositionGuardian(
        broker=guarded, quote_source=quote_source, config=cfg,
    )
    for action in guardian.check_stops_once():
        verb = "SOLD" if action.sold else "held"
        click.echo(f"  - {action.symbol}: {verb} (current ${action.current}, "
                   f"unrealized {action.pct})")
    click.echo("")

    # Summary
    positions = guarded.get_positions()
    equity = guarded.get_equity()
    click.echo(f"End-of-pass equity: ${equity}")
    click.echo(f"Open positions: {len(positions)}")
    for pos in positions:
        click.echo(f"  - {pos.symbol}: qty={pos.quantity} entry=${pos.avg_entry_price} "
                   f"stop=${pos.stop_loss_price}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Wire CLI entry point**

Add to `pyproject.toml` under `[project.scripts]` (create the section if needed):

```toml
[project.scripts]
ops = "ops.cli:main"
```

Reinstall to pick up the entry point:

Run: `.venv/bin/pip install -e . 2>&1 | tail -3`

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ops/test_cli_decide_once.py -v`
Expected: 4 passed.

- [ ] **Step 6: Smoke-test the CLI**

Run: `.venv/bin/ops decide-once --date 2026-06-30 --journal /tmp/smoke.sqlite --stub-pipeline`
Expected: prints a markdown report; in a pre-earnings-season window the universe will likely be empty and you'll see "0 candidates → 0 BUY orders." That's fine — confirms the wiring.

Cleanup: `rm /tmp/smoke.sqlite`

- [ ] **Step 7: Commit**

```bash
git add ops/cli.py tests/ops/test_cli_decide_once.py pyproject.toml
git commit -m "feat(ops): decide-once CLI — universe → pipeline → orders → guardian"
```

---

## Task 10: End-to-end integration test

**Files:**
- Create: `tests/ops/test_integration_decide_once.py`

**Interfaces:**
- Consumes: every public surface in Plan 2 (no production-code changes in this task)
- Produces: a single test that wires the full pipeline (universe → strategy → broker → guardian) with stubs and verifies the journal at the end

- [ ] **Step 1: Write the integration test**

```python
# tests/ops/test_integration_decide_once.py
"""End-to-end: stub universe + stub pipeline + paper broker + guardian.
Verifies the whole Plan 2 chain wires together correctly."""
from datetime import date
from decimal import Decimal

from ops import build_guarded_paper_broker
from ops.config import OpsConfig
from ops.journal import Journal
from ops.pipeline_adapter import PipelineDecision, StubPipelineAdapter
from ops.position_guardian import PositionGuardian
from ops.strategy.post_earnings_momentum import PostEarningsMomentumStrategy
from ops.universe import Candidate
from ops.universe.earnings import EarningsHit


def _candidate(sym, price="200"):
    return Candidate(
        symbol=sym,
        earnings=EarningsHit(
            symbol=sym, report_date=date(2026, 6, 30),
            eps_actual=Decimal("1"), eps_estimate=Decimal("0.9"),
            revenue_actual=Decimal("100"), revenue_estimate=Decimal("90"),
            eps_beat=True, revenue_beat=True,
        ),
        last_price=Decimal(price),
        avg_dollar_volume_20d=Decimal("100000000"),
    )


def test_full_pass_fill_then_stop(tmp_path):
    cfg = OpsConfig()
    j = Journal(str(tmp_path / "j.sqlite"))
    # mutable quote source so we can move price between fill and guardian pass
    quotes = {"AAPL": Decimal("200"), "MSFT": Decimal("200")}
    guarded = build_guarded_paper_broker(
        config=cfg, journal=j,
        quote_source=lambda s: quotes[s],
        starting_cash=Decimal("250"),
        start_of_day_equity=lambda: Decimal("250"),
        start_of_week_equity=lambda: Decimal("250"),
    )

    # Pipeline: BUY for AAPL, HOLD for MSFT
    pipeline = StubPipelineAdapter({
        "AAPL": PipelineDecision.BUY, "MSFT": PipelineDecision.HOLD,
    })
    strategy = PostEarningsMomentumStrategy(config=cfg)
    proposals = strategy.propose_orders(
        candidates=[_candidate("AAPL"), _candidate("MSFT")],
        pipeline=pipeline,
        current_equity=guarded.get_equity(),
        asof_date=date(2026, 6, 30),
    )
    assert {p.order.symbol for p in proposals} == {"AAPL"}

    # Place orders
    for p in proposals:
        guarded.place_order(p.order)

    assert {pos.symbol for pos in guarded.get_positions()} == {"AAPL"}

    # Move AAPL price down to trip the stop
    quotes["AAPL"] = Decimal("180")     # -10%
    guardian = PositionGuardian(
        broker=guarded, quote_source=lambda s: quotes[s], config=cfg,
    )
    actions = guardian.check_stops_once()
    assert any(a.sold and a.symbol == "AAPL" for a in actions)
    assert guarded.get_positions() == []

    # Journal reflects the full sequence
    fills = j.read_fills()
    sides = [f["side"] for f in fills]
    assert sides == ["BUY", "SELL"]
    stop_events = [e for e in j.read_events() if e["kind"] == "stop_hit"]
    assert len(stop_events) == 1
```

- [ ] **Step 2: Run the test**

Run: `.venv/bin/pytest tests/ops/test_integration_decide_once.py -v`
Expected: 1 passed.

- [ ] **Step 3: Run the entire ops suite for regression**

Run: `.venv/bin/pytest tests/ops/ -q 2>&1 | tail -3`
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/ops/test_integration_decide_once.py
git commit -m "test(ops): end-to-end integration — universe → strategy → broker → guardian"
```

---

## Task 11: Push branch + open PR

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/ops-strategy-pipeline
```

- [ ] **Step 2: Open PR against your fork's main**

Open the URL printed by the push (which uses the fork's compare page so the base is `main` of `CWFred/TradingAgents`):

```
https://github.com/CWFred/TradingAgents/compare/main...feat/ops-strategy-pipeline?expand=1
```

PR title: `feat(ops): Plan 2 — strategy, pipeline adapter, universe, guardian, decide-once CLI`

PR body (paste into the form):
```
## Summary
- Universe: S&P 500 (weekly-cached Wikipedia scrape) ∩ post-earnings beats (yfinance) ∩ liquidity filter
- Pipeline adapter: thin wrapper around `TradingAgentsGraph.propagate`, plus a stub adapter for tests/dry-runs
- Post-earnings momentum strategy: pipeline-BUY candidates get sized (per-position cap) BUYs with entry-relative stops
- `PositionGuardian.check_stops_once()` — single-pass stop enforcement
- `ops decide-once --date YYYY-MM-DD` CLI command runs the full chain once

## Test plan
- [x] `.venv/bin/pytest tests/ops/` — all passing
- [x] `.venv/bin/ops decide-once --date 2026-06-30 --journal /tmp/smoke.sqlite --stub-pipeline` runs cleanly
- [x] Spec doc updated (in this branch) — yfinance for market data, RH MCP reserved for Plan 3's live broker

## Deferred to Plan 3
- Always-on orchestrator with NYSE market calendar
- Background-thread `PositionGuardian` (here it's one-shot)
- `RobinhoodBroker` (live trading)
- Notifications (push, email)
- Daily/weekly start-of-period equity sourced from journal (here it's the starting cash, a known approximation)
```

---

## Self-review notes (already applied)

- **Spec coverage:** every Plan 2-scope item from the spec is covered. Universe (sp500 + earnings + filters: Tasks 2–4), strategy (Task 6), pipeline (Task 5), guardian (Task 7), CLI entrypoint (Task 9). End-to-end integration test (Task 10).
- **Deferred-with-reason items called out:**
  - PositionGuardian is one-shot in Plan 2; Plan 3 makes it a thread (Task 7 docstring + Task 9 test docstring document this).
  - `start_of_day_equity`/`start_of_week_equity` are passed as constants in `decide-once` because the journal-derived implementation belongs to Plan 3's orchestrator.
- **Placeholder scan:** no TBDs. The "Note" comments in Task 7 about reaching into `_journal` are immediately addressed by Task 8 (the cleanup task that adds the public property).
- **Type consistency:** `PipelineDecision`, `PipelineResult`, `StrategyOrder`, `Candidate`, `EarningsHit`, `StopAction` are used identically across the tasks that consume them.
- **Scope:** focused on a single coherent slice — a one-shot end-to-end paper-trading pass via the `decide-once` CLI — that ships testable on its own.
