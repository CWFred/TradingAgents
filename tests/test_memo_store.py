"""Unit tests for the structured memo schema and SQLite store."""

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from tradingagents.memos import (
    EventThesis,
    EvidenceItem,
    Falsifier,
    Memo,
    MemoStore,
    Resolution,
    ValueThesis,
)

pytestmark = pytest.mark.unit


def _value_memo(**overrides) -> Memo:
    defaults = {
        "ticker": "acme",
        "as_of_date": date(2026, 7, 1),
        "thesis_type": "value",
        "thesis": "Cheap quality compounder with a fresh CEO and depressed screen optics.",
        "evidence": [
            EvidenceItem(
                claim="ROIC averaged 15% over FY21-FY25.",
                source_type="filing",
                source_ref="0001234567-26-000010:10-K item 7",
            )
        ],
        "value_block": ValueThesis(
            why_cheap="Guidance cut last quarter read as structural; it was a one-time contract loss.",
            change_trigger="CEO change plus a three-insider buying cluster in June 2026.",
            normalized_earnings_view="Through-cycle EBIT ~USD 60M vs USD 38M optical LTM.",
            quality_assessment="Net cash, 55% gross margins stable for a decade.",
        ),
        "conviction_tier": "medium",
        "entry_price_ref": 18.40,
        "price_target_low": 26.0,
        "price_target_high": 34.0,
        "expected_holding_months": 18,
        "must_be_true": ["Margins recover to 12% EBIT within 4 quarters."],
        "falsifiers": [
            Falsifier(
                description="Gross margin below 50% for two consecutive quarters.",
                check_type="fundamental",
                metric="gross_margin_pct",
                operator="<",
                threshold=50.0,
                consecutive_periods=2,
            )
        ],
    }
    defaults.update(overrides)
    return Memo(**defaults)


def _event_memo(**overrides) -> Memo:
    defaults = {
        "ticker": "SPUN",
        "as_of_date": date(2026, 7, 1),
        "thesis_type": "event",
        "thesis": "Orphaned spinoff being dumped by index funds; worth 2x post-flowback.",
        "evidence": [
            EvidenceItem(
                claim="Form 10 shows segment EBIT of USD 90M against a USD 400M market cap.",
                source_type="filing",
                source_ref="0007654321-26-000003:10-12B info statement",
            )
        ],
        "event_block": EventThesis(
            event_type="spinoff",
            seller_identity="S&P 500 index funds receiving unwanted small-cap shares.",
            why_non_economic="Parent is an index member; recipients must sell regardless of price.",
            pressure_end_estimate=date(2026, 10, 15),
        ),
        "conviction_tier": "high",
        "entry_price_ref": 12.0,
        "price_target_low": 20.0,
        "price_target_high": 26.0,
        "expected_holding_months": 6,
        "must_be_true": ["Selling pressure abates within two quarters of distribution."],
        "falsifiers": [
            Falsifier(
                description="Distribution terms amended to include a larger float.",
                check_type="event",
            )
        ],
    }
    defaults.update(overrides)
    return Memo(**defaults)


def _resolution(**overrides) -> Resolution:
    defaults = {
        "resolved_at": datetime(2027, 1, 15, tzinfo=timezone.utc),
        "exit_price": 24.0,
        "realized_return_pct": 0.30,
        "benchmark_return_pct": 0.08,
        "holding_days": 198,
        "outcome_label": "thesis_right_made_money",
        "narrative": "Flowback ended in October as projected; rerated on first standalone print.",
    }
    defaults.update(overrides)
    return Resolution(**defaults)


@pytest.fixture
def store(tmp_path):
    return MemoStore(tmp_path / "memos.db")


class TestSchema:
    def test_ticker_uppercased_on_save_roundtrip(self, store):
        memo = _value_memo()
        store.save(memo)
        assert store.list(ticker="ACME")[0].memo_id == memo.memo_id

    def test_block_must_match_thesis_type(self, store):
        mismatched = _value_memo(value_block=None)  # value type without a value block
        with pytest.raises(ValueError, match="thesis_type"):
            store.save(mismatched)

    def test_both_blocks_rejected(self, store):
        memo = _event_memo(value_block=_value_memo().value_block)
        with pytest.raises(ValueError, match="exactly one"):
            store.save(memo)

    def test_requires_falsifier_and_evidence(self):
        with pytest.raises(ValidationError):
            _value_memo(falsifiers=[])
        with pytest.raises(ValidationError):
            _value_memo(evidence=[])


class TestStore:
    def test_roundtrip_preserves_full_payload(self, store):
        memo = _event_memo()
        store.save(memo)
        loaded = store.get(memo.memo_id)
        assert loaded == memo
        assert loaded.event_block.pressure_end_estimate == date(2026, 10, 15)

    def test_get_missing_returns_none(self, store):
        assert store.get("nope") is None

    def test_duplicate_save_rejected(self, store):
        memo = _value_memo()
        store.save(memo)
        with pytest.raises(sqlite3.IntegrityError):
            store.save(memo)

    def test_list_filters_compose(self, store):
        store.save(_value_memo())
        store.save(_event_memo())
        assert len(store.list()) == 2
        assert [m.ticker for m in store.list(thesis_type="event")] == ["SPUN"]
        assert store.list(ticker="acme", thesis_type="event") == []

    def test_resolve_updates_status_and_payload(self, store):
        memo = _event_memo()
        store.save(memo)
        resolved = store.resolve(memo.memo_id, _resolution())
        assert resolved.status == "resolved"
        assert store.get(memo.memo_id).resolution.outcome_label == "thesis_right_made_money"
        assert store.open_memos() == []

    def test_double_resolve_rejected(self, store):
        memo = _value_memo()
        store.save(memo)
        store.resolve(memo.memo_id, _resolution())
        with pytest.raises(ValueError, match="already resolved"):
            store.resolve(memo.memo_id, _resolution())

    def test_resolve_unknown_memo_raises(self, store):
        with pytest.raises(KeyError):
            store.resolve("missing", _resolution())

    def test_passed_memos_still_resolvable(self, store):
        memo = _value_memo()
        store.save(memo)
        store.mark_passed(memo.memo_id)
        assert store.get(memo.memo_id).status == "passed"
        assert store.open_memos() == []
        store.resolve(memo.memo_id, _resolution(exit_price=None))
        assert store.get(memo.memo_id).status == "resolved"

    def test_resolved_corpus_oldest_first(self, store):
        older = _value_memo(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        newer = _event_memo(created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
        store.save(newer)
        store.save(older)
        store.resolve(older.memo_id, _resolution())
        store.resolve(newer.memo_id, _resolution())
        assert [m.memo_id for m in store.resolved_corpus()] == [older.memo_id, newer.memo_id]

    def test_due_for_resolution_uses_expected_holding(self, store):
        created = datetime.now(timezone.utc) - timedelta(days=200)
        due = _event_memo(created_at=created)  # 6 months expected -> overdue at 200 days
        fresh = _value_memo(created_at=created)  # 18 months expected -> not due
        store.save(due)
        store.save(fresh)
        assert [m.memo_id for m in store.due_for_resolution()] == [due.memo_id]


def test_memo_without_authored_by_model_deserializes(tmp_path):
    # Simulate a pre-Phase-D stored payload: dump a memo, strip the new field,
    # write the row back raw, read it through the store.
    import json
    import sqlite3

    store = MemoStore(tmp_path / "memos.sqlite")
    memo = _value_memo()
    store.save(memo)
    payload = json.loads(memo.model_dump_json())
    payload.pop("authored_by_model", None)
    with sqlite3.connect(tmp_path / "memos.sqlite") as conn:
        conn.execute("UPDATE memos SET payload = ? WHERE memo_id = ?",
                     (json.dumps(payload), memo.memo_id))
    loaded = store.get(memo.memo_id)
    assert loaded is not None
    assert loaded.authored_by_model == ""


def test_default_memo_store_path_env_override(monkeypatch):
    from tradingagents.memos.store import default_memo_store_path

    monkeypatch.setenv("OPS_MEMO_STORE_PATH", "/tmp/custom-memos.sqlite")
    assert default_memo_store_path() == "/tmp/custom-memos.sqlite"
    monkeypatch.delenv("OPS_MEMO_STORE_PATH")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/state")
    assert default_memo_store_path() == "/tmp/state/tradingagents/memos.sqlite"
