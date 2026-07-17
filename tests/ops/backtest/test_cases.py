from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from ops.backtest.cases import (
    HISTORICAL_SOURCE_MODE,
    RECONSTRUCTION_SOURCE_MODE,
    CaseCandidate,
    CurrentUniverseReconstructionSource,
    HistoricalCaseSource,
    collect_candidates,
    construct_case,
    sample_sessions,
    select_candidates,
)
from ops.backtest.models import CaseSource, CutoffViolation


def _candidate(symbol: str, asof: date, score=1, *, ref: str | None = None):
    return CaseCandidate(
        symbol=symbol,
        asof=asof,
        score=score,
        trigger={"kind": "screen", "ref": ref or symbol},
        screen_payload={"symbol": symbol},
        source_ref=ref,
    )


def test_sample_sessions_is_sorted_unique_and_exchange_session_based():
    start = date(2025, 6, 2)
    sessions = [start + timedelta(days=n) for n in (11, 0, 4, 1, 2, 4, 8, 9, 10)]

    assert sample_sessions(sessions, start=start, end=start + timedelta(days=20),
                           spacing_sessions=3) == (
        start,
        start + timedelta(days=4),
        start + timedelta(days=10),
    )


def test_sample_sessions_validates_range_and_spacing():
    today = date(2025, 6, 2)
    with pytest.raises(ValueError, match="before start"):
        sample_sessions([], start=today, end=today - timedelta(days=1))
    with pytest.raises(ValueError, match="positive"):
        sample_sessions([], start=today, end=today, spacing_sessions=0)


def test_selection_spreads_dates_then_takes_next_rank_and_caps_each_date():
    d1, d2, d3 = date(2025, 6, 2), date(2025, 6, 16), date(2025, 6, 30)
    candidates = [
        _candidate("A2", d1, 8), _candidate("A1", d1, 10), _candidate("A3", d1, 7),
        _candidate("B2", d2, 8), _candidate("B1", d2, 10), _candidate("B3", d2, 7),
        _candidate("C2", d3, 8), _candidate("C1", d3, 10), _candidate("C3", d3, 7),
    ]

    selected = select_candidates(candidates, target_count=5, per_date_cap=2)

    assert [(hit.symbol, hit.asof) for hit in selected] == [
        ("A1", d1), ("B1", d2), ("C1", d3), ("A2", d1), ("B2", d2),
    ]


def test_selection_deduplicates_symbol_asof_using_score_then_stable_provenance():
    asof = date(2025, 6, 2)
    candidates = [
        _candidate(" zzz ", asof, "4", ref="later"),
        _candidate("ZZZ", asof, Decimal("5"), ref="z-ref"),
        _candidate("zzz", asof, Decimal("5.0"), ref="a-ref"),
        _candidate("BBB", asof, 5),
        _candidate("AAA", asof, 5),
    ]

    selected = select_candidates(candidates, target_count=10, per_date_cap=10)

    assert [hit.symbol for hit in selected] == ["AAA", "BBB", "ZZZ"]
    assert selected[-1].source_ref == "a-ref"


def test_selection_rejects_invalid_inputs():
    asof = date(2025, 6, 2)
    with pytest.raises(ValueError, match="target_count"):
        select_candidates([], target_count=-1, per_date_cap=1)
    with pytest.raises(ValueError, match="per_date_cap"):
        select_candidates([], target_count=1, per_date_cap=0)
    with pytest.raises(ValueError, match="finite"):
        select_candidates([_candidate("ABC", asof, "NaN")], target_count=1, per_date_cap=1)


def test_source_adapters_are_explicitly_labeled_and_validate_requested_asof():
    asof = date(2025, 6, 2)
    historical = HistoricalCaseSource(lambda requested: [_candidate("ABC", requested)])
    fallback = CurrentUniverseReconstructionSource(
        lambda requested: [_candidate("XYZ", requested)]
    )

    assert historical.source_mode == HISTORICAL_SOURCE_MODE
    assert fallback.source_mode == RECONSTRUCTION_SOURCE_MODE
    assert [hit.symbol for hit in collect_candidates(historical, [asof])] == ["ABC"]

    broken = HistoricalCaseSource(lambda _requested: [_candidate("ABC", asof + timedelta(days=1))])
    with pytest.raises(ValueError, match="requested date"):
        collect_candidates(broken, [asof])


def test_construct_case_allows_exact_cutoff_and_normalizes_symbol():
    cutoff = date(2025, 6, 1)
    made = construct_case(
        _candidate(" abc ", cutoff, "1.25"),
        sleeve="research",
        cutoff=cutoff,
        source=CaseSource.POINT_IN_TIME,
    )

    assert made.symbol == "ABC"
    assert made.asof == cutoff
    assert made.score == Decimal("1.25")
    assert made.source is CaseSource.POINT_IN_TIME
    assert made.trigger["screen_payload"] == {"symbol": " abc "}


def test_construct_case_rejects_one_day_before_cutoff_before_calling_factory():
    cutoff = date(2025, 6, 1)
    with pytest.raises(CutoffViolation, match="precedes effective backtest cutoff"):
        construct_case(
            _candidate("ABC", cutoff - timedelta(days=1)),
            sleeve="research",
            cutoff=cutoff,
            source=CaseSource.POINT_IN_TIME,
        )
