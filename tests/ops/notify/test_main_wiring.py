from decimal import Decimal

from ops.config import OpsConfig
from ops.journal import Journal
from ops.live_gate import MARKER_KIND, record_flip_marker
from ops.main import _build_dispatcher, _notify_tick, _daily_summary_tick


def test_build_dispatcher_returns_dispatcher(tmp_path, monkeypatch):
    monkeypatch.delenv("OPS_PUSHOVER_USER_KEY", raising=False)
    j = Journal(str(tmp_path / "j.sqlite"))
    d = _build_dispatcher(j)
    assert d is not None
    # transports disabled without creds, but dispatch must not raise
    j.record_event("fill", {"symbol": "AAPL", "side": "BUY",
                            "quantity": "0.1", "price": "200", "context": "place"})
    _notify_tick(d)                       # wrapped; must not raise
    assert j.get_cursor("notify") == 1    # advanced even with disabled transports


def test_build_dispatcher_honors_notify_enabled_false(tmp_path, monkeypatch):
    """When OPS_NOTIFY_ENABLED is unset/false, the dispatcher must be built
    with disabled transports regardless of credentials — nothing is
    delivered even if push/email creds are present — but dispatching still
    advances the cursor (the event is consumed, not held)."""
    monkeypatch.delenv("OPS_NOTIFY_ENABLED", raising=False)
    monkeypatch.setenv("OPS_PUSHOVER_USER_KEY", "user-key")
    monkeypatch.setenv("OPS_PUSHOVER_APP_TOKEN", "app-token")
    j = Journal(str(tmp_path / "j.sqlite"))
    d = _build_dispatcher(j)
    j.record_event("fill", {"symbol": "AAPL", "side": "BUY",
                            "quantity": "0.1", "price": "200", "context": "place"})
    n = d.dispatch_once()
    assert n == 0                          # nothing delivered
    assert j.get_cursor("notify") == 1     # cursor still advances


def test_notify_tick_swallows_errors(tmp_path):
    class Boom:
        def dispatch_once(self):
            raise RuntimeError("kaboom")
    _notify_tick(Boom())                  # must not raise


def test_flip_marker_written_in_robinhood_mode(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    assert record_flip_marker(j) is True
    assert len([e for e in j.read_events() if e["kind"] == MARKER_KIND]) == 1


def test_daily_summary_tick_swallows_errors(tmp_path):
    """A broker/journal error inside emit_daily_summary must not raise; it
    is recorded as a daily_summary_error event instead (scheduler-safe)."""
    j = Journal(str(tmp_path / "j.sqlite"))

    class BoomBroker:
        def get_equity(self):
            raise RuntimeError("boom")

    _daily_summary_tick(j, BoomBroker())  # must not raise
    kinds = [e["kind"] for e in j.read_events()]
    assert "daily_summary_error" in kinds
