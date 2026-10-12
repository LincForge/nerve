"""Server-Sent Events (SSE) server for real-time log streaming.

Implements a WebDriver BiDi / SSE style event subscription model so that
clients can listen to device logs continuously instead of polling.

Also handles POST /tools/* (JSON tool calls), GET /health (liveness),
and GET /devices (fleet enumeration) via the HTTP router.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib
import json
import os
import pkgutil
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import structlog

from linc_nerve.core.crash_watcher import classify_log_line_crash
from linc_nerve.core.http_port import DEFAULT_HTTP_PORT, resolve_http_port
from linc_nerve.mcp._core import _ensure_initialized, _get_controller, _resolve_device
from linc_nerve.mcp.request_guard import check_request

log = structlog.get_logger().bind(domain="sse_server")

SSE_PORT = DEFAULT_HTTP_PORT

# Bound on draining a rejected-oversized body off the socket before closing
# the connection (see `_read_json_tool_body`) — a client that declares a
# huge Content-Length and never actually sends it must not hang the
# coroutine forever.
_DRAIN_TIMEOUT_S = 5.0

# Bound on reading a normal (in-budget) POST body off the socket
# (linc-nerve#128/129 round 1 finding 1 — REGRESSION). The round-0 repair's
# `readexactly(content_length)` in the in-budget branch had NO timeout: a
# client that declares a genuine (in-budget) Content-Length and then either
# never finishes sending the body, or sends fewer bytes and holds the
# connection open, parks the handler coroutine forever — an unauthenticated
# DoS (each parked handler occupies a connection indefinitely). The
# oversized branch already had exactly this protection via
# `_DRAIN_TIMEOUT_S`; this mirrors it for the in-budget read.
_READ_TIMEOUT_S = 10.0

# Chunk size + memory cap for draining a REJECTED oversized body off the
# socket (linc-nerve#128/129 round 2 finding 4 — NEW DoS, REGRESSION).
# `content_length` here is UNVALIDATED ATTACKER INPUT parsed straight out
# of the request headers by `_read_headers`; the round-1 drain used
# `readexactly(content_length)`, which ACCUMULATES the entire declared
# length in the StreamReader's internal buffer before returning, regardless
# of how many bytes the client actually sends — measured RSS 98 -> 167 MB
# draining a `Content-Length: 209715200` (200 MB) request with only 67 MB
# actually streamed. `_DRAIN_CHUNK_BYTES` bounds each individual `read()`
# call; `_DRAIN_CAP_BYTES` bounds the TOTAL ever drained regardless of what
# the client declares, so memory stays flat no matter the declared length —
# at the cost of occasionally still triggering the client-observed TCP RST
# (see `_read_json_tool_body`'s docstring) for a body larger than the cap,
# an acceptable trade against unbounded memory growth.
_DRAIN_CHUNK_BYTES = 65_536  # 64 KiB per read() call
_DRAIN_CAP_BYTES = 1_048_576  # 1 MiB total drained, independent of content_length


def _warn_invalid_http_port(value: str) -> None:
    log.warning("nerve_http_port_invalid", value=value)


def _resolve_http_port(explicit: int | None) -> int:
    """Pick the port: explicit arg > NERVE_HTTP_PORT env > SSE_PORT default.

    Delegates to the side-effect-free ``core.http_port`` resolver that
    ``nerve doctor`` shares, so client probe and server bind never drift.
    """
    return resolve_http_port(explicit, on_invalid=_warn_invalid_http_port)


# Per-lane single-worker pools for off-loop tool execution (issue #85).
#
# One lane per device serial + one global lane: same-serial calls serialize
# through their lane's single worker (the per-device safety invariant — no new
# concurrency on a single device's adb/simctl channel), while calls targeting
# DIFFERENT serials run concurrently, so one slow per-device op (a long BVT
# step) can no longer starve every other device's in-flight calls.
#
# The GLOBAL lane carries everything without a device serial: /devices
# enumeration (whose _DEVICES_CACHE in http_router relies on no
# thread-stampede), no-serial tools like nerve_discover, and the cli/main.py
# poll loop (which calls _get_tool_executor() with no args). Keeping all of
# those on ONE worker preserves their pre-existing mutual serialization.
#
# Lane count is bounded: past _MAX_DEVICE_LANES distinct serials, new serials
# collapse into a single shared overflow lane (over-serialization is safe;
# unbounded thread growth is not). Lazily created so imports stay cheap and
# tests can reset via _shutdown_tool_executor().
_TOOL_EXECUTORS: dict[str, ThreadPoolExecutor] = {}
_TOOL_EXECUTORS_LOCK = threading.Lock()
_GLOBAL_LANE = "__global__"
_OVERFLOW_LANE = "__overflow__"
_MAX_DEVICE_LANES = 64  # comfortably above the 54-device farm

_DEFAULT_TOOL_TIMEOUT = 180.0  # seconds — above the longest legit op (iOS install ~120s)


def _lane_for_request(path: str, body: dict) -> str:
    """Execution lane key for a /tools request: device serial, or the global lane.

    Any request whose JSON body carries a non-empty string ``device`` is routed
    to that serial's lane; everything else (global tools, /devices, malformed
    device values) shares the global lane.
    """
    device = body.get("device") if isinstance(body, dict) else None
    if isinstance(device, str) and device:
        return device
    return _GLOBAL_LANE


def _get_tool_executor(lane: str = _GLOBAL_LANE) -> ThreadPoolExecutor:
    """Return the single-worker executor for ``lane``, creating it lazily.

    Each lane is exactly one worker — that is the serialization invariant.
    Past _MAX_DEVICE_LANES distinct device lanes, new serials share the
    overflow lane so lane/thread count stays bounded.
    """
    with _TOOL_EXECUTORS_LOCK:
        executor = _TOOL_EXECUTORS.get(lane)
        if executor is not None:
            return executor

        if lane not in (_GLOBAL_LANE, _OVERFLOW_LANE):
            device_lane_count = sum(
                1 for key in _TOOL_EXECUTORS if key not in (_GLOBAL_LANE, _OVERFLOW_LANE)
            )
            if device_lane_count >= _MAX_DEVICE_LANES:
                log.warning("tool_lane_overflow", lane=lane, cap=_MAX_DEVICE_LANES)
                lane = _OVERFLOW_LANE
                existing = _TOOL_EXECUTORS.get(lane)
                if existing is not None:
                    return existing

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"nerve-tool-{lane}")
        _TOOL_EXECUTORS[lane] = executor
        return executor


def _shutdown_tool_executor() -> None:
    """Tear down all tool-executor lanes without blocking on stuck workers.

    wait=False + cancel_futures=True drops queued work and returns immediately so
    a worker stuck on an undiscovered unbounded subprocess can't block process
    exit (the default wait=True would reproduce the SIGKILL-on-shutdown hang).
    """
    with _TOOL_EXECUTORS_LOCK:
        for executor in _TOOL_EXECUTORS.values():
            executor.shutdown(wait=False, cancel_futures=True)
        _TOOL_EXECUTORS.clear()


def _resolve_tool_timeout() -> float:
    """Per-tool-call wall-clock ceiling: NERVE_TOOL_TIMEOUT env > default 180s."""
    env = os.environ.get("NERVE_TOOL_TIMEOUT")
    if env:
        try:
            return float(env)
        except ValueError:
            log.warning("nerve_tool_timeout_invalid", value=env)
    return _DEFAULT_TOOL_TIMEOUT


async def _probe_existing_daemon(
    host: str,
    port: int,
    connect_timeout: float = 0.5,
    read_timeout: float = 0.2,
) -> bool:
    """Return True if a healthy nerve daemon already answers on host:port.

    Sends a minimal GET /health request and checks for a 200 status line.
    Connection refused → no daemon → False (port is ours to bind).

    Connect timeout stays tight (0.5s) — TCP-listen reachability is fast on
    localhost. Read timeout is also tight (200ms): after inbox 9abb5f9d,
    ``/health`` is a sub-50ms liveness probe; a slow response means the
    existing daemon is degraded or stuck, and we'd rather fail the probe
    and surface the bind error than skip binding under false confidence.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=connect_timeout
        )
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False
    try:
        writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout=read_timeout)
        return b" 200 " in status_line
    except (TimeoutError, OSError):
        return False
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


# Lazy-initialised registry: tool_name → sync callable
_TOOL_REGISTRY: dict[str, object] | None = None


def _discover_tool_modules() -> list[str]:
    """Derive the tool-module list from the package contents on disk.

    Every ``linc_nerve/mcp/*_tools.py`` module is a tool module by convention
    (enforced by tests/test_deploy_determinism.py, which also catches a
    registrations module named WITHOUT the suffix). Deriving the list — instead
    of hand-maintaining it — kills the dual-registration footgun (#37/#38/#79):
    a new tool module is picked up here AND by server.py (which imports this
    list), so it can never be live on stdio but 404 over :8111.
    """
    package_dir = os.path.dirname(os.path.abspath(__file__))
    return sorted(
        f"linc_nerve.mcp.{module_info.name}"
        for module_info in pkgutil.iter_modules([package_dir])
        if module_info.name.endswith("_tools")
    )


_TOOL_MODULES = _discover_tool_modules()


def _get_tool_registry() -> dict[str, object]:
    """Lazily build a map of tool_name → sync callable from all nerve modules.

    Scans each module for functions named ``_nerve_<name>_sync`` and maps
    them to the tool name ``nerve_<name>``.
    """
    global _TOOL_REGISTRY
    if _TOOL_REGISTRY is not None:
        return _TOOL_REGISTRY

    registry: dict[str, object] = {}
    for module_path in _TOOL_MODULES:
        try:
            mod = importlib.import_module(module_path)
        except Exception as exc:  # pragma: no cover
            log.warning("tool_module_import_failed", module=module_path, error=str(exc))
            continue

        for attr_name in dir(mod):
            if attr_name.startswith("_nerve_") and attr_name.endswith("_sync"):
                # Strip leading `_` and trailing `_sync` → tool name
                tool_name = attr_name[1:-5]  # removes "_" prefix and "_sync" suffix
                registry[tool_name] = getattr(mod, attr_name)

    _TOOL_REGISTRY = registry
    log.info("tool_registry_built", count=len(registry))
    return registry


#: Reason phrases for every status this server can put on the wire.
#:
#: MUST cover every status reachable by ``_send_json_response`` — both the
#: literals passed here directly and the ``{"status": NNN}`` dicts built in
#: ``linc_nerve.daemon.http_router`` and forwarded through this function. A
#: missing entry does not fail loudly; it silently ships
#: ``HTTP/1.1 <code> Unknown``, and a test asserting only the numeric code
#: cannot see it (that is exactly how 413 and 409 shipped wrong).
#: ``test_status_text_covers_every_status_the_server_can_emit`` enforces this
#: structurally, so adding a status to the router without a phrase here fails.
_STATUS_TEXT: dict[int, str] = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    408: "Request Timeout",
    409: "Conflict",
    # RFC 9110 renamed 413 from "Payload Too Large" to "Content Too Large".
    413: "Content Too Large",
    415: "Unsupported Media Type",
    500: "Internal Server Error",
    504: "Gateway Timeout",
}


def _send_json_response(writer: asyncio.StreamWriter, status: int, body: dict) -> None:
    """Write a complete HTTP/1.1 JSON response."""
    payload = json.dumps(body).encode("utf-8")
    status_text = _STATUS_TEXT.get(status, "Unknown")
    writer.write(f"HTTP/1.1 {status} {status_text}\r\n".encode())
    writer.write(b"Content-Type: application/json\r\n")
    writer.write(f"Content-Length: {len(payload)}\r\n".encode())
    writer.write(b"\r\n")
    writer.write(payload)


def _log_events_for_line(decoded: str, platform: str) -> list[dict]:
    """SSE event payloads for one log line.

    Always a ``log``/``log_error`` event; plus a structured ``app_crashed``
    event when the line matches a crash signature, so an AI/BVT consumer learns
    about a mid-test crash immediately instead of via a later wrong-screen
    assertion failure.
    """
    event_type = "log"
    if " E " in decoded or " Error " in decoded or "Exception" in decoded:
        event_type = "log_error"
    events: list[dict] = [{"event": event_type, "data": decoded}]
    crash = classify_log_line_crash(decoded, platform)
    if crash is not None:
        events.append(crash)
    return events


async def _stream_logs_to_client(
    proc: asyncio.subprocess.Process,
    writer: asyncio.StreamWriter,
    device: str,
    platform: str = "",
) -> None:
    if proc.stdout is None:
        return

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break

            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue

            for event in _log_events_for_line(decoded, platform):
                writer.write(f"data: {json.dumps(event)}\n\n".encode())
                await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        log.info("sse_client_disconnected", device=device)
    except Exception as e:
        log.error("sse_stream_error", device=device, error=str(e))
    finally:
        # Writer teardown is owned by _handle_client's finally; here we only
        # clean up the streaming subprocess.
        try:
            proc.terminate()
            await proc.wait()
        except ProcessLookupError:
            pass


async def _reject_via_stream_guards(
    writer: asyncio.StreamWriter, device: str, filter_str: str
) -> bool:
    """Run the shared pre-dispatch guard chain for the log-stream routes.

    linc-nerve#128/129 finding 4: `/stream` and `/tools/nerve_logcat_stream`
    used to reach device I/O (spawn a log-tailing subprocess with
    `device`/`filter_str` in argv) WITHOUT going through any of the size/
    traversal/shell-metacharacter/reservation guards that gate POST
    /tools/*. This wires them into the same `_run_pre_dispatch_guards` chain
    `route_request` uses.

    linc-nerve#128/129 round 1 finding 6: the guard chain includes
    `_check_reservation_conflict` -> `DeviceManager.guard()`, which may
    issue a real sqlite DELETE+commit against the shared reservations DB
    (see `http_router._reservation_conflict`'s docstring). Running that
    directly on the event loop stalls the WHOLE daemon — including
    `/health` — for the duration of a slow/contended sqlite call. The guard
    chain and device resolution are both offloaded via `run_in_executor` on
    the same per-device lane the POST /tools/* dispatch path already uses,
    so this stays consistent with that path's serialization invariant
    (same-serial calls still serialize) instead of introducing a second,
    differently-behaved concurrency model.

    Returns True — and has already written + drained a rejection response —
    if the request was rejected; the caller must return without proceeding.
    Returns False if the request may proceed.
    """
    from linc_nerve.daemon.http_router import _run_pre_dispatch_guards

    body = {"device": device, "filter": filter_str}
    loop = asyncio.get_running_loop()
    call = functools.partial(_run_pre_dispatch_guards, "nerve_logcat_stream", body)
    executor = _get_tool_executor(_lane_for_request("/stream", body))
    rejection = await loop.run_in_executor(executor, call)
    if rejection is None:
        return False
    _send_json_response(writer, rejection["status"], rejection["body"])
    await writer.drain()
    return True


async def _resolve_device_off_loop(device: str) -> tuple:
    """Resolve a device serial on the per-device executor lane, off the event loop.

    Mirrors `_reject_via_stream_guards`: resolving a device can trigger
    synchronous discovery on cache miss (~5.5s), which must not stall the event
    loop or inline probes like `/health` (audit F2).
    """
    loop = asyncio.get_running_loop()
    executor = _get_tool_executor(_lane_for_request("/stream", {"device": device}))
    call = functools.partial(_resolve_device, device)
    return await loop.run_in_executor(executor, call)


async def _validate_stream_request(
    writer: asyncio.StreamWriter, path: str
) -> tuple[str, str] | None:
    """Parse query params and run every pre-flight check for the log-stream
    routes (missing-device + the shared pre-dispatch guard chain).

    Returns `(device, filter_str)` if the request may proceed. Returns None
    if it was rejected — a response has already been written + drained.
    """
    url_parts = urllib.parse.urlparse(path)
    query = urllib.parse.parse_qs(url_parts.query)

    device = query.get("device", [""])[0]
    filter_str = query.get("filter", [""])[0]

    if not device:
        writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\nMissing 'device' parameter")
        await writer.drain()
        return None

    if await _reject_via_stream_guards(writer, device, filter_str):
        return None

    return device, filter_str


async def _handle_log_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    path: str,
) -> None:
    """Handle GET /stream — stream device logs as SSE events."""
    validated = await _validate_stream_request(writer, path)
    if validated is None:
        return
    device, filter_str = validated

    dev, platform = await _resolve_device_off_loop(device)
    if dev is None:
        writer.write(f"HTTP/1.1 404 Not Found\r\n\r\n{platform}".encode())
        await writer.drain()
        return

    ctrl = _get_controller(platform)
    try:
        cmd = ctrl.get_log_stream_cmd(device, filter_str)
    except Exception as e:
        writer.write(f"HTTP/1.1 400 Bad Request\r\n\r\n{e!s}".encode())
        await writer.drain()
        return

    # Send SSE headers
    writer.write(b"HTTP/1.1 200 OK\r\n")
    writer.write(b"Content-Type: text/event-stream\r\n")
    writer.write(b"Cache-Control: no-cache\r\n")
    writer.write(b"Connection: keep-alive\r\n")
    writer.write(b"\r\n")
    await writer.drain()

    log.info("sse_client_connected", device=device, filter=filter_str)

    # Launch log stream subprocess
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    await _stream_logs_to_client(proc, writer, device, platform)


async def _read_headers(reader: asyncio.StreamReader) -> dict[str, str]:
    """Read HTTP headers into a dict keyed by lower-cased header name.

    Values are whitespace-stripped; a repeated header keeps its last value;
    a line with no ``:`` is ignored.
    """
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line or line == b"\r\n":
            break
        name, colon, value = line.decode("latin-1").partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
    return headers


def _content_length(headers: dict[str, str]) -> int:
    """Declared Content-Length, or 0 when absent or not an integer."""
    try:
        return int(headers.get("content-length", "0"))
    except ValueError:
        return 0


async def _reject_guarded_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    path: str,
    headers: dict[str, str],
) -> bool:
    """Apply `request_guard.check_request` before any routing.

    On rejection: log ``request_rejected`` with the offending Host/Origin,
    drain (bounded, see `_drain_rejected_body`) any body so the client sees
    the response instead of a TCP RST, answer 403/415 JSON, and return True.
    """
    rejection = check_request(method, headers)
    if rejection is None:
        return False
    status, reason = rejection
    log.warning(
        "request_rejected",
        status=status,
        reason=reason,
        method=method,
        path=path,
        host=headers.get("host"),
        origin=headers.get("origin"),
    )
    with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError, TimeoutError):
        await asyncio.wait_for(
            _drain_rejected_body(reader, _content_length(headers)), timeout=_DRAIN_TIMEOUT_S
        )
    _send_json_response(writer, status, {"error": reason})
    await writer.drain()
    return True


async def _drain_rejected_body(reader: asyncio.StreamReader, content_length: int) -> None:
    """Drain up to `_DRAIN_CAP_BYTES` off the socket, in `_DRAIN_CHUNK_BYTES`
    chunks, before closing a connection whose declared `content_length` was
    rejected as oversized.

    linc-nerve#128/129 round 2 finding 4 (NEW DoS, REGRESSION): `content_
    length` is unvalidated attacker input. The round-1 drain called
    `reader.readexactly(content_length)` directly, which accumulates the
    ENTIRE declared length in the StreamReader's internal buffer before
    returning — an attacker who declares an arbitrarily large Content-Length
    controls this process's RAM (measured 98 -> 167 MB draining a 200 MB
    declared / 67 MB actually-streamed body). Reading in bounded chunks and
    discarding each one immediately (never accumulating more than one
    chunk's worth), under a hard total cap, keeps drained memory flat
    regardless of what the client declares or sends.

    Deliberately does NOT attempt to drain the full `content_length` — only
    up to `_DRAIN_CAP_BYTES`, enough to usually avoid the client-observed
    TCP RST (see the caller's docstring) without reopening the memory
    amplification this exists to close. Caller wraps this in `asyncio.
    wait_for(..., timeout=_DRAIN_TIMEOUT_S)`, so a client that never sends
    anything still can't hang the coroutine.
    """
    target = min(content_length, _DRAIN_CAP_BYTES)
    drained = 0
    while drained < target:
        chunk = await reader.read(min(_DRAIN_CHUNK_BYTES, target - drained))
        if not chunk:
            break
        drained += len(chunk)


async def _read_json_tool_body(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, content_length: int
) -> tuple[dict, bool]:
    """Read + parse a POST body, enforcing the size budget before reading.

    Body size is rejected on the DECLARED Content-Length before the body is
    JSON-parsed (linc-nerve#128/129 finding 8) — genuinely cheap compared to
    the old post-parse size check. The body is then read with `readexactly`
    (finding 5): a plain `reader.read(n)` short-reads large bodies under
    load, so the (still-present, defense-in-depth) size guard in
    `route_request` never actually saw the full parsed body — a truncated
    read produced a JSON-parse 400 before the 413 guard could ever run,
    making it dead code on the real transport.

    The oversized branch still DRAINS (bounded — see `_drain_rejected_body`,
    round 2 finding 4) some of the declared `content_length` off the socket
    before responding, even though it discards the bytes without parsing
    them: closing the connection while the client is still mid-write on a
    large body — with unread bytes sitting in the OS receive buffer —
    triggers a TCP RST on this platform, which the client observes as a
    connection reset INSTEAD of ever receiving this clean 413 (reproduced
    intermittently once the size check moved ahead of the read). Draining is
    bounded by `_DRAIN_TIMEOUT_S` so a client that declares a huge
    Content-Length and then never sends it can't hang this coroutine
    forever — the connection is closed either way once the timeout elapses.

    The in-budget branch's `readexactly` is likewise bounded, by
    `_READ_TIMEOUT_S` (linc-nerve#128/129 round 1 finding 1 — REGRESSION:
    the round-0 repair introduced this read with no timeout at all, so an
    in-budget Content-Length that never finishes arriving parked the
    handler forever — the same DoS shape the oversized branch already
    guarded against, reopened for every in-budget request). Unlike the
    oversized-drain branch, this `readexactly` is capped at `_MAX_BODY_
    BYTES` (256 KiB) by the `content_length > _MAX_BODY_BYTES` check above
    it, so it has no unbounded-memory exposure to fix.

    Returns `(body, rejected)`. If `rejected` is True, a response has
    already been written + drained and the caller must return without
    dispatching.
    """
    from linc_nerve.daemon.http_router import _MAX_BODY_BYTES

    if content_length > _MAX_BODY_BYTES:
        log.warning("tool_call_rejected_oversized_payload_precheck", content_length=content_length)
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            await asyncio.wait_for(
                _drain_rejected_body(reader, content_length), timeout=_DRAIN_TIMEOUT_S
            )
        _send_json_response(
            writer, 413, {"error": f"Request body exceeds {_MAX_BODY_BYTES} byte budget"}
        )
        await writer.drain()
        return {}, True

    if content_length == 0:
        return {}, False

    try:
        raw = await asyncio.wait_for(reader.readexactly(content_length), timeout=_READ_TIMEOUT_S)
    except asyncio.IncompleteReadError:
        _send_json_response(
            writer, 400, {"error": "Request body shorter than declared Content-Length"}
        )
        await writer.drain()
        return {}, True
    except TimeoutError:  # asyncio.TimeoutError is an alias on 3.11+
        # linc-nerve#128/129 round 1 finding 1 (REGRESSION): a lying
        # Content-Length that never finishes arriving must not park this
        # coroutine forever — bounded the same way the oversized-body drain
        # already is (`_DRAIN_TIMEOUT_S`).
        log.warning("tool_call_rejected_body_read_timeout", content_length=content_length)
        _send_json_response(writer, 408, {"error": "Timed out waiting for request body"})
        await writer.drain()
        return {}, True

    try:
        return json.loads(raw), False
    except json.JSONDecodeError:
        _send_json_response(writer, 400, {"error": "Invalid JSON body"})
        await writer.drain()
        return {}, True


async def _handle_json_tool_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    path: str,
    content_length: int,
) -> None:
    """Handle a JSON tool call (POST /tools/<name>), health (GET /health),
    or device enumeration (GET /devices).

    /health is served INLINE on the event loop (pure in-memory, no I/O) so the
    liveness probe answers even while tool workers are busy or stuck.
    /tools/* and /devices run on per-lane single-worker executors (lane =
    device serial, or the shared global lane for no-serial calls) so a
    slow/hung tool cannot block the event loop OR starve other devices'
    calls, while same-serial calls still serialize. asyncio.wait_for is a
    backstop that frees THIS coroutine (returns 504); the worker keeps running
    until its own subprocess timeout (Layer 2) returns — and because the
    zombie occupies its lane's only worker, later same-serial calls still
    queue behind it (the invariant survives timeouts).

    Connection teardown (writer.close) is handled by _handle_client's finally so
    it runs on every path, including a drain() that raises ConnectionResetError.
    """
    from linc_nerve.daemon.http_router import route_request

    body, rejected = await _read_json_tool_body(reader, writer, content_length)
    if rejected:
        return

    registry = _get_tool_registry()

    if method == "GET" and path == "/health":
        result = route_request(method=method, path=path, body=body, registry=registry)
    else:
        loop = asyncio.get_running_loop()
        timeout = _resolve_tool_timeout()
        call = functools.partial(
            route_request, method=method, path=path, body=body, registry=registry
        )
        executor = _get_tool_executor(_lane_for_request(path, body))
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(executor, call),
                timeout=timeout,
            )
        except TimeoutError:  # asyncio.TimeoutError is an alias on 3.11+
            log.error("tool_timeout", path=path, timeout_s=timeout)
            result = {
                "status": 504,
                "body": {"error": f"tool timed out after {timeout:.0f}s"},
            }

    _send_json_response(writer, result["status"], result["body"])
    await writer.drain()


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Handle an incoming HTTP request — route to SSE streaming or JSON tool call.

    All teardown funnels through the finally block so a client that disconnects
    mid-response (ConnectionResetError/BrokenPipeError at drain()) can never leak
    its socket in CLOSE_WAIT. Those disconnects are normal under load and logged
    at debug, not as errors.
    """
    try:
        request_line = await reader.readline()
        if not request_line:
            return

        req = request_line.decode("utf-8").strip()
        parts = req.split(" ")
        if len(parts) < 2:
            return

        method, path = parts[0], parts[1]

        headers = await _read_headers(reader)
        if await _reject_guarded_request(reader, writer, method, path, headers):
            return
        content_length = _content_length(headers)

        # Route: SSE log stream (GET /stream* or GET /tools/nerve_logcat_stream*)
        if method == "GET" and (
            path.startswith("/stream") or path.startswith("/tools/nerve_logcat_stream")
        ):
            await _handle_log_stream(reader, writer, path)
            return

        # Route: JSON tool call (POST /tools/<name>), health (GET /health),
        # or device enumeration (GET /devices).
        if (method == "POST" and path.startswith("/tools/")) or (
            method == "GET" and path in ("/health", "/devices")
        ):
            await _handle_json_tool_request(reader, writer, method, path, content_length)
            return

        # Fallback: 404
        _send_json_response(writer, 404, {"error": f"Not found: {method} {path}"})
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        log.debug("client_disconnected")
    except Exception as exc:
        log.error("client_handler_error", error=str(exc))
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


async def start_http_server(host: str = "127.0.0.1", port: int | None = None) -> None:
    """Start the HTTP server handling SSE streaming, tool calls, and health checks.

    Resolution order for port: explicit arg > NERVE_HTTP_PORT env > SSE_PORT (8111).

    If a healthy nerve daemon is already serving the resolved port (e.g. a
    launchd-managed `nerve daemon`), skip binding and return — bellows can
    rely on the running daemon instead of fighting for the socket.
    """
    _ensure_initialized()
    resolved_port = _resolve_http_port(port)

    if await _probe_existing_daemon(host, resolved_port):
        log.info(
            "http_server_skipped_existing_daemon",
            host=host,
            port=resolved_port,
        )
        return

    try:
        server = await asyncio.start_server(_handle_client, host, resolved_port)
    except OSError as exc:
        log.warning(
            "http_server_bind_failed",
            host=host,
            port=resolved_port,
            error=str(exc),
        )
        return

    log.info("http_server_started", host=host, port=resolved_port)
    try:
        async with server:
            await server.serve_forever()
    finally:
        # Drop the tool executor without blocking on a stuck worker so a SIGTERM
        # (server task cancelled) can't hang behind an in-flight tool call — the
        # default wait=True would reproduce today's SIGKILL-on-shutdown.
        _shutdown_tool_executor()


# Backward-compatible alias — bellows CLI command imports this name
start_sse_server = start_http_server
