"""`ops notify overview` (DO-Task 2): build_daily_overview is a pure,
cross-sleeve journal + memo-store reader.

Every test seeds the three journals (tmp) + a memo store and asserts on the
returned dict — never on formatted text except for the render/no-exceptions
checks, mirroring tests/ops/test_status.py and
tests/ops/research/test_report.py's build/format split. The overriding
constraint (per the plan) is that build_daily_overview must be safe to run
from an empty system on day one: no broker, no network, no quotes, no LLM.
"""
from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ops import events
from ops.config import OpsConfig
from ops.journal import Journal
from ops.notify.overview import build_daily_overview, format_daily_overview, overview_headline
from tradingagents.memos.schema import EvidenceItem, Falsifier, Memo, ValueThesis
from tradingagents.memos.store import MemoStore

pytestmark = pytest.mark.unit

# 2026-07-07 15:00 UTC is 2026-07-07 11:00 ET -- comfortably inside the ET
# trading day, so trading_day_start(NOW) == 2026-07-07 04:00 UTC.
NOW = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)
DAY_START = datetime(2026, 7, 7, 4, 0, tzinfo=timezone.utc)


@pytest.fixture
def stores(tmp_path):
    main_journal = Journal(str(tmp_path / "main.sqlite"))
    research_journal = Journal(str(tmp_path / "research.sqlite"))
    baseline_journal = Journal(str(tmp_path / "baseline.sqlite"))
    memo_store = MemoStore(tmp_path / "memos.sqlite")
    yield main_journal, research_journal, baseline_journal, memo_store
    main_journal.close()
    research_journal.close()
    baseline_journal.close()


def _memo(ticker, *, created_at, conviction_tier="high", status="open"):
    memo = Memo(
        ticker=ticker, as_of_date=date(2026, 1, 5), thesis_type="value",
        thesis="Mispriced.", created_at=created_at,
        evidence=[EvidenceItem(claim="c", source_type="filing", source_ref="a:mdna")],
        value_block=ValueThesis(
            why_cheap="x", change_trigger="y",
            normalized_earnings_view="z", quality_assessment="q",
        ),
        conviction_tier=conviction_tier,
        entry_price_ref=10.0, price_target_low=15.0, price_target_high=20.0,
        expected_holding_months=6, must_be_true=["m"],
        falsifiers=[Falsifier(description="d", check_type="price",
                               metric="drawdown_from_cost_pct", operator="<",
                               threshold=-30.0)],
    )
    memo.status = status
    return memo


def _seed_full_day(main_journal, research_journal, baseline_journal, memo_store):
    """The plan's representative day: a couple of analysis_decisions incl. a
    HOLD, a fill, an exit_decision, a research_monitor_run with 1 trip, a
    research_trade_run, a baseline_screen_run, one anomaly (order_rejected)
    -- plus a memo written today and one from last week (must be excluded)."""
    # --- main / momentum journal ---
    main_journal.record_equity_snapshot(
        kind="open_day", equity=Decimal("9900"), cash=Decimal("2000"),
        at=datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
    )
    main_journal.record_equity_snapshot(
        kind="open_day", equity=Decimal("10000"), cash=Decimal("2200"),
        at=datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc),
    )
    main_journal.record_event(
        events.KIND_DAILY_CYCLE_RUN,
        events.daily_cycle_run_payload(asof_date=date(2026, 7, 7)),
        at=NOW,
    )
    main_journal.record_event(
        events.KIND_UNIVERSE_DIAGNOSTICS,
        events.universe_diagnostics_payload(
            asof_date=date(2026, 7, 7), candidates=12,
            fetch_ok=95, fetch_failed=5, by_label={},
        ),
        at=NOW,
    )
    for symbol, decision in (("AAPL", "BUY"), ("MSFT", "HOLD"), ("TSLA", "SELL")):
        main_journal.record_event(
            events.KIND_ANALYSIS_DECISION,
            events.analysis_decision_payload(
                symbol=symbol, decision=decision, source="momentum", asof="2026-07-07",
            ),
            at=NOW,
        )
    main_journal.record_event(
        events.KIND_FILL,
        events.fill_payload(
            client_order_id="c-aapl", order_id="o-aapl", symbol="AAPL", side="BUY",
            quantity=Decimal("1"), price=Decimal("200"), filled_at=NOW,
            context="entry", broker_mode="paper",
        ),
        at=NOW,
    )
    main_journal.record_event(
        events.KIND_ORDER_REJECTED,
        events.order_rejected_payload(
            rule="daily_notional_cap", reason="would exceed daily cap",
            client_order_id="c-nflx", symbol="NFLX", side="BUY",
            notional_dollars=Decimal("50"),
        ),
        at=NOW,
    )
    main_journal.record_event(
        events.KIND_EXIT_DECISION,
        events.exit_decision_payload(symbol="TSLA", rule="trailing_stop", evidence="ev"),
        at=NOW,
    )

    # --- research journal (only position_opened/closed + the research_run
    # equity snapshot actually live here -- see trading.py:142/215) ---
    research_journal.record_equity_snapshot(
        kind="research_run", equity=Decimal("5000"), cash=Decimal("1000"), at=NOW,
    )
    # --- main journal: Phase C (ops/research/monitor.py) and Phase D
    # (ops/research/trading.py:272) both record onto the daemon's main
    # journal, not the research journal. ---
    main_journal.record_event(
        events.KIND_RESEARCH_MONITOR_RUN,
        events.research_monitor_run_payload(
            asof="2026-07-07", memos_checked=5, falsifiers_evaluated=3, tripped=1,
            unevaluable=0, escalations=1, resolution_due=1, catalyst_due=0, errors=[],
        ),
        at=NOW,
    )
    main_journal.record_event(
        events.KIND_FALSIFIER_TRIPPED,
        events.falsifier_tripped_payload(
            memo_id="m-abc", ticker="ABC", falsifier_index="0",
            description="d", metric="drawdown_from_cost_pct",
            observed="-40", threshold="-30", consecutive_periods=1,
        ),
        at=NOW,
    )
    main_journal.record_event(
        events.KIND_RESEARCH_ESCALATION,
        events.research_escalation_payload(
            ticker="ABC", memo_id="m-abc", reason="falsifier tripped", hit_id=1,
        ),
        at=NOW,
    )
    main_journal.record_event(
        events.KIND_RESOLUTION_DUE,
        events.resolution_due_payload(
            memo_id="m-xyz", ticker="XYZ", thesis_type="value", status="open",
            expected_holding_months=6, elapsed_days=190, checklist="check",
        ),
        at=NOW,
    )
    main_journal.record_event(
        events.KIND_RESEARCH_TRADE_RUN,
        events.research_trade_run_payload(
            asof="2026-07-07", entered=["DEF"], exited=[],
            skipped=["GHI: quote unavailable"], equity="5000", cash="1000",
        ),
        at=NOW,
    )
    research_journal.record_event(
        events.KIND_RESEARCH_POSITION_OPENED,
        events.research_position_opened_payload(
            symbol="DEF", memo_id="m-def", conviction_tier="high",
            entry_date="2026-07-07", client_order_id="c-def", notional="500",
        ),
        at=NOW,
    )

    # --- baseline journal ---
    baseline_journal.record_equity_snapshot(
        kind="baseline_run", equity=Decimal("20000"), cash=Decimal("4000"), at=NOW,
    )
    baseline_journal.record_event(
        events.KIND_BASELINE_SCREEN_RUN,
        events.baseline_screen_run_payload(
            asof="2026-07-07", passers=8, buys=["QQQ"], exits=["SPY"],
            skipped=["IWM"], equity=Decimal("20000"),
        ),
        at=NOW,
    )

    # --- memo store: one today, one last week (must be excluded) ---
    memo_store.save(_memo("DEF", created_at=NOW))
    memo_store.save(_memo("OLD", created_at=NOW - timedelta(days=8)))


def test_full_day_momentum_section(stores):
    main_journal, research_journal, baseline_journal, memo_store = stores
    _seed_full_day(main_journal, research_journal, baseline_journal, memo_store)
    report = build_daily_overview(
        main_journal=main_journal, baseline_journal=baseline_journal,
        research_journal=research_journal, memo_store=memo_store,
        config=OpsConfig(), now=NOW,
    )
    m = report["momentum"]
    assert m["cycle_ran"] is True
    assert m["universe"] == {"checked": 100, "fetch_failures": 5, "candidates": 12}
    assert m["universe_blind"] is False
    assert m["analyzed_decided"] == {
        "total": 3,
        "by_verdict": {"BUY": ["AAPL"], "HOLD": ["MSFT"], "SELL": ["TSLA"]},
    }
    assert m["buys_filled"] == ["AAPL"]
    assert m["rejected"] == [{"symbol": "NFLX", "reason": "would exceed daily cap"}]
    assert m["exits"] == [{"symbol": "TSLA", "rule": "trailing_stop"}]
    assert m["day_equity"] == Decimal("10000")
    # (10000 - 9900) / 9900
    assert m["day_pnl_pct"] == pytest.approx(Decimal("100") / Decimal("9900"))
    assert report["quiet"] is False


def test_full_day_research_section(stores):
    main_journal, research_journal, baseline_journal, memo_store = stores
    _seed_full_day(main_journal, research_journal, baseline_journal, memo_store)
    report = build_daily_overview(
        main_journal=main_journal, baseline_journal=baseline_journal,
        research_journal=research_journal, memo_store=memo_store,
        config=OpsConfig(), now=NOW,
    )
    r = report["research"]
    # Only today's memo -- last week's "OLD" is excluded.
    assert r["memos"] == [
        {"ticker": "DEF", "thesis_type": "value", "tier": "high", "status": "open"}
    ]
    assert r["monitor"]["counts"] == {
        "memos_checked": 5, "falsifiers_evaluated": 3, "tripped": 1,
        "unevaluable": 0, "escalations": 1, "resolution_due": 1, "catalyst_due": 0,
    }
    assert r["monitor"]["tripped"] == ["ABC"]
    assert r["monitor"]["escalations"] == ["ABC"]
    assert r["monitor"]["resolution_due"] == ["XYZ"]
    assert r["monitor"]["catalyst_due"] == []
    assert r["trades"] == {
        "entered": ["DEF"], "exited": [], "skipped": ["GHI: quote unavailable"],
        "equity": Decimal("5000"), "cash": Decimal("1000"),
    }
    assert r["positions_opened"] == [
        {"symbol": "DEF", "memo_id": "m-def", "tier": "high"}
    ]
    assert r["positions_closed"] == []


def test_research_events_are_read_from_the_main_journal(stores):
    """Phase C (ops/research/monitor.py) and Phase D (ops/research/trading.py
    line 272) both record research_monitor_run/falsifier_tripped/
    research_trade_run on the MAIN journal, not the research journal -- the
    research journal only ever gets research_position_opened/closed and the
    research_run equity snapshot. This seeds production-accurately (main
    journal only, research journal left empty of these kinds) and asserts
    the research section still surfaces them -- it must not silently read an
    empty research-journal slice instead."""
    main_journal, research_journal, baseline_journal, memo_store = stores
    main_journal.record_event(
        events.KIND_RESEARCH_MONITOR_RUN,
        events.research_monitor_run_payload(
            asof="2026-07-07", memos_checked=5, falsifiers_evaluated=3, tripped=1,
            unevaluable=0, escalations=1, resolution_due=1, catalyst_due=0, errors=[],
        ),
        at=NOW,
    )
    main_journal.record_event(
        events.KIND_FALSIFIER_TRIPPED,
        events.falsifier_tripped_payload(
            memo_id="m-abc", ticker="ABC", falsifier_index="0",
            description="d", metric="drawdown_from_cost_pct",
            observed="-40", threshold="-30", consecutive_periods=1,
        ),
        at=NOW,
    )
    main_journal.record_event(
        events.KIND_RESEARCH_TRADE_RUN,
        events.research_trade_run_payload(
            asof="2026-07-07", entered=["DEF"], exited=[],
            skipped=["GHI: quote unavailable"], equity="5000", cash="1000",
        ),
        at=NOW,
    )
    report = build_daily_overview(
        main_journal=main_journal, baseline_journal=baseline_journal,
        research_journal=research_journal, memo_store=memo_store,
        config=OpsConfig(), now=NOW,
    )
    r = report["research"]
    assert r["monitor"]["counts"] == {
        "memos_checked": 5, "falsifiers_evaluated": 3, "tripped": 1,
        "unevaluable": 0, "escalations": 1, "resolution_due": 1, "catalyst_due": 0,
    }
    assert r["monitor"]["tripped"] == ["ABC"]
    assert r["trades"] == {
        "entered": ["DEF"], "exited": [], "skipped": ["GHI: quote unavailable"],
        "equity": Decimal("5000"), "cash": Decimal("1000"),
    }
    assert report["quiet"] is False


def test_research_error_anomalies_are_read_from_the_main_journal(stores):
    """research_monitor_error/research_trade_error (ops/main.py's
    _research_monitor_tick/_research_trade_tick) are recorded on the same
    `journal` param as research_monitor_run/research_trade_run -- the main
    journal, not the research journal -- so the anomalies section must read
    them from main_by_kind too."""
    main_journal, research_journal, baseline_journal, memo_store = stores
    main_journal.record_event(
        events.KIND_RESEARCH_MONITOR_ERROR,
        events.research_monitor_error_payload(error="boom"),
        at=NOW,
    )
    main_journal.record_event(
        events.KIND_RESEARCH_TRADE_ERROR,
        events.research_trade_error_payload(error="kaboom"),
        at=NOW,
    )
    report = build_daily_overview(
        main_journal=main_journal, baseline_journal=baseline_journal,
        research_journal=research_journal, memo_store=memo_store,
        config=OpsConfig(), now=NOW,
    )
    kinds = {a["kind"] for a in report["anomalies"]}
    assert kinds == {events.KIND_RESEARCH_MONITOR_ERROR, events.KIND_RESEARCH_TRADE_ERROR}


def test_full_day_baseline_and_header_and_anomalies(stores):
    main_journal, research_journal, baseline_journal, memo_store = stores
    _seed_full_day(main_journal, research_journal, baseline_journal, memo_store)
    report = build_daily_overview(
        main_journal=main_journal, baseline_journal=baseline_journal,
        research_journal=research_journal, memo_store=memo_store,
        config=OpsConfig(), now=NOW,
    )
    b = report["baseline"]
    assert b["screen"] == {
        "passers": 8, "buys": ["QQQ"], "exits": ["SPY"], "skipped": ["IWM"],
        "equity": Decimal("20000"),
    }
    assert b["exits"] == []
    assert b["writeoffs"] == []

    h = report["header"]
    assert h["date"] == date(2026, 7, 7)
    assert h["momentum"]["equity"] == Decimal("10000")
    assert h["research"]["equity"] == Decimal("5000")
    assert h["baseline"]["equity"] == Decimal("20000")

    anomalies = report["anomalies"]
    assert len(anomalies) == 1
    assert anomalies[0]["kind"] == events.KIND_ORDER_REJECTED
    assert anomalies[0]["payload"]["symbol"] == "NFLX"


def test_quiet_day_renders_without_errors(stores):
    main_journal, research_journal, baseline_journal, memo_store = stores
    report = build_daily_overview(
        main_journal=main_journal, baseline_journal=baseline_journal,
        research_journal=research_journal, memo_store=memo_store,
        config=OpsConfig(), now=NOW,
    )
    assert report["quiet"] is True
    assert report["momentum"]["day_equity"] is None
    assert report["header"]["momentum"] is None
    assert report["header"]["research"] is None
    assert report["header"]["baseline"] is None
    assert report["anomalies"] == []

    rendered = format_daily_overview(report)
    assert "Quiet day" in rendered
    for header in ("## Header", "## Momentum", "## Research", "## Baseline", "## Anomalies"):
        assert header in rendered

    headline = overview_headline(report)
    assert "\n" not in headline
    assert headline.startswith("2026-07-07:")


def test_full_day_format_renders_without_errors_and_contains_symbols(stores):
    main_journal, research_journal, baseline_journal, memo_store = stores
    _seed_full_day(main_journal, research_journal, baseline_journal, memo_store)
    report = build_daily_overview(
        main_journal=main_journal, baseline_journal=baseline_journal,
        research_journal=research_journal, memo_store=memo_store,
        config=OpsConfig(), now=NOW,
    )
    rendered = format_daily_overview(report)
    assert "Quiet day" not in rendered
    for needle in ("AAPL", "NFLX", "TSLA", "DEF", "ABC", "XYZ", "QQQ"):
        assert needle in rendered


def test_overview_headline_full_day(stores):
    main_journal, research_journal, baseline_journal, memo_store = stores
    _seed_full_day(main_journal, research_journal, baseline_journal, memo_store)
    report = build_daily_overview(
        main_journal=main_journal, baseline_journal=baseline_journal,
        research_journal=research_journal, memo_store=memo_store,
        config=OpsConfig(), now=NOW,
    )
    headline = overview_headline(report)
    assert "\n" not in headline
    assert headline == (
        "2026-07-07: momentum 1 buy/1 exit, research 1 memo/1 trip, "
        "equity $10,000 (+1.01%)"
    )


def test_analysis_decision_hold_is_not_a_buy_or_sell(stores):
    main_journal, research_journal, baseline_journal, memo_store = stores
    main_journal.record_event(
        events.KIND_ANALYSIS_DECISION,
        events.analysis_decision_payload(
            symbol="MSFT", decision="HOLD", source="momentum", asof="2026-07-07",
        ),
        at=NOW,
    )
    report = build_daily_overview(
        main_journal=main_journal, baseline_journal=baseline_journal,
        research_journal=research_journal, memo_store=memo_store,
        config=OpsConfig(), now=NOW,
    )
    ad = report["momentum"]["analyzed_decided"]
    assert ad == {"total": 1, "by_verdict": {"BUY": [], "HOLD": ["MSFT"], "SELL": []}}
    # A HOLD-only day is still "quiet" for buys/exits purposes but is not
    # a fully quiet day (something was analyzed).
    assert report["quiet"] is False


def test_header_equity_uses_latest_snapshot_even_if_stale(stores):
    """Header (unlike section 1's day-scoped equity) uses the latest
    snapshot regardless of date -- 'momentum from main open_day/latest
    snapshot' per the plan."""
    main_journal, research_journal, baseline_journal, memo_store = stores
    stale_at = NOW - timedelta(days=10)
    main_journal.record_equity_snapshot(
        kind="open_day", equity=Decimal("8000"), cash=Decimal("1000"), at=stale_at,
    )
    report = build_daily_overview(
        main_journal=main_journal, baseline_journal=baseline_journal,
        research_journal=research_journal, memo_store=memo_store,
        config=OpsConfig(), now=NOW,
    )
    assert report["header"]["momentum"] == {"equity": Decimal("8000"), "at": stale_at}
    # But section 1's day-scoped equity has no snapshot *today*, so it is None.
    assert report["momentum"]["day_equity"] is None


def test_module_has_no_broker_network_or_llm_imports():
    """Pure read-and-render: no quotes, no network, no LLM, no broker
    imports. Verified structurally (AST) rather than by grepping strings so
    it survives reformatting."""
    src_path = Path(__file__).resolve().parents[3] / "ops" / "notify" / "overview.py"
    tree = ast.parse(src_path.read_text())
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    forbidden_prefixes = (
        "ops.broker", "ops.universe", "ops.exits", "ops.position_guardian",
        "ops.scheduler", "tradingagents.dataflows", "tradingagents.agents",
        "yfinance", "requests", "httpx", "openai", "anthropic", "langchain",
    )
    for module in imported_modules:
        assert not module.startswith(forbidden_prefixes), (
            f"ops/notify/overview.py imports {module!r} -- must stay pure "
            "read-and-render (no quotes/network/LLM/broker)"
        )
