"""CLI entry point for linc-nerve."""

from __future__ import annotations

import asyncio
import functools
import signal
from typing import Any

import click
from linc_cli_kit import (
    HttpDaemonTransport,
    LocalTransport,
    ToolError,
    UsageError,
    current_mode,
    current_output,
    current_timeout,
    emit,
    emit_exception,
    install_globals,
    mount_tools,
    registry_of,
)

# CRITICAL: structlog to stderr, BEFORE any linc_nerve module is imported — stdout carries
# the CLI's JSON payload. `linc_nerve.mcp.server` does the same for the MCP process, but the
# CLI must never import that module (it installs SIGTERM/SIGINT handlers + a faulthandler at
# import). Unconfigured structlog's PrintLoggerFactory writes to STDOUT, and modules that bind
# a logger at import time (`mcp/sse_server.py`, `core/device_manager.py`) resolve the factory
# right then — so configuring any later than this leaves their records on stdout and breaks
# `nerve <cmd> | jq` on the local-fallback path. Regression: tests/test_cli_fallback_stdout.py.
# `linc_nerve.live_stderr` imports no other linc_nerve module, so it is safe this early; its
# sink follows sys.stderr at write time (tests/test_structlog_live_stderr.py).
from linc_nerve.live_stderr import configure_structlog_to_stderr

configure_structlog_to_stderr()

from linc_nerve.cli.errors import honest_error, honest_toolerror  # noqa: E402
from linc_nerve.core import adb_health  # noqa: E402
from linc_nerve.core.android import AndroidController  # noqa: E402
from linc_nerve.core.ios import IOSController  # noqa: E402
from linc_nerve.daemon.polling import DeviceDiff, poll_devices  # noqa: E402
from linc_nerve.daemon.wda_manager import DaemonWDAManager  # noqa: E402
from linc_nerve.mcp._core import _ensure_initialized  # noqa: E402
from linc_nerve.mcp.sse_server import (  # noqa: E402
    _get_tool_executor,
    _handle_client,
    _probe_existing_daemon,
)


@click.group()
def main() -> None:
    """Nerve — unified device control for LINC.

    JSON on stdout by default; --human for text, --stream for JSON Lines.
    """


install_globals(main)


# Registered via add_command so doctor.py stays importable (and unit-testable)
# without pulling in the controller/daemon imports above.
from linc_nerve.cli.doctor import doctor  # noqa: E402

main.add_command(doctor)


def _registry_server() -> Any:
    """Return the FastMCP server with every tool module imported (registration is at import).

    Deliberately does NOT import ``linc_nerve.mcp.server`` — that module installs
    SIGTERM/SIGINT handlers and a faulthandler at import time, which a CLI process must not
    inherit. ``_TOOL_MODULES`` is the same list ``mcp/server.py`` iterates.
    """
    import importlib

    from linc_nerve.mcp._core import server
    from linc_nerve.mcp.sse_server import _TOOL_MODULES

    for module_path in _TOOL_MODULES:
        importlib.import_module(module_path)
    return server


def _patch_mcp_zero_buffers() -> None:
    """Monkey-patch zero-buffer channels in the MCP SDK.

    mcp 1.26.0 uses ``anyio.create_memory_object_stream(0)`` (unbuffered
    rendezvous channels) in two places:

    1. **Transport layer** (``mcp.server.stdio.stdio_server``) — we replace
       this entirely with ``buffered_stdio_server``.
    2. **Session layer** (``mcp.server.session.ServerSession.__init__``) —
       the ``_incoming_message_stream`` uses buffer=0, creating a deadlock
       between ``_receive_loop`` pushing requests and ``Server.run()``
       consuming them.  After 2 successful round-trips, the rendezvous
       timing fails and the 3rd+ request hangs forever.

    This patch wraps ``ServerSession.__init__`` to replace the zero-buffer
    stream with a buffered one (size=32) after the original init runs.
    """
    import anyio
    from mcp.server.session import ServerRequestResponder, ServerSession

    _original_init = ServerSession.__init__

    def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        _original_init(self, *args, **kwargs)
        # Replace the zero-buffer incoming message stream with a buffered one.
        # The original creates: anyio.create_memory_object_stream(0)
        # We need buffer > 0 so _receive_loop can push requests without
        # waiting for Server.run() to be in an exact receive-ready state.
        self._incoming_message_stream_writer, self._incoming_message_stream_reader = (
            anyio.create_memory_object_stream[ServerRequestResponder](32)
        )

    ServerSession.__init__ = _patched_init


@main.command()
def bellows() -> None:
    """Start the Nerve MCP server (stdio transport).

    Patches two zero-buffer deadlocks in mcp 1.26.0:
    1. Transport-level: replaced with buffered_stdio_server
    2. Session-level: ServerSession._incoming_message_stream patched to buffer=32
    """
    import anyio

    _patch_mcp_zero_buffers()

    from linc_nerve.mcp.server import server
    from linc_nerve.mcp.sse_server import start_sse_server
    from linc_nerve.mcp.stdio_buffered import buffered_stdio_server

    async def _run() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(start_sse_server)
            async with buffered_stdio_server() as (read_stream, write_stream):
                await server._mcp_server.run(
                    read_stream,
                    write_stream,
                    server._mcp_server.create_initialization_options(),
                )

    anyio.run(_run)


def _poll_cycle(
    android: AndroidController,
    ios: IOSController,
    wda_mgr: DaemonWDAManager,
    known_serials: set[str],
) -> DeviceDiff:
    """Run one poll + WDA-reconcile cycle. Blocking — runs OFF the event loop.

    Every call here is blocking subprocess work: poll_devices shells out to adb
    getprop and simctl/xctrace list, each up to several seconds cold. The daemon
    dispatches this to the shared single-worker tool executor (see _run_poll_loop)
    so it can't freeze the event loop or the inline /health probe.

    The WDA reconcile reuses the iOS device list poll_devices already discovered
    this tick (diff.raw_devices["ios"]) — exactly ONE ios.discover() per tick
    (issue #84). If iOS discovery failed, the "ios" key is absent and reconcile
    is skipped, matching the old skip-on-discover-error semantics.
    """
    import structlog

    diff = poll_devices(android, ios, known_serials)
    android_devices = diff.raw_devices.get("android")
    if android_devices is not None:
        # Publish nerve's OWN Android view so the transport watchdog can compare
        # it against a fresh `adb devices`. A device adb can see but nerve
        # cannot is stale-client drift — the same family of bug as the wedge,
        # and it silently cost the android-watch lane its nightly runs.
        adb_health.record_nerve_android_serials(d.serial for d in android_devices)
    ios_devices = diff.raw_devices.get("ios")
    if ios_devices is not None:
        try:
            wda_mgr.reconcile(ios_devices)
        except Exception as wda_err:
            structlog.get_logger().debug("wda_reconcile_error", error=str(wda_err))
    return diff


async def _run_poll_loop(
    android: AndroidController,
    ios: IOSController,
    wda_mgr: DaemonWDAManager,
    stop_event: asyncio.Event,
) -> None:
    """Device poll + WDA reconcile loop. Runs alongside the HTTP server.

    The blocking discovery/reconcile cycle is offloaded to the daemon's shared
    single-worker tool executor via run_in_executor — a slow cold discovery would
    otherwise freeze the event loop (and the inline /health liveness probe) for
    its whole duration (improvement 4244d1fe). The single worker is shared with
    tool execution so the poll cycle and tool calls never touch adb/simctl
    concurrently, preserving the daemon's serial device-access semantics.
    """
    loop = asyncio.get_running_loop()
    known_serials: set[str] = set()
    while not stop_event.is_set():
        diff = await loop.run_in_executor(
            _get_tool_executor(),
            functools.partial(_poll_cycle, android, ios, wda_mgr, known_serials),
        )
        for serial, info in diff.connected.items():
            click.echo(f"  [+] {info['platform']} {serial} ({info['model']})")
        for serial in diff.disconnected:
            click.echo(f"  [-] {serial} disconnected")
        known_serials = diff.all_serials
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30.0)
            break
        except TimeoutError:
            pass


async def _run_adb_watchdog(
    android: AndroidController,
    stop_event: asyncio.Event,
    interval: float = adb_health.WATCHDOG_INTERVAL_SECONDS,
) -> None:
    """Sample the adb transport and self-heal a busy-spinning adb server.

    Runs on its OWN thread (``asyncio.to_thread``), deliberately NOT on the
    shared single-worker tool executor the poll loop uses. That worker is the
    first thing a wedged transport blocks: if the watchdog queued behind it, it
    would go silent in exactly the situation it exists to detect. Its probe is a
    hard-timed-out ``adb devices`` subprocess, so it cannot inherit the hang
    from the daemon's persistent adbutils socket either.

    ``on_recover`` drops that persistent client after the server is replaced —
    without it the daemon keeps talking to a dead process and the bench stays
    dark until a human bounces the daemon (the 2026-09-03 manual repair).
    """
    adb_health.mark_watchdog_started()
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(
                functools.partial(
                    adb_health.watchdog_tick,
                    on_recover=android._reset_client,
                    on_drift=android._reset_client,
                )
            )
        except Exception as exc:  # pragma: no cover — watchdog must never die
            import structlog

            structlog.get_logger().error("adb_watchdog_tick_failed", error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except TimeoutError:
            pass


async def run_daemon_async(port: int) -> bool:
    """Run the daemon HTTP server + poll loop.

    Returns True if the daemon bound the port and served, False if a healthy
    peer was detected on :port and the daemon deferred. Launchd will respawn
    after ThrottleInterval on either exit, but the deferred path skips the
    OSError traceback that would otherwise spam stderr during the handoff
    window when bellows is still alive in a Claude Code session.
    """
    if await _probe_existing_daemon("127.0.0.1", port):
        click.echo(
            f"Nerve daemon: healthy peer already on :{port} — deferring (launchd will retry)."
        )
        return False

    _ensure_initialized()

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    server = await asyncio.start_server(_handle_client, "127.0.0.1", port)

    android = AndroidController()
    ios = IOSController()
    wda_mgr = DaemonWDAManager()

    def _handle_signal() -> None:
        click.echo("Received SIGTERM, shutting down...")
        stop_event.set()
        server.close()
        wda_mgr.shutdown()

    loop.add_signal_handler(signal.SIGTERM, _handle_signal)
    loop.add_signal_handler(signal.SIGINT, _handle_signal)

    async def _serve() -> None:
        async with server:
            await server.serve_forever()

    click.echo(f"Nerve daemon starting on port {port}...")
    async with asyncio.TaskGroup() as tg:
        tg.create_task(_serve())
        tg.create_task(_run_poll_loop(android, ios, wda_mgr, stop_event))
        tg.create_task(_run_adb_watchdog(android, stop_event))
    return True


@main.command()
@click.option("--port", default=8111, help="HTTP server port")
def daemon(port: int) -> None:
    """Start the nerve daemon (HTTP server + device polling, no stdio MCP).

    Designed to run as a launchd service for always-on device access. If a
    healthy nerve daemon already owns the port (e.g. a `nerve bellows` is
    serving HTTP in a live Claude Code session), exit cleanly so launchd
    can ThrottleInterval-respawn instead of crash-looping.
    """
    asyncio.run(run_daemon_async(port))


_DAEMON_ONLY = frozenset(
    {
        "nerve_record_start",
        "nerve_record_stop",
        "nerve_record_fixed",
        "nerve_stream_start",
        "nerve_stream_stop",
        "nerve_stream_status",
        "nerve_stream_screenshot",
        "nerve_stream_logs",
        "nerve_reserve",
        "nerve_release",
        "nerve_pool",
        # The action-log session registry (core/action_log.REGISTRY) lives in the daemon
        # process; a local fallback would open a session that dies with the CLI process.
        "nerve_actionlog_start",
        "nerve_actionlog_stop",
        # Backed by the process-memory StateSessionManager (_core._state_mgr): run
        # locally they read a cold, empty state manager and return a confidently WRONG
        # answer rather than refusing — nerve_diff reports a first-observation result and
        # nerve_session_stats reports {"sessions": {}, "count": 0}.
        "nerve_diff",
        "nerve_session_stats",
    }
)


def _local_fn(spec: Any) -> Any:
    """Resolve a tool to nerve's sync registry — the same path the daemon takes.

    The sync fns may return the inline-image sentinel key, which is not JSON-serialisable;
    ``_strip_inline_sentinel`` removes it exactly as ``daemon/http_router`` does.
    """
    from linc_nerve.daemon.http_router import _strip_inline_sentinel
    from linc_nerve.mcp.sse_server import _get_tool_registry

    fn = _get_tool_registry().get(spec.name)
    if fn is None:
        raise ToolError(
            f"Tool {spec.name} not found",
            code="TOOL_NOT_FOUND",
            category="not_found",
        )

    def _wrapped(**payload: Any) -> Any:
        try:
            return _strip_inline_sentinel(fn(**payload))
        except (TypeError, ValueError, KeyError, LookupError) as exc:
            # Exit-code parity with the daemon path: `daemon/http_router._error_status_and_body`
            # maps exactly these to HTTP 400, which the kit transport turns into a UsageError
            # (exit 2). Without this, the same bad `--device` exits 2 with the daemon up and 1
            # with it down.
            raise UsageError(str(exc)) from exc

    return _wrapped


def _local_before() -> None:
    _ensure_initialized()


class NerveTransport(HttpDaemonTransport):
    """HttpDaemonTransport that raises a precise ToolError for nerve's well-known error shapes.

    Without this, every tool error dict (unknown serial, reserved device, unsupported op)
    reaches the kit's `errors.classify` unmapped and comes out as a generic
    `TOOL_ERROR`/`internal`/non-retryable outcome with no hint. `errors.honest_error` /
    `errors.honest_toolerror` do the mapping; this class is just the seam that calls them on
    both the success-path result (a tool-level error dict) and an exception escaping
    `super().call` (the daemon's HTTP 400 -> kit `UsageError` path).
    """

    def call(
        self, spec: Any, payload: dict[str, Any], *, timeout: float | None = None
    ) -> Any:
        try:
            result = super().call(spec, payload, timeout=timeout)
        except ToolError as exc:
            raise honest_toolerror(exc) from exc
        mapped = honest_error(result)
        if mapped is not None:
            raise mapped
        return result


TRANSPORT = NerveTransport(
    "nerve",
    "http://127.0.0.1:8111",
    fallback=LocalTransport(fn_for=_local_fn, before=_local_before),
    daemon_only=_DAEMON_ONLY,
)


def _spec_for(tool_name: str) -> Any:
    spec = next((s for s in registry_of(_registry_server()) if s.name == tool_name), None)
    if spec is None:
        raise ToolError(f"Tool {tool_name} not found", code="TOOL_NOT_FOUND", category="not_found")
    return spec


def _call(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Hand-written commands route through the same transport as generated ones."""
    res = TRANSPORT.call(_spec_for(tool_name), payload, timeout=current_timeout())
    return res if isinstance(res, dict) else {"result": res}


def _contract(fn: Any) -> Any:
    """Route any escaping exception into the kit's stderr envelope (exit 1/2, empty stdout)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        ctx = click.get_current_context()
        try:
            return fn(*args, **kwargs)
        except click.exceptions.Exit:
            raise
        except click.UsageError as exc:
            ctx.exit(emit_exception(UsageError(exc.format_message()), mode=current_mode()))
        except Exception as exc:
            ctx.exit(emit_exception(exc, mode=current_mode()))

    return wrapper


def _emit(res: Any, *, tool: str, renderer: Any = None) -> int:
    return emit(
        res, mode=current_mode(), output_path=current_output(), renderer=renderer, tool=tool
    )


def _render_devices(res: dict[str, Any]) -> str:
    devices = res.get("devices") or []
    if not devices:
        return "No devices found."
    icons = {"android": "\U0001f916", "ios": "\U0001f34e"}
    lines = []
    for d in devices:
        icon = icons.get(d.get("platform"), "\U0001f4f1")
        lines.append(
            f"  {icon} {d.get('serial')}  {d.get('model')}  "
            f"({d.get('device_type')}, {d.get('os_version')})"
        )
    lines.append(f"\n{res.get('count', len(devices))} device(s) found.")
    return "\n".join(lines)


@main.command()
@_contract
def discover() -> None:
    """List all connected devices. Example: nerve discover"""
    ctx = click.get_current_context()
    res = _call("nerve_discover", {})
    ctx.exit(_emit(res, tool="discover", renderer=_render_devices))


@main.command()
@click.argument("device")
@click.option("-o", "--output", "output", help="Output PNG file path", type=click.Path())
@_contract
def screenshot(device: str, output: str | None) -> None:
    """Capture a screenshot from a device. Example: nerve screenshot SERIAL -o shot.png"""
    import shutil

    ctx = click.get_current_context()
    res = _call("nerve_screenshot", {"device": device})
    if not res.get("error"):
        target = output or current_output()
        src_path = res.get("screenshot")
        if target and not src_path:
            # -o promises a file at that path. Exiting 0 with nothing written is the worst
            # possible answer for a scripted caller.
            res = {"error": "No screenshot path returned"}
        elif target and src_path != target:
            shutil.copyfile(src_path, target)
            res["screenshot"] = target
    ctx.exit(
        _emit(
            res,
            tool="screenshot",
            renderer=lambda r: f"Screenshot saved to: {r.get('screenshot')}",
        )
    )


@main.command()
@click.argument("device")
@click.argument("package")
@_contract
def launch(device: str, package: str) -> None:
    """Launch an app on a device. Example: nerve launch SERIAL com.example.app"""
    ctx = click.get_current_context()
    res = _call("nerve_launch", {"device": device, "package": package})
    ctx.exit(_emit(res, tool="launch", renderer=lambda r: f"Launched {package} on {device}"))


@main.command()
@click.argument("device")
@click.argument("package")
@_contract
def kill(device: str, package: str) -> None:
    """Stop an app on a device. Example: nerve kill SERIAL com.example.app"""
    ctx = click.get_current_context()
    res = _call("nerve_kill", {"device": device, "package": package})
    ctx.exit(_emit(res, tool="kill", renderer=lambda r: f"Killed {package} on {device}"))


@main.command()
@click.argument("device")
@_contract
def elements(device: str) -> None:
    """Inspect UI hierarchy elements on a device. Example: nerve elements SERIAL"""
    import json

    ctx = click.get_current_context()
    res = _call("nerve_elements", {"device": device})
    ctx.exit(_emit(res, tool="elements", renderer=lambda r: json.dumps(r, indent=2, default=str)))


@main.command()
@click.argument("device")
@click.argument("x", type=int)
@click.argument("y", type=int)
@_contract
def tap(device: str, x: int, y: int) -> None:
    """Tap coordinates on a device screen. Example: nerve tap SERIAL 200 400"""
    ctx = click.get_current_context()
    res = _call("nerve_tap", {"device": device, "x": x, "y": y})
    ctx.exit(_emit(res, tool="tap", renderer=lambda r: f"Tapped ({x}, {y}) on {device}"))


def _mount() -> None:
    """Reflect every remaining MCP tool onto the group (hand-written commands win)."""
    mount_tools(main, _registry_server(), prefix="nerve_", transport=TRANSPORT)


_mount()
