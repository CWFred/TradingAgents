"""RobinhoodMCPClient protocol + typed DTOs.

Concrete implementations:
- RealRobinhoodMCPClient — production, wraps the mcp Python SDK.
- FakeMCPClient (tests/ops/broker/fakes.py) — in-memory, deterministic.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import socket
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Coroutine, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlparse

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from ops.broker.types import OrderType, Side


class MCPUnavailable(Exception):
    """Raised when the MCP endpoint fails (network, auth, protocol error, or timeout)."""


class MCPProtocolError(Exception):
    """Raised when an MCP response doesn't match the expected shape.

    Distinguished from `MCPUnavailable`: this is a parse/shape mismatch (a
    renamed field, a missing key, an unexpected type) against a server that
    *did* respond — not a transport/timeout/auth failure. Callers should not
    treat this as "the broker is unreachable, retry later."
    """


@dataclass(frozen=True)
class AccountInfo:
    cash: Decimal
    equity: Decimal
    buying_power: Decimal


@dataclass(frozen=True)
class MCPPosition:
    symbol: str
    quantity: Decimal
    avg_price: Decimal
    shares_available_for_sells: Decimal


@dataclass(frozen=True)
class MCPOrderAck:
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal | None
    notional: Decimal | None
    status: str    # "queued" | "filled" | "rejected"
    fill_price: Decimal | None


@runtime_checkable
class RobinhoodMCPClient(Protocol):
    def get_account(self) -> AccountInfo: ...
    def get_positions(self) -> list[MCPPosition]: ...
    def get_quote(self, symbol: str) -> Decimal: ...
    def place_equity_order(
        self, *, symbol: str, side: Side,
        notional: Decimal | None, quantity: Decimal | None,
        order_type: OrderType, limit_price: Decimal | None,
        client_order_id: str,
    ) -> MCPOrderAck: ...
    def cancel_equity_order(self, order_id: str) -> None: ...


# --- Token file helpers -----------------------------------------------------
#
# Token cached on disk so the OAuth browser flow only runs on first use.
# Default location: ~/.config/tradingagents/robinhood_token.json
# Override via OPS_RH_TOKEN_PATH (used by tests to avoid touching $HOME).


def _resolve_token_path() -> Path:
    override = os.environ.get("OPS_RH_TOKEN_PATH")
    if override:
        return Path(override)
    home = Path(os.environ.get("HOME", "~")).expanduser()
    return home / ".config" / "tradingagents" / "robinhood_token.json"


def _write_token(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write with strict perms from creation — avoid a window where the
    # token file is briefly world/group readable.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
    except Exception:
        # If the file existed with laxer perms, tighten before we bail.
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
        raise


def _read_token(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open() as f:
        return json.load(f)


# --- _FileTokenStorage --------------------------------------------------------
#
# Implements the real SDK's `mcp.client.auth.TokenStorage` Protocol (verified
# via `inspect.getsource`: async `get_tokens`/`set_tokens`/`get_client_info`/
# `set_client_info`, typed `OAuthToken | None` / `OAuthClientInformationFull |
# None`). Both the bearer token and the dynamic-client-registration info the
# SDK needs to resume without re-registering are persisted in the *same*
# on-disk JSON payload the pre-existing `_read_token`/`_write_token` helpers
# already manage (0600 perms, `OPS_RH_TOKEN_PATH` override), as two top-level
# keys: `{"token": {...}, "client_info": {...}}`. Either key may be absent.
#
# `model_dump(mode="json")` is used (not the default python mode) because
# `OAuthClientInformationFull` carries pydantic `AnyHttpUrl` fields (e.g.
# `client_uri`) that `json.dump` cannot serialize directly; `mode="json"`
# renders them as plain strings, which `model_validate` parses back on read.


class _FileTokenStorage(TokenStorage):
    """`TokenStorage` backed by the token file at `path`."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def get_tokens(self) -> OAuthToken | None:
        payload = _read_token(self._path)
        if not payload or "token" not in payload:
            return None
        return OAuthToken.model_validate(payload["token"])

    async def set_tokens(self, tokens: OAuthToken) -> None:
        payload = _read_token(self._path) or {}
        payload["token"] = tokens.model_dump(mode="json", exclude_none=True)
        _write_token(self._path, payload)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        payload = _read_token(self._path)
        if not payload or "client_info" not in payload:
            return None
        return OAuthClientInformationFull.model_validate(payload["client_info"])

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        payload = _read_token(self._path) or {}
        payload["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        _write_token(self._path, payload)


# --- OAuth browser + localhost callback ---------------------------------------
#
# The SDK's `OAuthClientProvider` only invokes `redirect_handler`/
# `callback_handler` when an authorization-code grant is actually needed (no
# cached/refreshable token) — verified by reading
# `mcp.client.auth.oauth2.OAuthContext._perform_authorization_code_grant`.
# Construction of `_LocalhostOAuthCallback` below does no I/O; the socket is
# opened lazily inside `wait_for_callback`, which only ever runs on the
# worker loop, and only when the SDK decides a fresh grant is required — i.e.
# never in the default unit-test suite, which fakes out the transport before
# any real `auth_flow` executes. It is exercised for real only by the opt-in
# live tests (Task 6).
#
# A fixed localhost port is used rather than an OS-assigned ephemeral one so
# that the redirect_uri baked into the OAuth client metadata (built once, at
# provider-construction time) doesn't need to know about a socket that isn't
# bound until later. Dynamic client registration means no external
# pre-registration of this redirect_uri is required.

_OAUTH_CALLBACK_PORT = 51823

_OAUTH_CALLBACK_RESPONSE_BODY = (
    b"<html><body>Robinhood MCP authorization complete. You may close this tab.</body></html>"
)


def _parse_oauth_callback_request(request: bytes) -> tuple[str | None, str | None]:
    """Extract `code` and `state` query params from a raw HTTP GET request.

    Pure/no I/O so it can be unit-tested without a real socket. `request` is
    the raw bytes read off the accepted connection, e.g.
    `b"GET /callback?code=abc&state=xyz HTTP/1.1\\r\\n..."`.
    """
    request_line = request.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    parts = request_line.split(" ")
    if len(parts) < 2:
        return None, None
    query = parse_qs(urlparse(parts[1]).query)
    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    return code, state


class _LocalhostOAuthCallback:
    """Supplies `OAuthClientProvider`'s `redirect_handler`/`callback_handler`.

    `redirect_handler` opens the authorization URL in the user's browser;
    `callback_handler` accepts exactly one localhost connection carrying the
    `code`/`state` query params and returns them. Construction is side-effect
    free; the listening socket is opened only inside `wait_for_callback`.
    """

    def __init__(self, port: int = _OAUTH_CALLBACK_PORT) -> None:
        self.port = port

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/callback"

    async def redirect_handler(self, authorization_url: str) -> None:
        webbrowser.open(authorization_url)

    async def wait_for_callback(self) -> tuple[str, str | None]:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        sock.listen(1)
        sock.setblocking(False)
        try:
            conn, _addr = await loop.sock_accept(sock)
            try:
                data = await loop.sock_recv(conn, 65536)
                code, state = _parse_oauth_callback_request(data)
                await loop.sock_sendall(
                    conn,
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/html\r\n"
                    b"Content-Length: " + str(len(_OAUTH_CALLBACK_RESPONSE_BODY)).encode("ascii") + b"\r\n"
                    b"Connection: close\r\n\r\n" + _OAUTH_CALLBACK_RESPONSE_BODY,
                )
            finally:
                conn.close()
        finally:
            sock.close()
        if not code:
            raise MCPUnavailable("OAuth callback did not include an authorization code")
        return code, state


def _oauth_client_metadata(redirect_uri: str) -> OAuthClientMetadata:
    return OAuthClientMetadata(
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        client_name="tradingagents-ops-robinhood-mcp",
    )


# --- _AsyncWorker -------------------------------------------------------------
#
# The installed `mcp` SDK (1.28.1) is async-only, and its transports use
# anyio cancel scopes that are bound to the asyncio Task that entered them.
# A per-call `asyncio.run(...)` (or `loop.run_until_complete(...)` invoked
# repeatedly on a stored-but-idle loop) runs each call in a *new* Task, so a
# transport/session entered under one Task cannot be used from another —
# anyio raises. The fix is structural: own exactly one event loop on one
# background thread for the client's entire lifetime, and always schedule
# coroutines onto that same loop via `run_coroutine_threadsafe`, so every
# coroutine (session open in Task 2, and every tool call) runs as a Task on
# the *same* loop, in the *same* thread, satisfying anyio's task affinity.


class _AsyncWorker:
    """Owns one asyncio event loop on one daemon thread, for a client's lifetime.

    Sync callers submit coroutines (created on the calling thread, but never
    awaited there) via `submit()` (blocks for the result) or `spawn()` (fires
    the coroutine as a Task on the worker loop and returns immediately); they
    are scheduled onto the worker loop with `asyncio.run_coroutine_threadsafe`
    and actually run on the worker thread. This keeps every coroutine that
    ever touches the MCP session bound to a single loop/thread, which is
    required for the SDK's anyio-based transports (see module note above).

    Task affinity, not just loop affinity: anyio's cancel scopes (used by the
    SDK's transports/`ClientSession`) additionally require that the *same
    asyncio Task* that entered an async context manager's cancel scope is the
    one that exits it — a coroutine that enters such a CM must not `return`
    (ending its Task) and have a *different* submitted coroutine exit it
    later, even on the same loop. `spawn()` exists so a caller can start one
    long-lived Task (`RealRobinhoodMCPClient._serve`) that enters AND exits
    the session's CMs itself, rather than splitting that across two
    `submit()` calls (see `_serve`'s docstring for the full story).
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Spawn the daemon thread and block until its loop is running."""
        if self._thread is not None:
            return  # already started
        ready = threading.Event()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=_run, daemon=True, name="mcp-async-worker")
        thread.start()
        ready.wait()
        self._thread = thread

    def submit(self, coro: Coroutine, *, timeout: float):
        """Schedule `coro` on the worker loop and block for its result.

        `coro` must be created by the caller but is only ever awaited on the
        worker thread's loop (never on the calling thread) — that hand-off is
        exactly what `run_coroutine_threadsafe` is for.

        On timeout, the future (not the loop) is cancelled: the worker loop
        keeps running and remains usable for subsequent submits.
        """
        if self._loop is None or self._thread is None or not self._thread.is_alive():
            coro.close()  # avoid a "coroutine was never awaited" warning
            raise MCPUnavailable("MCP worker is not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise MCPUnavailable(f"MCP call timed out after {timeout}s") from None

    def spawn(self, coro: Coroutine) -> concurrent.futures.Future:
        """Schedule `coro` on the worker loop and return its Future immediately.

        Unlike `submit()`, this never blocks on the result — for a coroutine
        whose lifetime is intentionally longer than the calling thread's need
        for a return value (e.g. `RealRobinhoodMCPClient._serve`, which owns
        the MCP session for the client's entire lifetime on a single Task; see
        that method's docstring for why that Task-affinity matters).
        """
        if self._loop is None or self._thread is None or not self._thread.is_alive():
            coro.close()  # avoid a "coroutine was never awaited" warning
            raise MCPUnavailable("MCP worker is not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call_soon_threadsafe(self, callback) -> None:
        """Run `callback()` on the worker loop's thread, if the loop is alive.

        Used to signal an `asyncio.Event` created on the worker loop (e.g.
        `_serve`'s shutdown event) from a different thread: `Event.set()`
        wakes waiter Futures that are bound to the loop that created them, so
        it must actually run on that loop's thread, not just be called from
        anywhere.
        """
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            self._loop.call_soon_threadsafe(callback)

    def stop(self) -> None:
        """Stop the loop and join the thread. Idempotent; safe if never started."""
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5.0)
        if self._loop is not None:
            self._loop.close()
        self._loop = None
        self._thread = None


# --- RealRobinhoodMCPClient --------------------------------------------------

_RH_MCP_ENDPOINT = "https://agent.robinhood.com/mcp/trading"

# Fixed, arbitrary namespace UUID (generated once for this codebase) used to
# derive a stable `ref_id` (the MCP's idempotency key) from our own
# `client_order_id` via uuid5 — same client_order_id always resolves to the
# same ref_id (so a transient-retry re-send is deduplicated by Robinhood),
# and the result is always syntactically a valid UUID even though our own
# client_order_id values (e.g. "pem-2026-...-ab12cd34") are not.
_REF_ID_NAMESPACE = uuid.UUID("2e6f9a8c-7b1d-4f5e-9c3a-1d8e6f2b4a90")

# get_equity_orders / place_equity_order `state` values that will never
# transition to "filled" — polling must stop immediately on any of these.
_ORDER_TERMINAL_FAILURE_STATES = frozenset({"rejected", "failed", "voided", "cancelled"})


class RealRobinhoodMCPClient:
    """Concrete MCP client. Handles token load/refresh + OAuth on first run.

    Construction is side-effect-free (no network, no filesystem writes) so
    factories can build one at import time; the OAuth flow and MCP session
    are established lazily on the first call that needs them, via connect().

    SDK note (mcp==1.28.1): the installed `mcp` Python SDK is async-only —
    `mcp.client.session.ClientSession.call_tool` is a coroutine, and the
    transports (`mcp.client.streamable_http.streamablehttp_client`,
    `sse_client`, etc.) are async context managers that must be entered
    before a session can be used. There is no sync ClientSession in this
    SDK version.

    This class exposes the RobinhoodMCPClient Protocol's *sync* methods, so
    each protocol method bridges into the SDK via `_call_tool`. The bridge is
    a long-lived `_AsyncWorker`: one daemon thread owns one asyncio event
    loop for this client's entire lifetime, and every coroutine (session
    open, every tool call) is scheduled onto that same loop via
    `asyncio.run_coroutine_threadsafe`. This is required because the SDK's
    transports use anyio cancel scopes bound to the asyncio Task that entered
    them — a per-call `asyncio.run(...)` (or repeated `run_until_complete` on
    an otherwise-idle stored loop) creates a new Task each time, so a
    transport/session entered in one call can't be reused from the next.
    Routing every call through the same worker thread's loop keeps it all on
    one Task-affine loop. See `_AsyncWorker` for the mechanics.

    `connect()`/`close()` and Task affinity: it is not enough for the
    transport/session to merely run on the worker's *loop* — anyio's cancel
    scopes additionally require the CM that entered a scope to be *exited by
    that same asyncio Task*. An earlier version of this class violated that:
    `connect()` submitted one coroutine that entered the CMs and returned
    (ending its Task), and `close()` later submitted a *second*, different
    coroutine to exit them — anyio raises `RuntimeError("Attempted to exit
    cancel scope in a different task than it was entered in")` in that
    shape, which isn't even a `MCPUnavailable`/`Exception`-shaped failure the
    rest of the code was prepared to suppress, so `close()` could return
    without ever reaching `self._worker.stop()` (a leaked daemon thread).

    The fix: `_serve()` is one coroutine that owns the entire connection
    lifetime — enter through exit — on a single long-lived Task, spawned via
    `_AsyncWorker.spawn()` (fire-and-forget; `connect()` does not block on
    its result). It enters `streamablehttp_client` (auth=`OAuthClientProvider`,
    backed by `_FileTokenStorage`) and `ClientSession`, calls `initialize()`,
    publishes `self._session` and unblocks the waiting `connect()` call via a
    `threading.Event`, then blocks itself — on that SAME Task — on an
    `asyncio.Event` until `close()` signals it (via `call_soon_threadsafe`,
    since the event's waiters are Futures bound to the worker loop's
    thread). Only then do the CMs exit, still on the Task that entered them.
    OAuth itself (browser flow on first run, cached token thereafter) is
    handled transparently by `OAuthClientProvider`, not by manual code here —
    see `_LocalhostOAuthCallback`.
    """

    def __init__(
        self,
        *,
        endpoint: str = _RH_MCP_ENDPOINT,
        token_path: Path | None = None,
        connect_timeout: float = 120.0,
    ):
        self._endpoint = endpoint
        self._token_path = token_path or _resolve_token_path()
        self._connect_timeout = connect_timeout
        self._session = None  # populated by _serve(); see class docstring
        self._worker: _AsyncWorker | None = None  # started lazily; see connect()
        self._lifetime: concurrent.futures.Future | None = None  # _serve()'s Future; see connect()
        self._ready: threading.Event | None = None  # signalled by _serve() once live or failed
        self._shutdown_event: asyncio.Event | None = None  # created by _serve(); set by close()
        self._connect_error: BaseException | None = None  # set by _serve() on failure
        self._account_number: str | None = None  # resolved lazily; see _resolve_account()

    async def _serve(self) -> None:
        """Own the MCP session's entire lifetime on a single asyncio Task.

        Enters the transport + `ClientSession` CMs, initializes the session,
        publishes it (`self._session`) and unblocks `connect()` (via
        `self._ready`), then blocks on `self._shutdown_event` — on THIS SAME
        Task — until `close()` signals it. The CMs exit only after that wait
        returns, still on this Task: that's what satisfies anyio's cancel-
        scope task affinity (see class docstring, "Task affinity").

        `except BaseException` (not `Exception`) is required, not stylistic:
        `asyncio.CancelledError` derives from `BaseException`, and this Task
        can be cancelled (e.g. `connect()` timing out and cancelling
        `self._lifetime` before `self._ready` ever fires). An `except
        Exception` here would let a cancellation skip straight past this
        handler — no `_connect_error` recorded, `self._ready` never set, and
        `connect()`'s waiter would block for the full timeout instead of
        observing the failure. Catching `BaseException` guarantees the
        waiter is always unblocked and `finally` always runs.
        """
        shutdown_event = asyncio.Event()
        self._shutdown_event = shutdown_event
        try:
            callback = _LocalhostOAuthCallback()
            provider = OAuthClientProvider(
                server_url=self._endpoint,
                client_metadata=_oauth_client_metadata(callback.redirect_uri),
                storage=_FileTokenStorage(self._token_path),
                redirect_handler=callback.redirect_handler,
                callback_handler=callback.wait_for_callback,
            )
            async with streamablehttp_client(url=self._endpoint, auth=provider) as (
                read, write, _get_session_id,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()
                    await shutdown_event.wait()
        except BaseException as exc:
            self._connect_error = exc
            self._ready.set()
            raise
        finally:
            self._session = None

    def connect(self) -> None:
        """Establish the MCP session (OAuth + transport + initialize).

        Starts this client's `_AsyncWorker` if needed, then spawns `_serve()`
        as a single long-lived Task on the worker loop (`_AsyncWorker.spawn`
        — fire-and-hold, not blocking on the coroutine's result) and waits,
        on a `threading.Event`, for `_serve()` to signal either "session
        live" or "failed". See `_serve`'s docstring for why the whole
        connection lifetime must run on one Task.

        `OAuthClientProvider`'s `redirect_handler`/`callback_handler` (see
        `_LocalhostOAuthCallback`) run the browser-based authorization-code
        grant transparently, but only if the cached token in
        `_FileTokenStorage` is absent/unrefreshable — there is no separate
        manual flow.

        Any transport/handshake failure (auth error, connection refused,
        timeout, a rejected `initialize()`) is mapped to `MCPUnavailable`; a
        connect that never signals readiness within `connect_timeout` cancels
        the `_serve()` Task, then calls `close()` (idempotent, best-effort)
        to stop the worker thread before raising `MCPUnavailable` — a timed-
        out connect must never leak the daemon thread, same as a
        successfully-closed one. Either way, no connection is leaked:
        `_serve`'s `except BaseException`/`finally` (Finding 2) guarantee
        cleanup runs even on that cancellation. A subsequent `connect()`
        call is safe to retry: it rebuilds the worker from scratch.

        Idempotent: a second call while already connected is a no-op.
        """
        if self._worker is None:
            self._worker = _AsyncWorker()
            self._worker.start()
        if self._session is not None:
            return  # already connected

        self._ready = threading.Event()
        self._connect_error = None
        self._shutdown_event = None
        self._lifetime = self._worker.spawn(self._serve())

        became_ready = self._ready.wait(timeout=self._connect_timeout)

        if not became_ready:
            self._lifetime.cancel()
            self.close()  # never leak the worker thread; see close()'s contract
            raise MCPUnavailable(f"MCP connect timed out after {self._connect_timeout}s")

        error = self._connect_error
        if error is None and self._lifetime.done():
            error = self._lifetime.exception()
        if error is not None:
            raise MCPUnavailable(f"MCP handshake failed: {error}") from error

        if self._session is None:
            # Defensive: _ready only fires once _serve() has either published
            # a live session or recorded _connect_error — this should be
            # unreachable, but never return claiming success without one.
            raise MCPUnavailable("MCP connect failed: session was not established")

    def close(self) -> None:
        """Tear down the MCP session, then stop the worker thread/loop.

        Signals `_serve`'s shutdown event via `_AsyncWorker.call_soon_threadsafe`
        (required because `asyncio.Event.set()` wakes waiter Futures bound to
        the worker loop's thread) and waits for the `_serve()` Task to finish.
        The transport/session CMs exit inside `_serve`, on the SAME Task that
        entered them — no cross-task cancel-scope violation (Finding 1).

        The worker is stopped in a `finally` so it is *always* reached, even
        if waiting on `_serve()`'s result raises (e.g. it was already
        cancelled, or raised for some other reason) — a connect()'d client
        must never leak its daemon thread. Idempotent; safe if `connect()`
        was never called.
        """
        if self._worker is None:
            return
        try:
            if self._lifetime is not None:
                if self._shutdown_event is not None:
                    self._worker.call_soon_threadsafe(self._shutdown_event.set)
                try:
                    self._lifetime.result(timeout=self._connect_timeout)
                except BaseException:
                    pass  # best-effort teardown; still stop the worker below
        finally:
            self._session = None
            self._lifetime = None
            self._shutdown_event = None
            self._ready = None
            self._connect_error = None
            worker, self._worker = self._worker, None
            worker.stop()

    def _call_tool(self, name: str, arguments: dict, *, timeout: float = 30.0) -> dict:
        """Sync bridge to the SDK's async `ClientSession.call_tool`.

        Submits the coroutine to this client's `_AsyncWorker` (started in
        `connect()`, or lazily here if a test/caller set `_session` without
        going through `connect()` first — matching the existing
        `if self._session is None: self.connect()` lazy pattern used by
        every Protocol method), which runs it on the worker thread's single
        long-lived event loop and blocks this calling thread for the
        result. Unpacks the real SDK response shape
        (`mcp.types.CallToolResult`), preferring `structuredContent` when
        the server provides it.
        """
        async def _call() -> dict:
            result = await self._session.call_tool(name, arguments)
            if result.isError:
                raise MCPUnavailable(f"MCP tool '{name}' returned an error result")
            if result.structuredContent is not None:
                return result.structuredContent
            return {"content": result.content}

        if self._worker is None:
            self._worker = _AsyncWorker()
            self._worker.start()
        return self._worker.submit(_call(), timeout=timeout)

    # Protocol methods delegate to the MCP session. Each wraps narrowly:
    # `MCPProtocolError`/`MCPUnavailable` raised deliberately below (shape
    # mismatch vs. resolution refusal) propagate unchanged; anything else
    # (session/transport errors) is wrapped as MCPUnavailable. This is the
    # split the design doc calls out: a renamed field must not read as an
    # outage, and an outage must not read as "the client is broken."
    def _resolve_account(self) -> str:
        """Resolve and memoize the single agentic-allowed account number.

        Defense-in-depth beneath the GuardedBroker rule chain and the SPOT
        hard-check: an account this agent cannot actually trade
        (`agentic_allowed=False`) must never be silently used just because
        it happened to be first in `get_accounts`' list. An `OPS_RH_ACCOUNT`
        override is honored ONLY if that account exists and is
        `agentic_allowed`; otherwise it's refused outright rather than
        silently falling back to another account.
        """
        if self._account_number is not None:
            return self._account_number

        result = self._call_tool("get_accounts", {})
        data = result.get("data")
        if not isinstance(data, dict) or "accounts" not in data:
            raise MCPProtocolError("get_accounts response missing 'data.accounts'")
        accounts = data["accounts"]
        if not isinstance(accounts, list):
            raise MCPProtocolError("get_accounts response 'data.accounts' is not a list")

        override = os.environ.get("OPS_RH_ACCOUNT")
        if override:
            match = next((a for a in accounts if a.get("account_number") == override), None)
            if match is None or not match.get("agentic_allowed"):
                raise MCPUnavailable(
                    f"OPS_RH_ACCOUNT={override!r} is not an agentic-allowed Robinhood account"
                )
            self._account_number = override
            return self._account_number

        agentic = [a for a in accounts if a.get("agentic_allowed") is True]
        if not agentic:
            raise MCPUnavailable("no agentic-allowed Robinhood account available to this agent")
        account_number = agentic[0].get("account_number")
        if not account_number:
            raise MCPProtocolError("get_accounts agentic account missing 'account_number'")
        self._account_number = account_number
        return self._account_number

    def get_account(self) -> AccountInfo:
        if self._session is None:
            self.connect()
        try:
            account_number = self._resolve_account()
            result = self._call_tool("get_portfolio", {"account_number": account_number})
            data = result.get("data")
            if not isinstance(data, dict):
                raise MCPProtocolError("get_portfolio response missing 'data'")
            equity_raw = data.get("equity_value", data.get("total_value"))
            if equity_raw is None:
                raise MCPProtocolError(
                    "get_portfolio response missing 'equity_value'/'total_value'"
                )
            cash_raw = data.get("cash")
            if cash_raw is None:
                raise MCPProtocolError("get_portfolio response missing 'cash'")
            buying_power_raw = data.get("buying_power")
            if not isinstance(buying_power_raw, dict) or "buying_power" not in buying_power_raw:
                raise MCPProtocolError(
                    "get_portfolio response missing 'buying_power.buying_power'"
                )
            return AccountInfo(
                cash=Decimal(str(cash_raw)),
                equity=Decimal(str(equity_raw)),
                buying_power=Decimal(str(buying_power_raw["buying_power"])),
            )
        except (MCPUnavailable, MCPProtocolError):
            raise
        except Exception as exc:
            raise MCPUnavailable(f"get_account failed: {exc}") from exc

    def get_positions(self) -> list[MCPPosition]:
        if self._session is None:
            self.connect()
        try:
            account_number = self._resolve_account()
            result = self._call_tool("get_equity_positions", {"account_number": account_number})
            data = result.get("data")
            if not isinstance(data, dict) or "positions" not in data:
                raise MCPProtocolError("get_equity_positions response missing 'data.positions'")
            positions_raw = data["positions"]
            if not isinstance(positions_raw, list):
                raise MCPProtocolError(
                    "get_equity_positions response 'data.positions' is not a list"
                )
            # NOTE (follow-up): pagination (`cursor`/`next`) is not handled
            # here — the agentic account holds few positions today. If a
            # `next` cursor ever appears in a response, this must page
            # through results rather than silently truncating.
            positions: list[MCPPosition] = []
            for row in positions_raw:
                for key in (
                    "symbol", "quantity", "average_buy_price", "shares_available_for_sells",
                ):
                    if key not in row:
                        raise MCPProtocolError(
                            f"get_equity_positions row missing {key!r}: {row!r}"
                        )
                positions.append(MCPPosition(
                    symbol=row["symbol"],
                    quantity=Decimal(str(row["quantity"])),
                    avg_price=Decimal(str(row["average_buy_price"])),
                    shares_available_for_sells=Decimal(str(row["shares_available_for_sells"])),
                ))
            return positions
        except (MCPUnavailable, MCPProtocolError):
            raise
        except Exception as exc:
            raise MCPUnavailable(f"get_positions failed: {exc}") from exc

    @staticmethod
    def _pick_quote_price(symbol: str, quote: dict) -> str:
        """Pick `last_trade_price`, falling back to `last_non_reg_trade_price`.

        The real captured quote schema (design doc, "Ground truth captured
        live" § Quotes) carries exactly one venue timestamp —
        `venue_last_trade_time` — and it is paired with `last_trade_price`;
        there is no separate timestamp for `last_non_reg_trade_price` to
        compare against. So `last_non_reg_trade_price` is used only as a
        fallback when `last_trade_price` is absent (e.g. before any
        regular-hours trade has occurred yet today), never as a "more
        recent" override.
        """
        last = quote.get("last_trade_price")
        if last is not None:
            return last
        non_reg = quote.get("last_non_reg_trade_price")
        if non_reg is not None:
            return non_reg
        raise MCPProtocolError(
            f"get_equity_quotes quote for {symbol!r} has neither "
            "'last_trade_price' nor 'last_non_reg_trade_price'"
        )

    def get_quote(self, symbol: str) -> Decimal:
        if self._session is None:
            self.connect()
        try:
            result = self._call_tool("get_equity_quotes", {"symbols": [symbol]})
            data = result.get("data")
            if not isinstance(data, dict) or "results" not in data:
                raise MCPProtocolError("get_equity_quotes response missing 'data.results'")
            results = data["results"]
            if not isinstance(results, list) or not results:
                raise MCPProtocolError(f"get_equity_quotes returned no results for {symbol!r}")
            quote = results[0].get("quote")
            if not isinstance(quote, dict):
                raise MCPProtocolError("get_equity_quotes result missing 'quote'")
            if not quote.get("has_traded", False) or quote.get("state") != "active":
                raise MCPUnavailable(
                    f"no reliable price for {symbol}: "
                    f"has_traded={quote.get('has_traded')!r}, state={quote.get('state')!r}"
                )
            price = self._pick_quote_price(symbol, quote)
            return Decimal(str(price))
        except (MCPUnavailable, MCPProtocolError):
            raise
        except Exception as exc:
            raise MCPUnavailable(f"get_quote failed: {exc}") from exc

    def place_equity_order(
        self, *, symbol: str, side: Side,
        notional: Decimal | None, quantity: Decimal | None,
        order_type: OrderType, limit_price: Decimal | None,
        client_order_id: str,
    ) -> MCPOrderAck:
        """Place an order, then poll it to a terminal fill (or raise).

        Real `place_equity_order` acks in a non-terminal state and settles
        asynchronously — this method never returns a non-terminal
        `MCPOrderAck`; it either returns one confirmed `status="filled"`
        (satisfying `RobinhoodBroker._require_filled`) or raises.

        Notional (dollar-amount) orders require `type="market"` on the real
        endpoint — fractional shares are market-only — so `order_type` is
        ignored whenever `notional` is given; it only governs `type`/
        `limit_price` for the `quantity` path. Exactly one of
        `notional`/`quantity` must be given (mirrors the two order-placement
        call sites in `RobinhoodBroker`, which never send both).

        `ref_id` (the MCP's idempotency key, must be a UUID — unlike our own
        `client_order_id`) is derived deterministically from
        `client_order_id` via `uuid5`, so re-sending the same logical order
        (e.g. after a transient failure) reuses the same `ref_id`.
        """
        if self._session is None:
            self.connect()
        if (notional is None) == (quantity is None):
            raise ValueError("place_equity_order requires exactly one of notional/quantity")
        if notional is not None and order_type == OrderType.LIMIT:
            raise ValueError(
                "dollar-notional orders must be market; LIMIT+notional is not "
                "supported by the Robinhood dollar_amount API"
            )

        try:
            account_number = self._resolve_account()
            ref_id = str(uuid.uuid5(_REF_ID_NAMESPACE, client_order_id))
            params: dict = {
                "account_number": account_number,
                "symbol": symbol,
                "side": side.value.lower(),
                "time_in_force": "gfd",
                "market_hours": "regular_hours",
                "ref_id": ref_id,
            }
            if notional is not None:
                params["dollar_amount"] = str(notional)
                params["type"] = "market"
            else:
                params["quantity"] = str(quantity)
                params["type"] = order_type.value.lower()
                if limit_price is not None:
                    params["limit_price"] = str(limit_price)

            result = self._call_tool("place_equity_order", params)
            data = result.get("data")
            if not isinstance(data, dict) or "id" not in data:
                raise MCPProtocolError("place_equity_order response missing 'data.id'")
            order_id = data["id"]
            if data.get("state") in _ORDER_TERMINAL_FAILURE_STATES:
                raise MCPUnavailable(
                    f"order {order_id} was {data.get('state')!r} on placement"
                )

            order = self._await_fill(order_id, account_number=account_number)
            return self._order_to_ack(
                order, order_id=order_id, client_order_id=client_order_id,
                symbol=symbol, side=side,
            )
        except (MCPUnavailable, MCPProtocolError):
            raise
        except Exception as exc:
            raise MCPUnavailable(f"place_equity_order failed: {exc}") from exc

    def _await_fill(
        self, order_id: str, *, account_number: str,
        window_s: float = 30.0, poll_interval_s: float = 1.5,
        sleep_fn=time.sleep, clock_fn=time.monotonic,
    ) -> dict:
        """Poll `get_equity_orders(order_id)` until a terminal state.

        Real orders ack non-terminal (`new`/`queued`/`confirmed`/
        `unconfirmed`) and settle asynchronously; `partially_filled` is also
        non-terminal here — keep polling until it either completes to
        `filled` or the window elapses. On `filled`, returns the raw order
        dict (the caller builds the `MCPOrderAck`). On any of
        `_ORDER_TERMINAL_FAILURE_STATES`, raises `MCPUnavailable` — no
        retrying a rejected/failed/voided/cancelled order.

        On window timeout with no terminal fill, best-effort cancels the
        order (this is the home for the spec's "cancel pending orders"
        behavior) and raises `MCPUnavailable`, cancel outcome notwithstanding
        (a cancel that itself fails must not mask the original timeout).

        `sleep_fn`/`clock_fn` are injected (default `time.sleep`/
        `time.monotonic`) so unit tests drive the state machine with a fake
        clock — no real wall-clock waiting in the test suite.
        """
        deadline = clock_fn() + window_s
        while True:
            result = self._call_tool(
                "get_equity_orders", {"account_number": account_number, "order_id": order_id},
            )
            data = result.get("data")
            if not isinstance(data, dict) or "orders" not in data:
                raise MCPProtocolError("get_equity_orders response missing 'data.orders'")
            orders = data["orders"]
            if not isinstance(orders, list) or not orders:
                raise MCPProtocolError(f"get_equity_orders returned no orders for {order_id!r}")
            order = orders[0]
            state = order.get("state")
            if state == "filled":
                return order
            if state in _ORDER_TERMINAL_FAILURE_STATES:
                raise MCPUnavailable(f"order {order_id} did not fill: state={state!r}")
            if clock_fn() >= deadline:
                break
            sleep_fn(poll_interval_s)

        try:
            self.cancel_equity_order(order_id)
        except (MCPUnavailable, MCPProtocolError):
            pass  # best-effort; the timeout below is the primary signal
        raise MCPUnavailable(
            f"order {order_id} did not fill within {window_s}s; cancel requested"
        )

    @staticmethod
    def _order_to_ack(
        order: dict, *, order_id: str, client_order_id: str, symbol: str, side: Side,
    ) -> MCPOrderAck:
        """Build a terminal, filled `MCPOrderAck` from a `filled` order dict.

        `cumulative_quantity`/`average_price` are the fields that actually
        populate on fill (a fresh dollar-amount order's plain `quantity` is
        `null` until then) — falls back to `quantity` if `cumulative_quantity`
        is present-but-null or absent (falsy-fallback, not `dict.get`'s
        absent-only default). Missing either on a `state=="filled"` order is
        a shape mismatch against a server that DID respond, so it raises
        `MCPProtocolError` (fail loud), not `MCPUnavailable`.
        """
        cumulative_qty = order.get("cumulative_quantity") or order.get("quantity")
        avg_price = order.get("average_price")
        if cumulative_qty is None or avg_price is None:
            raise MCPProtocolError(
                f"filled order {order_id!r} missing 'cumulative_quantity'/'average_price': "
                f"{order!r}"
            )
        dollar_amount = order.get("dollar_based_amount")
        notional_val = (
            Decimal(str(dollar_amount["amount"]))
            if isinstance(dollar_amount, dict) and dollar_amount.get("amount") is not None
            else None
        )
        return MCPOrderAck(
            order_id=order_id, client_order_id=client_order_id,
            symbol=symbol, side=side,
            quantity=Decimal(str(cumulative_qty)),
            notional=notional_val,
            status="filled",
            fill_price=Decimal(str(avg_price)),
        )

    def cancel_equity_order(self, order_id: str) -> None:
        if self._session is None:
            self.connect()
        try:
            account_number = self._resolve_account()
            self._call_tool(
                "cancel_equity_order", {"account_number": account_number, "order_id": order_id},
            )
        except (MCPUnavailable, MCPProtocolError):
            raise
        except Exception as exc:
            raise MCPUnavailable(f"cancel_equity_order failed: {exc}") from exc
