"""Activity breadcrumb events: kind constants + payload builders."""
from ops import events


def test_activity_kind_constants():
    assert events.KIND_ACTIVITY_STARTED == "activity_started"
    assert events.KIND_ACTIVITY_FINISHED == "activity_finished"


def test_started_payload_full():
    p = events.activity_started_payload(
        scope="item", job="overnight", stage="vetting",
        symbol="CRC", seq="2/5", reason=None,
    )
    assert p == {
        "scope": "item", "job": "overnight", "stage": "vetting",
        "symbol": "CRC", "seq": "2/5",
    }


def test_started_payload_omits_none_fields():
    p = events.activity_started_payload(
        scope="job", job="daily_cycle", reason="attempt 1 of 3",
    )
    assert p == {"scope": "job", "job": "daily_cycle", "reason": "attempt 1 of 3"}


def test_finished_payload():
    p = events.activity_finished_payload(
        scope="job", job="overnight", ok=True, duration_s=12.5,
        outcome="researched 4, vetted 2",
    )
    assert p == {
        "scope": "job", "job": "overnight", "ok": True,
        "duration_s": 12.5, "outcome": "researched 4, vetted 2",
    }


def test_finished_payload_failure_omits_outcome():
    p = events.activity_finished_payload(
        scope="item", job="daily_cycle", stage="analyzing", symbol="BAH",
        ok=False, duration_s=3.0,
    )
    assert p == {
        "scope": "item", "job": "daily_cycle", "stage": "analyzing",
        "symbol": "BAH", "ok": False, "duration_s": 3.0,
    }
