"""Loopback-only HTTP server for the ops dashboard.

The bind host is the literal 127.0.0.1 below — deliberately not a config
knob, so no env-var typo can ever expose this beyond the machine. The
server is read-only end to end: no mutating routes exist, and everything
it serves comes from mode=ro snapshot/event readers.
"""
from __future__ import annotations

import json
import os
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ops.config import OpsConfig, load_config
from ops.dashboard.events_view import merged_events
from ops.dashboard.snapshot import build_snapshot

DEFAULT_PORT = 8321
_HOST = "127.0.0.1"
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
_MAX_LOG_LINES = 2000


def _log_files() -> dict[str, Path]:
    # Enum key -> known path. Never a client-supplied path: the querystring
    # picks a key, the server owns the mapping.
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    logs = Path(base).expanduser() / "tradingagents" / "logs"
    return {"out": logs / "ops.out.log", "err": logs / "ops.err.log"}


def _tail(path: Path, lines: int) -> str:
    try:
        with path.open("r", errors="replace") as f:
            return "".join(deque(f, maxlen=lines))
    except OSError:
        return ""


class _Handler(BaseHTTPRequestHandler):
    config: OpsConfig  # injected by make_server via subclassing

    def log_message(self, *args) -> None:  # noqa: D102 — quiet by design
        pass

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status: int = 200) -> None:
        self._send(status, "application/json",
                   json.dumps(obj, default=str).encode())

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/snapshot":
                self._send_json(build_snapshot(self.config))
            elif parsed.path == "/api/events":
                self._api_events(query)
            elif parsed.path == "/api/logs":
                self._api_logs(query)
            elif parsed.path.startswith("/api/"):
                self._send_json({"error": "not found"}, status=404)
            else:
                self._static(parsed.path)
        except (BrokenPipeError, ConnectionResetError):
            # A tab closing mid-response drops the socket; a second write
            # then raises. Swallow both so launchd's dashboard.err.log
            # doesn't fill with tracebacks for ordinary client disconnects.
            pass
        except Exception as exc:  # noqa: BLE001 — a handler crash kills the tab
            self._send_json(
                {"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def _api_events(self, query) -> None:
        limit = min(500, max(1, int(query.get("limit", ["100"])[0])))
        kinds_raw = query.get("kinds", [""])[0]
        kinds = frozenset(k for k in kinds_raw.split(",") if k) or None
        paths = {
            "momentum": self.config.journal_path,
            "research": self.config.research_journal_path,
            "baseline": self.config.baseline_journal_path,
        }
        self._send_json(merged_events(paths, limit=limit, kinds=kinds))

    def _api_logs(self, query) -> None:
        key = query.get("file", [""])[0]
        files = _log_files()
        if key not in files:
            self._send_json(
                {"error": f"file must be one of {sorted(files)}"}, status=400)
            return
        lines = min(_MAX_LOG_LINES, max(1, int(query.get("lines", ["200"])[0])))
        self._send_json({"file": key, "text": _tail(files[key], lines)})

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (_STATIC_DIR / name).resolve()
        # resolve() + is_relative_to: no client path may escape static/.
        if not target.is_relative_to(_STATIC_DIR) or not target.is_file():
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, ctype, target.read_bytes())


def make_server(config: OpsConfig, port: int) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"config": config})
    return ThreadingHTTPServer((_HOST, port), handler)


def serve(port: int | None = None) -> int:
    config = load_config()
    if port is None:
        port = int(os.environ.get("OPS_DASHBOARD_PORT", DEFAULT_PORT))
    server = make_server(config, port)
    print(f"ops dashboard (read-only): http://{_HOST}:{server.server_address[1]}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
