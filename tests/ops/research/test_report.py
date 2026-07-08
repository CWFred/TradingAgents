"""`ops research report` (Phase D task 8): the quarterly calibration report.

Every test seeds a tmp MemoStore + tmp research/baseline journals and
asserts on build_report's dict — format_report is checked separately for
headers/no-exceptions/the small-corpus honesty string. Mirrors
tests/ops/test_status.py's build/format split.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from ops import events
from ops.journal import Journal
from ops.research.report import build_report, format_report
from tradingagents.memos.schema import (
    EventThesis,
    EvidenceItem,
    Falsifier,
    Memo,
    Resolution,
    ReturnScenario,
    ValueThesis,
)
from tradingagents.memos.store import MemoStore

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)


def _value_memo(
    ticker, *, created_at, conviction_tier="medium", authored_by_model="",
    scenarios=None,
):
    return Memo(
        ticker=ticker, as_of_date=date(2026, 1, 5), thesis_type="value",
        thesis="Mispriced.", created_at=created_at,
        evidence=[EvidenceItem(claim="c", source_type="filing", source_ref="a:mdna")],
        value_block=ValueThesis(
            why_cheap="x", change_trigger="y",
            normalized_earnings_view="z", quality_assessment="q",
        ),
        conviction_tier=conviction_tier, authored_by_model=authored_by_model,
        entry_price_ref=10.0, price_target_low=15.0, price_target_high=20.0,
        expected_holding_months=6, must_be_true=["m"],
        falsifiers=[Falsifier(description="d", check_type="price",
                              metric="drawdown_from_cost_pct", operator="<",
                              threshold=-30.0)],
        scenarios=scenarios or [],
    )


def _event_memo(
    ticker, *, created_at, conviction_tier="medium", authored_by_model="",
    scenarios=None,
):
    return Memo(
        ticker=ticker, as_of_date=date(2026, 1, 5), thesis_type="event",
        thesis="Forced seller.", created_at=created_at,
        evidence=[EvidenceItem(claim="c", source_type="filing", source_ref="a:mdna")],
        event_block=EventThesis(
            event_type="spinoff", seller_identity="index funds",
            why_non_economic="index tracking, price-insensitive",
        ),
        conviction_tier=conviction_tier, authored_by_model=authored_by_model,
        entry_price_ref=10.0, price_target_low=15.0, price_target_high=20.0,
        expected_holding_months=6, must_be_true=["m"],
        falsifiers=[Falsifier(description="d", check_type="event")],
        scenarios=scenarios or [],
    )


def _resolve(
    memo_store, memo, *, realized_return_pct, outcome_label, resolved_at=NOW,
    benchmark_return_pct=0.05, holding_days=180, exit_price=12.0,
):
    resolution = Resolution(
        resolved_at=resolved_at, exit_price=exit_price,
        realized_return_pct=realized_return_pct,
        benchmark_return_pct=benchmark_return_pct, holding_days=holding_days,
        outcome_label=outcome_label, narrative="did the thing",
    )
    return memo_store.resolve(memo.memo_id, resolution)


def _record_position_opened(research_journal, memo, *, at):
    research_journal.record_event(
        events.KIND_RESEARCH_POSITION_OPENED,
        events.research_position_opened_payload(
            symbol=memo.ticker, memo_id=memo.memo_id, conviction_tier=memo.conviction_tier,
            entry_date=memo.as_of_date.isoformat(), client_order_id=f"buy-{memo.ticker}",
            notional="500",
        ),
        at=at,
    )


@pytest.fixture
def stores(tmp_path):
    memo_store = MemoStore(tmp_path / "memos.sqlite")
    research_journal = Journal(str(tmp_path / "research.sqlite"))
    baseline_journal = Journal(str(tmp_path / "baseline.sqlite"))
    yield memo_store, research_journal, baseline_journal
    research_journal.close()
    baseline_journal.close()


@pytest.fixture
def seeded(stores):
    """The brief's corpus mix: 2 resolved (different labels/models/scenarios,
    both bought/positioned), 1 open, 1 passed-resolved with no position event
    (the shadow track)."""
    memo_store, research_journal, baseline_journal = stores

    memo_a = _value_memo(
        "AAA", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        conviction_tier="high", authored_by_model="anthropic:claude-sonnet-5",
        scenarios=[
            ReturnScenario(probability=0.7, return_pct=0.4, description="bull"),
            ReturnScenario(probability=0.3, return_pct=-0.2, description="bear"),
        ],
    )
    memo_store.save(memo_a)
    _record_position_opened(research_journal, memo_a, at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    _resolve(memo_store, memo_a, realized_return_pct=0.35, outcome_label="thesis_right_made_money")

    memo_b = _event_memo(
        "BBB", created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        conviction_tier="medium", authored_by_model="anthropic:claude-haiku-5",
        scenarios=[
            ReturnScenario(probability=0.5, return_pct=0.1, description="bull"),
            ReturnScenario(probability=0.5, return_pct=-0.05, description="bear"),
        ],
    )
    memo_store.save(memo_b)
    _record_position_opened(research_journal, memo_b, at=datetime(2026, 2, 2, tzinfo=timezone.utc))
    # Thesis wrong, but made money anyway: the luck cell.
    _resolve(memo_store, memo_b, realized_return_pct=0.20, outcome_label="thesis_wrong_made_money")

    memo_d = _event_memo(
        "DDD", created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        conviction_tier="medium",
    )
    memo_store.save(memo_d)  # left open

    memo_c = _value_memo(
        "CCC", created_at=datetime(2026, 4, 5, tzinfo=timezone.utc),
        conviction_tier="starter",
        scenarios=[
            ReturnScenario(probability=0.6, return_pct=-0.15, description="down"),
            ReturnScenario(probability=0.4, return_pct=0.05, description="flat"),
        ],
    )
    memo_store.save(memo_c)
    memo_store.mark_passed(memo_c.memo_id)  # shadow-tracked, never bought
    _resolve(memo_store, memo_c, realized_return_pct=-0.10, outcome_label="thesis_wrong_lost_money")

    return memo_store, research_journal, baseline_journal, {
        "a": memo_a, "b": memo_b, "c": memo_c, "d": memo_d,
    }


def test_corpus_section_counts_and_date_range(seeded):
    memo_store, research_journal, baseline_journal, _ = seeded
    report = build_report(
        memo_store=memo_store, research_journal=research_journal,
        baseline_journal=baseline_journal, now=NOW,
    )
    corpus = report["corpus"]
    assert corpus["total"] == 4
    assert corpus["by_status"] == {"open": 1, "passed": 0, "resolved": 3}
    assert corpus["by_thesis_type"] == {"value": 2, "event": 2}
    assert corpus["by_conviction_tier"] == {"starter": 1, "medium": 2, "high": 1}
    assert corpus["oldest_memo_at"] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert corpus["newest_memo_at"] == datetime(2026, 4, 5, tzinfo=timezone.utc)


def test_outcome_matrix_counts_means_and_luck_cell(seeded):
    memo_store, research_journal, baseline_journal, _ = seeded
    report = build_report(
        memo_store=memo_store, research_journal=research_journal,
        baseline_journal=baseline_journal, now=NOW,
    )
    matrix = report["outcome_matrix"]
    assert matrix["n"] == 3
    cells = matrix["cells"]
    assert cells["thesis_right_made_money"] == {
        "count": 1, "mean_realized_return_pct": pytest.approx(0.35),
    }
    assert cells["thesis_wrong_made_money"] == {
        "count": 1, "mean_realized_return_pct": pytest.approx(0.20),
    }
    assert cells["thesis_wrong_lost_money"] == {
        "count": 1, "mean_realized_return_pct": pytest.approx(-0.10),
    }
    assert cells["thesis_right_lost_money"] == {
        "count": 0, "mean_realized_return_pct": None,
    }


def test_scenario_calibration_small_corpus_honesty(seeded):
    memo_store, research_journal, baseline_journal, _ = seeded
    report = build_report(
        memo_store=memo_store, research_journal=research_journal,
        baseline_journal=baseline_journal, now=NOW,
    )
    section = report["scenario_calibration"]
    assert section["n"] == 3
    assert section["too_small"] is True
    assert section["mean_signed_gap_pct"] is None
    rendered = format_report(report)
    assert "corpus too small (n=3 < 5) — numbers are noise" in rendered


def test_scenario_calibration_numeric_path_with_five_resolved(stores):
    """Five resolved memos with known stated/realized returns: exact gap and
    directional-hit-rate arithmetic, above the small-corpus threshold."""
    memo_store, research_journal, baseline_journal = stores
    # (stated_expected_return, realized_return_pct) pairs.
    cases = [
        (0.30, 0.20),   # gap = +0.10, both positive -> hit
        (0.10, -0.05),  # gap = +0.15, sign mismatch -> miss
        (-0.10, -0.20), # gap = +0.10, both negative -> hit
        (0.05, 0.05),   # gap = 0.00, both positive -> hit
        (-0.05, 0.10),  # gap = -0.15, sign mismatch -> miss
    ]
    for i, (stated, realized) in enumerate(cases):
        memo = _value_memo(
            f"T{i}", created_at=datetime(2026, 1, 1 + i, tzinfo=timezone.utc),
            scenarios=[ReturnScenario(probability=1.0, return_pct=stated, description="only")],
        )
        memo_store.save(memo)
        _resolve(
            memo_store, memo, realized_return_pct=realized,
            outcome_label="thesis_right_made_money" if realized >= 0 else "thesis_right_lost_money",
        )

    report = build_report(
        memo_store=memo_store, research_journal=research_journal,
        baseline_journal=baseline_journal, now=NOW,
    )
    section = report["scenario_calibration"]
    assert section["n"] == 5
    assert section["too_small"] is False
    gaps = [0.10, 0.15, 0.10, 0.00, -0.15]
    assert section["mean_signed_gap_pct"] == pytest.approx(sum(gaps) / 5)
    assert section["mean_abs_gap_pct"] == pytest.approx(sum(abs(g) for g in gaps) / 5)
    assert section["directional_hit_rate"] == pytest.approx(3 / 5)


def test_bought_vs_passed_counts_and_means(seeded):
    memo_store, research_journal, baseline_journal, _ = seeded
    report = build_report(
        memo_store=memo_store, research_journal=research_journal,
        baseline_journal=baseline_journal, now=NOW,
    )
    section = report["bought_vs_passed"]
    assert section["n_resolved"] == 3
    assert section["bought"]["count"] == 2
    assert section["bought"]["mean_realized_return_pct"] == pytest.approx((0.35 + 0.20) / 2)
    assert section["passed"]["count"] == 1
    assert section["passed"]["mean_realized_return_pct"] == pytest.approx(-0.10)


def test_sleeve_vs_baseline_overlapping_window(stores):
    memo_store, research_journal, baseline_journal = stores
    research_journal.record_equity_snapshot(
        kind="research_run", equity=1000, cash=500,
        at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    research_journal.record_equity_snapshot(
        kind="research_run", equity=1100, cash=500,
        at=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )
    research_journal.record_equity_snapshot(
        kind="research_run", equity=1200, cash=500,
        at=datetime(2026, 1, 10, tzinfo=timezone.utc),
    )
    baseline_journal.record_equity_snapshot(
        kind="baseline_run", equity=2000, cash=0,
        at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )
    baseline_journal.record_equity_snapshot(
        kind="baseline_run", equity=2050, cash=0,
        at=datetime(2026, 1, 8, tzinfo=timezone.utc),
    )
    baseline_journal.record_equity_snapshot(
        kind="baseline_run", equity=2100, cash=0,
        at=datetime(2026, 1, 12, tzinfo=timezone.utc),
    )

    report = build_report(
        memo_store=memo_store, research_journal=research_journal,
        baseline_journal=baseline_journal, now=NOW,
    )
    section = report["sleeve_vs_baseline"]
    assert section["available"] is True
    assert section["window_start"] == datetime(2026, 1, 3, tzinfo=timezone.utc)
    assert section["window_end"] == datetime(2026, 1, 10, tzinfo=timezone.utc)
    assert section["sleeve"]["first_equity"] == pytest.approx(1100.0)
    assert section["sleeve"]["last_equity"] == pytest.approx(1200.0)
    assert section["sleeve"]["return_pct"] == pytest.approx((1200.0 - 1100.0) / 1100.0)
    assert section["baseline"]["first_equity"] == pytest.approx(2000.0)
    assert section["baseline"]["last_equity"] == pytest.approx(2050.0)
    assert section["baseline"]["return_pct"] == pytest.approx((2050.0 - 2000.0) / 2000.0)


def test_sleeve_vs_baseline_no_data_when_one_series_empty(stores):
    memo_store, research_journal, baseline_journal = stores
    research_journal.record_equity_snapshot(
        kind="research_run", equity=1000, cash=500,
        at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    # baseline_journal has no baseline_run snapshots at all.
    report = build_report(
        memo_store=memo_store, research_journal=research_journal,
        baseline_journal=baseline_journal, now=NOW,
    )
    section = report["sleeve_vs_baseline"]
    assert section["available"] is False
    rendered = format_report(report)
    assert "no data yet" in rendered.split("## 5. Sleeve vs baseline")[1]


def test_per_model_attribution_groups_and_unattributed_label(seeded):
    memo_store, research_journal, baseline_journal, _ = seeded
    report = build_report(
        memo_store=memo_store, research_journal=research_journal,
        baseline_journal=baseline_journal, now=NOW,
    )
    section = report["per_model"]
    assert section["n_resolved"] == 3
    models = section["models"]
    assert set(models) == {
        "anthropic:claude-sonnet-5", "anthropic:claude-haiku-5", "(unattributed)",
    }
    sonnet = models["anthropic:claude-sonnet-5"]
    assert sonnet["count"] == 1
    assert sonnet["mean_realized_return_pct"] == pytest.approx(0.35)
    assert sonnet["outcome_counts"]["thesis_right_made_money"] == 1
    unattributed = models["(unattributed)"]
    assert unattributed["count"] == 1
    assert unattributed["mean_realized_return_pct"] == pytest.approx(-0.10)
    assert unattributed["outcome_counts"]["thesis_wrong_lost_money"] == 1


def test_format_report_has_all_section_headers(seeded):
    memo_store, research_journal, baseline_journal, _ = seeded
    report = build_report(
        memo_store=memo_store, research_journal=research_journal,
        baseline_journal=baseline_journal, now=NOW,
    )
    rendered = format_report(report)
    for header in (
        "# Research calibration report",
        "## 1. Corpus", "## 2. Outcome 2x2", "## 3. Scenario calibration",
        "## 4. Bought vs passed", "## 5. Sleeve vs baseline",
        "## 6. Per-model attribution",
    ):
        assert header in rendered


def test_format_report_renders_empty_store_with_no_data_yet_everywhere(stores):
    """Day-one render: an empty store/journals must not raise, and every
    section must say so plainly rather than showing empty tables."""
    memo_store, research_journal, baseline_journal = stores
    report = build_report(
        memo_store=memo_store, research_journal=research_journal,
        baseline_journal=baseline_journal, now=NOW,
    )
    rendered = format_report(report)
    for header in (
        "## 1. Corpus", "## 2. Outcome 2x2", "## 3. Scenario calibration",
        "## 4. Bought vs passed", "## 5. Sleeve vs baseline",
        "## 6. Per-model attribution",
    ):
        assert header in rendered
    assert rendered.count("no data yet") == 6


def test_scenario_calibration_excludes_empty_scenarios_from_n_and_gaps(stores):
    """Add a 6th memo with empty scenarios to the 5-memo numeric test: verify
    that it is excluded from n and gap calculations, counted as unscored, and
    doesn't affect the scored mean gaps or hit rate."""
    memo_store, research_journal, baseline_journal = stores
    # The 5 scored cases from test_scenario_calibration_numeric_path_with_five_resolved.
    cases = [
        (0.30, 0.20),   # gap = +0.10, both positive -> hit
        (0.10, -0.05),  # gap = +0.15, sign mismatch -> miss
        (-0.10, -0.20), # gap = +0.10, both negative -> hit
        (0.05, 0.05),   # gap = 0.00, both positive -> hit
        (-0.05, 0.10),  # gap = -0.15, sign mismatch -> miss
    ]
    for i, (stated, realized) in enumerate(cases):
        memo = _value_memo(
            f"T{i}", created_at=datetime(2026, 1, 1 + i, tzinfo=timezone.utc),
            scenarios=[ReturnScenario(probability=1.0, return_pct=stated, description="only")],
        )
        memo_store.save(memo)
        _resolve(
            memo_store, memo, realized_return_pct=realized,
            outcome_label="thesis_right_made_money" if realized >= 0 else "thesis_right_lost_money",
        )

    # 6th memo: empty scenarios (unscored).
    memo_unscored = _value_memo(
        "T5", created_at=datetime(2026, 1, 6, tzinfo=timezone.utc),
        scenarios=[],  # empty: should be excluded
    )
    memo_store.save(memo_unscored)
    _resolve(
        memo_store, memo_unscored, realized_return_pct=0.25,
        outcome_label="thesis_right_made_money",
    )

    report = build_report(
        memo_store=memo_store, research_journal=research_journal,
        baseline_journal=baseline_journal, now=NOW,
    )
    section = report["scenario_calibration"]
    # Scored n should remain 5 (the empty-scenario memo is excluded).
    assert section["n"] == 5
    assert section["too_small"] is False
    # Gaps and hit rate unchanged from the 5-memo test.
    gaps = [0.10, 0.15, 0.10, 0.00, -0.15]
    assert section["mean_signed_gap_pct"] == pytest.approx(sum(gaps) / 5)
    assert section["mean_abs_gap_pct"] == pytest.approx(sum(abs(g) for g in gaps) / 5)
    assert section["directional_hit_rate"] == pytest.approx(3 / 5)
    # Unscored count.
    assert section["unscored"] == 1
    # Verify the rendered markdown mentions it.
    rendered = format_report(report)
    assert "unscored (no stated scenarios): 1" in rendered


def test_scenario_calibration_all_empty_scenarios_small_corpus(stores):
    """All resolved memos have empty scenarios: scored n=0, but we count
    unscored; the n=0 honesty path (empty) should still apply."""
    memo_store, research_journal, baseline_journal = stores
    # 3 resolved memos, all with empty scenarios.
    for i in range(3):
        memo = _value_memo(
            f"E{i}", created_at=datetime(2026, 1, 1 + i, tzinfo=timezone.utc),
            scenarios=[],  # all empty
        )
        memo_store.save(memo)
        _resolve(
            memo_store, memo, realized_return_pct=0.10 * (i + 1),
            outcome_label="thesis_right_made_money",
        )

    report = build_report(
        memo_store=memo_store, research_journal=research_journal,
        baseline_journal=baseline_journal, now=NOW,
    )
    section = report["scenario_calibration"]
    # Scored n=0 (all memos excluded).
    assert section["n"] == 0
    assert section["empty"] is True
    # All 3 are unscored.
    assert section["unscored"] == 3
    # Render should say "no data yet" for the n=0 case.
    rendered = format_report(report)
    assert "no data yet" in rendered.split("## 3. Scenario calibration")[1]


def test_build_report_is_journal_and_store_only_no_network_imports():
    """Design-by-inspection guard: report.py must never import a broker or
    a price/quote fetcher — build_report's whole point is safety without
    network access (mirrors ops/status.py's discipline)."""
    import ops.research.report as report_mod

    with open(report_mod.__file__) as f:
        import_lines = [
            line for line in f
            if line.strip().startswith("import ") or line.strip().startswith("from ")
        ]
    banned = ("broker", "ops.quotes", "research.prices", "yfinance")
    for line in import_lines:
        low = line.lower()
        for token in banned:
            assert token not in low, f"report.py must not import network code: {line!r}"
