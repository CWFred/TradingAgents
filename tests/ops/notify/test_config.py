import pytest

from ops.notify.config import NotifyConfig, load_notify_config


def test_defaults_disabled():
    c = NotifyConfig()
    assert c.notify_enabled is False
    assert c.smtp_port == 587
    assert c.pushover_user_key is None


def test_load_from_env(monkeypatch):
    monkeypatch.setenv("OPS_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("OPS_PUSHOVER_USER_KEY", "uk")
    monkeypatch.setenv("OPS_PUSHOVER_APP_TOKEN", "at")
    monkeypatch.setenv("OPS_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("OPS_SMTP_PORT", "465")
    monkeypatch.setenv("OPS_SMTP_TO", "me@example.com")
    c = load_notify_config()
    assert c.notify_enabled is True
    assert c.pushover_user_key == "uk" and c.pushover_app_token == "at"
    assert c.smtp_host == "smtp.example.com" and c.smtp_port == 465
    assert c.smtp_to == "me@example.com"


def test_load_defaults_when_unset(monkeypatch):
    for k in ("OPS_NOTIFY_ENABLED", "OPS_PUSHOVER_USER_KEY", "OPS_SMTP_HOST"):
        monkeypatch.delenv(k, raising=False)
    c = load_notify_config()
    assert c.notify_enabled is False and c.smtp_host is None


def test_non_numeric_smtp_port_raises_with_var_name(monkeypatch):
    monkeypatch.setenv("OPS_SMTP_PORT", "not-a-port")
    with pytest.raises(ValueError, match="OPS_SMTP_PORT"):
        load_notify_config()


def test_heartbeat_url_default_none(monkeypatch):
    """A1.3: the dead-man's switch is off unless OPS_HEARTBEAT_URL is set."""
    monkeypatch.delenv("OPS_HEARTBEAT_URL", raising=False)
    assert NotifyConfig().heartbeat_url is None
    assert load_notify_config().heartbeat_url is None


def test_heartbeat_url_from_env(monkeypatch):
    monkeypatch.setenv("OPS_HEARTBEAT_URL", "https://hc-ping.com/abc123")
    assert load_notify_config().heartbeat_url == "https://hc-ping.com/abc123"
