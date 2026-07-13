"""Merged event feed: ordering, filtering, resilient rendering."""
from ops.dashboard.events_view import merged_events, render_event
from ops.journal import Journal


def test_merge_orders_newest_first_across_sources(tmp_path):
    p1, p2 = str(tmp_path / "a.sqlite"), str(tmp_path / "b.sqlite")
    with Journal(p1) as j:
        j.record_event("service_started", {"pid": 1})
    with Journal(p2) as j:
        j.record_event("research_vetting_run", {"vetted": 2, "passed": 1})
    items = merged_events({"momentum": p1, "research": p2})
    assert len(items) == 2
    assert items[0]["at"] >= items[1]["at"]
    assert {i["source"] for i in items} == {"momentum", "research"}


def test_limit_and_kind_filter(tmp_path):
    p = str(tmp_path / "a.sqlite")
    with Journal(p) as j:
        for i in range(10):
            j.record_event("fill", {"symbol": f"S{i}", "side": "buy",
                                    "quantity": "1", "price": "10"})
        j.record_event("daily_halt", {})
    only_halt = merged_events({"m": p}, kinds=frozenset({"daily_halt"}))
    assert [i["kind"] for i in only_halt] == ["daily_halt"]
    assert len(merged_events({"m": p}, limit=5)) == 5


def test_missing_journal_contributes_nothing(tmp_path):
    items = merged_events({"m": str(tmp_path / "nope.sqlite")})
    assert items == []


def test_render_known_kind_is_a_sentence():
    text = render_event("fill", {"symbol": "XYZ", "side": "buy",
                                 "quantity": "10", "price": "34.10"})
    assert "XYZ" in text and "34.10" in text
    assert not text.startswith("fill:")  # rendered, not fallback


def test_render_unknown_kind_falls_back_compact():
    text = render_event("brand_new_kind", {"a": 1})
    assert text.startswith("brand_new_kind")
    assert len(text) <= 220
