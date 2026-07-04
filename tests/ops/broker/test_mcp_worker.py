"""Unit tests for `_AsyncWorker`, the daemon-thread asyncio transport.

Pure asyncio coroutines only — no network, no mcp SDK. These pin down the
concurrency contract: submit() runs a coroutine on the worker's single
long-lived loop and returns its result; a timeout cancels only the future,
not the loop, so the worker stays usable afterward; stop() is a clean,
idempotent shutdown.
"""
import asyncio
import time

import pytest

from ops.broker.mcp_client import MCPUnavailable, RealRobinhoodMCPClient, _AsyncWorker


@pytest.fixture
def worker():
    w = _AsyncWorker()
    w.start()
    yield w
    w.stop()


def test_submit_returns_coroutine_result(worker):
    async def _ok():
        return 42

    assert worker.submit(_ok(), timeout=1.0) == 42


def test_submit_timeout_raises_mcp_unavailable_and_worker_stays_usable(worker):
    async def _slow():
        await asyncio.sleep(5)
        return "should not get here"

    with pytest.raises(MCPUnavailable):
        worker.submit(_slow(), timeout=0.05)

    # The loop must still be alive and serving new work after a timeout.
    async def _ok():
        return "still alive"

    assert worker.submit(_ok(), timeout=1.0) == "still alive"


def test_stop_joins_cleanly_and_subsequent_submit_raises():
    w = _AsyncWorker()
    w.start()
    w.stop()

    async def _ok():
        return 1

    with pytest.raises(MCPUnavailable):
        w.submit(_ok(), timeout=1.0)


def test_stop_is_idempotent_and_safe_when_never_started():
    w = _AsyncWorker()
    w.stop()  # never started — must not raise
    w.stop()  # idempotent

    w2 = _AsyncWorker()
    w2.start()
    w2.stop()
    w2.stop()  # idempotent after start


def test_submit_before_start_raises_mcp_unavailable():
    w = _AsyncWorker()

    async def _ok():
        return 1

    with pytest.raises(MCPUnavailable):
        w.submit(_ok(), timeout=1.0)


def test_real_client_construction_does_no_io():
    c = RealRobinhoodMCPClient()
    assert c._worker is None


def test_connect_timeout_stops_worker_thread():
    """A connect() that never becomes ready within connect_timeout must not
    leak the worker daemon thread — the same never-leak contract close()
    documents (Finding 1, final review). Fakes `_serve` as a coroutine that
    hangs forever (no real network); connect_timeout is tiny so the test
    doesn't wait beyond a fraction of a second."""
    client = RealRobinhoodMCPClient(connect_timeout=0.05)

    async def _hanging_serve():
        await asyncio.sleep(100)

    client._serve = _hanging_serve

    with pytest.raises(MCPUnavailable, match="timed out"):
        client.connect()

    assert client._worker is None
    assert client._session is None

    # Also verify it's actually safe to retry: connect() rebuilds a fresh
    # worker rather than reusing a half-torn-down one.
    client._serve = _hanging_serve
    with pytest.raises(MCPUnavailable, match="timed out"):
        client.connect()
    assert client._worker is None
