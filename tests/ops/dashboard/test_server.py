"""Dashboard HTTP server: loopback bind, routes, traversal safety."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from ops.config import OpsConfig
from ops.dashboard.server import make_server
from ops.journal import Journal


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    # Isolate the log dir so "missing file" tests don't read real machine
    # state (a live ops.out.log/ops.err.log on the dev box).
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return OpsConfig(
        journal_path=str(tmp_path / "ops.sqlite"),
        baseline_journal_path=str(tmp_path / "baseline.sqlite"),
        research_journal_path=str(tmp_path / "research.sqlite"),
        short_journal_path=str(tmp_path / "short.sqlite"),
        insider_journal_path=str(tmp_path / "insider.sqlite"),
        screen_store_path=str(tmp_path / "screen.sqlite"),
        memo_store_path=str(tmp_path / "memos.sqlite"),
        guardian_liveness_path=str(tmp_path / "guardian.alive"),
        research_pause_flag_path=str(tmp_path / "research.paused"),
    )


@pytest.fixture()
def base_url(cfg):
    with Journal(cfg.journal_path) as j:
        j.record_event("service_started", {"pid": 1})
    server = make_server(cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    assert host == "127.0.0.1"  # the security property, asserted directly
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read()


def test_snapshot_route_returns_json(base_url):
    status, body = _get(base_url + "/api/snapshot")
    assert status == 200
    snap = json.loads(body)
    assert "health" in snap and "sleeves" in snap


def test_events_route_with_filter(base_url):
    status, body = _get(base_url + "/api/events?limit=10&kinds=service_started")
    assert status == 200
    items = json.loads(body)
    assert items and items[0]["kind"] == "service_started"


def test_logs_route_rejects_unknown_file(base_url):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base_url + "/api/logs?file=../../etc/passwd")
    assert e.value.code == 400


def test_logs_route_missing_file_empty_text(base_url):
    status, body = _get(base_url + "/api/logs?file=out")
    assert status == 200
    assert json.loads(body)["text"] == ""


def test_index_served(base_url):
    status, body = _get(base_url + "/")
    assert status == 200 and b'src="/assets/app.js"' in body


def test_static_traversal_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base_url + "/..%2f..%2fconfig.py")
    assert e.value.code == 404


def test_unknown_route_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base_url + "/api/nope")
    assert e.value.code == 404


def test_pnl_unknown_sleeve_400(base_url):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base_url + "/api/pnl?sleeve=nope")
    assert e.value.code == 400


def test_pnl_route_returns_rows(cfg, monkeypatch):
    from datetime import datetime, timezone
    from decimal import Decimal

    # Real replay-accepted BUY: side="BUY" plus a matching order row, mirroring
    # tests/ops/dashboard/test_pnl.py::_seed_long / test_snapshot_sleeves.py.
    at = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
    with Journal(cfg.journal_path) as j:
        j.record_event("service_started", {"pid": 1})
        j.record_cash_adjustment(kind="seed", amount=Decimal("10000"), note="t")
        j.record_order(client_order_id="c1", symbol="BAH", side="BUY",
                       notional_dollars=Decimal("1000"),
                       stop_loss_price=Decimal("92"))
        j.record_fill(order_id="o1", client_order_id="c1", symbol="BAH",
                      side="BUY", quantity=Decimal("10"), price=Decimal("100"),
                      filled_at=at, stop_loss_price=Decimal("92"))
    monkeypatch.setattr(
        "ops.dashboard.server.make_yfinance_quote_source",
        lambda **_: (lambda s: Decimal("110")))
    server = make_server(cfg, port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address
    try:
        status, body = _get(f"http://127.0.0.1:{port}/api/pnl?sleeve=momentum")
        data = json.loads(body)
        assert status == 200
        assert data["sleeve"] == "momentum"
        assert data["positions"][0]["symbol"] == "BAH"
        assert data["positions"][0]["pnl_dollar"] == "100"
    finally:
        server.shutdown()
        server.server_close()


def test_events_route_merges_short_and_insider(base_url, cfg):
    with Journal(cfg.short_journal_path) as j:
        j.record_event("fill", {"side": "SHORT", "quantity": "30",
                                "symbol": "GME", "price": "24.10"})
    with Journal(cfg.insider_journal_path) as j:
        j.record_event("fill", {"side": "BUY", "quantity": "15",
                                "symbol": "OKTA", "price": "98.40"})
    status, body = _get(base_url + "/api/events?limit=50")
    assert status == 200
    sources = {e["source"] for e in json.loads(body)}
    assert {"short", "insider"} <= sources
