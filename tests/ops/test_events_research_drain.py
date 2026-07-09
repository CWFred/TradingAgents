from ops import events


def test_research_drain_payloads_round_trip():
    run = events.research_drain_run_payload(
        asof="2026-07-09", screened_this_run=True, researched=4,
        failed=1, still_pending=2, hit_deadline=False,
    )
    assert run == {
        "asof": "2026-07-09", "screened_this_run": True, "researched": 4,
        "failed": 1, "still_pending": 2, "hit_deadline": False,
    }
    err = events.research_drain_error_payload(error="RuntimeError: boom")
    assert err == {"error": "RuntimeError: boom"}


def test_research_drain_kinds_registered_and_audit_only():
    for kind in (events.KIND_RESEARCH_DRAIN_RUN, events.KIND_RESEARCH_DRAIN_ERROR):
        assert kind in events.BUILDERS
        assert kind in events.AUDIT_ONLY
