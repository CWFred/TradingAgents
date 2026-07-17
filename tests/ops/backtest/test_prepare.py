from datetime import date
from decimal import Decimal

import pytest

from ops.backtest.cases import CaseCandidate, HistoricalCaseSource
from ops.backtest.context import ContextArtifact
from ops.backtest.prepare import PreparedContext, prepare_cases
from ops.backtest.store import BacktestStore

pytestmark = pytest.mark.unit


class _ContextBuilder:
    def build(self, case):
        return PreparedContext((ContextArtifact(
            kind="screen", source_ref=f"screen:{case.case_id}",
            available_at=case.asof, content="sealed screen payload",
        ),))


def test_prepare_selects_and_seals_cases(tmp_path):
    sessions = [date(2025, 6, day) for day in range(2, 22)]

    def fetch(asof):
        return [
            CaseCandidate("BBB", asof, Decimal("1"), {"kind": "screen"}),
            CaseCandidate("AAA", asof, Decimal("2"), {"kind": "screen"}),
        ]

    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        summary = prepare_cases(
            store=store, source=HistoricalCaseSource(fetch),
            context_builder=_ContextBuilder(), exchange_sessions=sessions,
            start=date(2025, 6, 2), end=date(2025, 6, 21),
            target_count=2, per_date_cap=1, sleeve="research",
            cutoff=date(2025, 6, 1), spacing_sessions=10,
        )
        assert len(summary.selected_case_ids) == 2
        for case_id in summary.selected_case_ids:
            manifest = store.get_context_manifest(case_id)
            assert manifest is not None
            assert manifest.included[0].content == "sealed screen payload"


def test_prepare_rejects_future_context_without_persisting_manifest(tmp_path):
    class FutureBuilder:
        def build(self, case):
            return PreparedContext((ContextArtifact(
                kind="filing", source_ref="future", available_at=date(2025, 6, 3),
                content="future",
            ),))

    source = HistoricalCaseSource(lambda asof: [
        CaseCandidate("AAA", asof, 1, {"kind": "screen"}),
    ])
    with BacktestStore(tmp_path / "backtest.sqlite") as store:
        summary = prepare_cases(
            store=store, source=source, context_builder=FutureBuilder(),
            exchange_sessions=[date(2025, 6, 2)],
            start=date(2025, 6, 2), end=date(2025, 6, 2),
            target_count=1, per_date_cap=1, sleeve="research",
            cutoff=date(2025, 6, 1),
        )
        manifest = store.get_context_manifest(summary.selected_case_ids[0])
        assert manifest.included == ()
        assert manifest.excluded[0].source_ref == "future"
