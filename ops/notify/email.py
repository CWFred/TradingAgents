"""SMTP email transport (synchronous, stdlib smtplib)."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from ops.notify.config import NotifyConfig
from ops.notify.transport import DisabledTransport, NotifyMessage, Transport

_TIMEOUT = 20


class EmailTransport:
    enabled = True

    def __init__(self, *, host: str, port: int, user: str | None,
                 password: str | None, from_addr: str, to_addr: str):
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._from = from_addr
        self._to = to_addr

    def send(self, message: NotifyMessage) -> None:
        msg = EmailMessage()
        msg["Subject"] = message.title
        msg["From"] = self._from
        msg["To"] = self._to
        msg.set_content(message.body)
        with smtplib.SMTP(self._host, self._port, timeout=_TIMEOUT) as smtp:
            smtp.starttls()
            if self._user and self._password:
                smtp.login(self._user, self._password)
            smtp.send_message(msg)


def build_email_transport(cfg: NotifyConfig) -> Transport:
    if cfg.smtp_host and cfg.smtp_from and cfg.smtp_to:
        return EmailTransport(
            host=cfg.smtp_host, port=cfg.smtp_port,
            user=cfg.smtp_user, password=cfg.smtp_password,
            from_addr=cfg.smtp_from, to_addr=cfg.smtp_to,
        )
    return DisabledTransport("smtp: host/from/to not configured")
