from datetime import datetime, timezone
from ops.journal import Journal
from ops.live_gate import (
    record_flip_marker, flip_epoch, count_live_buy_fills, MARKER_KIND,
)


def test_marker_recorded_once(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    assert record_flip_marker(j) is True
    assert record_flip_marker(j) is False   # idempotent
    assert len([e for e in j.read_events() if e["kind"] == MARKER_KIND]) == 1
    assert flip_epoch(j) is not None


def test_count_zero_without_marker(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("fill", {"side": "BUY", "symbol": "AAPL"})
    assert count_live_buy_fills(j) == 0     # no flip marker => gate fully active


def test_counts_buy_fills_after_marker_only(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("fill", {"side": "BUY", "symbol": "PRE"})   # before marker
    record_flip_marker(j)
    j.record_event("fill", {"side": "BUY", "symbol": "AAPL"})
    j.record_event("fill", {"side": "SELL", "symbol": "AAPL"})  # SELL doesn't count
    j.record_event("fill", {"side": "BUY", "symbol": "MSFT"})
    assert count_live_buy_fills(j) == 2
