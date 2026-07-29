from datetime import date, timedelta
from decimal import Decimal

import pytest

from ops.backtest.cases import CaseCandidate, HistoricalCaseSource
from ops.backtest.context import ContextArtifact
from ops.backtest.models import CaseSource, ContextManifest
from ops.backtest.prepare import PreparedContext, prepare_cases
from ops.backtest.store import BacktestStore
from ops.config import OpsConfig

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


class _FakeReconstructionFetcher:
    """Stands in for ReconstructionScreenerFetcher: __call__(asof) -> candidates."""

    def __init__(self, symbols):
        self._symbols = symbols
        self.calls = []

    def __call__(self, asof):
        self.calls.append(asof)
        return tuple(
            CaseCandidate(
                symbol=symbol,
                asof=asof,
                score=Decimal(str(score)),
                trigger={"kind": "historical_screener_replay", "asof": asof.isoformat()},
                screen_payload={"passed": True},
                source_ref=f"reconstruction:{asof.isoformat()}:{symbol}",
            )
            for symbol, score in self._symbols
        )


def test_reconstruction_prepare_inserts_matured_cases(tmp_path, monkeypatch):
    import ops.backtest.service as service
    from ops.backtest.service import _reconstruction_prepare_cases

    path = tmp_path / "backtest.sqlite"
    config = OpsConfig(backtest_store_path=str(path))

    # Deterministic session list: 11 trading days in the window. With
    # spacing_sessions=10 this samples index 0 and index 10 -> two dates.
    sessions = [date(2025, 6, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16)]
    monkeypatch.setattr(
        "ops.scheduler.market_calendar.MarketCalendar.sessions_between",
        lambda self, start, end: sessions,
    )
    # Reuse a fake sealed context builder so no EDGAR/network access happens.
    monkeypatch.setattr(
        service,
        "_sealed_context_builder",
        lambda cfg: (
            lambda case, _candidate: ContextManifest.create(
                case_id=case.case_id, asof=case.asof,
            )
        ),
    )

    fetcher = _FakeReconstructionFetcher([("AAA", 2), ("BBB", 1)])
    with BacktestStore(path) as store:
        cases = _reconstruction_prepare_cases(
            store=store, config=config, sleeve="research",
            start=date(2025, 6, 2), end=date(2025, 7, 1), case_count=4,
            spacing_sessions=10, universe=["AAA", "BBB"], fetcher=fetcher,
        )
        assert len(cases) == 4
        assert {c.source for c in cases} == {CaseSource.CURRENT_UNIVERSE_RECONSTRUCTION}
        assert all(date(2025, 6, 2) <= c.asof <= date(2025, 7, 1) for c in cases)
        assert fetcher.calls == [date(2025, 6, 2), date(2025, 6, 16)]

        stored = store.list_cases(sleeve="research")
        assert len(stored) == 4
        assert {c.source for c in stored} == {CaseSource.CURRENT_UNIVERSE_RECONSTRUCTION}
        for case in stored:
            assert store.get_context_manifest(case.case_id) is not None


def test_reconstruction_prepare_reaches_case_count_on_floor_edge(tmp_path, monkeypatch):
    """per_date_cap must use CEIL: floor(5/3)==1 would cap selection at 3.

    Three sampled dates each yield two candidates (six available). Requesting
    five cases must actually insert five: ceil(5/3)==2 per date allows up to
    six, whereas the old floor(5/3)==1 would have capped at one-per-date -> 3.
    """
    import ops.backtest.service as service
    from ops.backtest.service import _reconstruction_prepare_cases

    path = tmp_path / "backtest.sqlite"
    config = OpsConfig(backtest_store_path=str(path))

    # 21 sessions so spacing_sessions=10 samples indices 0, 10, 20 -> three dates.
    sessions = [date(2025, 6, 1) + timedelta(days=d) for d in range(21)]
    monkeypatch.setattr(
        "ops.scheduler.market_calendar.MarketCalendar.sessions_between",
        lambda self, start, end: sessions,
    )
    monkeypatch.setattr(
        service,
        "_sealed_context_builder",
        lambda cfg: (
            lambda case, _candidate: ContextManifest.create(
                case_id=case.case_id, asof=case.asof,
            )
        ),
    )

    fetcher = _FakeReconstructionFetcher([("AAA", 2), ("BBB", 1)])
    with BacktestStore(path) as store:
        cases = _reconstruction_prepare_cases(
            store=store, config=config, sleeve="research",
            start=sessions[0], end=sessions[-1], case_count=5,
            spacing_sessions=10, universe=["AAA", "BBB"], fetcher=fetcher,
        )
        # ceil path reaches 5; floor path would have stopped at 3.
        assert len(cases) == 5
        assert len(fetcher.calls) == 3


def test_reconstruction_prepare_tops_up_and_never_duplicates(tmp_path, monkeypatch):
    """--append top-up: a candidate whose (symbol, asof) is already in the
    store must be skipped so the same case is never inserted twice."""
    import ops.backtest.service as service
    from ops.backtest.service import _reconstruction_prepare_cases

    path = tmp_path / "backtest.sqlite"
    config = OpsConfig(backtest_store_path=str(path))

    sessions = [date(2025, 6, 16)]
    monkeypatch.setattr(
        "ops.scheduler.market_calendar.MarketCalendar.sessions_between",
        lambda self, start, end: sessions,
    )
    monkeypatch.setattr(
        service,
        "_sealed_context_builder",
        lambda cfg: (
            lambda case, _candidate: ContextManifest.create(
                case_id=case.case_id, asof=case.asof,
            )
        ),
    )

    # AAA is already present (dup, must be skipped); CCC is new.
    fetcher = _FakeReconstructionFetcher([("AAA", 2), ("CCC", 1)])
    with BacktestStore(path) as store:
        cases = _reconstruction_prepare_cases(
            store=store, config=config, sleeve="research",
            start=date(2025, 6, 2), end=date(2025, 7, 1), case_count=1,
            spacing_sessions=10, universe=["AAA", "CCC"], fetcher=fetcher,
            existing=(("AAA", date(2025, 6, 16)),),
        )
        assert [c.symbol for c in cases] == ["CCC"]


def test_sealed_context_builder_fails_closed_without_price_bars(tmp_path, monkeypatch):
    """An empty price cache must abort manifest sealing, not silently omit
    price history (2026-07-27 incident: 40 memos guardrail-rejected because
    manifests were sealed minutes before the price backfill ran)."""
    from ops.backtest.models import BacktestCase
    from ops.backtest.service import MissingBacktestArtifacts, _sealed_context_builder
    from tradingagents.dataflows import edgar

    monkeypatch.setattr(edgar, "get_user_agent", lambda: "test-agent")
    monkeypatch.setattr(edgar, "list_filings", lambda ticker, **kwargs: [])
    config = OpsConfig(backtest_store_path=str(tmp_path / "backtest.sqlite"))
    case = BacktestCase.create(sleeve="research", symbol="AAA", asof=date(2025, 6, 16))
    with pytest.raises(MissingBacktestArtifacts, match="price"):
        _sealed_context_builder(config)(case, None)
