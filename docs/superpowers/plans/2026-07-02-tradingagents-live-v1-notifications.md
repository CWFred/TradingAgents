# TradingAgents live-v1 — Plan 3c: Notifications + LIVE_MAX_POSITION gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pull-based journal-event notification subsystem (Pushover + SMTP) and a code-enforced $10 cap on the first 20 live BUY fills after the paper→robinhood flip.

**Architecture:** A durable cursor over the journal's `events` table feeds a `NotifyDispatcher` that routes each event by a per-kind policy table to Pushover/SMTP transports, scrubbing SPOT from every body. Fills — today table-only and invisible to an events cursor — start emitting a `fill` event from `GuardedBroker`. A `LiveMaxPositionRule` in the existing rule chain caps early live BUYs, counting live BUY fills since a one-time `broker_mode_live` marker event.

**Tech Stack:** Python 3.12, `requests` (already a dep), stdlib `smtplib`/`email`, APScheduler (already wired in `ops/main.py`), SQLite journal, `click` CLI.

## Global Constraints

- **SPOT is contractually blacklisted.** Never weaken `DenyListRule` or `RobinhoodBroker._enforce_spot_hard_check`. Every notification body MUST pass through a SPOT-scrub step (defense in depth). Copy verbatim: deny list contains `"SPOT"` (`ops/config.py:12`).
- **Journal is authoritative.** All new journal writes are appends to the `events` table or the new `dispatch_cursors` table. Never write broker state outside the journal path, or the paper reconciler halts on restart.
- **Scheduler-safe polls.** Any APScheduler job body wraps its work in try/except that journals an error and never raises (mirror `PositionGuardian.check_stops_once` → `guardian_check_error`).
- **No asyncio in notification code.** `RealRobinhoodMCPClient` owns its own event loop; notification code is fully synchronous (`requests`, `smtplib`).
- **Opt-in live tests only.** Real Pushover/SMTP network tests are gated behind `OPS_NOTIFY_LIVE_TESTS=1`, never in the default suite (mirror `OPS_RH_LIVE_TESTS` in `tests/ops/broker/test_robinhood_live.py`).
- **Test command:** `.venv/bin/pytest tests/ops/` — baseline 248 passing / 4 skipped on `main`.
- **Config default values (verbatim from spec §Graduation criteria):** `live_max_position = Decimal("10")`, `live_fill_gate_count = 20`.

---

### Task 1: Journal cursor API — `read_events_since` + `dispatch_cursors`

**Files:**
- Modify: `ops/journal.py` (schema block `~15-55`, add methods after `read_events` `~121`)
- Test: `tests/ops/test_journal.py`

**Interfaces:**
- Consumes: existing `Journal(path)`, `record_event(kind, payload)`.
- Produces:
  - `Journal.read_events_since(min_id: int, limit: int | None = None) -> list[dict]` — each dict is `{"id": int, "at": datetime, "kind": str, "payload": dict}`, ordered by `id`, only rows with `id > min_id`.
  - `Journal.get_cursor(consumer: str) -> int` — returns `0` when the consumer has no stored cursor.
  - `Journal.set_cursor(consumer: str, last_event_id: int) -> None` — upsert.

- [ ] **Step 1: Write the failing tests**

Add to `tests/ops/test_journal.py`:

```python
def test_read_events_since_returns_id_and_filters(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    j.record_event("a", {"n": 1})
    j.record_event("b", {"n": 2})
    j.record_event("c", {"n": 3})
    all_ev = j.read_events_since(0)
    assert [e["kind"] for e in all_ev] == ["a", "b", "c"]
    assert all_ev[0]["id"] == 1 and all_ev[2]["id"] == 3
    # only rows after id=1
    after = j.read_events_since(1)
    assert [e["kind"] for e in after] == ["b", "c"]
    # limit
    assert len(j.read_events_since(0, limit=2)) == 2


def test_dispatch_cursor_roundtrip_and_default(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    assert j.get_cursor("notify") == 0          # default when absent
    j.set_cursor("notify", 5)
    assert j.get_cursor("notify") == 5
    j.set_cursor("notify", 9)                    # upsert, not duplicate
    assert j.get_cursor("notify") == 9
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/test_journal.py::test_read_events_since_returns_id_and_filters tests/ops/test_journal.py::test_dispatch_cursor_roundtrip_and_default -v`
Expected: FAIL — `AttributeError: 'Journal' object has no attribute 'read_events_since'`.

- [ ] **Step 3: Implement**

In `ops/journal.py`, add a table to the `_SCHEMA` string (after the `equity_snapshots` table, before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS dispatch_cursors (
    consumer TEXT PRIMARY KEY,
    last_event_id INTEGER NOT NULL
);
```

Add these methods to `Journal` (after `read_events`, `~line 121`):

```python
    def read_events_since(self, min_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, at, kind, payload FROM events WHERE id > ? ORDER BY id"
        params: tuple[Any, ...] = (min_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (min_id, limit)
        cur = self._conn.execute(sql, params)
        return [
            {"id": row[0], "at": _from_iso(row[1]), "kind": row[2],
             "payload": json.loads(row[3])}
            for row in cur
        ]

    def get_cursor(self, consumer: str) -> int:
        row = self._conn.execute(
            "SELECT last_event_id FROM dispatch_cursors WHERE consumer = ?",
            (consumer,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def set_cursor(self, consumer: str, last_event_id: int) -> None:
        self._conn.execute(
            "INSERT INTO dispatch_cursors (consumer, last_event_id) VALUES (?, ?)"
            " ON CONFLICT(consumer) DO UPDATE SET last_event_id = excluded.last_event_id",
            (consumer, last_event_id),
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/ops/test_journal.py -v`
Expected: PASS (all journal tests, old + new).

- [ ] **Step 5: Commit**

```bash
git add ops/journal.py tests/ops/test_journal.py
git commit -m "feat(ops/journal): read_events_since cursor + dispatch_cursors table"
```

---

### Task 2: `GuardedBroker` emits a `fill` event

**Files:**
- Modify: `ops/broker/guarded.py` (`place_order` `~67-81`, `close_position` `~122-139`)
- Test: `tests/ops/broker/test_guarded.py`

**Interfaces:**
- Consumes: `Journal.record_event`, `Fill` (`ops/broker/types.py`).
- Produces: a journal `fill` event on every successful fill (place or close). Payload:
  `{"client_order_id", "order_id", "symbol", "side" (str "BUY"/"SELL"), "quantity" (str), "price" (str), "filled_at" (iso str), "context" ("place"|"close")}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/ops/broker/test_guarded.py` (helpers `_stack`, imports already present):

```python
def test_guarded_emits_fill_event_on_place(tmp_path):
    j, paper, guarded = _stack(tmp_path)
    guarded.place_order(Order(
        client_order_id="c1", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_loss_price=Decimal("184"),
    ))
    fills = [e for e in j.read_events() if e["kind"] == "fill"]
    assert len(fills) == 1
    p = fills[0]["payload"]
    assert p["symbol"] == "AAPL" and p["side"] == "BUY" and p["context"] == "place"
    assert Decimal(p["price"]) == Decimal("200")


def test_guarded_no_fill_event_on_rejection(tmp_path):
    j, paper, guarded = _stack(tmp_path)
    with pytest.raises(OrderRejected):
        guarded.place_order(Order(
            client_order_id="c1", symbol="BANNED", side=Side.BUY,
            notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
            stop_loss_price=Decimal("184"),
        ))
    assert [e for e in j.read_events() if e["kind"] == "fill"] == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/broker/test_guarded.py::test_guarded_emits_fill_event_on_place -v`
Expected: FAIL — no `fill` event found (`len(fills) == 1` fails, got 0).

- [ ] **Step 3: Implement**

In `ops/broker/guarded.py`, add a private helper on `GuardedBroker`:

```python
    def _journal_fill_event(self, fill: Fill, context: str) -> None:
        self._journal.record_event(
            "fill",
            {
                "client_order_id": fill.client_order_id,
                "order_id": fill.order_id,
                "symbol": fill.symbol,
                "side": fill.side.value,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "filled_at": fill.filled_at.isoformat(),
                "context": context,
            },
        )
```

Change `place_order`'s success branch (`~68`) from `return self.__inner.place_order(order)` to:

```python
            try:
                fill = self.__inner.place_order(order)
                self._journal_fill_event(fill, "place")
                return fill
            except BrokerError as exc:
```

Change `close_position`'s success branch (`~123`) from `return self.__inner.close_position(...)` to:

```python
            try:
                fill = self.__inner.close_position(
                    symbol, client_order_id=close_order.client_order_id,
                )
                self._journal_fill_event(fill, "close")
                return fill
            except BrokerError as exc:
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/ops/broker/test_guarded.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/broker/guarded.py tests/ops/broker/test_guarded.py
git commit -m "feat(ops/broker): GuardedBroker emits fill events for the notify cursor"
```

---

### Task 3: `notify` package + `NotifyConfig`

**Files:**
- Create: `ops/notify/__init__.py` (empty), `ops/notify/config.py`
- Test: `tests/ops/notify/__init__.py` (empty), `tests/ops/notify/test_config.py`

**Interfaces:**
- Produces:
  - `NotifyConfig` frozen dataclass with fields: `notify_enabled: bool = False`, `pushover_user_key: str | None = None`, `pushover_app_token: str | None = None`, `smtp_host: str | None = None`, `smtp_port: int = 587`, `smtp_user: str | None = None`, `smtp_password: str | None = None`, `smtp_from: str | None = None`, `smtp_to: str | None = None`.
  - `load_notify_config() -> NotifyConfig` — reads `OPS_*` env vars.

- [ ] **Step 1: Write the failing test**

`tests/ops/notify/test_config.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/notify/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.notify'`.

- [ ] **Step 3: Implement**

Create `ops/notify/__init__.py` and `tests/ops/notify/__init__.py` as empty files. Create `ops/notify/config.py`:

```python
"""Delivery configuration for notifications, kept separate from OpsConfig
so delivery secrets never mix with the risk-parameter object."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class NotifyConfig:
    notify_enabled: bool = False
    pushover_user_key: str | None = None
    pushover_app_token: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def load_notify_config() -> NotifyConfig:
    port_raw = os.environ.get("OPS_SMTP_PORT")
    return NotifyConfig(
        notify_enabled=_env_bool("OPS_NOTIFY_ENABLED"),
        pushover_user_key=os.environ.get("OPS_PUSHOVER_USER_KEY"),
        pushover_app_token=os.environ.get("OPS_PUSHOVER_APP_TOKEN"),
        smtp_host=os.environ.get("OPS_SMTP_HOST"),
        smtp_port=int(port_raw) if port_raw else 587,
        smtp_user=os.environ.get("OPS_SMTP_USER"),
        smtp_password=os.environ.get("OPS_SMTP_PASSWORD"),
        smtp_from=os.environ.get("OPS_SMTP_FROM"),
        smtp_to=os.environ.get("OPS_SMTP_TO"),
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/ops/notify/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/notify/__init__.py ops/notify/config.py tests/ops/notify/
git commit -m "feat(ops/notify): NotifyConfig + load_notify_config from env"
```

---

### Task 4: `Transport` protocol + `NotifyMessage` + disabled no-op

**Files:**
- Create: `ops/notify/transport.py`
- Test: `tests/ops/notify/test_transport.py`

**Interfaces:**
- Produces:
  - `NotifyMessage` frozen dataclass: `title: str`, `body: str`, `urgency: str = "normal"` (`"normal"` | `"high"`).
  - `Transport` — a `typing.Protocol` with `send(self, message: NotifyMessage) -> None` and property `enabled: bool`.
  - `DisabledTransport(reason: str)` — `enabled = False`; `send` is a no-op (logs the reason once at construction).

- [ ] **Step 1: Write the failing test**

`tests/ops/notify/test_transport.py`:

```python
from ops.notify.transport import NotifyMessage, DisabledTransport


def test_notify_message_defaults():
    m = NotifyMessage(title="t", body="b")
    assert m.urgency == "normal"


def test_disabled_transport_is_noop():
    t = DisabledTransport("no creds")
    assert t.enabled is False
    t.send(NotifyMessage(title="t", body="b"))  # must not raise
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/notify/test_transport.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.notify.transport'`.

- [ ] **Step 3: Implement**

`ops/notify/transport.py`:

```python
"""Transport protocol and a disabled no-op fallback."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger("ops.notify")


@dataclass(frozen=True)
class NotifyMessage:
    title: str
    body: str
    urgency: str = "normal"  # "normal" | "high"


@runtime_checkable
class Transport(Protocol):
    @property
    def enabled(self) -> bool: ...

    def send(self, message: NotifyMessage) -> None: ...


class DisabledTransport:
    """Stands in for a transport whose credentials are missing. send() is a
    no-op so the dispatcher can treat a missing channel as 'nothing to do'
    rather than a delivery failure."""

    enabled = False

    def __init__(self, reason: str):
        self._reason = reason
        logger.info("notify transport disabled: %s", reason)

    def send(self, message: NotifyMessage) -> None:
        return None
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/ops/notify/test_transport.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/notify/transport.py tests/ops/notify/test_transport.py
git commit -m "feat(ops/notify): Transport protocol, NotifyMessage, disabled no-op"
```

---

### Task 5: `PushoverTransport`

**Files:**
- Create: `ops/notify/push.py`
- Test: `tests/ops/notify/test_push.py`

**Interfaces:**
- Consumes: `NotifyConfig`, `NotifyMessage`, `DisabledTransport`.
- Produces: `build_push_transport(cfg: NotifyConfig) -> Transport` — returns `PushoverTransport` when both `pushover_user_key` and `pushover_app_token` are set, else `DisabledTransport`. `PushoverTransport.send` POSTs to the Pushover API; `urgency == "high"` → `priority=1`.

- [ ] **Step 1: Write the failing test**

`tests/ops/notify/test_push.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/notify/test_push.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.notify.push'`.

- [ ] **Step 3: Implement**

`ops/notify/push.py`:

```python
"""Pushover push transport (synchronous, via requests)."""
from __future__ import annotations

import requests

from ops.notify.config import NotifyConfig
from ops.notify.transport import DisabledTransport, NotifyMessage, Transport

_API_URL = "https://api.pushover.net/1/messages.json"
_TIMEOUT = 10


class PushoverTransport:
    enabled = True

    def __init__(self, *, user_key: str, app_token: str):
        self._user_key = user_key
        self._app_token = app_token

    def send(self, message: NotifyMessage) -> None:
        data = {
            "token": self._app_token,
            "user": self._user_key,
            "title": message.title,
            "message": message.body,
            "priority": 1 if message.urgency == "high" else 0,
        }
        resp = requests.post(_API_URL, data=data, timeout=_TIMEOUT)
        resp.raise_for_status()


def build_push_transport(cfg: NotifyConfig) -> Transport:
    if cfg.pushover_user_key and cfg.pushover_app_token:
        return PushoverTransport(
            user_key=cfg.pushover_user_key, app_token=cfg.pushover_app_token,
        )
    return DisabledTransport("pushover: user_key/app_token not configured")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/ops/notify/test_push.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/notify/push.py tests/ops/notify/test_push.py
git commit -m "feat(ops/notify): PushoverTransport"
```

---

### Task 6: `EmailTransport`

**Files:**
- Create: `ops/notify/email.py`
- Test: `tests/ops/notify/test_email.py`

**Interfaces:**
- Consumes: `NotifyConfig`, `NotifyMessage`, `DisabledTransport`.
- Produces: `build_email_transport(cfg: NotifyConfig) -> Transport` — returns `EmailTransport` when `smtp_host`, `smtp_from`, and `smtp_to` are all set, else `DisabledTransport`. `EmailTransport.send` connects via `smtplib.SMTP`, `starttls()`, optional `login`, and `send_message`.

- [ ] **Step 1: Write the failing test**

`tests/ops/notify/test_email.py`:

```python
from unittest.mock import MagicMock
from ops.notify.config import NotifyConfig
from ops.notify.transport import NotifyMessage
from ops.notify.email import build_email_transport, EmailTransport


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
    smtp_instance.starttls.assert_called_once()
    smtp_instance.login.assert_called_once_with("u", "p")
    sent_msg = smtp_instance.send_message.call_args[0][0]
    assert sent_msg["Subject"] == "Daily"
    assert sent_msg["From"] == "from@example.com" and sent_msg["To"] == "to@example.com"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/notify/test_email.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.notify.email'`.

- [ ] **Step 3: Implement**

`ops/notify/email.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/ops/notify/test_email.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/notify/email.py tests/ops/notify/test_email.py
git commit -m "feat(ops/notify): EmailTransport (smtplib)"
```

---

### Task 7: Policy table + renderer + SPOT scrub

**Files:**
- Create: `ops/notify/policy.py`
- Test: `tests/ops/notify/test_policy.py`

**Interfaces:**
- Consumes: `NotifyMessage`.
- Produces:
  - `PolicyEntry` frozen dataclass: `channels: tuple[str, ...]`, `urgency: str`, `cooldown_seconds: int | None`.
  - `POLICY: dict[str, PolicyEntry]` — the table below. Kinds absent from the dict are not notified.
  - `scrub_spot(text: str) -> str` — replaces any `SPOT` token (word-boundary, case-insensitive) with `[redacted]`.
  - `render(kind: str, payload: dict) -> NotifyMessage` — builds a `(title, body)` per kind, then applies `scrub_spot` to both.

- [ ] **Step 1: Write the failing test**

`tests/ops/notify/test_policy.py`:

```python
from ops.notify.policy import POLICY, PolicyEntry, scrub_spot, render


def test_policy_channels():
    assert POLICY["kill_switch"].channels == ("push", "email")
    assert POLICY["kill_switch"].urgency == "high"
    assert POLICY["fill"].channels == ("push",)
    assert POLICY["broker_unreachable"].cooldown_seconds is not None
    assert "order_rejected" not in POLICY          # not notified


def test_scrub_spot():
    assert scrub_spot("Bought SPOT at 500") == "Bought [redacted] at 500"
    assert scrub_spot("spot check") == "[redacted] check"
    assert scrub_spot("SPOTIFY unrelated") == "SPOTIFY unrelated"  # word boundary


def test_render_fill_scrubs_spot():
    msg = render("fill", {"symbol": "SPOT", "side": "BUY",
                          "quantity": "0.1", "price": "500", "context": "place"})
    assert "SPOT" not in msg.body and "[redacted]" in msg.body


def test_render_kill_switch_high_urgency():
    msg = render("kill_switch", {"reason": "weekly -15%"})
    assert msg.urgency == "high"
    assert "kill" in msg.title.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/notify/test_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.notify.policy'`.

- [ ] **Step 3: Implement**

`ops/notify/policy.py`:

```python
"""Per-event-kind notification policy, rendering, and SPOT redaction."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ops.notify.transport import NotifyMessage

_SPOT_RE = re.compile(r"\bspot\b", re.IGNORECASE)


def scrub_spot(text: str) -> str:
    return _SPOT_RE.sub("[redacted]", text)


@dataclass(frozen=True)
class PolicyEntry:
    channels: tuple[str, ...]
    urgency: str
    cooldown_seconds: int | None


_INSTANT_CRITICAL = PolicyEntry(("push", "email"), "high", None)
_PUSH_ONLY = PolicyEntry(("push",), "normal", None)
_EMAIL_THROTTLED = PolicyEntry(("email",), "normal", 600)

POLICY: dict[str, PolicyEntry] = {
    "kill_switch": _INSTANT_CRITICAL,
    "stop_failed": _INSTANT_CRITICAL,
    "kill_switch_close_failed": _INSTANT_CRITICAL,
    "inconsistency": _INSTANT_CRITICAL,
    "startup_halted": _INSTANT_CRITICAL,
    "positions_recovered_without_stops": _INSTANT_CRITICAL,
    "stop_hit": _PUSH_ONLY,
    "daily_halt": _PUSH_ONLY,
    "fill": _PUSH_ONLY,
    "broker_unreachable": _EMAIL_THROTTLED,
    "orchestrator_tick_error": _EMAIL_THROTTLED,
    "guardian_check_error": _EMAIL_THROTTLED,
    "quote_unavailable": _EMAIL_THROTTLED,
    "daily_summary": PolicyEntry(("push", "email"), "normal", None),
}


def _title(kind: str) -> str:
    return kind.replace("_", " ").title()


def render(kind: str, payload: dict) -> NotifyMessage:
    entry = POLICY.get(kind)
    urgency = entry.urgency if entry is not None else "normal"
    if kind == "fill":
        title = f"Fill: {payload.get('symbol')}"
        body = (f"{payload.get('side')} {payload.get('symbol')} "
                f"qty {payload.get('quantity')} @ ${payload.get('price')} "
                f"({payload.get('context')})")
    elif kind == "kill_switch":
        title = "KILL SWITCH TRIPPED"
        body = f"Kill switch: {payload.get('reason', '')}"
    elif kind == "daily_summary":
        title = payload.get("headline", "Daily summary")
        body = payload.get("body", str(payload))
    else:
        title = _title(kind)
        body = "; ".join(f"{k}={v}" for k, v in payload.items()) or kind
    return NotifyMessage(title=scrub_spot(title), body=scrub_spot(body), urgency=urgency)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/ops/notify/test_policy.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/notify/policy.py tests/ops/notify/test_policy.py
git commit -m "feat(ops/notify): policy table, renderer, SPOT scrub"
```

---

### Task 8: `NotifyDispatcher`

**Files:**
- Create: `ops/notify/dispatcher.py`
- Test: `tests/ops/notify/test_dispatcher.py`

**Interfaces:**
- Consumes: `Journal` (`read_events_since`, `get_cursor`, `set_cursor`, `record_event`), `POLICY`, `render`, `Transport`, `NotifyMessage`.
- Produces: `NotifyDispatcher(journal, transports: dict[str, Transport], *, consumer="notify", policy=POLICY, now: Callable[[], datetime] | None = None)` with `dispatch_once() -> int` (returns number of messages sent). Behaviour: reads events since cursor; per event, if a transport raises, journals `notify_dispatch_error` and stops WITHOUT advancing past the failed event (retry next call); not-notified and cooldown-skipped events still advance the cursor.

- [ ] **Step 1: Write the failing tests**

`tests/ops/notify/test_dispatcher.py`:

```python
from datetime import datetime, timezone, timedelta
from ops.journal import Journal
from ops.notify.transport import NotifyMessage
from ops.notify.dispatcher import NotifyDispatcher


class FakeTransport:
    enabled = True
    def __init__(self, fail=False):
        self.sent = []
        self._fail = fail
    def send(self, message: NotifyMessage) -> None:
        if self._fail:
            raise RuntimeError("transport down")
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/notify/test_dispatcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.notify.dispatcher'`.

- [ ] **Step 3: Implement**

`ops/notify/dispatcher.py`:

```python
"""Pull-based journal-event dispatcher. Reads events since a durable cursor
and routes each to the configured transports per the policy table. At-least-
once: on a transport failure the cursor is not advanced past the failed
event, so it is retried on the next call."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from ops.journal import Journal
from ops.notify.policy import POLICY, PolicyEntry, render
from ops.notify.transport import Transport

logger = logging.getLogger("ops.notify")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NotifyDispatcher:
    def __init__(
        self,
        journal: Journal,
        transports: dict[str, Transport],
        *,
        consumer: str = "notify",
        policy: dict[str, PolicyEntry] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self._journal = journal
        self._transports = transports
        self._consumer = consumer
        self._policy = policy if policy is not None else POLICY
        self._now = now if now is not None else _utcnow
        self._last_sent: dict[str, datetime] = {}

    def dispatch_once(self) -> int:
        cursor = self._journal.get_cursor(self._consumer)
        sent = 0
        for ev in self._journal.read_events_since(cursor):
            try:
                sent += self._handle(ev)
            except Exception as exc:  # transport failure — hold the cursor
                self._journal.record_event(
                    "notify_dispatch_error",
                    {"event_id": ev["id"], "kind": ev["kind"],
                     "error": f"{type(exc).__name__}: {exc}"},
                )
                logger.warning("notify dispatch failed at event %s: %s", ev["id"], exc)
                break
            self._journal.set_cursor(self._consumer, ev["id"])
        return sent

    def _handle(self, ev: dict) -> int:
        entry = self._policy.get(ev["kind"])
        if entry is None:
            return 0  # not notified; cursor still advances
        if entry.cooldown_seconds is not None:
            last = self._last_sent.get(ev["kind"])
            now = self._now()
            if last is not None and (now - last).total_seconds() < entry.cooldown_seconds:
                return 0  # suppressed; cursor still advances
            self._last_sent[ev["kind"]] = now
        message = render(ev["kind"], ev["payload"])
        sent = 0
        for channel in entry.channels:
            transport = self._transports.get(channel)
            if transport is None or not transport.enabled:
                continue
            transport.send(message)  # may raise -> caught by dispatch_once
            sent += 1
        return sent
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/ops/notify/test_dispatcher.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/notify/dispatcher.py tests/ops/notify/test_dispatcher.py
git commit -m "feat(ops/notify): NotifyDispatcher — cursor, cooldown, at-least-once"
```

---

### Task 9: `daily_summary` emitter

**Files:**
- Create: `ops/notify/summary.py`
- Test: `tests/ops/notify/test_summary.py`

**Interfaces:**
- Consumes: `Journal` (`has_event_today`, `record_event`, `get_latest_equity_snapshot`, `read_fills`), a broker with `get_equity()` and `get_positions()`.
- Produces: `emit_daily_summary(journal, broker, *, now: datetime | None = None) -> bool` — computes today's summary and records one `daily_summary` event; returns `False` (no-op) if one already exists today. Payload keys: `headline` (one-line push), `body` (full email text), `equity` (str), `n_fills_today` (int). SPOT is excluded from any position listing before it reaches the payload.

- [ ] **Step 1: Write the failing test**

`tests/ops/notify/test_summary.py`:

```python
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from ops.journal import Journal
from ops.broker.types import Position
from ops.notify.summary import emit_daily_summary


def _broker(equity, positions):
    b = MagicMock()
    b.get_equity.return_value = Decimal(equity)
    b.get_positions.return_value = positions
    return b


def test_emits_once_per_day(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    now = datetime(2026, 7, 2, 20, 5, tzinfo=timezone.utc)
    b = _broker("260", [Position("AAPL", Decimal("0.1"), Decimal("200"))])
    assert emit_daily_summary(j, b, now=now) is True
    assert emit_daily_summary(j, b, now=now) is False   # idempotent
    events = [e for e in j.read_events() if e["kind"] == "daily_summary"]
    assert len(events) == 1
    assert events[0]["payload"]["equity"] == "260"


def test_summary_excludes_spot(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    now = datetime(2026, 7, 2, 20, 5, tzinfo=timezone.utc)
    b = _broker("260", [
        Position("AAPL", Decimal("0.1"), Decimal("200")),
        Position("SPOT", Decimal("0.1"), Decimal("500")),
    ])
    emit_daily_summary(j, b, now=now)
    body = [e for e in j.read_events() if e["kind"] == "daily_summary"][0]["payload"]["body"]
    assert "SPOT" not in body
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/notify/test_summary.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.notify.summary'`.

- [ ] **Step 3: Implement**

`ops/notify/summary.py`:

```python
"""Market-close daily summary: computes a one-line + full-body summary from
the journal + broker and records a single daily_summary event per day."""
from __future__ import annotations

from datetime import datetime, timezone


def emit_daily_summary(journal, broker, *, now: datetime | None = None) -> bool:
    when = now if now is not None else datetime.now(timezone.utc)
    if journal.has_event_today("daily_summary", now=when):
        return False

    equity = broker.get_equity()
    positions = [p for p in broker.get_positions() if p.symbol.upper() != "SPOT"]
    start = journal.get_latest_equity_snapshot(kind="open_day")
    day_pnl = (equity - start.equity) if start is not None else None

    day_str = when.date().isoformat()
    fills_today = [
        f for f in journal.read_fills()
        if f["at"].date() == when.date()
    ]

    pnl_txt = f"${day_pnl}" if day_pnl is not None else "n/a"
    headline = f"{day_str}: equity ${equity}, P&L {pnl_txt}, {len(fills_today)} fill(s)"
    lines = [
        headline,
        "",
        "Open positions:",
        *[f"  {p.symbol}: qty {p.quantity} entry ${p.avg_entry_price}"
          for p in positions],
    ]
    payload = {
        "headline": headline,
        "body": "\n".join(lines),
        "equity": str(equity),
        "n_fills_today": len(fills_today),
    }
    journal.record_event("daily_summary", payload)
    return True
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/ops/notify/test_summary.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/notify/summary.py tests/ops/notify/test_summary.py
git commit -m "feat(ops/notify): daily_summary emitter (idempotent, SPOT-excluded)"
```

---

### Task 10: `LiveMaxPositionRule` + config fields + chain wiring

**Files:**
- Modify: `ops/config.py` (dataclass `~20-31`, `__post_init__` `~33-56`, `load_config` `~114-118`)
- Modify: `ops/guardrails/sizing_rules.py` (add rule)
- Modify: `ops/__init__.py` (`build_default_rule_chain` `~38-64`)
- Test: `tests/ops/guardrails/test_sizing_rules.py`, `tests/ops/test_config.py`

**Interfaces:**
- Consumes: `RuleContext`, `Side`, `OpsConfig`.
- Produces:
  - `OpsConfig.live_max_position: Decimal = Decimal("10")`, `OpsConfig.live_fill_gate_count: int = 20`.
  - `LiveMaxPositionRule(live_fill_count: Callable[[], int])` in `sizing_rules.py`.
  - `build_default_rule_chain(*, start_of_day_equity, start_of_week_equity, live_fill_count: Callable[[], int] = lambda: 0)` — inserts `LiveMaxPositionRule` immediately before `PerPositionCapRule`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/ops/guardrails/test_sizing_rules.py` (reuses the `_ctx` helper; add `LiveMaxPositionRule` to the import line):

```python
from ops.guardrails.sizing_rules import LiveMaxPositionRule
from decimal import Decimal
from ops.config import OpsConfig


def _live_cfg(count_ok=True):
    return OpsConfig(broker_mode="robinhood")


def test_live_gate_inert_in_paper():
    rule = LiveMaxPositionRule(live_fill_count=lambda: 0)
    r = rule.check(_ctx("50", [], "250", "250", cfg=OpsConfig(broker_mode="paper")))
    assert r.allowed is True


def test_live_gate_blocks_big_buy_during_window():
    rule = LiveMaxPositionRule(live_fill_count=lambda: 0)
    r = rule.check(_ctx("15", [], "250", "250", cfg=OpsConfig(broker_mode="robinhood")))
    assert r.allowed is False


def test_live_gate_allows_small_buy_during_window():
    rule = LiveMaxPositionRule(live_fill_count=lambda: 0)
    r = rule.check(_ctx("9", [], "250", "250", cfg=OpsConfig(broker_mode="robinhood")))
    assert r.allowed is True


def test_live_gate_boundary_20th_still_capped_21st_free():
    cfg = OpsConfig(broker_mode="robinhood")   # gate=20, cap=$10
    at_20 = LiveMaxPositionRule(live_fill_count=lambda: 19)  # 20th fill = index 19 seen? see note
    r20 = at_20.check(_ctx("15", [], "250", "250", cfg=cfg))
    assert r20.allowed is False                # still within first 20
    at_21 = LiveMaxPositionRule(live_fill_count=lambda: 20)  # 20 fills done, gate lifted
    r21 = at_21.check(_ctx("15", [], "250", "250", cfg=cfg))
    assert r21.allowed is True                 # normal cap now applies (handled elsewhere)


def test_live_gate_allows_sell():
    rule = LiveMaxPositionRule(live_fill_count=lambda: 0)
    o = _ctx("15", [], "250", "250", cfg=OpsConfig(broker_mode="robinhood"))
    sell = RuleContext(
        order=Order(client_order_id="c", symbol="AAPL", side=Side.SELL,
                    notional_dollars=Decimal("15"), order_type=OrderType.MARKET),
        broker=o.broker, config=o.config,
    )
    assert rule.check(sell).allowed is True
```

Add to `tests/ops/test_config.py`:

```python
def test_live_gate_defaults():
    from decimal import Decimal
    c = OpsConfig()
    assert c.live_max_position == Decimal("10")
    assert c.live_fill_gate_count == 20


def test_live_gate_from_env(monkeypatch):
    from decimal import Decimal
    monkeypatch.setenv("OPS_LIVE_MAX_POSITION", "8")
    monkeypatch.setenv("OPS_LIVE_FILL_GATE_COUNT", "30")
    c = load_config()
    assert c.live_max_position == Decimal("8") and c.live_fill_gate_count == 30
```

(Ensure `Order`, `Side`, `OpsConfig`, `RuleContext`, `OrderType`, `load_config` are imported in the respective test files — `test_sizing_rules.py` already imports the first five; `test_config.py` already imports `OpsConfig`/`load_config`.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/guardrails/test_sizing_rules.py -k live_gate tests/ops/test_config.py -k live_gate -v`
Expected: FAIL — `ImportError: cannot import name 'LiveMaxPositionRule'` and `AttributeError` on `live_max_position`.

- [ ] **Step 3: Implement**

In `ops/config.py`, add two fields after `journal_path` (`~31`):

```python
    live_max_position: Decimal = Decimal("10")
    live_fill_gate_count: int = 20
```

Add validation at the end of `__post_init__` (`~56`):

```python
        if self.live_max_position <= 0:
            raise ValueError(f"live_max_position must be > 0, got {self.live_max_position}")
        if self.live_fill_gate_count < 0:
            raise ValueError(
                f"live_fill_gate_count must be >= 0, got {self.live_fill_gate_count}"
            )
```

Add to `load_config` before the `return` (`~117`):

```python
    live_max_position = _env_decimal("OPS_LIVE_MAX_POSITION")
    if live_max_position is not None:
        kwargs["live_max_position"] = live_max_position

    live_fill_gate_count = _env_int("OPS_LIVE_FILL_GATE_COUNT")
    if live_fill_gate_count is not None:
        kwargs["live_fill_gate_count"] = live_fill_gate_count
```

In `ops/guardrails/sizing_rules.py`, add the import and rule:

```python
from typing import Callable
```

```python
class LiveMaxPositionRule(Rule):
    """During the first `live_fill_gate_count` live BUY fills after the
    paper->robinhood flip, cap each BUY notional at `live_max_position`.
    Inert in paper mode and after the gate lifts. Independent of
    PerPositionCapRule (the stricter of the two applies while active)."""

    def __init__(self, live_fill_count: Callable[[], int]):
        self._live_fill_count = live_fill_count

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.order.side != Side.BUY:
            return RuleResult.allow()
        if ctx.config.broker_mode != "robinhood":
            return RuleResult.allow()
        if self._live_fill_count() >= ctx.config.live_fill_gate_count:
            return RuleResult.allow()
        if ctx.order.notional_dollars > ctx.config.live_max_position:
            return RuleResult.reject(
                f"live-gate: first {ctx.config.live_fill_gate_count} live fills "
                f"capped at ${ctx.config.live_max_position}, "
                f"order ${ctx.order.notional_dollars}"
            )
        return RuleResult.allow()
```

In `ops/__init__.py`, import the rule (add to the `sizing_rules` import block `~18-23`):

```python
    LiveMaxPositionRule,
```

Change `build_default_rule_chain` signature and body (`~38-64`):

```python
def build_default_rule_chain(
    *,
    start_of_day_equity: EquityFn,
    start_of_week_equity: EquityFn,
    live_fill_count: Callable[[], int] = lambda: 0,
) -> list:
    ...
    return [
        DenyListRule(),
        NoMarginRule(),
        NoOptionsRule(),
        NoCryptoRule(),
        LongOnlyRule(),
        StopAttachedRule(),
        FractionalSharesOnlyRule(),
        PerTradeDollarFloorRule(),
        LiveMaxPositionRule(live_fill_count=live_fill_count),
        PerPositionCapRule(),
        MaxOpenPositionsRule(),
        CashReserveRule(),
        DailyDrawdownRule(start_of_day_equity=start_of_day_equity),
        WeeklyDrawdownRule(start_of_week_equity=start_of_week_equity),
    ]
```

> **Note on the boundary test:** `live_fill_count()` returns the number of live BUY fills already completed. While it is `< live_fill_gate_count` (i.e. fills 0..19 done → 20th order in flight), the cap applies. Once it reaches `live_fill_gate_count` (20 fills done → 21st order), the gate lifts.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/ops/guardrails/test_sizing_rules.py tests/ops/test_config.py tests/ops/test_factory.py -v`
Expected: PASS (existing factory tests still pass — `build_default_rule_chain`'s new param has a default).

- [ ] **Step 5: Commit**

```bash
git add ops/config.py ops/guardrails/sizing_rules.py ops/__init__.py \
        tests/ops/guardrails/test_sizing_rules.py tests/ops/test_config.py
git commit -m "feat(ops/guardrails): LiveMaxPositionRule + live-gate config fields"
```

---

### Task 11: Flip marker + live-fill counter, wired into the robinhood factory

**Files:**
- Create: `ops/live_gate.py`
- Modify: `ops/__init__.py` (`build_guarded_robinhood_broker` `~122-146`)
- Test: `tests/ops/test_live_gate.py`

**Interfaces:**
- Consumes: `Journal` (`record_event`, `read_events`).
- Produces:
  - `MARKER_KIND = "broker_mode_live"`.
  - `record_flip_marker(journal) -> bool` — records the marker once (only if none exists); returns `True` if it wrote one.
  - `flip_epoch(journal) -> datetime | None` — the `at` of the earliest marker, or `None`.
  - `count_live_buy_fills(journal) -> int` — number of `fill` events with `side == "BUY"` and `at >= flip_epoch`; `0` when no marker exists.
  - `build_guarded_robinhood_broker` passes `live_fill_count=lambda: count_live_buy_fills(journal)` into `build_default_rule_chain`.

- [ ] **Step 1: Write the failing tests**

`tests/ops/test_live_gate.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/test_live_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.live_gate'`.

- [ ] **Step 3: Implement**

`ops/live_gate.py`:

```python
"""First-N live-fills gate helpers: a one-time paper->robinhood marker event
and a counter of live BUY fills since that marker. Used to enforce
LIVE_MAX_POSITION on early live trading (spec Graduation criteria #5)."""
from __future__ import annotations

from datetime import datetime

from ops.journal import Journal

MARKER_KIND = "broker_mode_live"


def record_flip_marker(journal: Journal) -> bool:
    if any(e["kind"] == MARKER_KIND for e in journal.read_events()):
        return False
    journal.record_event(MARKER_KIND, {"note": "paper->robinhood flip"})
    return True


def flip_epoch(journal: Journal) -> datetime | None:
    markers = [e for e in journal.read_events() if e["kind"] == MARKER_KIND]
    return markers[0]["at"] if markers else None


def count_live_buy_fills(journal: Journal) -> int:
    epoch = flip_epoch(journal)
    if epoch is None:
        return 0
    return sum(
        1
        for e in journal.read_events()
        if e["kind"] == "fill"
        and e["at"] >= epoch
        and e["payload"].get("side") == "BUY"
    )
```

In `ops/__init__.py`, update `build_guarded_robinhood_broker` to build and pass the counter closure:

```python
    from ops.broker.mcp_client import RealRobinhoodMCPClient
    from ops.broker.robinhood import RobinhoodBroker
    from ops.live_gate import count_live_buy_fills

    client = mcp_client if mcp_client is not None else RealRobinhoodMCPClient()
    inner = RobinhoodBroker(client=client, journal=journal)
    engine = RuleEngine(
        build_default_rule_chain(
            start_of_day_equity=start_of_day_equity,
            start_of_week_equity=start_of_week_equity,
            live_fill_count=lambda: count_live_buy_fills(journal),
        )
    )
    return GuardedBroker(inner=inner, engine=engine, journal=journal, config=config)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/ops/test_live_gate.py tests/ops/test_factory.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/live_gate.py ops/__init__.py tests/ops/test_live_gate.py
git commit -m "feat(ops): broker_mode_live marker + live BUY-fill counter for the gate"
```

---

### Task 12: Wire dispatcher + daily_summary + flip marker into `ops run`; add `ops notify-once`

**Files:**
- Modify: `ops/main.py` (imports, `_build_broker` `~57-61`, `_start_full_scheduler` `~96-109`, a new `_build_dispatcher` helper, `run` `~127-158`)
- Modify: `ops/cli.py` (add `notify-once` command)
- Test: `tests/ops/notify/test_main_wiring.py`, `tests/ops/test_cli_notify.py`

**Interfaces:**
- Consumes: `load_notify_config`, `build_push_transport`, `build_email_transport`, `NotifyDispatcher`, `emit_daily_summary`, `record_flip_marker`.
- Produces:
  - `ops/main.py:_build_dispatcher(journal) -> NotifyDispatcher` — assembles transports from `load_notify_config()`.
  - `_start_full_scheduler(orchestrator, guardian, dispatcher, summary_job)` gains a `notify_poll` `IntervalTrigger(seconds=20)` job wrapping `dispatcher.dispatch_once` in try/except, and a `daily_summary` `CronTrigger(hour=16, minute=5, day_of_week="mon-fri")` job.
  - `run()` calls `record_flip_marker(journal)` when `broker_mode == "robinhood"`.
  - CLI `ops notify-once` — one-shot `dispatch_once`.

- [ ] **Step 1: Write the failing tests**

`tests/ops/notify/test_main_wiring.py`:

```python
from decimal import Decimal
from ops.journal import Journal
from ops.config import OpsConfig
from ops.main import _build_dispatcher, _notify_tick, _daily_summary_tick
from ops.live_gate import MARKER_KIND, record_flip_marker


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


def test_notify_tick_swallows_errors(tmp_path):
    class Boom:
        def dispatch_once(self):
            raise RuntimeError("kaboom")
    _notify_tick(Boom())                  # must not raise


def test_flip_marker_written_in_robinhood_mode(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    assert record_flip_marker(j) is True
    assert len([e for e in j.read_events() if e["kind"] == MARKER_KIND]) == 1
```

`tests/ops/test_cli_notify.py`:

```python
from click.testing import CliRunner
from ops.cli import cli
from ops.journal import Journal


def test_notify_once_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("OPS_PUSHOVER_USER_KEY", raising=False)
    path = str(tmp_path / "j.sqlite")
    j = Journal(path)
    j.record_event("fill", {"symbol": "AAPL", "side": "BUY",
                            "quantity": "0.1", "price": "200", "context": "place"})
    j.close()
    res = CliRunner().invoke(cli, ["notify-once", "--journal", path])
    assert res.exit_code == 0
    assert "dispatched" in res.output.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/ops/notify/test_main_wiring.py tests/ops/test_cli_notify.py -v`
Expected: FAIL — `ImportError: cannot import name '_build_dispatcher'` / no `notify-once` command.

- [ ] **Step 3: Implement**

In `ops/main.py`, add imports near the top:

```python
from ops.notify.config import load_notify_config
from ops.notify.dispatcher import NotifyDispatcher
from ops.notify.push import build_push_transport
from ops.notify.email import build_email_transport
from ops.notify.summary import emit_daily_summary
from ops.live_gate import record_flip_marker
```

Add helpers:

```python
def _build_dispatcher(journal: Journal) -> NotifyDispatcher:
    cfg = load_notify_config()
    transports = {
        "push": build_push_transport(cfg),
        "email": build_email_transport(cfg),
    }
    return NotifyDispatcher(journal, transports)


def _notify_tick(dispatcher) -> None:
    try:
        dispatcher.dispatch_once()
    except Exception as exc:  # scheduler-safe: never let the job die
        print(f"notify tick error: {exc}", file=sys.stderr)


def _daily_summary_tick(journal: Journal, broker) -> None:
    try:
        emit_daily_summary(journal, broker)
    except Exception as exc:
        journal.record_event("daily_summary_error", {"error": f"{type(exc).__name__}: {exc}"})
```

Change `_start_full_scheduler` to accept and schedule the notify + summary jobs:

```python
def _start_full_scheduler(orchestrator, guardian, dispatcher, journal, broker) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="America/New_York")
    sched.add_job(
        orchestrator.tick,
        CronTrigger(minute="0,30", hour="9-15", day_of_week="mon-fri"),
        id="orchestrator_tick", max_instances=1, misfire_grace_time=60,
    )
    sched.add_job(
        guardian.check_stops_once,
        IntervalTrigger(seconds=60),
        id="guardian_poll", max_instances=1, misfire_grace_time=15,
    )
    sched.add_job(
        lambda: _notify_tick(dispatcher),
        IntervalTrigger(seconds=20),
        id="notify_poll", max_instances=1, misfire_grace_time=15,
    )
    sched.add_job(
        lambda: _daily_summary_tick(journal, broker),
        CronTrigger(hour=16, minute=5, day_of_week="mon-fri"),
        id="daily_summary", max_instances=1, misfire_grace_time=300,
    )
    sched.start()
    return sched
```

In `run()`: after `broker = _build_broker(config, journal)` and before wiring, record the flip marker in live mode; build the dispatcher; pass new args to `_start_full_scheduler`:

```python
        broker = _build_broker(config, journal)
        if config.broker_mode == "robinhood":
            record_flip_marker(journal)
        orchestrator, guardian, calendar = _wire(broker, journal, config)
        dispatcher = _build_dispatcher(journal)
        ...
        sched = _start_full_scheduler(orchestrator, guardian, dispatcher, journal, broker)
```

(The guardian-only reconciliation-halted path keeps `_start_guardian_only` but should still deliver alerts: also start the dispatcher there. Add a `notify_poll` job to `_start_guardian_only` by giving it the same `dispatcher` argument — update its signature to `_start_guardian_only(guardian, dispatcher)` and add the `notify_poll` job identically. Build `dispatcher` before the `if result.diffs:` branch so both paths share it.)

In `ops/cli.py`, add:

```python
@cli.command("notify-once")
@click.option("--journal", "journal_path", default="ops_journal.sqlite",
              type=click.Path(dir_okay=False), help="SQLite journal path")
def notify_once(journal_path: str) -> None:
    """Dispatch any pending journal events to notification transports once."""
    from ops.journal import Journal
    from ops.main import _build_dispatcher
    journal = Journal(journal_path)
    try:
        n = _build_dispatcher(journal).dispatch_once()
        click.echo(f"dispatched {n} message(s)")
    finally:
        journal.close()
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/ops/notify/test_main_wiring.py tests/ops/test_cli_notify.py tests/ops/test_main.py tests/ops/test_cli_run.py -v`
Expected: PASS. (Check `tests/ops/test_main.py` — if it calls `_start_full_scheduler`/`_start_guardian_only` directly with the old signature, update those call sites in the test to pass the new args, or assert via `run()` as before.)

- [ ] **Step 5: Run the full ops suite**

Run: `.venv/bin/pytest tests/ops/`
Expected: PASS — 248 prior + new tests, 4 opt-in skipped.

- [ ] **Step 6: Commit**

```bash
git add ops/main.py ops/cli.py tests/ops/notify/test_main_wiring.py tests/ops/test_cli_notify.py
git commit -m "feat(ops): wire notify dispatcher + daily_summary + flip marker into ops run; add notify-once CLI"
```

---

### Task 13: Integration test — restart resume + opt-in live smoke

**Files:**
- Test: `tests/ops/notify/test_integration_dispatch.py`
- Test (opt-in): `tests/ops/notify/test_live_transports.py`

**Interfaces:**
- Consumes: everything above. No new production code.

- [ ] **Step 1: Write the restart-resume integration test**

`tests/ops/notify/test_integration_dispatch.py`:

```python
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
```

- [ ] **Step 2: Write the opt-in live smoke test (skipped by default)**

`tests/ops/notify/test_live_transports.py`:

```python
import os
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPS_NOTIFY_LIVE_TESTS") != "1",
    reason="opt-in: set OPS_NOTIFY_LIVE_TESTS=1 to hit real Pushover/SMTP",
)


def test_live_pushover_send():
    from ops.notify.config import load_notify_config
    from ops.notify.push import build_push_transport
    from ops.notify.transport import NotifyMessage
    t = build_push_transport(load_notify_config())
    assert t.enabled, "configure OPS_PUSHOVER_* to run this"
    t.send(NotifyMessage(title="ops live test", body="pushover smoke", urgency="normal"))


def test_live_smtp_send():
    from ops.notify.config import load_notify_config
    from ops.notify.email import build_email_transport
    from ops.notify.transport import NotifyMessage
    t = build_email_transport(load_notify_config())
    assert t.enabled, "configure OPS_SMTP_* to run this"
    t.send(NotifyMessage(title="ops live test", body="smtp smoke", urgency="normal"))
```

- [ ] **Step 3: Run to verify**

Run: `.venv/bin/pytest tests/ops/notify/test_integration_dispatch.py -v && .venv/bin/pytest tests/ops/notify/test_live_transports.py -v`
Expected: integration PASS; live tests SKIPPED (2 skipped).

- [ ] **Step 4: Full suite**

Run: `.venv/bin/pytest tests/ops/`
Expected: all pass, `4 + 2 = 6` skipped total.

- [ ] **Step 5: Commit**

```bash
git add tests/ops/notify/test_integration_dispatch.py tests/ops/notify/test_live_transports.py
git commit -m "test(ops/notify): restart-resume integration + opt-in live transport smoke"
```

---

## Post-plan verification

- [ ] `.venv/bin/pytest tests/ops/` green (248 baseline + new; 6 skipped: 4 RH + 2 notify live).
- [ ] Manual smoke: `OPS_PUSHOVER_USER_KEY=... OPS_PUSHOVER_APP_TOKEN=... .venv/bin/python -m ops.cli notify-once --journal ops_journal.sqlite` after a `decide-once` produced a fill — confirm a push arrives and the body has no `SPOT`.
- [ ] Then invoke `superpowers:requesting-code-review` for a whole-branch review before opening the PR (mirrors 3a/3b).
