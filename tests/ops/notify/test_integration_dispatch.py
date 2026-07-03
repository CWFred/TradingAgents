"""Integration test: restart-resume from cursor without duplicates.

Verifies that after a restart (new Journal + dispatcher over the same sqlite path),
already-acked events are NOT resent, and new events ARE.
"""
from ops.journal import Journal
from ops.notify.transport import NotifyMessage
from ops.notify.dispatcher import NotifyDispatcher


class FakeTransport:
    enabled = True
    def __init__(self):
        self.sent = []
    def send(self, m: NotifyMessage):
        self.sent.append(m)


def test_restart_resumes_from_cursor_no_duplicates(tmp_path):
    """After restart, already-acked events are not resent; new events are."""
    path = str(tmp_path / "j.sqlite")
    j = Journal(path)
    j.record_event("fill", {"symbol": "AAPL", "side": "BUY",
                            "quantity": "0.1", "price": "200", "context": "place"})
    push = FakeTransport()
    NotifyDispatcher(j, {"push": push, "email": FakeTransport()}).dispatch_once()
    assert len(push.sent) == 1
    j.close()

    # "restart": brand-new Journal + dispatcher over the same file
    j2 = Journal(path)
    push2 = FakeTransport()
    d2 = NotifyDispatcher(j2, {"push": push2, "email": FakeTransport()})
    assert d2.dispatch_once() == 0          # already-acked event not resent
    j2.record_event("fill", {"symbol": "MSFT", "side": "BUY",
                             "quantity": "0.1", "price": "300", "context": "place"})
    assert d2.dispatch_once() == 1          # only the new one
    assert len(push2.sent) == 1
