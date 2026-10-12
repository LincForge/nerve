from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linc_nerve.mcp.sse_server import _handle_client


@pytest.mark.asyncio
async def test_sse_server_missing_device() -> None:
    # Test HTTP 400 missing device
    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    reader.feed_data(b"GET /stream HTTP/1.1\r\n\r\n")
    reader.feed_eof()

    await _handle_client(reader, writer)

    writer.write.assert_any_call(b"HTTP/1.1 400 Bad Request\r\n\r\nMissing 'device' parameter")
    writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_sse_server_device_not_found() -> None:
    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    reader.feed_data(b"GET /stream?device=UNKNOWN HTTP/1.1\r\n\r\n")
    reader.feed_eof()

    with patch(
        "linc_nerve.mcp.sse_server._resolve_device", return_value=(None, "Device not found")
    ):
        await _handle_client(reader, writer)

    writer.write.assert_any_call(b"HTTP/1.1 404 Not Found\r\n\r\nDevice not found")
    writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_sse_server_stream_logs() -> None:
    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    reader.feed_data(b"GET /stream?device=ABC&filter=com.app HTTP/1.1\r\n\r\n")
    reader.feed_eof()

    mock_device = MagicMock()

    mock_ctrl = MagicMock()
    mock_ctrl.get_log_stream_cmd.return_value = ["echo", "log line 1\nError line 2"]

    with (
        patch("linc_nerve.mcp.sse_server._resolve_device", return_value=(mock_device, "android")),
        patch("linc_nerve.mcp.sse_server._get_controller", return_value=mock_ctrl),
    ):
        # Replace create_subprocess_exec to mock proc
        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_stdout = asyncio.StreamReader()
        mock_stdout.feed_data(b"log line 1\n")
        mock_stdout.feed_data(b"Exception line 2\n")
        mock_stdout.feed_eof()
        mock_proc.stdout = mock_stdout

        async def mock_exec(*args: Any, **kwargs: Any) -> Any:
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await _handle_client(reader, writer)

    # Verify SSE headers
    writer.write.assert_any_call(b"HTTP/1.1 200 OK\r\n")
    writer.write.assert_any_call(b"Content-Type: text/event-stream\r\n")

    # Verify stream content
    expected_event1 = json.dumps({"event": "log", "data": "log line 1"})
    writer.write.assert_any_call(f"data: {expected_event1}\n\n".encode())

    expected_event2 = json.dumps({"event": "log_error", "data": "Exception line 2"})
    writer.write.assert_any_call(f"data: {expected_event2}\n\n".encode())


@pytest.mark.asyncio
async def test_sse_server_stream_rejects_path_traversal_in_device() -> None:
    """linc-nerve#128/129 finding 4: GET /stream must route through the same
    pre-dispatch guards as POST /tools/* — a `../` in the device field must
    be rejected 400 BEFORE any subprocess is spawned, not silently accepted."""
    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    reader.feed_data(b"GET /stream?device=../../etc/passwd HTTP/1.1\r\n\r\n")
    reader.feed_eof()

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        await _handle_client(reader, writer)

    mock_exec.assert_not_called()
    written = b"".join(
        c.args[0] for c in writer.write.call_args_list if c.args and isinstance(c.args[0], bytes)
    )
    assert b"HTTP/1.1 400" in written


@pytest.mark.asyncio
async def test_sse_server_logcat_stream_route_rejects_path_traversal() -> None:
    """The `/tools/nerve_logcat_stream` alias route must ALSO route through
    the guards (finding 4 names both bypassed routes explicitly)."""
    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    reader.feed_data(
        b"GET /tools/nerve_logcat_stream?device=../../etc/passwd HTTP/1.1\r\n\r\n"
    )
    reader.feed_eof()

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        await _handle_client(reader, writer)

    mock_exec.assert_not_called()
    written = b"".join(
        c.args[0] for c in writer.write.call_args_list if c.args and isinstance(c.args[0], bytes)
    )
    assert b"HTTP/1.1 400" in written


@pytest.mark.asyncio
async def test_sse_server_stream_rejects_leading_dash_filter() -> None:
    """linc-nerve#128/129 round 1 finding 3: a `filter` query param
    starting with `-` on GET /stream must be rejected 400 BEFORE any
    subprocess is spawned — `filter` was in neither guard's param-name set,
    so it used to flow straight through to `create_subprocess_exec`'s argv."""
    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    reader.feed_data(b"GET /stream?device=ABC&filter=-c HTTP/1.1\r\n\r\n")
    reader.feed_eof()

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        await _handle_client(reader, writer)

    mock_exec.assert_not_called()
    written = b"".join(
        c.args[0] for c in writer.write.call_args_list if c.args and isinstance(c.args[0], bytes)
    )
    assert b"HTTP/1.1 400" in written


@pytest.mark.asyncio
async def test_sse_server_stream_rejects_device_reserved_by_another_session(
    tmp_path: Any,
) -> None:
    """GET /stream for a device reserved by a DIFFERENT nerve session is
    rejected 409 before any subprocess is spawned (finding 4)."""
    from linc_nerve.core.device_manager import DeviceManager
    from linc_nerve.mcp import _core

    db_path = tmp_path / "devices.db"
    rival = DeviceManager(db_path=db_path)
    rival.reserve("SHARED-DEVICE", purpose="rival session")
    daemon_mgr = DeviceManager(db_path=db_path)

    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    reader.feed_data(b"GET /stream?device=SHARED-DEVICE HTTP/1.1\r\n\r\n")
    reader.feed_eof()

    with (
        patch.object(_core, "_device_mgr", daemon_mgr),
        patch("asyncio.create_subprocess_exec") as mock_exec,
    ):
        await _handle_client(reader, writer)

    mock_exec.assert_not_called()
    written = b"".join(
        c.args[0] for c in writer.write.call_args_list if c.args and isinstance(c.args[0], bytes)
    )
    assert b"HTTP/1.1 409" in written
    rival.release_all()
    daemon_mgr.release_all()


@pytest.mark.asyncio
async def test_sse_server_stream_still_works_when_unguarded() -> None:
    """No false positive: an ordinary, unreserved device still streams fine
    through the new guard chain (existing happy-path behavior preserved)."""
    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    reader.feed_data(b"GET /stream?device=ABC&filter=com.app HTTP/1.1\r\n\r\n")
    reader.feed_eof()

    mock_ctrl = MagicMock()
    mock_ctrl.get_log_stream_cmd.return_value = ["echo", "log line"]

    with (
        patch("linc_nerve.mcp.sse_server._resolve_device", return_value=(MagicMock(), "android")),
        patch("linc_nerve.mcp.sse_server._get_controller", return_value=mock_ctrl),
    ):
        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_stdout = asyncio.StreamReader()
        mock_stdout.feed_data(b"log line\n")
        mock_stdout.feed_eof()
        mock_proc.stdout = mock_stdout

        async def mock_exec(*args: Any, **kwargs: Any) -> Any:
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await _handle_client(reader, writer)

    writer.write.assert_any_call(b"HTTP/1.1 200 OK\r\n")


@pytest.mark.asyncio
async def test_oversized_content_length_rejected_without_json_parsing() -> None:
    """linc-nerve#128/129 finding 8 correction: the size guard decides on
    the DECLARED Content-Length before any JSON-parsing happens — a
    genuinely cheap rejection compared to the old post-parse guess that
    already paid the cost of reading + json-parsing the hostile body first.

    The oversized branch still DRAINS (bounded — round 2 finding 4, see
    `_drain_rejected_body`'s docstring) some of the declared body off the
    socket before closing — responding and closing while the client is
    still mid-write on a large body triggers a TCP RST that the client sees
    as a connection reset instead of this clean 413 (reproduced
    intermittently without the drain). So `reader.read` IS called here —
    the guarantee under test is that the bytes are never handed to
    `json.loads`, not that the socket is never touched.
    """
    from linc_nerve.daemon.http_router import _MAX_BODY_BYTES
    from linc_nerve.mcp import sse_server

    oversized = _MAX_BODY_BYTES + 1
    reader = MagicMock()
    reader.read = AsyncMock(side_effect=lambda n: b"A" * n)
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()

    with patch("json.loads") as mock_loads:
        await sse_server._handle_json_tool_request(
            reader, writer, "POST", "/tools/nerve_type", oversized
        )

    mock_loads.assert_not_called()
    assert reader.read.await_count >= 1, "the drain must still touch the socket"
    written = b"".join(
        c.args[0] for c in writer.write.call_args_list if c.args and isinstance(c.args[0], bytes)
    )
    assert b"413" in written


@pytest.mark.asyncio
async def test_oversized_drain_never_accumulates_more_than_cap_in_memory() -> None:
    """linc-nerve#128/129 round 2 finding 4 (NEW DoS, REGRESSION):
    `readexactly(content_length)` used to accumulate the ENTIRE declared
    Content-Length in the StreamReader's internal buffer before returning —
    an attacker choosing an arbitrary declared Content-Length controlled
    the daemon's RAM (measured RSS 98 -> 167 MB draining a 200 MB
    declared / 67 MB actually-streamed body). The bounded chunked drain
    must never request more than `_DRAIN_CHUNK_BYTES` in a single `read()`
    call, and must never drain more than `_DRAIN_CAP_BYTES` total — no
    matter how large `content_length` claims to be."""
    from linc_nerve.mcp import sse_server

    huge_declared = 200 * 1024 * 1024  # 200 MB declared, mirrors the measured attack
    read_sizes: list[int] = []

    async def _bounded_read(n: int) -> bytes:
        read_sizes.append(n)
        return b"A" * n  # pretend the client actually has that many bytes ready

    reader = MagicMock()
    reader.read = _bounded_read
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()

    await sse_server._handle_json_tool_request(
        reader, writer, "POST", "/tools/nerve_type", huge_declared
    )

    assert read_sizes, "the drain must still touch the socket"
    assert all(n <= sse_server._DRAIN_CHUNK_BYTES for n in read_sizes), (
        "no single read() may request more than _DRAIN_CHUNK_BYTES"
    )
    assert sum(read_sizes) <= sse_server._DRAIN_CAP_BYTES, (
        "total drained must never exceed _DRAIN_CAP_BYTES regardless of content_length"
    )
    written = b"".join(
        c.args[0] for c in writer.write.call_args_list if c.args and isinstance(c.args[0], bytes)
    )
    assert b"413" in written


@pytest.mark.asyncio
async def test_oversized_drain_is_bounded_by_timeout() -> None:
    """A client that declares a huge Content-Length and then never actually
    sends the body must not hang the coroutine forever — the drain is
    bounded by `_DRAIN_TIMEOUT_S`, and the 413 is still sent once it elapses."""
    from linc_nerve.daemon.http_router import _MAX_BODY_BYTES
    from linc_nerve.mcp import sse_server

    async def _hangs_forever(_n: int) -> bytes:
        await asyncio.sleep(3600)
        return b""

    reader = MagicMock()
    reader.read = _hangs_forever
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()

    with patch.object(sse_server, "_DRAIN_TIMEOUT_S", 0.05):
        await sse_server._handle_json_tool_request(
            reader, writer, "POST", "/tools/nerve_type", _MAX_BODY_BYTES + 1
        )

    written = b"".join(
        c.args[0] for c in writer.write.call_args_list if c.args and isinstance(c.args[0], bytes)
    )
    assert b"413" in written


@pytest.mark.asyncio
async def test_json_tool_request_uses_readexactly_for_full_body() -> None:
    """linc-nerve#128/129 finding 5: `reader.read(n)` short-reads a large
    body under load, so the oversized guard never actually saw the full
    parsed body (it 400'd on truncated JSON first, before the 413 guard
    ever ran). `readexactly` must consume the FULL declared Content-Length
    before JSON-parsing, for any within-budget body."""
    from linc_nerve.mcp import sse_server

    body_bytes = b'{"device": "D1", "text": "hi"}'
    reader = asyncio.StreamReader()
    reader.feed_data(body_bytes)
    reader.feed_eof()
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()

    registry = {"nerve_type": lambda device="", text="": {"success": True, "text": text}}
    with patch.object(sse_server, "_get_tool_registry", return_value=registry):
        await sse_server._handle_json_tool_request(
            reader, writer, "POST", "/tools/nerve_type", len(body_bytes)
        )

    written = b"".join(
        c.args[0] for c in writer.write.call_args_list if c.args and isinstance(c.args[0], bytes)
    )
    assert b"200" in written


@pytest.mark.asyncio
async def test_json_tool_request_short_body_yields_400_not_hang() -> None:
    """A Content-Length that lies (declares more bytes than the client
    actually sends) must reject 400 cleanly, not hang or raise unhandled —
    guards `readexactly`'s IncompleteReadError."""
    from linc_nerve.mcp import sse_server

    reader = asyncio.StreamReader()
    reader.feed_data(b'{"device"')  # far short of the declared length
    reader.feed_eof()
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()

    await sse_server._handle_json_tool_request(reader, writer, "POST", "/tools/nerve_type", 10_000)

    written = b"".join(
        c.args[0] for c in writer.write.call_args_list if c.args and isinstance(c.args[0], bytes)
    )
    assert b"400" in written


@pytest.mark.asyncio
async def test_in_budget_read_is_bounded_by_timeout() -> None:
    """linc-nerve#128/129 round 1 finding 1 (REGRESSION): the round-0
    repair's in-budget `readexactly(content_length)` had NO timeout — a
    lying Content-Length (declares e.g. 100 bytes, sends fewer, then holds
    the socket open) parked the handler coroutine forever, a trivial
    unauthenticated DoS. `reader.readexactly` here never returns (models a
    StreamReader that never feeds EOF and never delivers the declared
    bytes); the fix must still answer with a clean 408 within
    `_READ_TIMEOUT_S`, not hang."""
    from linc_nerve.mcp import sse_server

    async def _hangs_forever(_n: int) -> bytes:
        await asyncio.sleep(3600)
        return b""

    reader = MagicMock()
    reader.readexactly = _hangs_forever
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()

    with patch.object(sse_server, "_READ_TIMEOUT_S", 0.05):
        await sse_server._handle_json_tool_request(
            reader, writer, "POST", "/tools/nerve_type", 100
        )

    written = b"".join(
        c.args[0] for c in writer.write.call_args_list if c.args and isinstance(c.args[0], bytes)
    )
    assert b"408" in written


@pytest.mark.asyncio
async def test_stream_guard_chain_runs_off_event_loop_thread() -> None:
    """linc-nerve#128/129 round 1 finding 6: `_reject_via_stream_guards`
    must run the (potentially sqlite-blocking, via
    `DeviceManager.guard()`'s auto-reap DELETE+commit) guard chain via
    `loop.run_in_executor`, exactly like the POST /tools/* dispatch path —
    not directly on the event loop, where a slow/blocked guard call would
    stall every other coroutine on the daemon, including `/health`."""
    import threading

    from linc_nerve.daemon import http_router

    main_thread = threading.current_thread()
    guard_threads: list[threading.Thread] = []

    def _recording_guard(tool_name: str, body: dict) -> dict | None:
        guard_threads.append(threading.current_thread())
        return None

    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    reader.feed_data(b"GET /stream?device=ABC&filter=com.app HTTP/1.1\r\n\r\n")
    reader.feed_eof()

    mock_ctrl = MagicMock()
    mock_ctrl.get_log_stream_cmd.return_value = ["echo", "log line"]

    with (
        patch.object(http_router, "_run_pre_dispatch_guards", side_effect=_recording_guard),
        patch("linc_nerve.mcp.sse_server._resolve_device", return_value=(MagicMock(), "android")),
        patch("linc_nerve.mcp.sse_server._get_controller", return_value=mock_ctrl),
    ):
        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_stdout = asyncio.StreamReader()
        mock_stdout.feed_data(b"log line\n")
        mock_stdout.feed_eof()
        mock_proc.stdout = mock_stdout

        async def mock_exec(*args: Any, **kwargs: Any) -> Any:
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await _handle_client(reader, writer)

    assert len(guard_threads) == 1
    assert guard_threads[0] is not main_thread, (
        "the guard chain ran directly on the event-loop thread — a slow "
        "guard call (e.g. DeviceManager.guard()'s sqlite DELETE+commit) "
        "would stall the whole daemon, including /health"
    )


@pytest.mark.asyncio
async def test_stream_resolve_device_runs_off_event_loop_thread() -> None:
    """audit F2: `_resolve_device` must run via `loop.run_in_executor` on the
    per-device executor lane, not on the event-loop thread where a slow or
    cache-miss discover() would stall the whole daemon, including /health."""
    import threading

    main_thread = threading.current_thread()
    resolve_threads: list[threading.Thread] = []

    def _recording_resolve(device: str) -> tuple[Any, str]:
        resolve_threads.append(threading.current_thread())
        return MagicMock(), "android"

    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    reader.feed_data(b"GET /stream?device=ABC HTTP/1.1\r\n\r\n")
    reader.feed_eof()

    mock_ctrl = MagicMock()
    mock_ctrl.get_log_stream_cmd.return_value = ["echo", "log line"]

    with (
        patch("linc_nerve.mcp.sse_server._resolve_device", side_effect=_recording_resolve),
        patch("linc_nerve.mcp.sse_server._get_controller", return_value=mock_ctrl),
    ):
        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_stdout = asyncio.StreamReader()
        mock_stdout.feed_data(b"log line\n")
        mock_stdout.feed_eof()
        mock_proc.stdout = mock_stdout

        async def mock_exec(*args: Any, **kwargs: Any) -> Any:
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await _handle_client(reader, writer)

    assert len(resolve_threads) == 1
    assert resolve_threads[0] is not main_thread, (
        "_resolve_device ran directly on the event-loop thread — a slow "
        "or cache-miss discover() would stall the whole daemon, including /health"
    )


@pytest.mark.asyncio
async def test_stream_resolve_device_does_not_stall_health() -> None:
    """audit F2: latency test. When `_resolve_device` takes 0.5s (e.g. slow
    discovery), running /stream and /health concurrently must allow /health
    to complete in under 0.2s wall time because /health is served inline on
    the event loop while /stream resolves off-loop."""
    import time

    def _slow_resolve(device: str) -> tuple[Any, str]:
        time.sleep(0.5)
        return None, "Device not found"

    stream_reader = asyncio.StreamReader()
    stream_writer = MagicMock()
    stream_writer.drain = AsyncMock()
    stream_writer.wait_closed = AsyncMock()
    stream_reader.feed_data(b"GET /stream?device=NOPE HTTP/1.1\r\n\r\n")
    stream_reader.feed_eof()

    health_reader = asyncio.StreamReader()
    health_writer = MagicMock()
    health_writer.drain = AsyncMock()
    health_writer.wait_closed = AsyncMock()
    health_reader.feed_data(b"GET /health HTTP/1.1\r\n\r\n")
    health_reader.feed_eof()

    health_elapsed = 0.0
    t0 = time.monotonic()

    async def _health() -> None:
        nonlocal health_elapsed
        await _handle_client(health_reader, health_writer)
        health_elapsed = time.monotonic() - t0

    with patch("linc_nerve.mcp.sse_server._resolve_device", side_effect=_slow_resolve):
        await asyncio.gather(
            _handle_client(stream_reader, stream_writer),
            _health(),
        )

    assert health_elapsed < 0.2, (
        f"/health took {health_elapsed:.3f}s (>= 0.2s) while /stream resolved device; "
        "device resolution on the event loop stalled /health"
    )
    stream_writer.write.assert_any_call(b"HTTP/1.1 404 Not Found\r\n\r\nDevice not found")


def test_log_events_for_line_emits_crash_event() -> None:
    from linc_nerve.mcp.sse_server import _log_events_for_line

    events = _log_events_for_line("E AndroidRuntime: FATAL EXCEPTION: main", "android")
    # A raw log event for the line, plus a structured crash event alongside it.
    assert events[0]["event"] in ("log", "log_error")
    crash = next(e for e in events if e["event"] == "app_crashed")
    assert crash["signature"] == "FATAL_EXCEPTION"
    assert crash["platform"] == "android"

    # A line that also trips the error heuristic gets a log_error + crash.
    err_events = _log_events_for_line("java.lang.NullPointerException: boom", "android")
    assert err_events[0]["event"] == "log_error"
    assert any(e["event"] == "app_crashed" for e in err_events)


def test_log_events_for_line_plain_line_no_crash() -> None:
    from linc_nerve.mcp.sse_server import _log_events_for_line

    events = _log_events_for_line("D MyApp: hello", "android")
    assert len(events) == 1
    assert events[0]["event"] == "log"


@pytest.mark.asyncio
async def test_sse_stream_pushes_app_crashed_event() -> None:
    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    reader.feed_data(b"GET /stream?device=ABC HTTP/1.1\r\n\r\n")
    reader.feed_eof()

    mock_ctrl = MagicMock()
    mock_ctrl.get_log_stream_cmd.return_value = ["echo", "x"]

    with (
        patch("linc_nerve.mcp.sse_server._resolve_device", return_value=(MagicMock(), "android")),
        patch("linc_nerve.mcp.sse_server._get_controller", return_value=mock_ctrl),
    ):
        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_stdout = asyncio.StreamReader()
        mock_stdout.feed_data(b"E AndroidRuntime: FATAL EXCEPTION: main\n")
        mock_stdout.feed_eof()
        mock_proc.stdout = mock_stdout

        async def mock_exec(*args: Any, **kwargs: Any) -> Any:
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await _handle_client(reader, writer)

    written = b"".join(
        c.args[0] for c in writer.write.call_args_list if c.args and isinstance(c.args[0], bytes)
    )
    assert b'"event": "app_crashed"' in written
    assert b'"signature": "FATAL_EXCEPTION"' in written


# --- Round-cap finding: _STATUS_TEXT coverage (linc-nerve#129, CEO hand-fix) --
#
# MINOS round-cap finding `correctness`: this PR newly emits 413
# (_read_json_tool_body / _reject_oversized) and 409 (_check_reservation_conflict,
# including on the /stream routes) through _send_json_response, but _STATUS_TEXT
# was extended with 408 only. Those two shipped with a wrong reason phrase
# (`HTTP/1.1 413 Unknown`). The existing tests assert the numeric code only
# (b"413" in written, b"HTTP/1.1 409" in written), so CI was blind to it.
#
# The guard is structural rather than "add the two missing entries", because the
# next status added to the router would reopen exactly the same hole.


def _status_literals_in(path) -> set[int]:
    """Every HTTP status literal a module hands to _send_json_response.

    Covers both shapes in use: the `{"status": NNN, "body": ...}` dicts built in
    http_router and returned through _send_json_response, and direct
    `_send_json_response(writer, NNN, ...)` calls.
    """
    import re
    src = path.read_text()
    found = {int(m) for m in re.findall(r'"status"\s*:\s*(\d{3})', src)}
    found |= {int(m) for m in re.findall(r'_send_json_response\(\s*writer\s*,\s*(\d{3})', src)}
    # Third shape (MINOS round-cap finding
    # `structural-status-guard-misses-the-error-mapper-shape`): the dispatch
    # error mapper `_error_status_and_body` returns a bare `(status, body)`
    # tuple — `return 500, {"error": ...}` — which `_dispatch_tool` forwards as
    # `{"status": status, ...}` and sse_server puts on the wire. Neither regex
    # above matches it, so the mapper (the most likely place a new status gets
    # added) was invisible to this guard.
    found |= {int(m) for m in re.findall(r'\breturn\s+(\d{3})\s*,', src)}
    return found


def test_status_text_covers_every_status_the_server_can_emit():
    """No response may ship with an 'Unknown' reason phrase."""
    from pathlib import Path

    import linc_nerve.daemon.http_router as router_mod
    import linc_nerve.mcp.sse_server as sse_mod

    emitted = _status_literals_in(Path(sse_mod.__file__)) | _status_literals_in(
        Path(router_mod.__file__)
    )
    missing = sorted(s for s in emitted if s not in sse_mod._STATUS_TEXT)
    assert not missing, (
        f"statuses emitted with no _STATUS_TEXT entry (they ship as "
        f"'HTTP/1.1 <code> Unknown'): {missing}"
    )


def test_status_literal_scan_sees_the_error_mapper_tuple_shape():
    """Non-vacuity guard for the third regex above.

    In `http_router`, 500 is emitted ONLY by `_error_status_and_body`'s bare
    tuple return (`return 500, {"error": str(exc)}` at the end of the mapper) —
    there is no `"status": 500` dict literal and no `_send_json_response(writer,
    500, ...)` call in that module. So if the scan stops matching the tuple
    shape, 500 disappears from `emitted` and
    `test_status_text_covers_every_status_the_server_can_emit` silently goes
    blind to the whole error mapper while still reporting green — which is
    exactly how the hole was introduced.
    """
    from pathlib import Path

    import linc_nerve.daemon.http_router as router_mod

    literals = _status_literals_in(Path(router_mod.__file__))
    assert 500 in literals, (
        "the status scan no longer sees `_error_status_and_body`'s tuple "
        "returns — the error mapper is unguarded"
    )


def test_status_text_has_the_two_statuses_this_pr_introduced():
    """Explicit regression for the reported pair, so the finding stays legible
    even if the structural scan above is ever refactored."""
    import linc_nerve.mcp.sse_server as sse_mod

    assert sse_mod._STATUS_TEXT[413] == "Content Too Large"
    assert sse_mod._STATUS_TEXT[409] == "Conflict"


def test_send_json_response_emits_a_real_reason_phrase_for_413_and_409():
    """End to end: the wire bytes carry the phrase, not 'Unknown'."""
    import linc_nerve.mcp.sse_server as sse_mod

    class _W:
        def __init__(self):
            self.buf = b""

        def write(self, b):
            self.buf += b

    for status, phrase in ((413, b"Content Too Large"), (409, b"Conflict")):
        w = _W()
        sse_mod._send_json_response(w, status, {"error": "x"})
        assert w.buf.startswith(f"HTTP/1.1 {status} ".encode() + phrase), w.buf[:40]
        assert b"Unknown" not in w.buf
