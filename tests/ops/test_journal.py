import pytest
from datetime import datetime, timezone
from decimal import Decimal
from ops.journal import Journal


def test_journal_records_and_reads_event(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("test_kind", {"foo": "bar", "n": 1})
    events = j.read_events()
    assert len(events) == 1
    assert events[0]["kind"] == "test_kind"
    assert events[0]["payload"] == {"foo": "bar", "n": 1}
    assert isinstance(events[0]["at"], datetime)


def test_journal_records_order_and_fill(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_order(
        client_order_id="cid-1", symbol="AAPL", side="BUY",
        notional_dollars=Decimal("25.00"), stop_loss_price=Decimal("180.00"),
    )
    j.record_fill(
        order_id="oid-1", client_order_id="cid-1", symbol="AAPL", side="BUY",
        quantity=Decimal("0.1245"), price=Decimal("200.80"),
        filled_at=datetime(2026, 6, 30, 14, 30, tzinfo=timezone.utc),
    )
    orders = j.read_orders()
    fills = j.read_fills()
    assert orders[0]["symbol"] == "AAPL"
    assert orders[0]["notional_dollars"] == Decimal("25.00")
    assert fills[0]["price"] == Decimal("200.80")


def test_journal_records_equity_snapshot(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_equity_snapshot(
        kind="manual",
        at=datetime(2026, 6, 30, 13, 30, tzinfo=timezone.utc),
        equity=Decimal("250.00"), cash=Decimal("250.00"),
    )
    snaps = j.read_equity_snapshots()
    assert snaps[0]["equity"] == Decimal("250.00")


def test_record_fill_rejects_naive_datetime(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    naive = datetime(2026, 6, 30, 14, 30)
    with pytest.raises(ValueError, match="naive"):
        j.record_fill(
            order_id="oid", client_order_id="cid", symbol="AAPL", side="BUY",
            quantity=Decimal("1"), price=Decimal("100"),
            filled_at=naive,
        )


def test_record_equity_snapshot_rejects_naive_datetime(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    naive = datetime(2026, 6, 30, 13, 30)
    with pytest.raises(ValueError, match="naive"):
        j.record_equity_snapshot(kind="manual", at=naive, equity=Decimal("250"), cash=Decimal("250"))


def test_record_and_get_latest_equity_snapshot_by_kind(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    ts1 = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
    ts2 = datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)
    j.record_equity_snapshot(kind="open_day", equity=Decimal("250"), cash=Decimal("250"), at=ts1)
    j.record_equity_snapshot(kind="open_day", equity=Decimal("245"), cash=Decimal("100"), at=ts2)
    j.record_equity_snapshot(kind="open_week", equity=Decimal("250"), cash=Decimal("250"), at=ts1)
    latest_day = j.get_latest_equity_snapshot(kind="open_day")
    assert latest_day.equity == Decimal("245")
    assert latest_day.at == ts2
    latest_week = j.get_latest_equity_snapshot(kind="open_week")
    assert latest_week.equity == Decimal("250")


def test_get_latest_equity_snapshot_since_filter(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    old = datetime(2026, 6, 25, 13, 30, tzinfo=timezone.utc)
    new = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
    j.record_equity_snapshot(kind="open_week", equity=Decimal("250"), cash=Decimal("250"), at=old)
    j.record_equity_snapshot(kind="open_week", equity=Decimal("240"), cash=Decimal("240"), at=new)
    # Query "since Monday 2026-06-29" — should get the new one only.
    monday = datetime(2026, 6, 29, tzinfo=timezone.utc)
    latest = j.get_latest_equity_snapshot(kind="open_week", since=monday)
    assert latest.at == new


def test_get_latest_equity_snapshot_none_when_empty(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    assert j.get_latest_equity_snapshot(kind="open_day") is None


def test_equity_snapshot_note_preserved(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_equity_snapshot(
        kind="manual", equity=Decimal("100"), cash=Decimal("50"),
        note="pre-migration snapshot",
    )
    latest = j.get_latest_equity_snapshot(kind="manual")
    assert latest.note == "pre-migration snapshot"


def test_equity_snapshot_schema_migrates_pre_existing_db(tmp_path):
    """A DB created before this change (no kind column) should be usable
    after Journal(path) reopens it."""
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE equity_snapshots ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  at TEXT NOT NULL,"
        "  equity TEXT NOT NULL,"
        "  cash TEXT NOT NULL"
        ")"
    )
    conn.close()
    j = Journal(path)  # migration runs
    j.record_equity_snapshot(kind="open_day", equity=Decimal("10"), cash=Decimal("10"))
    assert j.get_latest_equity_snapshot(kind="open_day") is not None


def test_context_manager_closes_connection(tmp_path):
    path = str(tmp_path / "j.sqlite")
    with Journal(path) as j:
        j.record_event("k", {})
    # After exiting, a second connection should still be able to open and read
    j2 = Journal(path)
    assert len(j2.read_events()) == 1
    j2.close()
