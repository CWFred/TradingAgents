"""Journal(readonly=True): the dashboard's hard mode=ro guarantee."""
import sqlite3

import pytest

from ops.journal import Journal


def _seed(path: str) -> None:
    with Journal(path) as j:
        j.record_event("service_started", {"pid": 1})


def test_readonly_reads_existing_journal(tmp_path):
    p = str(tmp_path / "j.sqlite")
    _seed(p)
    ro = Journal(p, readonly=True)
    try:
        events = ro.read_events()
        assert len(events) == 1
        assert events[0]["kind"] == "service_started"
    finally:
        ro.close()


def test_readonly_rejects_writes(tmp_path):
    p = str(tmp_path / "j.sqlite")
    _seed(p)
    ro = Journal(p, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.record_event("fill", {"symbol": "XYZ"})
    finally:
        ro.close()


def test_readonly_missing_file_raises_not_creates(tmp_path):
    p = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        Journal(str(p), readonly=True)
    assert not p.exists()  # ro open must not have created the file


def test_readonly_concurrent_with_writer(tmp_path):
    """WAL: a ro reader sees committed writes from a live rw connection."""
    p = str(tmp_path / "j.sqlite")
    rw = Journal(p)
    ro = Journal(p, readonly=True)
    try:
        rw.record_event("fill", {"symbol": "ABC"})
        assert any(e["kind"] == "fill" for e in ro.read_events())
    finally:
        ro.close()
        rw.close()
