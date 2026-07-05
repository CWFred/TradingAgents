from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ops.journal import Journal


def test_journal_creates_missing_parent_directories(tmp_path):
    """The default journal_path now lives under an XDG state directory that
    may not exist yet on first run (e.g. ~/.local/state/tradingagents/).
    Journal must create it on open rather than raising sqlite3.OperationalError."""
    nested = tmp_path / "state" / "tradingagents" / "ops_journal.sqlite"
    assert not nested.parent.exists()
    j = Journal(str(nested))
    assert nested.parent.exists()
    j.record_event("test_kind", {"ok": True})
    j.close()


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


def test_fills_gain_stop_loss_price_column(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    ts = datetime(2026, 7, 2, 14, tzinfo=timezone.utc)
    j.record_fill(
        order_id="o-1", client_order_id="c-1", symbol="AAPL",
        side="BUY", quantity=Decimal("5"), price=Decimal("10"),
        filled_at=ts, stop_loss_price=Decimal("9.2"),
    )
    fills = j.read_fills()
    assert fills[0]["stop_loss_price"] == Decimal("9.2")


def test_record_fill_stop_loss_price_default_none(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    ts = datetime(2026, 7, 2, 14, tzinfo=timezone.utc)
    j.record_fill(
        order_id="o-1", client_order_id="c-1", symbol="AAPL",
        side="SELL", quantity=Decimal("5"), price=Decimal("10"),
        filled_at=ts,
    )
    assert j.read_fills()[0]["stop_loss_price"] is None


def test_last_buy_fill_for_returns_most_recent_buy(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    older = datetime(2026, 6, 30, tzinfo=timezone.utc)
    newer = datetime(2026, 7, 2, tzinfo=timezone.utc)
    j.record_fill(order_id="o-1", client_order_id="c-1", symbol="AAPL",
                  side="BUY", quantity=Decimal("5"), price=Decimal("10"),
                  filled_at=older, stop_loss_price=Decimal("9"))
    j.record_fill(order_id="o-2", client_order_id="c-2", symbol="AAPL",
                  side="BUY", quantity=Decimal("3"), price=Decimal("11"),
                  filled_at=newer, stop_loss_price=Decimal("10.1"))
    last = j.last_buy_fill_for("AAPL")
    assert last["stop_loss_price"] == Decimal("10.1")
    assert last["filled_at"] == newer


def test_last_buy_fill_for_none_when_missing(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    assert j.last_buy_fill_for("AAPL") is None


def test_last_buy_fill_for_ignores_sells(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    ts = datetime(2026, 7, 2, tzinfo=timezone.utc)
    j.record_fill(order_id="o-1", client_order_id="c-1", symbol="AAPL",
                  side="SELL", quantity=Decimal("5"), price=Decimal("10"),
                  filled_at=ts)
    assert j.last_buy_fill_for("AAPL") is None


def test_has_event_today_true_when_event_today(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("daily_halt", {"reason": "drawdown"})
    now = datetime.now(timezone.utc)
    assert j.has_event_today("daily_halt", now=now) is True


def test_has_event_today_false_when_no_event_today(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    now = datetime.now(timezone.utc)
    assert j.has_event_today("daily_halt", now=now) is False


def test_has_event_since_last_monday_true(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("kill_switch", {"reason": "weekly"})
    # 2026-07-02 is a Thursday; last Monday is 2026-06-29.
    now = datetime(2026, 7, 2, 15, tzinfo=timezone.utc)
    assert j.has_event_since_last_monday("kill_switch", now=now) is True


def test_migrates_pre_existing_fills_without_stop_column(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            order_id TEXT NOT NULL,
            client_order_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity TEXT NOT NULL,
            price TEXT NOT NULL,
            filled_at TEXT NOT NULL
        );
    """)
    conn.close()
    j = Journal(path)
    ts = datetime(2026, 7, 2, tzinfo=timezone.utc)
    j.record_fill(order_id="o-1", client_order_id="c-1", symbol="AAPL",
                  side="BUY", quantity=Decimal("1"), price=Decimal("10"),
                  filled_at=ts, stop_loss_price=Decimal("9"))
    assert j.read_fills()[0]["stop_loss_price"] == Decimal("9")


def test_read_events_since_returns_id_and_filters(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("a", {"n": 1})
    j.record_event("b", {"n": 2})
    j.record_event("c", {"n": 3})
    all_ev = j.read_events_since(0)
    assert [e["kind"] for e in all_ev] == ["a", "b", "c"]
    assert all_ev[0]["id"] == 1 and all_ev[2]["id"] == 3
    # only rows after id=1
    after = j.read_events_since(1)
    assert [e["kind"] for e in after] == ["b", "c"]
    # limit
    assert len(j.read_events_since(0, limit=2)) == 2


def test_dispatch_cursor_roundtrip_and_default(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    assert j.get_cursor("notify") == 0          # default when absent
    j.set_cursor("notify", 5)
    assert j.get_cursor("notify") == 5
    j.set_cursor("notify", 9)                    # upsert, not duplicate
    assert j.get_cursor("notify") == 9


def test_cash_adjustment_roundtrip(tmp_path):
    from decimal import Decimal
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_cash_adjustment(kind="seed", amount=Decimal("250"), note="initial")
    j.record_cash_adjustment(kind="deposit", amount=Decimal("100.50"))
    adjs = j.read_cash_adjustments()
    assert len(adjs) == 2
    assert adjs[0]["kind"] == "seed" and adjs[0]["amount"] == Decimal("250")
    assert adjs[0]["note"] == "initial"
    assert adjs[1]["kind"] == "deposit" and adjs[1]["amount"] == Decimal("100.50")
    assert adjs[1]["at"].tzinfo is not None


def test_duplicate_client_order_id_raises_integrity_error(tmp_path):
    """M3: client_order_id is meant to be a unique idempotency key. A second
    order row with the same client_order_id must be a write-time error, not
    silent replay corruption (journal replay keys fills/orders by
    client_order_id — last write wins on a collision)."""
    import sqlite3

    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_order(
        client_order_id="dupe-1", symbol="AAPL", side="BUY",
        notional_dollars=Decimal("25.00"), stop_loss_price=None,
    )
    with pytest.raises(sqlite3.IntegrityError):
        j.record_order(
            client_order_id="dupe-1", symbol="MSFT", side="BUY",
            notional_dollars=Decimal("30.00"), stop_loss_price=None,
        )


def test_cash_adjustment_migrates_existing_db(tmp_path):
    """A journal created before cash_adjustments existed must gain the table
    on reopen (same defensive-migration pattern as the other tables)."""
    import sqlite3
    from decimal import Decimal
    path = str(tmp_path / "old.sqlite")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)")
    conn.commit()
    conn.close()
    j = Journal(path)
    j.record_cash_adjustment(kind="seed", amount=Decimal("250"))
    assert j.read_cash_adjustments()[0]["amount"] == Decimal("250")


def test_to_iso_normalizes_non_utc_offsets_for_string_comparison(tmp_path):
    """L3: journal queries compare ISO strings lexicographically, which only
    works if every stored/compared timestamp has the same UTC offset. An
    ET-aware datetime passed as `at=` must be normalized to UTC, not stored
    with its -04:00 offset (which sorts before any +00:00 string of the
    same day and silently corrupts >= comparisons)."""
    from datetime import datetime, timezone
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    j = Journal(str(tmp_path / "j.sqlite"))
    # 09:30 ET == 13:30 UTC on 2026-07-06 (EDT)
    et = datetime(2026, 7, 6, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    j.record_equity_snapshot(kind="open_day", equity=Decimal("250"),
                             cash=Decimal("250"), at=et)
    # A since= of 13:00 UTC is BEFORE the snapshot instant; the snapshot
    # must be found. (With offset-preserving storage, "…T09:30:00-04:00"
    # compares lexicographically below "…T13:00:00+00:00" and is missed.)
    since = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
    snap = j.get_latest_equity_snapshot(kind="open_day", since=since)
    assert snap is not None
    assert snap.equity == Decimal("250")
    # And a since= AFTER the instant must exclude it.
    after = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
    assert j.get_latest_equity_snapshot(kind="open_day", since=after) is None


def test_last_event_id_before(tmp_path):
    from datetime import datetime, timedelta, timezone
    j = Journal(str(tmp_path / "j.sqlite"))
    assert j.last_event_id_before(datetime.now(timezone.utc)) is None
    j.record_event("a", {})
    j.record_event("b", {})
    future = datetime.now(timezone.utc) + timedelta(seconds=1)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert j.last_event_id_before(future) == 2
    assert j.last_event_id_before(past) is None


def test_first_event_at_and_count_events(tmp_path):
    """L2: SQL-side event lookups for the live gate — no full-table scan +
    JSON parse per rule evaluation."""
    from datetime import timedelta
    from decimal import Decimal  # noqa: F401 - parity with sibling tests
    j = Journal(str(tmp_path / "j.sqlite"))
    assert j.first_event_at("broker_mode_live") is None
    j.record_event("broker_mode_live", {"note": "flip"})
    epoch = j.first_event_at("broker_mode_live")
    assert epoch is not None and epoch.tzinfo is not None

    j.record_event("fill", {"side": "BUY", "broker_mode": "robinhood"})
    j.record_event("fill", {"side": "SELL", "broker_mode": "robinhood"})
    j.record_event("fill", {"side": "BUY", "broker_mode": "paper"})
    j.record_event("fill", {"side": "BUY"})  # historical: no broker_mode

    assert j.count_events(
        "fill", since=epoch,
        payload_equals={"side": "BUY", "broker_mode": "robinhood"},
    ) == 1
    assert j.count_events("fill", since=epoch) == 4
    assert j.count_events(
        "fill", since=epoch + timedelta(days=1),
        payload_equals={"side": "BUY"},
    ) == 0


def test_count_events_rejects_unsafe_payload_keys(tmp_path):
    import pytest
    j = Journal(str(tmp_path / "j.sqlite"))
    with pytest.raises(ValueError):
        j.count_events("fill", payload_equals={"side') OR 1=1 --": "BUY"})


def test_last_event_returns_most_recent_of_kind(tmp_path):
    """`ops status` (A4) reports the last service_started/service_stopping
    and the last anomaly of each kind — a single-row indexed query, not a
    Python-side scan of read_events()."""
    j = Journal(str(tmp_path / "j.sqlite"))
    assert j.last_event("service_started") is None
    j.record_event("service_started", {"pid": 1})
    j.record_event("other_kind", {"x": 1})
    j.record_event("service_started", {"pid": 2})
    last = j.last_event("service_started")
    assert last is not None
    assert last["kind"] == "service_started"
    assert last["payload"] == {"pid": 2}
    assert isinstance(last["at"], datetime) and last["at"].tzinfo is not None


def test_journal_path_property(tmp_path):
    """`ops status` prints the journal it actually opened (which may be a
    --journal override, not config.journal_path), so the path must be
    readable off the Journal itself."""
    p = str(tmp_path / "j.sqlite")
    j = Journal(p)
    assert j.path == p
