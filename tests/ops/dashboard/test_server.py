"""Dashboard HTTP server: loopback bind, routes, traversal safety."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from ops.config import OpsConfig
from ops.dashboard.server import make_server
from ops.journal import Journal


@pytest.fixture()
def base_url(tmp_path, monkeypatch):
    # Isolate the log dir so "missing file" tests don't read real machine
    # state (a live ops.out.log/ops.err.log on the dev box).
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = OpsConfig(
        journal_path=str(tmp_path / "ops.sqlite"),
        baseline_journal_path=str(tmp_path / "baseline.sqlite"),
        research_journal_path=str(tmp_path / "research.sqlite"),
        screen_store_path=str(tmp_path / "screen.sqlite"),
        memo_store_path=str(tmp_path / "memos.sqlite"),
        guardian_liveness_path=str(tmp_path / "guardian.alive"),
        research_pause_flag_path=str(tmp_path / "research.paused"),
    )
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
    assert status == 200 and b"ops dashboard" in body.lower()


def test_static_traversal_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base_url + "/..%2f..%2fconfig.py")
    assert e.value.code == 404


def test_unknown_route_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base_url + "/api/nope")
    assert e.value.code == 404
