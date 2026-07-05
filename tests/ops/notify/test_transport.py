from ops.notify.transport import DisabledTransport, NotifyMessage


def test_notify_message_defaults():
    m = NotifyMessage(title="t", body="b")
    assert m.urgency == "normal"


def test_disabled_transport_is_noop():
    t = DisabledTransport("no creds")
    assert t.enabled is False
    t.send(NotifyMessage(title="t", body="b"))  # must not raise
