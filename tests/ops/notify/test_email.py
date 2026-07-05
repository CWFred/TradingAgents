import ssl
from unittest.mock import MagicMock

from ops.notify.config import NotifyConfig
from ops.notify.email import EmailTransport, build_email_transport
from ops.notify.transport import NotifyMessage


def test_disabled_without_host():
    assert build_email_transport(NotifyConfig()).enabled is False


def test_sends_via_smtp(monkeypatch):
    cfg = NotifyConfig(
        smtp_host="smtp.example.com", smtp_port=587,
        smtp_user="u", smtp_password="p",
        smtp_from="from@example.com", smtp_to="to@example.com",
    )
    t = build_email_transport(cfg)
    assert t.enabled is True and isinstance(t, EmailTransport)

    smtp_instance = MagicMock()
    smtp_ctx = MagicMock()
    smtp_ctx.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_ctx.__exit__ = MagicMock(return_value=False)
    smtp_cls = MagicMock(return_value=smtp_ctx)
    monkeypatch.setattr("ops.notify.email.smtplib.SMTP", smtp_cls)

    t.send(NotifyMessage(title="Daily", body="summary"))
    smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=20)
    # M5: starttls must be called with an SSL context that verifies.
    call_args = smtp_instance.starttls.call_args
    assert call_args is not None
    context = call_args[1]["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    smtp_instance.login.assert_called_once_with("u", "p")
    sent_msg = smtp_instance.send_message.call_args[0][0]
    assert sent_msg["Subject"] == "Daily"
    assert sent_msg["From"] == "from@example.com" and sent_msg["To"] == "to@example.com"
