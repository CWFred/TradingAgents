from ops.notify.policy import POLICY, PolicyEntry, scrub_spot, render


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
