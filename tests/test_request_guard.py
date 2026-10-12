"""Daemon request guards — Host allowlist, browser-Origin rejection, JSON-only POST.

The :8111 daemon binds loopback, but a loopback bind alone does not stop a
browser: a malicious page can fire a cross-origin "simple" POST (no CORS
preflight for text/plain) at 127.0.0.1, and a DNS-rebinding page can read
GET /devices under its own hostname. `request_guard.check_request` closes
both: the Host header must be loopback or allowlisted (NERVE_ALLOWED_HOSTS),
any browser Origin must be allowlisted (NERVE_ALLOWED_ORIGINS), and every
POST must declare Content-Type application/json. Reject, never sanitise.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio

from linc_nerve.mcp import request_guard
from linc_nerve.mcp.request_guard import (
    allowed_hosts_from_env,
    allowed_origins_from_env,
    check_request,
)

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "[::1]"})
_JSON = {"content-type": "application/json"}


# --- allowed_hosts_from_env ---------------------------------------------------


def test_allowed_hosts_default_is_exactly_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NERVE_ALLOWED_HOSTS", raising=False)
    assert allowed_hosts_from_env() == _LOOPBACK


def test_allowed_hosts_env_entries_are_added_lowercased_and_port_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NERVE_ALLOWED_HOSTS", " Nerve-Host.Example.ts.net , box.lan:8111,,[FE80::1]:9"
    )
    assert allowed_hosts_from_env() == _LOOPBACK | {
        "nerve-host.example.ts.net",
        "box.lan",
        "[fe80::1]",
    }


def test_allowed_hosts_blank_env_is_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NERVE_ALLOWED_HOSTS", " , ")
    assert allowed_hosts_from_env() == _LOOPBACK


# --- allowed_origins_from_env -------------------------------------------------


def test_allowed_origins_default_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NERVE_ALLOWED_ORIGINS", raising=False)
    assert allowed_origins_from_env() == frozenset()


def test_allowed_origins_env_is_lowercased_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NERVE_ALLOWED_ORIGINS", " HTTPS://Dash.Example , ,http://127.0.0.1:3000")
    assert allowed_origins_from_env() == frozenset(
        {"https://dash.example", "http://127.0.0.1:3000"}
    )


# --- check_request: Host ------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.1:8111", "localhost", "LOCALHOST:18111", "[::1]", "[::1]:8111"],
)
def test_loopback_hosts_with_or_without_port_are_allowed(host: str) -> None:
    assert check_request("GET", {"host": host}, allowed_hosts=_LOOPBACK) is None


def test_missing_host_is_allowed() -> None:
    """Non-browser raw clients may omit Host; a browser never does, so a
    missing Host cannot be a rebinding or cross-origin request."""
    assert check_request("GET", {}, allowed_hosts=_LOOPBACK) is None


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:8111",
        "",
        "localhost:abc",
        "localhost:",
        "127.0.0.1.evil.example",
        "::1",
        "[::1]x",
    ],
)
def test_foreign_or_malformed_host_is_rejected_403(host: str) -> None:
    assert check_request("GET", {"host": host}, allowed_hosts=_LOOPBACK) == (
        403,
        "host not allowed",
    )


def test_allowlisted_host_with_port_is_allowed() -> None:
    hosts = _LOOPBACK | {"nerve-host.example.ts.net"}
    headers = {"host": "nerve-host.example.ts.net:8111"}
    assert check_request("GET", headers, allowed_hosts=hosts) is None


def test_host_check_reads_env_when_not_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NERVE_ALLOWED_HOSTS", "box.lan")
    assert check_request("GET", {"host": "box.lan:8111"}) is None
    assert check_request("GET", {"host": "other.lan"}) == (403, "host not allowed")


# --- check_request: Origin ----------------------------------------------------


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1:8111", ""])
def test_any_unlisted_origin_is_rejected_403(origin: str) -> None:
    headers = {"host": "127.0.0.1", "origin": origin, **_JSON}
    assert check_request("POST", headers, allowed_origins=frozenset()) == (
        403,
        "origin not allowed",
    )


def test_allowlisted_origin_is_allowed_case_insensitively() -> None:
    headers = {"host": "127.0.0.1", "origin": "HTTPS://Dash.Example", **_JSON}
    origins = frozenset({"https://dash.example"})
    assert check_request("POST", headers, allowed_origins=origins) is None


def test_origin_check_reads_env_when_not_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NERVE_ALLOWED_ORIGINS", "https://dash.example")
    assert check_request("GET", {"origin": "https://dash.example"}) is None
    assert check_request("GET", {"origin": "https://evil.example"}) == (
        403,
        "origin not allowed",
    )


def test_host_rejection_wins_over_origin_and_content_type() -> None:
    headers = {"host": "evil.example", "origin": "https://evil.example"}
    assert check_request("POST", headers, allowed_hosts=_LOOPBACK) == (403, "host not allowed")


# --- check_request: Content-Type ----------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "Application/JSON", "application/json; charset=utf-8"],
)
def test_post_with_json_content_type_is_allowed(content_type: str) -> None:
    assert check_request("POST", {"content-type": content_type}) is None


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"content-type": "text/plain"},
        {"content-type": "application/x-www-form-urlencoded"},
        {"content-type": "multipart/form-data; boundary=x"},
        {"content-type": "application/jsonx"},
    ],
)
def test_post_without_json_content_type_is_rejected_415(headers: dict[str, str]) -> None:
    assert check_request("POST", headers) == (415, "content-type must be application/json")


def test_get_needs_no_content_type() -> None:
    assert check_request("GET", {}) is None


def test_origin_rejection_wins_over_content_type() -> None:
    headers = {"origin": "https://evil.example", "content-type": "text/plain"}
    assert check_request("POST", headers, allowed_origins=frozenset()) == (
        403,
        "origin not allowed",
    )


# --- sse_server wiring --------------------------------------------------------


@pytest.mark.asyncio
async def test_read_headers_returns_lowercased_dict() -> None:
    from linc_nerve.mcp.sse_server import _read_headers

    reader = asyncio.StreamReader()
    reader.feed_data(
        b"Host: LocalHost:8111\r\nContent-Type:  application/json \r\n"
        b"X-Odd\r\nContent-Length: 12\r\n\r\nleftover"
    )
    headers = await _read_headers(reader)
    assert headers == {
        "host": "LocalHost:8111",
        "content-type": "application/json",
        "content-length": "12",
    }


def test_content_length_parses_and_defaults_to_zero() -> None:
    from linc_nerve.mcp.sse_server import _content_length

    assert _content_length({"content-length": "42"}) == 42
    assert _content_length({"content-length": "nope"}) == 0
    assert _content_length({}) == 0


def test_status_text_has_403_and_415() -> None:
    from linc_nerve.mcp import sse_server

    assert sse_server._STATUS_TEXT[403] == "Forbidden"
    assert sse_server._STATUS_TEXT[415] == "Unsupported Media Type"


def test_sse_server_source_has_no_wildcard_cors() -> None:
    from linc_nerve.mcp import sse_server

    source = Path(sse_server.__file__).read_text()
    assert "Access-Control-Allow-Origin" not in source


def test_request_guard_module_name_is_pinned() -> None:
    assert request_guard.__name__ == "linc_nerve.mcp.request_guard"


# --- end to end over a real socket --------------------------------------------


def _fake_echo(**kwargs: object) -> dict:
    return {"success": True, "echo": kwargs}


def _pick_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until_accepting(port: int, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.02)
            continue
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return
    raise TimeoutError(f"daemon on port {port} never started accepting connections")


@pytest_asyncio.fixture
async def guarded_port(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[int]:
    from linc_nerve.mcp import sse_server

    monkeypatch.delenv("NERVE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("NERVE_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setattr(sse_server, "_TOOL_REGISTRY", {"nerve_fake_echo": _fake_echo})
    port = _pick_free_port()
    task = asyncio.create_task(sse_server.start_http_server(port=port))
    await _wait_until_accepting(port)
    yield port
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _raw(port: int, head: str, body: bytes = b"") -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode() + b"\r\n" + body)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout=5.0)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    head_bytes, _, payload = raw.partition(b"\r\n\r\n")
    return int(head_bytes.split()[1]), json.loads(payload) if payload else {}


def _post_head(body: bytes, *extra: str) -> str:
    lines = ["POST /tools/nerve_fake_echo HTTP/1.1", "Host: 127.0.0.1", *extra]
    lines.append(f"Content-Length: {len(body)}")
    return "\r\n".join(lines) + "\r\n"


@pytest.mark.asyncio
async def test_e2e_cross_origin_simple_post_is_403(guarded_port: int) -> None:
    body = b'{"x": 1}'
    head = _post_head(body, "Origin: https://evil.example", "Content-Type: text/plain")
    assert await _raw(guarded_port, head, body) == (403, {"error": "origin not allowed"})


@pytest.mark.asyncio
async def test_e2e_cross_origin_json_post_is_403(guarded_port: int) -> None:
    body = b'{"x": 1}'
    head = _post_head(body, "Origin: https://evil.example", "Content-Type: application/json")
    assert await _raw(guarded_port, head, body) == (403, {"error": "origin not allowed"})


@pytest.mark.asyncio
async def test_e2e_dns_rebinding_get_devices_is_403(guarded_port: int) -> None:
    head = "GET /devices HTTP/1.1\r\nHost: evil.example:8111\r\n"
    assert await _raw(guarded_port, head) == (403, {"error": "host not allowed"})


@pytest.mark.asyncio
async def test_e2e_rebinding_on_stream_route_is_403(guarded_port: int) -> None:
    head = "GET /stream?device=ABC HTTP/1.1\r\nHost: evil.example\r\n"
    assert await _raw(guarded_port, head) == (403, {"error": "host not allowed"})


@pytest.mark.asyncio
async def test_e2e_post_missing_content_type_is_415(guarded_port: int) -> None:
    body = b'{"x": 1}'
    assert await _raw(guarded_port, _post_head(body), body) == (
        415,
        {"error": "content-type must be application/json"},
    )


@pytest.mark.asyncio
async def test_e2e_json_post_without_origin_is_dispatched(guarded_port: int) -> None:
    body = b'{"x": 1}'
    head = _post_head(body, "Content-Type: application/json")
    status, payload = await _raw(guarded_port, head, body)
    assert status == 200
    assert payload == {"result": {"success": True, "echo": {"x": 1}}}


@pytest.mark.asyncio
async def test_e2e_health_on_loopback_host_is_200(guarded_port: int) -> None:
    status, payload = await _raw(guarded_port, "GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n")
    assert status == 200
    assert payload["ok"] in (True, False)


@pytest.mark.asyncio
async def test_e2e_response_carries_no_cors_header(guarded_port: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", guarded_port)
    writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout=5.0)
    writer.close()
    assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"access-control-allow-origin" not in raw.lower()


@pytest.mark.asyncio
async def test_e2e_rejection_is_logged_with_offending_host_and_origin(
    guarded_port: int,
) -> None:
    from structlog.testing import capture_logs

    body = b"{}"
    head = (
        "POST /tools/nerve_fake_echo HTTP/1.1\r\nHost: evil.example\r\n"
        "Origin: https://evil.example\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
    )
    with capture_logs() as logs:
        status, _ = await _raw(guarded_port, head, body)
    assert status == 403
    rejected = [e for e in logs if e["event"] == "request_rejected"]
    assert len(rejected) == 1
    entry = rejected[0]
    assert entry["status"] == 403
    assert entry["reason"] == "host not allowed"
    assert entry["method"] == "POST"
    assert entry["path"] == "/tools/nerve_fake_echo"
    assert entry["host"] == "evil.example"
    assert entry["origin"] == "https://evil.example"
    assert entry["log_level"] == "warning"
