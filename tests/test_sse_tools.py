"""Integration tests for HTTP tool routes on the SSE server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio


def _pick_free_port() -> int:
    """Find a free TCP port by binding to port 0."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _http_request(
    port: int,
    method: str,
    path: str,
    body: dict | None = None,
) -> tuple[int, dict]:
    """Minimal async HTTP client for testing."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    body_bytes = json.dumps(body).encode() if body else b""
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"\r\n"
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


@pytest_asyncio.fixture
async def server_port() -> AsyncGenerator[int]:
    """Start the extended SSE/HTTP server on a random port."""
    from linc_nerve.mcp.sse_server import start_http_server

    port = _pick_free_port()
    server_task = asyncio.create_task(start_http_server(port=port))
    await asyncio.sleep(0.2)  # Let server bind
    yield port
    server_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await server_task


@pytest.mark.asyncio
async def test_health_endpoint(server_port: int) -> None:
    """GET /health returns a lightweight liveness probe (no enumeration)."""
    status, body = await _http_request(server_port, "GET", "/health")
    assert status == 200
    assert body["ok"] is True
    assert "uptime_seconds" in body
    # Old shape carried `device_count` — that's now on /devices.
    assert "device_count" not in body


@pytest.mark.asyncio
async def test_devices_endpoint_is_routed_through_sse_server(server_port: int) -> None:
    """GET /devices reaches `route_request` through the actual HTTP dispatch.

    Council Carmack/Lattner/Hotz P0: without wiring `/devices` into the
    `_handle_client` routing condition in sse_server.py, this returns 404.
    Unit tests on `route_request` directly miss this gap entirely."""
    status, body = await _http_request(server_port, "GET", "/devices")
    assert status == 200
    # Either the enumeration succeeded (devices list present) or it gracefully
    # produced an empty list — either way it must NOT 404.
    assert "device_count" in body
    assert "platform_counts" in body


@pytest.mark.asyncio
async def test_oversized_body_rejected_413_over_real_socket(server_port: int) -> None:
    """linc-nerve#128/129 finding 5/6 regression guard, exercised over a real
    TCP connection: a body over the 256 KiB budget is rejected 413 — not
    masked as a 400 JSON-parse error, and not a connection reset."""
    from linc_nerve.daemon.http_router import _MAX_BODY_BYTES

    huge_text = "A" * (_MAX_BODY_BYTES + 50_000)
    status, body = await _http_request(
        server_port, "POST", "/tools/nerve_type", {"device": "PROBE", "text": huge_text}
    )
    assert status == 413
    assert "error" in body


@pytest.mark.asyncio
async def test_unknown_tool_returns_404(server_port: int) -> None:
    """POST /tools/nonexistent returns 404."""
    status, body = await _http_request(server_port, "POST", "/tools/nonexistent", {})
    assert status == 404
    assert "error" in body


@pytest.mark.asyncio
async def test_start_http_server_alias_exists() -> None:
    """start_sse_server alias must still exist for bellows backward compat."""
    from linc_nerve.mcp.sse_server import start_http_server, start_sse_server

    assert start_sse_server is start_http_server


@pytest.mark.asyncio
async def test_sse_route_still_returns_400_missing_device(server_port: int) -> None:
    """GET /stream without device still returns 400 (backward compat)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", server_port)

    request = b"GET /stream HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n"
    writer.write(request)
    await writer.drain()

    status_line = await reader.readline()
    status_code = int(status_line.split()[1])
    writer.close()
    await writer.wait_closed()

    assert status_code == 400


@pytest.mark.asyncio
async def test_start_http_server_skips_when_port_in_use_by_healthy_daemon(
    server_port: int,
) -> None:
    """When a healthy nerve daemon is already on the port, return without crashing.

    Avoids OSError on bind() when an MCP client spawns ``nerve bellows`` while a
    launchd-managed daemon is already serving :8111.
    """
    from linc_nerve.mcp.sse_server import start_http_server

    # First server is already running on `server_port`. Try to start a second.
    # Should detect the healthy daemon and return cleanly instead of raising.
    # Upper bound > probe's read timeout (5s) + connect (0.5s) + slack.
    await asyncio.wait_for(start_http_server(port=server_port), timeout=8.0)


@pytest.mark.asyncio
async def test_start_http_server_honors_nerve_http_port_env(monkeypatch: Any) -> None:
    """NERVE_HTTP_PORT env overrides default port without explicit arg."""
    import os

    from linc_nerve.mcp.sse_server import start_http_server

    port = _pick_free_port()
    monkeypatch.setenv("NERVE_HTTP_PORT", str(port))

    server_task = asyncio.create_task(start_http_server())
    await asyncio.sleep(0.2)
    try:
        # If env was honored, /health on `port` works.
        status, _ = await _http_request(port, "GET", "/health")
        assert status == 200
    finally:
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task

    # Cleanup env handled by monkeypatch fixture
    assert os.environ.get("NERVE_HTTP_PORT") == str(port)


@pytest.mark.asyncio
async def test_probe_rejects_slow_health_response() -> None:
    """A `/health` slower than the tight probe budget must be treated as
    degraded — the probe returns False and binding proceeds.

    After inbox 9abb5f9d, `/health` is a sub-50ms liveness probe; a
    slow response means the daemon is degraded or stuck and we must NOT
    treat it as "healthy existing daemon, skip binding." Council Carmack/
    Lattner P1: cut `read_timeout` to 200ms once `/health` is lightweight."""
    import asyncio

    from linc_nerve.mcp.sse_server import _probe_existing_daemon

    port = _pick_free_port()

    async def _slow_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(1.5)  # Far slower than the new tight budget.
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_slow_handler, "127.0.0.1", port)
    try:
        result = await _probe_existing_daemon("127.0.0.1", port)
        assert result is False, "slow /health must be rejected as degraded"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_accepts_fast_health_response() -> None:
    """A `/health` answering quickly must register as healthy."""
    import asyncio

    from linc_nerve.mcp.sse_server import _probe_existing_daemon

    port = _pick_free_port()

    async def _fast_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_fast_handler, "127.0.0.1", port)
    try:
        result = await _probe_existing_daemon("127.0.0.1", port)
        assert result is True, "fast /health must register as healthy"
    finally:
        server.close()
        await server.wait_closed()
