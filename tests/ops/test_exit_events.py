from datetime import date

from ops import events


def test_position_opened_payload_shape_and_optional_rank():
    p = events.position_opened_payload(
        symbol="NVDA", source="MOMENTUM", entry_date=date(2026, 7, 2),
        client_order_id="pem-x", entry_rank=3,
    )
    assert p == {"symbol": "NVDA", "source": "MOMENTUM",
                 "entry_date": "2026-07-02", "client_order_id": "pem-x",
                 "entry_rank": 3}
    p2 = events.position_opened_payload(
        symbol="MSFT", source="EARNINGS", entry_date=date(2026, 7, 2),
        client_order_id="pem-y",
    )
    assert "entry_rank" not in p2


def test_exit_event_builders_registered():
    for kind in (events.KIND_POSITION_OPENED, events.KIND_EXIT_DECISION,
                 events.KIND_EXIT_ORDER_PLACED,
                 events.KIND_EXIT_SKIPPED_MISSING_DATA,
                 events.KIND_EXIT_CHECK_ERROR):
        assert kind in events.BUILDERS


def test_exit_decision_payload_shape():
    p = events.exit_decision_payload(symbol="NVDA", rule="rank_decay",
                                     evidence="rank 31 > 25")
    assert p == {"symbol": "NVDA", "rule": "rank_decay",
                 "evidence": "rank 31 > 25"}
