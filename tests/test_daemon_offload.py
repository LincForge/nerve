"""L1/L3 daemon stability: off-loop tool execution + connection cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from linc_nerve.mcp import sse_server

# --- Task 1: helpers ---------------------------------------------------------


def test_resolve_tool_timeout_default() -> None:
    assert sse_server._resolve_tool_timeout() == 180.0


def test_resolve_tool_timeout_env_override(monkeypatch: Any) -> None:
    monkeypatch.setenv("NERVE_TOOL_TIMEOUT", "0.5")
    assert sse_server._resolve_tool_timeout() == 0.5


def test_resolve_tool_timeout_invalid_falls_back(monkeypatch: Any) -> None:
    monkeypatch.setenv("NERVE_TOOL_TIMEOUT", "not-a-number")
    assert sse_server._resolve_tool_timeout() == 180.0


def test_tool_executor_is_single_worker_singleton() -> None:
    # Default (global lane) executor: still a single-worker singleton — the
    # per-device lanes added for #85 are covered in test_tool_executor_lanes.py.
    sse_server._shutdown_tool_executor()
    ex1 = sse_server._get_tool_executor()
    ex2 = sse_server._get_tool_executor()
    assert ex1 is ex2
    assert ex1._max_workers == 1
    sse_server._shutdown_tool_executor()


def test_status_text_has_504() -> None:
    assert sse_server._STATUS_TEXT[504] == "Gateway Timeout"


# --- Task 2: off-loop dispatch (real loopback server) ------------------------


def _pick_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _http_request(
    port: int, method: str, path: str, body: dict | None = None
) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body_bytes = json.dumps(body).encode() if body else b""
    request = (
        f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body_bytes)}\r\n\r\n"
    )
    writer.write(request.encode() + body_bytes)
    await writer.drain()
    status_line = await reader.readline()
    status_code = int(status_line.split()[1])
    content_length = 0
    while True:
        line = await reader.readline()
        if line == b"\r\n":
            break
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":")[1].strip())
    response_body = await reader.read(content_length)
    writer.close()
    await writer.wait_closed()
    return status_code, json.loads(response_body) if response_body else {}


_FAKE_TOOL_BLOCK_SECS = 2.0


@pytest_asyncio.fixture
async def offload_server() -> AsyncGenerator[int]:
    """Real server on a random port with a fake tool that blocks ~2s.

    The block is unconditional (a synchronous time.sleep) so the liveness test
    can race /health against it: with the fix, /health (served inline) finishes
    long before the tool; without the fix, the blocked loop makes them finish
    together. A controllable Event can't distinguish the two because under the
    bug the event loop is frozen and the test can't observe the worker mid-block.
    """
    from linc_nerve.mcp.sse_server import _get_tool_registry, start_http_server

    def _fake_block(**kwargs: Any) -> dict:
        time.sleep(_FAKE_TOOL_BLOCK_SECS)
        return {"ok": True}

    registry = _get_tool_registry()
    registry["nerve_fake_block"] = _fake_block

    port = _pick_free_port()
    server_task = asyncio.create_task(start_http_server(port=port))
    await asyncio.sleep(0.2)
    try:
        yield port
    finally:
        registry.pop("nerve_fake_block", None)
        sse_server._shutdown_tool_executor()
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task


async def _timed_request(
    t0: float, port: int, method: str, path: str, body: dict | None = None
) -> tuple[int, float]:
    """Like _http_request but returns (status, seconds-since-t0-at-completion)."""
    status, _ = await _http_request(port, method, path, body)
    return status, time.monotonic() - t0


@pytest.mark.asyncio
async def test_health_stays_live_while_tool_blocks(offload_server: int) -> None:
    """/health must complete well before a concurrent ~2s tool call.

    Racing the two requests is what proves the loop is free: with the off-loop
    fix the tool runs in the worker and /health answers immediately; without it
    the loop is blocked for ~2s and both finish together. The `health < tool - 1`
    bound fails closed on a regression.
    """
    t0 = time.monotonic()
    tool = asyncio.create_task(
        _timed_request(t0, offload_server, "POST", "/tools/nerve_fake_block", {})
    )
    health = asyncio.create_task(_timed_request(t0, offload_server, "GET", "/health"))
    (tool_status, tool_t), (health_status, health_t) = await asyncio.gather(tool, health)

    assert health_status == 200
    assert tool_status == 200
    assert health_t < tool_t - 1.0, (
        f"/health finished at {health_t:.2f}s, tool at {tool_t:.2f}s — "
        "event loop was blocked by the tool call"
    )


@pytest.mark.asyncio
async def test_tool_exceeding_timeout_returns_504(offload_server: int, monkeypatch: Any) -> None:
    """A tool that outlasts NERVE_TOOL_TIMEOUT yields 504, loop stays responsive."""
    monkeypatch.setenv("NERVE_TOOL_TIMEOUT", "0.3")

    status, body = await _http_request(offload_server, "POST", "/tools/nerve_fake_block", {})
    assert status == 504
    assert "timed out" in body["error"]

    hstatus, _ = await _http_request(offload_server, "GET", "/health")
    assert hstatus == 200


# --- Task 3: connection cleanup (L3) -----------------------------------------


class _FakeWriter:
    """Minimal StreamWriter whose drain() raises ConnectionResetError once."""

    def __init__(self, raise_on_drain: bool = True) -> None:
        self.raise_on_drain = raise_on_drain
        self.closed = False
        self._buf: list[bytes] = []

    def write(self, data: bytes) -> None:
        self._buf.append(data)

    async def drain(self) -> None:
        if self.raise_on_drain:
            raise ConnectionResetError("Connection lost")

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, _name: str, default: Any = None) -> Any:
        return default


class _FakeReader:
    """Feeds a canned GET /health request, then EOF."""

    def __init__(self) -> None:
        self._lines = [b"GET /health HTTP/1.1\r\n", b"\r\n"]

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def read(self, _n: int) -> bytes:
        return b""


@pytest.mark.asyncio
async def test_client_reset_during_drain_does_not_leak_or_raise() -> None:
    """If drain() raises ConnectionResetError, _handle_client must swallow it
    (debug-level 'client gone') and still close the writer — no CLOSE_WAIT leak."""
    writer = _FakeWriter(raise_on_drain=True)
    reader = _FakeReader()
    await sse_server._handle_client(reader, writer)  # type: ignore[arg-type]
    assert writer.closed is True


# --- Task 6: shutdown ---------------------------------------------------------


def test_shutdown_tool_executor_is_idempotent_and_nonblocking() -> None:
    """Shutdown must not raise even if called twice or with no executor."""
    sse_server._shutdown_tool_executor()  # no executor yet
    sse_server._get_tool_executor()
    sse_server._shutdown_tool_executor()
    sse_server._shutdown_tool_executor()  # second call is a no-op
    assert sse_server._TOOL_EXECUTORS == {}
