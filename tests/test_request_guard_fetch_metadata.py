"""Daemon request guard — reject cross-site browser fetches via Fetch Metadata.

Security review 2026-10-02 (linc-nerve 0.1.0 pre-release), finding L2:
``check_request`` relied on ``Origin``, but browsers omit ``Origin`` on
no-cors GETs, so ``<img src=http://127.0.0.1:8111/stream?device=...>`` (or a
top-level navigation) on any web page reached device discovery and could start
an ``adb logcat`` stream. Browsers DO attach ``Sec-Fetch-Site`` /
``Sec-Fetch-Mode`` to every request, so the guard rejects (403 "cross-site
request") when ``Sec-Fetch-Site`` is ``cross-site``/``same-site`` or
``Sec-Fetch-Mode`` is ``no-cors``/``navigate`` — on every route. Non-browser
clients (curl, httpx, launchd health checks) send no Sec-Fetch headers and are
unaffected.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

from linc_nerve.mcp.request_guard import (
    CROSS_SITE_REJECTED,
    check_request,
    is_cross_site_fetch,
)

_CROSS_SITE = (403, "cross-site request")
_IMG_TAG_HEADERS = ("Sec-Fetch-Site: cross-site", "Sec-Fetch-Mode: no-cors")


# --- is_cross_site_fetch ------------------------------------------------------


@pytest.mark.parametrize("site", ["cross-site", "same-site", " Cross-Site "])
def test_cross_or_same_site_is_rejected(site: str) -> None:
    assert is_cross_site_fetch({"sec-fetch-site": site}) is True


@pytest.mark.parametrize("mode", ["no-cors", "navigate", "NAVIGATE"])
def test_no_cors_or_navigate_mode_is_rejected(mode: str) -> None:
    assert is_cross_site_fetch({"sec-fetch-mode": mode}) is True


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"sec-fetch-site": "same-origin", "sec-fetch-mode": "cors"},
        {"sec-fetch-site": "none", "sec-fetch-mode": "cors"},
        {"sec-fetch-site": "same-origin"},
        {"sec-fetch-mode": "same-origin"},
    ],
)
def test_headerless_and_same_origin_cors_pass(headers: dict[str, str]) -> None:
    assert is_cross_site_fetch(headers) is False


# --- check_request ------------------------------------------------------------


def test_cross_site_rejected_constant() -> None:
    assert CROSS_SITE_REJECTED == _CROSS_SITE


def test_check_request_rejects_img_tag_get() -> None:
    headers = {
        "host": "127.0.0.1:8111",
        "sec-fetch-site": "cross-site",
        "sec-fetch-mode": "no-cors",
    }
    assert check_request("GET", headers) == _CROSS_SITE


def test_check_request_curl_shaped_get_passes() -> None:
    assert check_request("GET", {"host": "127.0.0.1:8111", "accept": "*/*"}) is None


def test_host_rejection_still_wins_over_fetch_metadata() -> None:
    headers = {"host": "evil.example", "sec-fetch-site": "cross-site"}
    assert check_request("GET", headers) == (403, "host not allowed")


# --- end to end over a real socket, with recording fakes ----------------------


class _Recorder:
    """Recording fake: remembers every call, returns a canned result."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        return self.result


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


@pytest.fixture
def resolver() -> _Recorder:
    return _Recorder((None, "device not found"))


@pytest.fixture
def devices_builder() -> _Recorder:
    return _Recorder({"ok": True, "devices": [], "device_count": 0})


@pytest_asyncio.fixture
async def daemon_port(
    monkeypatch: pytest.MonkeyPatch, resolver: _Recorder, devices_builder: _Recorder
) -> AsyncGenerator[int]:
    from linc_nerve.daemon import http_router
    from linc_nerve.mcp import sse_server

    monkeypatch.delenv("NERVE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("NERVE_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setattr(sse_server, "_resolve_device", resolver)
    monkeypatch.setattr(http_router, "_build_devices", devices_builder)
    port = _pick_free_port()
    task = asyncio.create_task(sse_server.start_http_server(port=port))
    await _wait_until_accepting(port)
    yield port
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _get(port: int, path: str, *extra: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", *extra]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout=5.0)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    head, _, payload = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), payload


@pytest.mark.asyncio
async def test_e2e_img_tag_stream_is_403_without_discovery(
    daemon_port: int, resolver: _Recorder
) -> None:
    status, payload = await _get(daemon_port, "/stream?device=x", *_IMG_TAG_HEADERS)
    assert (status, json.loads(payload)) == (403, {"error": "cross-site request"})
    assert resolver.calls == []


@pytest.mark.asyncio
async def test_e2e_img_tag_devices_is_403_without_discovery(
    daemon_port: int, devices_builder: _Recorder
) -> None:
    status, payload = await _get(daemon_port, "/devices", *_IMG_TAG_HEADERS)
    assert (status, json.loads(payload)) == (403, {"error": "cross-site request"})
    assert devices_builder.calls == []


@pytest.mark.asyncio
async def test_e2e_cross_site_navigation_to_health_is_403(daemon_port: int) -> None:
    extra = ("Sec-Fetch-Site: cross-site", "Sec-Fetch-Mode: navigate")
    status, payload = await _get(daemon_port, "/health", *extra)
    assert (status, json.loads(payload)) == (403, {"error": "cross-site request"})


@pytest.mark.asyncio
async def test_e2e_curl_shaped_devices_is_200(
    daemon_port: int, devices_builder: _Recorder
) -> None:
    status, payload = await _get(daemon_port, "/devices", "User-Agent: curl/8.7.1", "Accept: */*")
    assert status == 200
    assert json.loads(payload)["device_count"] == 0
    assert len(devices_builder.calls) == 1


@pytest.mark.asyncio
async def test_e2e_curl_shaped_health_is_200(daemon_port: int) -> None:
    status, payload = await _get(daemon_port, "/health", "User-Agent: curl/8.7.1")
    assert status == 200
    assert "ok" in json.loads(payload)


@pytest.mark.asyncio
async def test_e2e_curl_shaped_stream_reaches_resolver(
    daemon_port: int, resolver: _Recorder
) -> None:
    status, _ = await _get(daemon_port, "/stream?device=x", "User-Agent: curl/8.7.1")
    assert status == 404  # recording resolver reports "device not found"
    assert resolver.calls == [(("x",), {})]


@pytest.mark.asyncio
async def test_e2e_rejection_logs_fetch_metadata(daemon_port: int) -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        status, _ = await _get(daemon_port, "/devices", *_IMG_TAG_HEADERS)
    assert status == 403
    rejected = [e for e in logs if e["event"] == "request_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "cross-site request"
    assert rejected[0]["path"] == "/devices"
