"""ops status (A4): build_status is a pure journal reader.

Every test seeds a journal and asserts on the returned dict — never on
formatted text (the CLI is a thin renderer). The overriding constraint is
that build_status must be safe to run beside the live service with the
broker unreachable: journal only, no broker/MCP/OAuth/quotes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ops import events
from ops.config import OpsConfig
from ops.journal import Journal
from ops.status import build_status, format_status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def journal(tmp_path):
    j = Journal(str(tmp_path / "status_j.sqlite"))
    yield j
    j.close()


def _seed_buy(journal: Journal, *, symbol="AAPL", notional="20",
              price="200", stop="184", filled_at=None) -> None:
    """One journaled BUY: order row + fill row, the shape replay expects."""
    coid = f"buy-{symbol}-{filled_at or 'now'}"
    journal.record_order(
        client_order_id=coid, symbol=symbol, side="BUY",
        notional_dollars=Decimal(notional), stop_loss_price=None,
    )
    journal.record_fill(
        order_id=f"oid-{coid}", client_order_id=coid, symbol=symbol,
        side="BUY", quantity=Decimal(notional) / Decimal(price),
        price=Decimal(price), filled_at=filled_at or _utcnow(),
        stop_loss_price=Decimal(stop),
    )


def test_build_status_is_journal_only_even_in_robinhood_mode(journal):
    """The whole point of `ops status`: constructing it in robinhood mode
    with no MCP reachable (there is no MCP client in this test process at
    all) must work — status takes only the journal and static config."""
    journal.record_cash_adjustment(kind="seed", amount=Decimal("250"))
    _seed_buy(journal)
    config = OpsConfig(broker_mode="robinhood")
    status = build_status(journal, config)
    assert status["service"]["broker_mode"] == "robinhood"
    for section in ("service", "positions", "cash", "baselines", "halts",
                    "fills", "notify", "live_gate", "anomalies_7d"):
        assert section in status, f"missing status section {section!r}"


def test_position_replay_never_quotes(journal):
    """PaperBroker.from_journal takes a quote_source; replay must never
    call it (status has no quotes and must not need them). The callable
    status passes raises on any call — an open position replaying cleanly
    is the proof, plus the direct assertion on the sentinel."""
    from ops.status import _refuse_quotes

    journal.record_cash_adjustment(kind="seed", amount=Decimal("250"))
    _seed_buy(journal)  # open position: a quoting replay would raise
    status = build_status(journal, OpsConfig())
    assert len(status["positions"]["items"]) == 1
    with pytest.raises(RuntimeError, match="journal-only"):
        _refuse_quotes("AAPL")


def test_service_section_reports_lifecycle_events_with_ages(journal):
    journal.record_event(
        events.KIND_SERVICE_STARTED,
        events.service_started_payload(
            broker_mode="paper", journal_path=journal.path, pid=123,
        ),
    )
    journal.record_event(
        events.KIND_SERVICE_STOPPING,
        events.service_stopping_payload(exit_code=0),
    )
    status = build_status(journal, OpsConfig())
    svc = status["service"]
    assert svc["journal_path"] == journal.path
    assert svc["broker_mode"] == "paper"
    assert svc["last_started"]["payload"]["pid"] == 123
    assert svc["last_started"]["age_seconds"] >= 0
    assert svc["last_stopping"]["payload"]["exit_code"] == 0
    assert svc["last_stopping"]["age_seconds"] >= 0


def test_service_section_none_when_never_run(journal):
    status = build_status(journal, OpsConfig())
    assert status["service"]["last_started"] is None
    assert status["service"]["last_stopping"] is None


def test_positions_and_cash_come_from_journal_replay(journal):
    journal.record_cash_adjustment(kind="seed", amount=Decimal("250"))
    _seed_buy(journal, symbol="AAPL", notional="20", price="200", stop="184")
    status = build_status(journal, OpsConfig())
    assert status["positions"]["source"] == "journal_replay"
    (pos,) = status["positions"]["items"]
    assert pos["symbol"] == "AAPL"
    assert pos["quantity"] == Decimal("0.1")
    assert pos["entry"] == Decimal("200")
    assert pos["stop"] == Decimal("184")
    assert status["cash"]["cash"] == Decimal("230")


def test_baselines_flag_stale_snapshots(journal):
    now = _utcnow()
    journal.record_equity_snapshot(
        kind="open_day", equity=Decimal("250"), cash=Decimal("250"),
        at=now - timedelta(days=10),
    )
    journal.record_equity_snapshot(
        kind="open_week", equity=Decimal("260"), cash=Decimal("260"),
        at=now,
    )
    status = build_status(journal, OpsConfig(), now=now)
    day = status["baselines"]["open_day"]
    week = status["baselines"]["open_week"]
    assert day["equity"] == Decimal("250")
    assert day["stale"] is True          # 10 days old: outside the ET day
    assert week["equity"] == Decimal("260")
    assert week["stale"] is False        # recorded now: inside the ET week
    assert status["baselines"]["open_day"]["at"] is not None


def test_baselines_none_when_absent(journal):
    status = build_status(journal, OpsConfig())
    assert status["baselines"]["open_day"] is None
    assert status["baselines"]["open_week"] is None


def test_halt_states(journal):
    status = build_status(journal, OpsConfig())
    assert status["halts"]["daily_halt_today"] is False
    assert status["halts"]["kill_switch_this_week"] is False

    journal.record_event(
        events.KIND_DAILY_HALT,
        events.daily_halt_payload(
            mode="paper", equity_now=Decimal("230"),
            equity_open_day=Decimal("250"),
            pct=Decimal("-0.08"), threshold=Decimal("-0.07"),
        ),
    )
    journal.record_event(
        events.KIND_KILL_SWITCH,
        events.kill_switch_payload(
            mode="paper", equity_now=Decimal("210"),
            equity_open_week=Decimal("250"),
            pct=Decimal("-0.16"), threshold=Decimal("-0.15"),
        ),
    )
    status = build_status(journal, OpsConfig())
    assert status["halts"]["daily_halt_today"] is True
    assert status["halts"]["kill_switch_this_week"] is True


def test_fills_today_filtered_by_filled_at_not_write_time(journal):
    now = _utcnow()
    journal.record_cash_adjustment(kind="seed", amount=Decimal("250"))
    # Written to the journal NOW, but filled three days ago: must not
    # count as today's fill (the same at-vs-filled_at bug the daily
    # summary had).
    _seed_buy(journal, symbol="OLD", filled_at=now - timedelta(days=3))
    _seed_buy(journal, symbol="NEW", filled_at=now)
    status = build_status(journal, OpsConfig(), now=now)
    assert status["fills"]["today_count"] == 1
    assert status["fills"]["today"][0]["symbol"] == "NEW"
    assert status["fills"]["last"]["symbol"] == "NEW"


def test_fills_last_none_on_empty_journal(journal):
    status = build_status(journal, OpsConfig())
    assert status["fills"]["today_count"] == 0
    assert status["fills"]["last"] is None


def test_notify_cursor_lag_and_error_counts(journal):
    for _ in range(5):
        journal.record_event("some_kind", {"x": 1})
    journal.set_cursor("notify", 2)
    journal.record_event(
        events.KIND_NOTIFY_EVENT_SKIPPED,
        events.notify_event_skipped_payload(
            event_id=1, kind="fill", consecutive_failures=10,
        ),
    )
    journal.record_event(
        events.KIND_NOTIFY_RENDER_ERROR,
        events.notify_render_error_payload(
            event_id=2, kind="fill", error_type="KeyError",
        ),
    )
    # 7 events total, cursor at 2 -> lag 5 (the two error events are
    # themselves journal events awaiting dispatch).
    status = build_status(journal, OpsConfig())
    assert status["notify"]["cursor"] == 2
    assert status["notify"]["max_event_id"] == 7
    assert status["notify"]["lag"] == 5
    assert status["notify"]["skipped_count"] == 1
    assert status["notify"]["render_error_count"] == 1


def test_live_gate_before_flip(journal):
    config = OpsConfig(live_max_position=Decimal("10"), live_fill_gate_count=20)
    status = build_status(journal, config)
    gate = status["live_gate"]
    assert gate["flip_marker_present"] is False
    assert gate["flip_at"] is None
    assert gate["live_buy_fills"] == 0
    assert gate["cap"] == Decimal("10")
    assert gate["gate_count"] == 20
    assert gate["remaining"] == 20


def test_live_gate_after_flip_counts_live_buy_fills(journal):
    from ops.live_gate import record_flip_marker

    record_flip_marker(journal)
    journal.record_event(
        events.KIND_FILL,
        events.fill_payload(
            client_order_id="c1", order_id="o1", symbol="AAPL", side="BUY",
            quantity=Decimal("0.1"), price=Decimal("100"),
            filled_at=_utcnow(), context="place", broker_mode="robinhood",
        ),
    )
    config = OpsConfig(live_fill_gate_count=20)
    status = build_status(journal, config)
    gate = status["live_gate"]
    assert gate["flip_marker_present"] is True
    assert gate["flip_at"] is not None
    assert gate["live_buy_fills"] == 1
    assert gate["remaining"] == 19


def test_anomalies_last_7_days(journal):
    journal.record_event(
        events.KIND_GUARDIAN_CHECK_ERROR,
        events.guardian_check_error_payload(error="ValueError: boom"),
    )
    status = build_status(journal, OpsConfig())
    anomalies = status["anomalies_7d"]
    for kind in (events.KIND_GUARDIAN_CHECK_ERROR,
                 events.KIND_ORCHESTRATOR_TICK_ERROR,
                 events.KIND_STOP_FAILED,
                 events.KIND_GUARDIAN_BLIND,
                 events.KIND_INCONSISTENCY):
        assert kind in anomalies, f"anomaly kind {kind!r} missing"
    assert anomalies[events.KIND_GUARDIAN_CHECK_ERROR]["count"] == 1
    assert anomalies[events.KIND_GUARDIAN_CHECK_ERROR]["last_at"] is not None
    assert anomalies[events.KIND_STOP_FAILED]["count"] == 0
    assert anomalies[events.KIND_STOP_FAILED]["last_at"] is None


def test_format_status_renders_plain_text(journal):
    journal.record_cash_adjustment(kind="seed", amount=Decimal("250"))
    _seed_buy(journal)
    text = format_status(build_status(journal, OpsConfig()))
    assert journal.path in text
    assert "journal view" in text           # position label per spec
    assert "AAPL" in text
