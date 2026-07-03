from datetime import datetime, timezone, timedelta
from ops.journal import Journal
from ops.notify.transport import NotifyMessage
from ops.notify.dispatcher import NotifyDispatcher


class FakeTransport:
    enabled = True
    def __init__(self, fail=False, fail_times=None):
        self.sent = []
        self._fail = fail
        # When set, an int count of leading calls that raise before
        # succeeding (fail-once-then-succeed pattern).
        self._fail_times = fail_times
        self._calls = 0
    def send(self, message: NotifyMessage) -> None:
        self._calls += 1
        if self._fail or (self._fail_times is not None and self._calls <= self._fail_times):
            raise RuntimeError("transport down: password=hunter2 host=smtp.internal.example.com")
        self.sent.append(message)


def _clock(t):
    return lambda: t


def test_routes_by_policy_and_advances_cursor(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("fill", {"symbol": "AAPL", "side": "BUY",
                            "quantity": "0.1", "price": "200", "context": "place"})
    j.record_event("order_rejected", {"symbol": "AAPL"})  # not notified
    push, email = FakeTransport(), FakeTransport()
    d = NotifyDispatcher(j, {"push": push, "email": email})
    sent = d.dispatch_once()
    assert sent == 1                       # only the fill, push channel
    assert len(push.sent) == 1 and len(email.sent) == 0
    assert j.get_cursor("notify") == 2     # advanced past both events
    assert d.dispatch_once() == 0          # nothing new


def test_failure_holds_cursor_and_journals_error(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("kill_switch", {"reason": "weekly -15%"})
    push = FakeTransport(fail=True)
    email = FakeTransport()
    d = NotifyDispatcher(j, {"push": push, "email": email})
    d.dispatch_once()
    assert j.get_cursor("notify") == 0     # NOT advanced past the failed event
    errs = [e for e in j.read_events() if e["kind"] == "notify_dispatch_error"]
    assert len(errs) == 1


def test_dispatch_error_payload_is_sanitized(tmp_path):
    """The notify_dispatch_error payload must never leak transport exception
    text (which can contain credentials/hostnames from smtplib/requests).
    Only the exception TYPE name, plus event id/kind, may be journaled."""
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("kill_switch", {"reason": "weekly -15%"})
    push = FakeTransport(fail=True)
    email = FakeTransport()
    d = NotifyDispatcher(j, {"push": push, "email": email})
    d.dispatch_once()
    errs = [e for e in j.read_events() if e["kind"] == "notify_dispatch_error"]
    assert len(errs) == 1
    payload = errs[0]["payload"]
    assert payload == {"event_id": 1, "kind": "kill_switch", "error_type": "RuntimeError"}
    serialized = str(payload)
    assert "hunter2" not in serialized
    assert "smtp.internal.example.com" not in serialized
    assert "transport down" not in serialized


def test_cooldown_suppresses_repeat_but_advances(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    t0 = datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
    j.record_event("broker_unreachable", {"err": "timeout"})
    j.record_event("broker_unreachable", {"err": "timeout"})
    email = FakeTransport()
    d = NotifyDispatcher(j, {"push": FakeTransport(), "email": email}, now=_clock(t0))
    d.dispatch_once()
    assert len(email.sent) == 1            # second suppressed by cooldown
    assert j.get_cursor("notify") == 2     # both events consumed


def test_failed_throttled_send_does_not_arm_cooldown_and_is_retried(tmp_path):
    """A throttled-kind event whose send fails must NOT arm the cooldown —
    otherwise the retry on the next dispatch_once (still within the cooldown
    window the failed attempt would have armed) is wrongly suppressed and
    the cursor advances past it, dropping the alert (at-least-once
    violation). The cooldown must only arm after a send that completes
    without raising."""
    j = Journal(str(tmp_path / "j.sqlite"))
    t0 = datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
    j.record_event("broker_unreachable", {"err": "timeout"})
    email = FakeTransport(fail_times=1)  # fails on 1st send, succeeds on 2nd
    d = NotifyDispatcher(j, {"push": FakeTransport(), "email": email},
                         now=_clock(t0))

    d.dispatch_once()                      # send raises; cursor held
    assert j.get_cursor("notify") == 0
    assert len(email.sent) == 0

    d.dispatch_once()                      # retry, still "now" == t0
    # The failed first attempt also journaled a notify_dispatch_error event
    # (id 2), consumed alongside the retried broker_unreachable (id 1) on
    # this second call, so the cursor advances past both.
    assert j.get_cursor("notify") == 2
    assert len(email.sent) == 1            # delivered exactly once overall
