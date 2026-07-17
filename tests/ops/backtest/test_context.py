from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from ops.backtest.context import (
    ContextArtifact,
    asof_gated_filings,
    build_context_manifest,
    filter_context_items,
    filter_eligible_lessons,
    filter_precedent_memos,
    filter_temporal_records,
)

ASOF = date(2025, 6, 15)


def _artifact(ref: str, available_at, content="safe"):
    return ContextArtifact(
        kind="filing",
        source_ref=ref,
        available_at=available_at,
        content=content,
        metadata={"form": "10-K"},
    )


def test_context_filter_includes_exact_asof_and_excludes_one_day_later():
    included, excluded = filter_context_items(
        [_artifact("exact", ASOF), _artifact("future", ASOF + timedelta(days=1))],
        asof=ASOF,
    )

    assert [item.source_ref for item in included] == ["exact"]
    assert [(item.source_ref, item.reason) for item in excluded] == [
        ("future", "available after case asof 2025-06-15")
    ]


def test_context_filter_excludes_undated_malformed_and_bad_metadata():
    included, excluded = filter_context_items(
        [
            _artifact("missing", None),
            _artifact("bad-date", "not-a-date"),
            ContextArtifact("filing", "bad-meta", ASOF, "x", metadata=[]),
            ContextArtifact("filing", "bad-content", ASOF, {"future": "nested"}),
            {"kind": "", "source_ref": "no-kind", "available_at": ASOF, "content": "x"},
        ],
        asof=ASOF,
    )

    assert included == ()
    assert {item.reason for item in excluded} == {
        "missing available_at", "malformed available_at", "malformed metadata",
        "malformed content", "missing kind or source_ref",
    }


def test_manifest_hash_cannot_be_influenced_by_future_artifact_content():
    first = build_context_manifest(
        case_id="case-1",
        asof=ASOF,
        artifacts=[_artifact("known", ASOF), _artifact("future", ASOF + timedelta(days=1), "A")],
        substitutions=["current universe", "current universe"],
    )
    second = build_context_manifest(
        case_id="case-1",
        asof=ASOF,
        artifacts=[_artifact("known", ASOF), _artifact("future", ASOF + timedelta(days=1), "B")],
        substitutions=["current universe"],
    )

    assert first.manifest_hash == second.manifest_hash
    assert [item.source_ref for item in first.included] == ["known"]
    assert [item.source_ref for item in first.excluded] == ["future"]
    assert first.substitutions == ("current universe",)


@dataclass(frozen=True)
class _Filing:
    accession_number: str
    form: str
    filing_date: object


def test_filing_wrapper_excludes_future_amendments_and_selects_newest_eligible():
    seen = {}
    filings = [
        _Filing("future-amendment", "10-K/A", ASOF + timedelta(days=1)),
        _Filing("eligible-k", "10-K", ASOF),
        _Filing("older-k", "10-K", ASOF - timedelta(days=50)),
        _Filing("eligible-q", "10-Q", ASOF - timedelta(days=2)),
        _Filing("undated", "10-Q", None),
    ]

    def fetch(ticker, **kwargs):
        seen.update(ticker=ticker, **kwargs)
        return list(reversed(filings))

    gated = asof_gated_filings(fetch, asof=ASOF)
    result = gated("ABC", limit=3)

    assert [filing.accession_number for filing in result] == [
        "eligible-k", "eligible-q", "older-k"
    ]
    assert seen["limit"] == 1000


def test_filing_wrapper_keeps_future_trigger_accession_out_of_brain_lookup():
    filings = [
        _Filing("trigger-future", "8-K", ASOF + timedelta(days=1)),
        _Filing("trigger-known", "8-K", ASOF),
    ]
    gated = asof_gated_filings(lambda *_args, **_kwargs: filings, asof=ASOF)

    by_accession = {f.accession_number: f for f in gated("ABC", limit=200)}

    assert "trigger-known" in by_accession
    assert "trigger-future" not in by_accession


def test_filing_wrapper_reapplies_forms_since_and_limit_to_untrusted_fetcher():
    filings = [
        _Filing("q-new", "10-Q", ASOF),
        _Filing("k-new", "10-K", ASOF),
        _Filing("k-old", "10-K", ASOF - timedelta(days=20)),
    ]
    gated = asof_gated_filings(lambda *_args, **_kwargs: filings, asof=ASOF)

    result = gated("ABC", forms={"10-K"}, since=ASOF - timedelta(days=5), limit=1)

    assert [filing.accession_number for filing in result] == ["k-new"]


def test_precedent_memos_must_come_from_strictly_earlier_cases():
    old = SimpleNamespace(memo_id="old", as_of_date=ASOF - timedelta(days=1))
    same = SimpleNamespace(memo_id="same", as_of_date=ASOF)
    future = SimpleNamespace(memo_id="future", as_of_date=ASOF + timedelta(days=1))
    malformed = SimpleNamespace(memo_id="bad", as_of_date="tomorrow-ish")

    assert filter_precedent_memos([future, same, old, malformed], asof=ASOF) == (old,)


def test_lessons_must_be_active_and_eligible_before_case_date():
    old = SimpleNamespace(lesson_id="old", eligible_from=ASOF - timedelta(days=1), active=True)
    same = SimpleNamespace(lesson_id="same", eligible_from=ASOF, active=True)
    future = SimpleNamespace(
        lesson_id="future", eligible_from=ASOF + timedelta(days=1), active=True
    )
    inactive = SimpleNamespace(
        lesson_id="inactive", eligible_from=ASOF - timedelta(days=2), active=False
    )

    assert filter_eligible_lessons([same, future, inactive, old], asof=ASOF) == (old,)


def test_temporal_record_filter_removes_future_and_malformed_price_bars():
    known = SimpleNamespace(session_date=ASOF, close=10)
    earlier = SimpleNamespace(session_date=ASOF - timedelta(days=1), close=9)
    future = SimpleNamespace(session_date=ASOF + timedelta(days=1), close=1000)
    malformed = SimpleNamespace(session_date="later", close=2000)

    assert filter_temporal_records(
        [known, future, malformed, earlier], asof=ASOF, date_field="session_date"
    ) == (earlier, known)


def test_context_accepts_timezone_aware_datetime_as_date_proof():
    item = _artifact("timed", datetime(2025, 6, 15, 23, tzinfo=timezone.utc))
    included, excluded = filter_context_items([item], asof=ASOF)
    assert [row.source_ref for row in included] == ["timed"]
    assert excluded == ()
