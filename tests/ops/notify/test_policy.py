from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ops import events
from ops.notify.policy import POLICY, render, scrub_spot


def test_policy_channels():
    assert POLICY["kill_switch"].channels == ("push", "email")
    assert POLICY["kill_switch"].urgency == "high"
    assert POLICY["fill"].channels == ("push",)
    assert POLICY["broker_unreachable"].cooldown_seconds is not None
    assert "order_rejected" not in POLICY          # not notified


def test_policy_new_event_kinds_channels():
    # Added post-plan: guardian failing to get quotes for >=5 consecutive
    # passes, and a live order dangling at the broker unfilled.
    assert POLICY["guardian_blind"].channels == ("push", "email")
    assert POLICY["guardian_blind"].urgency == "high"
    assert POLICY["guardian_blind"].cooldown_seconds is None

    assert POLICY["order_not_filled"].channels == ("push", "email")
    assert POLICY["order_not_filled"].urgency == "high"
    assert POLICY["order_not_filled"].cooldown_seconds is None


def test_journal_replay_orphan_sell_not_notified():
    # Audit-only event: must never be notified.
    assert "journal_replay_orphan_sell" not in POLICY


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


def test_render_scrubs_spot_from_title_and_body():
    # SPOT-SAFETY: a notification must never surface a SPOT price/position,
    # regardless of which field (title or body) it would otherwise appear in.
    msg = render("fill", {"symbol": "SPOT", "side": "SELL",
                          "quantity": "1", "price": "500", "context": "close"})
    assert "SPOT" not in msg.title
    assert "SPOT" not in msg.body
    assert "[redacted]" in msg.title
    assert "[redacted]" in msg.body

    msg2 = render("daily_summary", {"headline": "SPOT ripped today",
                                     "body": "SPOT is up, unrelated to SPOTIFY"})
    assert msg2.title == "[redacted] ripped today"
    assert msg2.body == "[redacted] is up, unrelated to SPOTIFY"


def test_render_kill_switch_with_guardian_payload(tmp_path):
    """M4: render a real guardian-shaped kill_switch payload → body contains
    pct, threshold, and both equity figures."""
    msg = render("kill_switch", {
        "mode": "robinhood",
        "equity_now": "220",
        "equity_open_week": "250",
        "pct": "-0.12",
        "threshold": "-0.15",
    })
    assert "KILL" in msg.title
    assert "-0.12" in msg.body
    assert "-0.15" in msg.body
    assert "220" in msg.body
    assert "250" in msg.body
    assert "robinhood" in msg.body


def test_render_kill_switch_non_empty_for_empty_payload():
    """M4: body is non-empty for a payload with no keys at all."""
    msg = render("kill_switch", {})
    assert len(msg.body) > 0


# --- A3 enforcement: typed event contracts ------------------------------
#
# The tests below are the actual point of ops/events.py: they make it
# impossible to (a) add a POLICY kind without a payload builder, (b) add a
# builder/kind without a conscious notify decision (POLICY or AUDIT_ONLY),
# or (c) ship a policy kind whose rendered notification is empty or
# contains a literal "None" — the three shipped-bug shapes this replaces
# (daily_halt consumed-but-never-produced, the empty kill-switch body,
# fills lacking broker_mode).

_TS = datetime(2026, 7, 3, 14, 30, tzinfo=timezone.utc)

# One sample builder call per POLICY kind. Values are realistic, non-None
# arguments; the render assertions below run against exactly these.
SAMPLE_BUILDER_ARGS: dict[str, dict] = {
    events.KIND_FILL: dict(
        client_order_id="cid-1", order_id="oid-1", symbol="AAPL",
        side="BUY", quantity=Decimal("0.125"), price=Decimal("200.40"),
        filled_at=_TS, context="place", broker_mode="paper",
    ),
    events.KIND_KILL_SWITCH: dict(
        mode="paper", equity_now=Decimal("212.50"),
        equity_open_week=Decimal("250.00"),
        pct=Decimal("-0.15"), threshold=Decimal("-0.15"),
    ),
    events.KIND_STOP_HIT: dict(
        symbol="AAPL", entry=Decimal("200"), current=Decimal("180"),
        pct=Decimal("-0.10"), mode="pct", threshold_repr="pct -0.08",
    ),
    events.KIND_STOP_FAILED: dict(
        symbol="AAPL", entry=Decimal("200"), current=Decimal("180"),
        pct=Decimal("-0.10"), mode="pct", threshold_repr="pct -0.08",
        error="BrokerError: mcp unavailable",
    ),
    events.KIND_KILL_SWITCH_CLOSE_FAILED: dict(
        symbol="AAPL", error="BrokerError: mcp unavailable",
    ),
    events.KIND_INCONSISTENCY: dict(
        diffs=[{"symbol": "AAPL", "journal_qty": "1", "broker_qty": "2",
                "kind": "qty_mismatch"}],
        cash_journal=Decimal("100.00"), cash_broker=Decimal("90.00"),
        cash_diff=Decimal("-10.00"),
    ),
    events.KIND_STARTUP_HALTED: dict(reason="reconciliation"),
    events.KIND_POSITIONS_RECOVERED_WITHOUT_STOPS: dict(symbols=["AAPL"]),
    events.KIND_GUARDIAN_BLIND: dict(consecutive_failed_passes=5),
    events.KIND_ORDER_NOT_FILLED: dict(
        order_id="oid-1", client_order_id="cid-1", symbol="AAPL",
        side="BUY", status="queued", quantity=Decimal("0.125"),
        fill_price=Decimal("200.40"),
    ),
    events.KIND_DAILY_HALT: dict(
        mode="paper", equity_now=Decimal("230.00"),
        equity_open_day=Decimal("250.00"),
        pct=Decimal("-0.08"), threshold=Decimal("-0.07"),
    ),
    events.KIND_BROKER_UNREACHABLE: dict(error_type="MCPUnavailable"),
    events.KIND_ORCHESTRATOR_TICK_ERROR: dict(error="ValueError: boom"),
    events.KIND_GUARDIAN_CHECK_ERROR: dict(error="ValueError: boom"),
    events.KIND_QUOTE_UNAVAILABLE: dict(
        symbol="AAPL", context="guardian_stop_check",
        error="no data for AAPL",
    ),
    events.KIND_HEARTBEAT_ERROR: dict(error_type="ConnectionError"),
    events.KIND_DAILY_SUMMARY: dict(
        headline="2026-07-03: equity $250, P&L $5, 2 fill(s)",
        body="2026-07-03: equity $250, P&L $5, 2 fill(s)\n\nOpen positions:",
        equity=Decimal("250.00"), n_fills_today=2,
    ),
}


def test_policy_and_audit_only_are_disjoint():
    overlap = set(POLICY) & events.AUDIT_ONLY
    assert not overlap, (
        f"kinds {sorted(overlap)} are in both POLICY and AUDIT_ONLY — "
        "an event is either notified or deliberately not, never both"
    )


def test_every_policy_kind_has_a_builder():
    missing = set(POLICY) - set(events.BUILDERS)
    assert not missing, (
        f"POLICY kinds {sorted(missing)} have no payload builder in "
        "ops/events.py — add a <kind>_payload() builder and register it "
        "in events.BUILDERS so producers and renderers share one contract"
    )


def test_every_builder_kind_is_classified():
    unclassified = set(events.BUILDERS) - set(POLICY) - events.AUDIT_ONLY
    assert not unclassified, (
        f"event kinds {sorted(unclassified)} are neither in notify POLICY "
        "nor in events.AUDIT_ONLY — every kind needs a conscious notify "
        "decision (this is how daily_halt shipped consumed-but-never-"
        "produced)"
    )


def test_every_policy_kind_has_sample_args():
    missing = set(POLICY) - set(SAMPLE_BUILDER_ARGS)
    assert not missing, (
        f"POLICY kinds {sorted(missing)} have no SAMPLE_BUILDER_ARGS entry "
        "in this test — add realistic sample kwargs so the render "
        "enforcement below covers them"
    )


@pytest.mark.parametrize("kind", sorted(POLICY))
def test_render_of_every_policy_kind_is_non_empty_and_none_free(kind):
    """Every notified kind must render a real notification from its own
    builder's payload: non-empty title AND body, and no literal 'None'
    leaking from a missing/optional field (the empty-kill-switch-body bug
    class)."""
    builder = events.BUILDERS.get(kind)
    assert builder is not None, f"no builder for POLICY kind {kind!r}"
    sample = SAMPLE_BUILDER_ARGS.get(kind)
    assert sample is not None, f"no sample args for POLICY kind {kind!r}"
    msg = render(kind, builder(**sample))
    assert msg.title.strip(), f"render({kind!r}) produced an empty title"
    assert msg.body.strip(), f"render({kind!r}) produced an empty body"
    assert "None" not in msg.title, (
        f"render({kind!r}) title contains literal 'None': {msg.title!r}"
    )
    assert "None" not in msg.body, (
        f"render({kind!r}) body contains literal 'None': {msg.body!r}"
    )


def test_builder_payloads_stringify_decimals():
    """Builders own the journal-boundary convention: Decimals go in as
    strings (json.dumps(default=str) would hide a raw Decimal, but replay
    and count_events compare strings)."""
    payload = events.kill_switch_payload(
        mode="paper", equity_now=Decimal("212.50"),
        equity_open_week=Decimal("250.00"),
        pct=Decimal("-0.15"), threshold=Decimal("-0.15"),
    )
    assert payload["equity_now"] == "212.50"
    assert payload["equity_open_week"] == "250.00"
    assert payload["pct"] == "-0.15"
    assert payload["threshold"] == "-0.15"
    assert payload["mode"] == "paper"


def test_fill_builder_keeps_dispatcher_and_live_gate_keys():
    """count_live_buy_fills filters on payload side/broker_mode via SQL
    json_extract, and the fill renderer reads symbol/side/quantity/price/
    context — these keys are load-bearing and must never change."""
    payload = events.fill_payload(**SAMPLE_BUILDER_ARGS[events.KIND_FILL])
    for key in ("client_order_id", "order_id", "symbol", "side",
                "quantity", "price", "filled_at", "context", "broker_mode"):
        assert key in payload, f"fill payload lost load-bearing key {key!r}"
    assert payload["side"] == "BUY"
    assert payload["broker_mode"] == "paper"
    assert payload["filled_at"] == _TS.isoformat()


def test_generic_render_omits_none_valued_keys():
    """order_not_filled legitimately carries quantity=None/fill_price=None
    for a queued/rejected live order — an INSTANT_CRITICAL push must not
    read 'quantity=None; fill_price=None'. The generic key=value renderer
    omits None-valued keys; the load-bearing keys (order_id, status) remain."""
    from decimal import Decimal

    from ops import events
    from ops.notify.policy import render

    m = render(events.KIND_ORDER_NOT_FILLED, events.order_not_filled_payload(
        order_id="rh-1", client_order_id="c-1", symbol="AAPL", side="BUY",
        status="queued", quantity=None, fill_price=None,
    ))
    assert "None" not in m.body
    assert "order_id=rh-1" in m.body and "status=queued" in m.body

    # And a filled-shape payload still shows its numbers.
    m2 = render(events.KIND_ORDER_NOT_FILLED, events.order_not_filled_payload(
        order_id="rh-2", client_order_id="c-2", symbol="MSFT", side="SELL",
        status="failed", quantity=Decimal("1.5"), fill_price=None,
    ))
    assert "quantity=1.5" in m2.body and "fill_price" not in m2.body
