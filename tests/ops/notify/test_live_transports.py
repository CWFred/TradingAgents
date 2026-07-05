"""Opt-in live-network tests against real Pushover and SMTP.

Gated on OPS_NOTIFY_LIVE_TESTS=1. Requires transport config (OPS_PUSHOVER_*,
OPS_SMTP_*). Skipped by default to avoid network calls in the default test suite.
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPS_NOTIFY_LIVE_TESTS") != "1",
    reason="opt-in: set OPS_NOTIFY_LIVE_TESTS=1 to hit real Pushover/SMTP",
)


def test_live_pushover_send():
    """Send a smoke-test notification to real Pushover.

    Requires OPS_PUSHOVER_USER_KEY and OPS_PUSHOVER_APP_TOKEN configured.
    """
    from ops.notify.config import load_notify_config
    from ops.notify.push import build_push_transport
    from ops.notify.transport import NotifyMessage
    t = build_push_transport(load_notify_config())
    assert t.enabled, "configure OPS_PUSHOVER_* to run this"
    t.send(NotifyMessage(title="ops live test", body="pushover smoke", urgency="normal"))


def test_live_smtp_send():
    """Send a smoke-test notification to real SMTP.

    Requires OPS_SMTP_* config (host, port, from, to, credentials).
    """
    from ops.notify.config import load_notify_config
    from ops.notify.email import build_email_transport
    from ops.notify.transport import NotifyMessage
    t = build_email_transport(load_notify_config())
    assert t.enabled, "configure OPS_SMTP_* to run this"
    t.send(NotifyMessage(title="ops live test", body="smtp smoke", urgency="normal"))
