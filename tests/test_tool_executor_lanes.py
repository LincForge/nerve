"""Per-device execution lanes for the daemon /tools executor (issue #85).

Invariants under test:
- same-serial calls SERIALIZE (per-device safety invariant, explicitly asserted)
- different-serial calls run CONCURRENTLY (no cross-device starvation)
- global (no-serial) calls are not starved by a busy device lane
- lane count is bounded (overflow serials collapse into one shared lane)

All tests use slow-op fakes — no real devices anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from linc_nerve.mcp import sse_server

# --- lane-key routing (pure unit) --------------------------------------------


def test_lane_for_device_tool_call_is_the_serial() -> None:
    lane = sse_server._lane_for_request("/tools/nerve_tap", {"device": "SER_A"})
    assert lane == "SER_A"


def test_lane_for_global_tool_call_is_global() -> None:
    assert sse_server._lane_for_request("/tools/nerve_discover", {}) == sse_server._GLOBAL_LANE


def test_lane_for_empty_or_non_string_device_is_global() -> None:
    assert sse_server._lane_for_request("/tools/nerve_tap", {"device": ""}) == (
        sse_server._GLOBAL_LANE
    )
    assert sse_server._lane_for_request("/tools/nerve_tap", {"device": 42}) == (
        sse_server._GLOBAL_LANE
    )


def test_lane_for_devices_enumeration_is_global() -> None:
    # /devices must stay serialized in the global lane — _DEVICES_CACHE in
    # http_router relies on no thread-stampede across enumeration calls.
    assert sse_server._lane_for_request("/devices", {}) == sse_server._GLOBAL_LANE


# --- executor lane lifecycle (pure unit) --------------------------------------


def test_executor_per_lane_is_singleton_and_single_worker() -> None:
    sse_server._shutdown_tool_executor()
    try:
        a1 = sse_server._get_tool_executor("SER_A")
        a2 = sse_server._get_tool_executor("SER_A")
        b = sse_server._get_tool_executor("SER_B")
        g = sse_server._get_tool_executor()
        assert a1 is a2
        assert a1 is not b
        assert g is not a1 and g is not b
        # Per-lane serialization invariant: every lane is exactly one worker.
        assert a1._max_workers == 1
        assert b._max_workers == 1
        assert g._max_workers == 1
    finally:
        sse_server._shutdown_tool_executor()


def test_default_executor_is_the_global_lane() -> None:
    # cli/main.py's poll loop calls _get_tool_executor() with no args and must
    # keep sharing the global lane (serialized with /devices + global tools).
    sse_server._shutdown_tool_executor()
    try:
        assert sse_server._get_tool_executor() is sse_server._get_tool_executor(
            sse_server._GLOBAL_LANE
        )
    finally:
        sse_server._shutdown_tool_executor()


def test_device_lane_count_is_bounded_via_overflow_lane() -> None:
    sse_server._shutdown_tool_executor()
    try:
        for i in range(sse_server._MAX_DEVICE_LANES):
            sse_server._get_tool_executor(f"SER_{i}")
        overflow1 = sse_server._get_tool_executor("SER_BEYOND_CAP_1")
        overflow2 = sse_server._get_tool_executor("SER_BEYOND_CAP_2")
        # Past the cap, new serials collapse into ONE shared overflow lane
        # (over-serialization is safe; unbounded threads are not).
        assert overflow1 is overflow2
        assert overflow1 is sse_server._get_tool_executor(sse_server._OVERFLOW_LANE)
        device_lanes = [
            k
            for k in sse_server._TOOL_EXECUTORS
            if k not in (sse_server._GLOBAL_LANE, sse_server._OVERFLOW_LANE)
        ]
        assert len(device_lanes) == sse_server._MAX_DEVICE_LANES
    finally:
        sse_server._shutdown_tool_executor()


def test_shutdown_clears_all_lanes_and_is_idempotent() -> None:
    sse_server._shutdown_tool_executor()  # no lanes yet — must not raise
    sse_server._get_tool_executor("SER_A")
    sse_server._get_tool_executor()
    sse_server._shutdown_tool_executor()
    assert sse_server._TOOL_EXECUTORS == {}
    sse_server._shutdown_tool_executor()  # second call is a no-op


# --- concurrency semantics through the real loopback server ------------------


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


_SLEEP_SECS = 0.8


class _IntervalRecorder:
    """Thread-safe (start, end) interval log for the fake slow tool."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.intervals: list[tuple[str, float, float]] = []

    def record(self, label: str, start: float, end: float) -> None:
        with self._lock:
            self.intervals.append((label, start, end))

    def for_label(self, label: str) -> list[tuple[float, float]]:
        with self._lock:
            return [(s, e) for lab, s, e in self.intervals if lab == label]


@pytest_asyncio.fixture
async def lane_server() -> AsyncGenerator[tuple[int, _IntervalRecorder]]:
    """Real server on a random port with slow fake tools that record intervals.

    - nerve_fake_lane_sleep(device=...) — device-targeted slow op
    - nerve_fake_global_sleep()          — global (no-serial) slow op
    """
    from linc_nerve.mcp.sse_server import _get_tool_registry, start_http_server

    recorder = _IntervalRecorder()

    def _fake_lane_sleep(device: str = "", **kwargs: Any) -> dict:
        start = time.monotonic()
        time.sleep(kwargs.get("sleep_s", _SLEEP_SECS))
        recorder.record(device or "__global__", start, time.monotonic())
        return {"ok": True, "device": device}

    def _fake_global_sleep(**kwargs: Any) -> dict:
        start = time.monotonic()
        time.sleep(kwargs.get("sleep_s", 0.1))
        recorder.record("__global__", start, time.monotonic())
        return {"ok": True}

    registry = _get_tool_registry()
    registry["nerve_fake_lane_sleep"] = _fake_lane_sleep
    registry["nerve_fake_global_sleep"] = _fake_global_sleep

    sse_server._shutdown_tool_executor()
    port = _pick_free_port()
    server_task = asyncio.create_task(start_http_server(port=port))
    await asyncio.sleep(0.2)
    try:
        yield port, recorder
    finally:
        registry.pop("nerve_fake_lane_sleep", None)
        registry.pop("nerve_fake_global_sleep", None)
        sse_server._shutdown_tool_executor()
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task


def _overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


@pytest.mark.asyncio
async def test_different_serials_execute_concurrently(
    lane_server: tuple[int, _IntervalRecorder],
) -> None:
    """Two concurrent /tools calls to DIFFERENT serials must overlap in time."""
    port, recorder = lane_server
    t0 = time.monotonic()
    results = await asyncio.gather(
        _http_request(port, "POST", "/tools/nerve_fake_lane_sleep", {"device": "SER_A"}),
        _http_request(port, "POST", "/tools/nerve_fake_lane_sleep", {"device": "SER_B"}),
    )
    elapsed = time.monotonic() - t0
    assert all(status == 200 for status, _ in results)

    (a_iv,) = recorder.for_label("SER_A")
    (b_iv,) = recorder.for_label("SER_B")
    assert _overlaps(a_iv, b_iv), (
        f"SER_A ran {a_iv}, SER_B ran {b_iv} — no overlap: lanes serialized cross-device"
    )
    # Belt-and-braces wall-clock bound: serialized would take >= 2 * sleep.
    assert elapsed < 2 * _SLEEP_SECS * 0.95, f"took {elapsed:.2f}s — calls serialized"


@pytest.mark.asyncio
async def test_same_serial_still_serializes(
    lane_server: tuple[int, _IntervalRecorder],
) -> None:
    """Two concurrent /tools calls to the SAME serial must NOT overlap.

    This is the per-device serialization safety invariant, asserted explicitly.
    """
    port, recorder = lane_server
    results = await asyncio.gather(
        _http_request(port, "POST", "/tools/nerve_fake_lane_sleep", {"device": "SER_A"}),
        _http_request(port, "POST", "/tools/nerve_fake_lane_sleep", {"device": "SER_A"}),
    )
    assert all(status == 200 for status, _ in results)

    intervals = recorder.for_label("SER_A")
    assert len(intervals) == 2
    first, second = sorted(intervals)
    assert not _overlaps(first, second), (
        f"same-serial calls overlapped: {first} vs {second} — "
        "per-device serialization invariant broken"
    )


@pytest.mark.asyncio
async def test_global_tool_not_starved_by_busy_device(
    lane_server: tuple[int, _IntervalRecorder],
) -> None:
    """A global (no-serial) call must complete while a device lane is busy."""
    port, _recorder = lane_server
    t0 = time.monotonic()
    device_task = asyncio.create_task(
        _http_request(
            port, "POST", "/tools/nerve_fake_lane_sleep", {"device": "SER_A", "sleep_s": 1.5}
        )
    )
    await asyncio.sleep(0.1)  # let the device call occupy its lane first
    gstatus, _ = await _http_request(port, "POST", "/tools/nerve_fake_global_sleep", {})
    global_done = time.monotonic() - t0

    dstatus, _ = await device_task
    device_done = time.monotonic() - t0

    assert gstatus == 200
    assert dstatus == 200
    assert global_done < device_done - 0.5, (
        f"global call finished at {global_done:.2f}s, device call at {device_done:.2f}s — "
        "global lane was starved behind the busy device"
    )
