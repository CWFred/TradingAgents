"""RobinhoodMCPClient protocol + typed DTOs.

Concrete implementations:
- RealRobinhoodMCPClient — production, wraps the mcp Python SDK.
- FakeMCPClient (tests/ops/broker/fakes.py) — in-memory, deterministic.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol, runtime_checkable

from ops.broker.types import OrderType, Side


class MCPUnavailable(Exception):
    """Raised when the MCP endpoint fails (network, auth, protocol error)."""


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


# --- RealRobinhoodMCPClient --------------------------------------------------

_RH_MCP_ENDPOINT = "https://agent.robinhood.com/mcp/trading"


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
    each protocol method bridges into the SDK via `_call_tool`, which runs
    the coroutine with `asyncio.run(...)`. This is a stub bridge: `connect()`
    does not yet open the transport/session (that requires a live OAuth
    token and network endpoint), so `self._session` remains `None` and
    `_call_tool` cannot actually be exercised until Task 12's opt-in live
    tests wire up the transport and OAuth flow end-to-end. The shape here
    (method signatures, CallToolResult parsing, error mapping) is what that
    wiring will plug into.
    """

    def __init__(self, *, endpoint: str = _RH_MCP_ENDPOINT, token_path: Path | None = None):
        self._endpoint = endpoint
        self._token_path = token_path or _resolve_token_path()
        self._session = None  # populated on connect(); see class docstring
        self._loop: asyncio.AbstractEventLoop | None = None  # created lazily; see connect()

    def connect(self) -> None:
        """Load (or mint via OAuth) a token, then establish the MCP session.

        Token load/mint is synchronous and side-effect-scoped to the token
        file. Establishing the actual MCP session over the SDK's async
        transport is deferred — see class docstring — so `self._session`
        stays `None` after this call until that bridging lands.

        Also creates this client's own event loop (`self._loop`), so every
        `_call_tool` bridge for the lifetime of this client runs on the
        same loop instead of `asyncio.run()` spinning up and tearing down a
        fresh one per call. That matters once Task 12 wires up real async
        streams/subscriptions on the SDK transport — those need to stay
        bound to a single loop.
        """
        token = _read_token(self._token_path)
        if token is None:
            token = self._run_oauth_browser_flow()
            _write_token(self._token_path, token)
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        # Establishing self._session requires entering the mcp SDK's async
        # transport/session context managers (streamablehttp_client +
        # ClientSession), which is out of scope for this task — see the
        # class docstring. Left as None; wired in Task 12.

    def close(self) -> None:
        """Close this client's owned event loop, if one was created."""
        if self._loop is not None:
            self._loop.close()
            self._loop = None

    def _run_oauth_browser_flow(self) -> dict:
        raise NotImplementedError(
            "OAuth browser flow — implemented against `mcp` SDK's OAuth helper "
            "(mcp.client.auth) as part of Task 12's opt-in live tests."
        )

    def _call_tool(self, name: str, arguments: dict) -> dict:
        """Sync bridge to the SDK's async `ClientSession.call_tool`.

        Runs the coroutine to completion on this client's own event loop
        (created in `connect()`, or lazily here if a test/caller set
        `_session` without going through `connect()` first — matching the
        existing `if self._session is None: self.connect()` lazy pattern
        used by every Protocol method) and unpacks the real SDK response
        shape (`mcp.types.CallToolResult`), preferring `structuredContent`
        when the server provides it.
        """
        async def _call() -> dict:
            result = await self._session.call_tool(name, arguments)
            if result.isError:
                raise MCPUnavailable(f"MCP tool '{name}' returned an error result")
            if result.structuredContent is not None:
                return result.structuredContent
            return {"content": result.content}

        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(_call())

    # Protocol methods delegate to the MCP session with narrow try/except
    # wrapping to MCPUnavailable.
    def get_account(self) -> AccountInfo:
        if self._session is None:
            self.connect()
        try:
            result = self._call_tool("get_accounts", {})
            row = result["accounts"][0]
            return AccountInfo(
                cash=Decimal(str(row["cash"])),
                equity=Decimal(str(row["equity"])),
                buying_power=Decimal(str(row.get("buying_power", row["cash"]))),
            )
        except Exception as exc:
            raise MCPUnavailable(f"get_account failed: {exc}") from exc

    def get_positions(self) -> list[MCPPosition]:
        if self._session is None:
            self.connect()
        try:
            result = self._call_tool("get_equity_positions", {})
            return [
                MCPPosition(
                    symbol=row["symbol"],
                    quantity=Decimal(str(row["quantity"])),
                    avg_price=Decimal(str(row["average_price"])),
                )
                for row in result.get("positions", [])
            ]
        except Exception as exc:
            raise MCPUnavailable(f"get_positions failed: {exc}") from exc

    def get_quote(self, symbol: str) -> Decimal:
        if self._session is None:
            self.connect()
        try:
            result = self._call_tool("get_equity_quotes", {"symbols": [symbol]})
            row = result["quotes"][0]
            return Decimal(str(row["last_trade_price"]))
        except Exception as exc:
            raise MCPUnavailable(f"get_quote failed: {exc}") from exc

    def place_equity_order(
        self, *, symbol: str, side: Side,
        notional: Decimal | None, quantity: Decimal | None,
        order_type: OrderType, limit_price: Decimal | None,
        client_order_id: str,
    ) -> MCPOrderAck:
        if self._session is None:
            self.connect()
        params: dict = {
            "symbol": symbol,
            "side": side.value.lower(),
            "type": order_type.value.lower(),
            "client_order_id": client_order_id,
        }
        if notional is not None:
            params["notional"] = str(notional)
        if quantity is not None:
            params["quantity"] = str(quantity)
        if limit_price is not None:
            params["limit_price"] = str(limit_price)
        try:
            result = self._call_tool("place_equity_order", params)
            return MCPOrderAck(
                order_id=result["id"], client_order_id=client_order_id,
                symbol=symbol, side=side,
                quantity=Decimal(str(result["quantity"])) if result.get("quantity") is not None else None,
                notional=Decimal(str(result["notional"])) if result.get("notional") is not None else None,
                status=result["status"],
                fill_price=Decimal(str(result["fill_price"])) if result.get("fill_price") is not None else None,
            )
        except Exception as exc:
            raise MCPUnavailable(f"place_equity_order failed: {exc}") from exc

    def cancel_equity_order(self, order_id: str) -> None:
        if self._session is None:
            self.connect()
        try:
            self._call_tool("cancel_equity_order", {"id": order_id})
        except Exception as exc:
            raise MCPUnavailable(f"cancel_equity_order failed: {exc}") from exc
