"""Integration-style unit tests for the screen composition root (all I/O injected)."""

from datetime import date
from decimal import Decimal

import pytest

from ops.config import OpsConfig
from ops.research import run as run_mod
from ops.research.prices import PriceContext
from ops.research.store import ScreenStore
from ops.research.triggers import Trigger
from ops.universe.smallcap import SmallcapMember, UniverseName

pytestmark = pytest.mark.unit

ASOF = date(2026, 7, 1)
D = Decimal


def _name(symbol, sector="Industrials"):
    member = SmallcapMember(
        symbol=symbol, name=f"{symbol} Co", sector=sector, industry="Machinery",
        market_cap=D("1000000000"), last_price=D("20"),
    )
    return UniverseName(member=member, last_price=D("20"), adv_20d=D("5000000"))


def _facts_for_passer():
    """Facts making a name cheap + quality AGAINST A $1B MARKET CAP.

    Dollar concepts must be scaled to the market cap or the FCF-yield and
    EV/EBIT bars can never pass: FCF 100M / cap 1B = 10% yield; EBIT 150M
    gives EV/EBIT ~6.7; equity 800M keeps ROIC ~17%.
    """
    def _row(val, year, instant=False):
        row = {"val": val, "end": f"{year}-12-31", "filed": f"{year + 1}-02-15",
               "form": "10-K", "fp": "FY", "accn": f"a{year}"}
        if not instant:
            row["start"] = f"{year}-01-01"
        return row

    def series(vals, instant=False):
        return [_row(v, 2021 + i, instant) for i, v in enumerate(vals)]

    m = 1_000_000
    concepts = {
        "OperatingIncomeLoss": series([130 * m, 135 * m, 140 * m, 145 * m, 150 * m]),
        "DepreciationDepletionAndAmortization": series([30 * m] * 5),
        "NetCashProvidedByUsedInOperatingActivities": series([120 * m] * 5),
        "PaymentsToAcquirePropertyPlantAndEquipment": series([20 * m] * 5),
        "StockholdersEquity": series([800 * m] * 5, instant=True),
        "CashAndCashEquivalentsAtCarryingValue": series([100 * m] * 5, instant=True),
        "Revenues": series([1000 * m, 1020 * m, 1040 * m, 1060 * m, 1080 * m]),
        "CostOfRevenue": series([600 * m, 612 * m, 624 * m, 636 * m, 648 * m]),
    }
    payload = {}
    for concept, rows in concepts.items():
        payload[concept] = {"units": {"USD": rows}}
    payload["EarningsPerShareDiluted"] = {"units": {"USD/shares": series(
        ["2.0", "2.2", "2.4", "2.6", "2.8"],
    )}}
    return {"facts": {"us-gaap": payload}}


def _price_ctx():
    from datetime import timedelta
    closes = {}
    d = ASOF
    while len(closes) < 1500:
        if d.weekday() < 5:
            closes[d] = D("20")
        d -= timedelta(days=1)
    return PriceContext(closes=closes)


@pytest.fixture
def config(tmp_path):
    return OpsConfig(
        journal_path=str(tmp_path / "j.sqlite"),
        baseline_journal_path=str(tmp_path / "b.sqlite"),
        screen_store_path=str(tmp_path / "s.sqlite"),
        baseline_starting_cash=D("100000"),
    )


def _run(config, *, dry_run=False, facts=None, triggers=None):
    universe = [_name("GOOD")] + [_name(f"PEER{i}") for i in range(5)]
    trigger = Trigger(kind="activist_stake", description="SC 13D", date=ASOF, source="a1")

    def fake_triggers(ticker, *, asof, lookback_days=90, list_filings=None):
        return [trigger] if ticker == "GOOD" else []

    return run_mod.run_screen(
        config=config, asof=ASOF, dry_run=dry_run,
        universe_builder=lambda: universe,
        facts_fetcher=facts or (lambda t: _facts_for_passer()),
        triggers_finder=triggers or fake_triggers,
        price_context_fetcher=lambda s: _price_ctx(),
        quote_source=lambda s: D("20"),
    )


def test_current_pe_price_is_in_the_latest_eps_era():
    """A split between the latest fiscal year end and asof must not deflate
    the current-P/E leg: the asof price is expressed in the era of the
    as-reported EPS (10:1 split on 2026-03-15 -> $20 close counts as $200)."""
    ctx = PriceContext(closes=_price_ctx().closes,
                       splits={date(2026, 3, 15): D("10")})
    ni = run_mod._name_inputs(
        _name("SPLT"), asof=ASOF,
        facts_fetcher=lambda t: _facts_for_passer(),
        triggers_finder=lambda t, *, asof, **kw: [],
        price_context_fetcher=lambda s: ctx,
    )
    assert ni.price == D("200")


def test_market_cap_rescale_is_split_immune():
    """A split between the universe snapshot and asof must not change market
    cap: snapshot $200/share, 4:1 split, adjusted close $50 (unchanged in
    real terms) -> cap stays at the snapshot value."""
    member = SmallcapMember(
        symbol="SPLT", name="Splt Co", sector="Industrials", industry="Machinery",
        market_cap=D("1000000000"), last_price=D("200"),
    )
    name = UniverseName(member=member, last_price=D("200"),
                        adv_20d=D("5000000"), snapshot_at=date(2026, 4, 1))
    closes = {d: D("50") for d in _price_ctx().closes}
    ctx = PriceContext(closes=closes, splits={date(2026, 5, 1): D("4")})
    ni = run_mod._name_inputs(
        name, asof=ASOF,
        facts_fetcher=lambda t: _facts_for_passer(),
        triggers_finder=lambda t, *, asof, **kw: [],
        price_context_fetcher=lambda s: ctx,
    )
    assert ni.market_cap == D("1000000000")


def test_full_run_screens_stores_and_buys_baseline(config):
    summary = _run(config)
    assert summary.universe_size == 6
    assert summary.screened == 6
    assert "GOOD" in summary.passed
    store = ScreenStore(config.screen_store_path)
    assert [h["symbol"] for h in store.pending_hits()] == list(summary.passed)
    assert summary.baseline is not None
    assert summary.baseline["buys"] == list(summary.passed)


def test_dry_run_touches_nothing(config, tmp_path):
    summary = _run(config, dry_run=True)
    assert "GOOD" in summary.passed
    assert summary.baseline is None
    assert ScreenStore(config.screen_store_path).last_run() is None


def test_per_name_errors_are_skipped_not_fatal(config):
    def exploding_facts(ticker):
        if ticker == "GOOD":
            raise KeyError("ticker not in SEC map")
        return _facts_for_passer()

    summary = _run(config, facts=exploding_facts)
    assert summary.screened == 5
    assert any("GOOD" in e for e in summary.errors)


def test_default_facts_fetcher_fails_fast_without_edgar_user_agent(config, monkeypatch):
    """Fix 2: a missing SEC_EDGAR_USER_AGENT must blow up BEFORE the sweep,
    not get swallowed ~1500 times by the per-name catch."""
    from tradingagents.dataflows.edgar import EdgarNotConfiguredError

    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)

    with pytest.raises(EdgarNotConfiguredError):
        run_mod.run_screen(
            config=config, asof=ASOF,
            universe_builder=lambda: [],
            triggers_finder=lambda ticker, *, asof, lookback_days=90, list_filings=None: [],
            price_context_fetcher=lambda s: _price_ctx(),
            quote_source=lambda s: D("20"),
        )


def test_baseline_failure_is_non_fatal(config, tmp_path):
    """Fix 3: a broken baseline journal must not take down the screen run —
    the store write and screen results must survive it."""
    broken_config = OpsConfig(
        journal_path=config.journal_path,
        baseline_journal_path=str(tmp_path),  # a directory, not a DB file
        screen_store_path=config.screen_store_path,
        baseline_starting_cash=config.baseline_starting_cash,
    )
    summary = _run(broken_config)
    assert summary.passed  # screen results survive
    assert ScreenStore(broken_config.screen_store_path).last_run() is not None
    assert summary.baseline is None
    assert any(e.startswith("baseline:") for e in summary.errors)


def test_silent_none_names_are_promoted_to_errors(config):
    """Fix 4: a name_inputs None (no price context / no close at asof) must
    not vanish silently — it must show up in errors, not just be absent
    from screened."""
    universe = [_name("GOOD")] + [_name(f"PEER{i}") for i in range(5)]
    trigger = Trigger(kind="activist_stake", description="SC 13D", date=ASOF, source="a1")

    def fake_triggers(ticker, *, asof, lookback_days=90, list_filings=None):
        return [trigger] if ticker == "GOOD" else []

    summary = run_mod.run_screen(
        config=config, asof=ASOF,
        universe_builder=lambda: universe,
        facts_fetcher=lambda t: _facts_for_passer(),
        triggers_finder=fake_triggers,
        price_context_fetcher=lambda s: None,
        quote_source=lambda s: D("20"),
    )
    assert summary.screened == 0
    symbols = [n.member.symbol for n in universe]
    for symbol in symbols:
        assert any(e.startswith(f"{symbol}: skipped") for e in summary.errors)


def test_summary_carries_per_bar_coverage(config):
    summary = _run(config)
    assert summary.coverage  # six bar names
    assert summary.coverage["fcf_yield"]["computed"] >= 1
    assert set(summary.coverage) == {
        "ev_ebit_vs_sector", "fcf_yield", "pe_vs_own_history",
        "roic_5y", "debt_to_ebitda", "gross_margin_stability",
    }


def test_year_end_prices_are_split_unadjusted(config):
    from ops.research.run import _name_inputs

    ctx = _price_ctx()
    # Rebuild with a 10:1 forward split newer than every fiscal year end.
    ctx = PriceContext(closes=ctx.closes, splits={ASOF: Decimal("10")})
    ni = _name_inputs(
        _name("GOOD"), asof=ASOF,
        facts_fetcher=lambda t: _facts_for_passer(),
        triggers_finder=lambda t, *, asof, lookback_days=90, list_filings=None: [],
        price_context_fetcher=lambda s: ctx,
    )
    assert ni is not None
    # Every year-end price is 10x the adjusted 20 -> 200.
    assert all(px == Decimal("200") for px in ni.year_end_prices.values())
