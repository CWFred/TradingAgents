from unittest.mock import MagicMock
from ops.notify.config import NotifyConfig
from ops.notify.transport import NotifyMessage
from ops.notify.push import build_push_transport, PushoverTransport


def test_disabled_without_creds():
    t = build_push_transport(NotifyConfig())
    assert t.enabled is False


def test_posts_to_pushover(monkeypatch):
    cfg = NotifyConfig(pushover_user_key="uk", pushover_app_token="at")
    t = build_push_transport(cfg)
    assert t.enabled is True and isinstance(t, PushoverTransport)
    fake_resp = MagicMock(status_code=200)
    fake_resp.raise_for_status.return_value = None
    post = MagicMock(return_value=fake_resp)
    monkeypatch.setattr("ops.notify.push.requests.post", post)
    t.send(NotifyMessage(title="Fill", body="AAPL filled", urgency="high"))
    args, kwargs = post.call_args
    assert args[0] == "https://api.pushover.net/1/messages.json"
    assert kwargs["data"]["token"] == "at" and kwargs["data"]["user"] == "uk"
    assert kwargs["data"]["title"] == "Fill" and kwargs["data"]["priority"] == 1
