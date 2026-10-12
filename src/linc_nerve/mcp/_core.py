"""Shared state, helpers, and decorators for linc-nerve MCP tools."""

from __future__ import annotations

import atexit
import contextlib
import inspect
import io
import os
import queue
import tempfile
import threading
import time
from functools import partial, wraps
from typing import Any

import anyio
import structlog
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

from linc_nerve.core import action_log
from linc_nerve.core.android import AndroidController
from linc_nerve.core.contracts import require
from linc_nerve.core.device import NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.emulator import EmulatorManager
from linc_nerve.core.ios import IOSController
from linc_nerve.core.metrics import MetricsCollector
from linc_nerve.core.observability import log_mcp_call
from linc_nerve.core.state_engine import StateSessionManager
from linc_nerve.mcp._confirm import DESTRUCTIVE_TOOLS
from linc_nerve.mcp._docstring_args import described_signature

log = structlog.get_logger().bind(domain="device_control")

server = FastMCP("linc-nerve")

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_android: AndroidController | None = None
_ios: IOSController | None = None
_state_mgr: StateSessionManager | None = None
_metrics: MetricsCollector | None = None
_device_mgr: DeviceManager | None = None
_emulator: EmulatorManager | None = None

# Per-device async semaphores — serialize MCP tool calls per device so
# concurrent tools don't exhaust ADB's per-device connection capacity.
_device_semaphores: dict[str, anyio.abc.Semaphore] = {}

# ---------------------------------------------------------------------------
# Device cache
# ---------------------------------------------------------------------------

_CACHE_TTL = 5.0  # seconds
# Monotonic clock for the device cache — a module-level seam so tests can
# drive TTL expiry with a fake clock instead of sleeping.
_clock = time.monotonic
_cached_devices: list[NerveDevice] = []
# _cache_timestamp == _INVALIDATED means "never filled, or explicitly
# invalidated" — distinct from a merely TTL-expired cache (_stale_ready_android).
_INVALIDATED = 0.0
_cache_timestamp: float = _INVALIDATED
# serial -> (confirmed_at, cached device it confirmed). A memo is honoured only
# for that exact cached NerveDevice, so any cache replacement voids it.
_stale_confirmed: dict[str, tuple[float, NerveDevice]] = {}

_init_lock = threading.Lock()
_cache_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Output directory (per-session unique filenames)
# ---------------------------------------------------------------------------

# Created on first use by ``_output_dir()`` — nothing touches the temp dir at
# import, and ``mkdtemp()`` gives an unpredictable, owner-only (0700) directory
# instead of a shared ``<tmp>/nerve-output`` parent (security review, Info).
_OUTPUT_DIR: str | None = None
_output_dir_lock = threading.Lock()

_file_counter = 0
_counter_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEVICE_CALL_TIMEOUT = 90  # seconds


# ---------------------------------------------------------------------------
# Thread dispatch helpers
# ---------------------------------------------------------------------------


def _run_with_timeout(fn: Any, serial: str) -> Any:
    """Run *fn* in a daemon thread with a hard timeout.

    Unlike ``anyio.fail_after``, this does NOT depend on the async event loop
    to deliver the timeout — it uses a ``queue.Queue.get(timeout=...)`` which
    fires reliably even when the MCP transport blocks the event loop.

    On timeout the daemon thread is abandoned (leaked). This is intentional:
    Python cannot kill threads, and letting the MCP server respond with an
    error is better than hanging forever.
    """
    result_q: queue.Queue[tuple[str, Any]] = queue.Queue()

    def worker() -> None:
        try:
            result_q.put(("ok", fn()))
        except Exception as exc:
            result_q.put(("error", exc))

    t = threading.Thread(target=worker, daemon=True, name=f"nerve-{serial[:8]}")
    t.start()

    try:
        status, value = result_q.get(timeout=_DEVICE_CALL_TIMEOUT)
    except queue.Empty:
        log.error(
            "device_call_timeout",
            serial=serial,
            timeout_s=_DEVICE_CALL_TIMEOUT,
        )
        return {
            "error": f"Operation timed out after {_DEVICE_CALL_TIMEOUT}s. "
            "Device may be unresponsive — try nerve_discover to check connectivity.",
            "device": serial,
        }

    if status == "ok":
        return value
    raise value


async def _run_on_device(serial: str, fn: Any) -> Any:
    """Run a blocking function with per-device serialization.

    Acquires a per-device semaphore (created on first use) so that only one
    MCP tool talks to a given device at a time.  The blocking *fn* is then
    dispatched to a worker thread with a hard timeout via daemon thread +
    queue (not anyio.fail_after, which can't fire when the MCP transport
    blocks the event loop).
    """
    if serial not in _device_semaphores:
        _device_semaphores[serial] = anyio.Semaphore(1)
    async with _device_semaphores[serial]:
        return await anyio.to_thread.run_sync(partial(_run_with_timeout, fn, serial))


# ---------------------------------------------------------------------------
# Tool registration decorators
# ---------------------------------------------------------------------------

# Single source of truth for the tool surface: every @device_tool/@global_tool
# registration records itself here. Docs (README/LINC.md tool counts + tables)
# and the nerve_capabilities matrix are derived from this registry — never
# hand-count tools (issue #81: 36/38/40 doc drift).
TOOL_REGISTRY: dict[str, dict[str, str]] = {}


def _record_tool(fn: Any, kind: str) -> str:
    """Record a tool registration and return its MCP tool name."""
    tool_name = fn.__name__.replace("_sync", "").lstrip("_")
    doc_lines = (fn.__doc__ or "").strip().splitlines()
    TOOL_REGISTRY[tool_name] = {
        "kind": kind,
        "module": fn.__module__,
        "description": doc_lines[0].strip() if doc_lines else "",
    }
    return tool_name


def _register_tool(wrapper: Any, fn: Any, tool_name: str) -> None:
    """Register *wrapper* (an ``@wraps(fn)`` async wrapper) with FastMCP as *tool_name*.

    The wrapper's signature is *fn*'s with each parameter's ``Args:`` docstring text attached
    as a pydantic ``Field(description=...)`` — FastMCP emits it as the inputSchema property
    ``description`` (and linc-cli-kit as each CLI option's help). Types/defaults unchanged.
    """
    wrapper.__signature__ = described_signature(fn)
    server.tool(name=tool_name, annotations=_annotations_for(tool_name))(wrapper)


# Sentinel key a @device_tool/@global_tool result dict can set to carry raw
# image bytes that should be delivered as an inline MCP ImageContent block
# alongside the rest of the (JSON-serialized) result — see linc-nerve#97.
# Opt-in and generic: any tool can use it; tools that don't set it are
# unaffected. Only applies at the MCP wire boundary — the underlying sync
# function's direct dict return (as seen by tests and other in-process
# callers) is unchanged.
_INLINE_IMAGE_KEY = "_inline_image_bytes"

# Agent-context vision-token budget (spec 2026-07-16-agent-token-efficiency).
# 300k px ≈ 400 vision tokens at Anthropic's px/750 rule; canonical 488x1080
# artifacts (~702 tokens) rescale to ~368x816. Applies ONLY in this wrapper
# layer — the daemon and in-process callers keep canonical bytes.
_AGENT_MAX_PIXELS = 300_000


def _rescale_for_agent(data: bytes, max_pixels: int = _AGENT_MAX_PIXELS) -> bytes:
    """Scale PNG bytes down to the agent vision-token pixel budget.

    No-op (returns the same object) when already under budget or when the
    bytes are not a decodable image — never raises.
    """
    try:
        from PIL import Image as PILImage

        img = PILImage.open(io.BytesIO(data))
        if img.width * img.height <= max_pixels:
            return data
        scale = (max_pixels / (img.width * img.height)) ** 0.5
        new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        buf = io.BytesIO()
        img.resize(new_size, PILImage.LANCZOS).save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return data


def _image_size(data: bytes) -> tuple[int, int] | None:
    """Pixel (width, height) of an image, or None if undecodable.

    Never raises — callers (nerve_screenshot / nerve_state result shaping)
    must not fail a capture just because the size couldn't be read back
    (reactor 1cac8e2f).
    """
    try:
        from PIL import Image as PILImage

        img = PILImage.open(io.BytesIO(data))
        return img.width, img.height
    except Exception:
        return None


def _screenshot_geometry(data: bytes, screen_size: dict[str, Any]) -> dict[str, Any]:
    """Pixel geometry for a captured screenshot: size, and scale when known.

    ``scale`` (tap-coordinate units per canonical screenshot pixel) is only
    included when *screen_size* carries a positive width AND the
    screenshot's own width decoded successfully — otherwise there's nothing
    to divide. Pulled out of ``_nerve_state_sync`` so that function's CRAP
    score doesn't rise (reactor 1cac8e2f).
    """
    size = _image_size(data)
    geometry: dict[str, Any] = {
        "screenshot_width": size[0] if size else None,
        "screenshot_height": size[1] if size else None,
    }
    device_width = screen_size.get("width") if screen_size else None
    if device_width and device_width > 0 and size and size[0]:
        geometry["scale"] = round(device_width / size[0], 3)
    return geometry


def _apply_agent_image_budget(result: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Apply the agent vision-token budget to a tool result, in place.

    Called ONLY from the FastMCP wrapper (device_tool) — the agent-exclusive
    layer. Two effects, both skipped when the call opted out via
    ``full_resolution=True`` or ``annotate=True``:

    1. Rescales ``_INLINE_IMAGE_KEY`` bytes (the inline ImageContent source).
    2. When ``result["screenshot"]`` is an existing over-budget PNG, writes a
       sibling ``<name>.agent.png`` and swaps the agent-visible path to it.
       The canonical file is never modified — the daemon, crucible, and
       humans keep full-size artifacts.

    Whenever either effect actually rescales something, adds
    ``agent_width``/``agent_height`` = the pixel size of the rescaled image,
    so an agent can convert between what it was handed and canonical/device
    coordinates. An unchanged result gets no ``agent_*`` keys.
    """
    if kwargs.get("full_resolution") or kwargs.get("annotate"):
        return result

    rescaled: bytes | None = None
    raw = result.get(_INLINE_IMAGE_KEY)
    if isinstance(raw, bytes):
        rescaled = _rescale_for_agent(raw)
        if rescaled is raw:
            rescaled = None  # under budget — nothing shrank
        else:
            result[_INLINE_IMAGE_KEY] = rescaled

    agent_bytes = rescaled

    path = result.get("screenshot")
    if isinstance(path, str) and path.endswith(".png") and os.path.exists(path):
        try:
            if rescaled is None:
                with open(path, "rb") as f:
                    source = f.read()
                rescaled_file = _rescale_for_agent(source)
                shrank = rescaled_file is not source
            else:
                # nerve_screenshot writes the same bytes to disk it put in the
                # sentinel — reuse the rescale, zero extra decode.
                rescaled_file = rescaled
                shrank = True
            if shrank:
                agent_path = path[: -len(".png")] + ".agent.png"
                with open(agent_path, "wb") as f:
                    f.write(rescaled_file)
                result["screenshot"] = agent_path
                agent_bytes = rescaled_file
        except OSError:
            pass  # unreadable path — leave the canonical path in place

    if agent_bytes is not None:
        size = _image_size(agent_bytes)
        if size:
            result["agent_width"], result["agent_height"] = size
    return result


def _wrap_inline_image(result: dict[str, Any]) -> list[Any]:
    """Convert a dict carrying raw inline-image bytes into MCP content blocks.

    Pops ``_INLINE_IMAGE_KEY``, size-budgets + encodes it via
    ``encode_inline_screenshot``, and returns ``[dict-without-sentinel,
    Image]`` so FastMCP's content conversion emits
    ``[TextContent(json), ImageContent]``. See linc-nerve#97.
    """
    from linc_nerve.core.android_screenshot import encode_inline_screenshot

    raw = result.pop(_INLINE_IMAGE_KEY)
    encoded, fmt = encode_inline_screenshot(raw)
    return [result, Image(data=encoded, format=fmt)]


def _annotations_for(tool_name: str) -> ToolAnnotations | None:
    """MCP tool annotations for *tool_name*, or None for the default.

    Destructive tools (DESTRUCTIVE_TOOLS — see mcp/_confirm.py) get
    ``destructiveHint=True, readOnlyHint=False`` so MCP clients can surface
    the hint independently of nerve's own confirm=True runtime gate. Every
    other tool's registration is unaffected.
    """
    if tool_name in DESTRUCTIVE_TOOLS:
        return ToolAnnotations(destructiveHint=True, readOnlyHint=False)
    return None


def _device_arg(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """The ``device`` serial of a device-tool call (first positional or keyword)."""
    return args[0] if args else kwargs.get("device")


def _call_args(
    signature: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Explicitly-passed arguments by name; raw kwargs when the call doesn't bind."""
    try:
        return dict(signature.bind(*args, **kwargs).arguments)
    except TypeError:
        return dict(kwargs)


# Guard reads lift the 80-node cap, look deeper and skip the 0.5 s elements
# cache (it can serve a pre-transition tree). Only kwargs the reader accepts
# are passed (iOS elements() has no max_nodes / force_refresh).
_UNCAPPED_READ: dict[str, Any] = {
    "max_nodes": None,
    "max_depth": action_log.GUARD_MAX_DEPTH,
    "force_refresh": True,
}
_ANNOTATION_DUMP_TIMEOUT_S = 3


def _guard_read_kwargs(reader: Any) -> dict[str, Any]:
    """The subset of :data:`_UNCAPPED_READ` *reader* accepts (no depth lift if it caps)."""
    try:
        params = inspect.signature(reader).parameters
    except (TypeError, ValueError):
        return {}
    wanted = _UNCAPPED_READ if "max_nodes" in params else {"force_refresh": True}
    return {key: value for key, value in wanted.items() if key in params}


def _annotation_read_kwargs(reader: Any) -> dict[str, Any]:
    """The subset of quick-dump kwargs *reader* accepts (iOS has no quick_timeout_s)."""
    try:
        params = inspect.signature(reader).parameters
    except (TypeError, ValueError):
        return {}
    return {"quick_timeout_s": _ANNOTATION_DUMP_TIMEOUT_S} if "quick_timeout_s" in params else {}


def _read_tree(device: str, guarded: bool) -> tuple[list[Any], bool] | str:
    """(element tree, capped) before an action — or why the read failed.

    Never raises: the caller decides whether a failed read fails the action
    (guarded session) or only leaves it un-annotated (plain action log).
    ``capped`` is True when the reader could not be asked to lift its node cap.
    """
    try:
        dev, platform = _resolve_device(device)
        if dev is None:
            return f"device {device} not found"
        reader = _get_controller(platform).elements
        kwargs = _guard_read_kwargs(reader) if guarded else _annotation_read_kwargs(reader)
        return list(reader(device, **kwargs)), "max_nodes" not in kwargs
    except Exception as exc:
        log.debug("action_log_target_failed", serial=device, error=str(exc))
        return str(exc) or type(exc).__name__


def _trusted_tree(device: str) -> list[Any] | dict[str, Any]:
    """The guard's element tree, or a ``target_unreadable`` refusal (fail closed).

    Unreadable, empty (a loading screen) and possibly-truncated trees are refused.
    """
    read = _read_tree(device, guarded=True)
    if isinstance(read, str):
        return action_log.unreadable_refusal(read)
    problem = action_log.tree_problem(*read)
    return read[0] if problem is None else action_log.unreadable_refusal(problem)


def _guarded_context(
    guard: action_log.GuardConfig, tool_name: str, device: str, call_args: dict[str, Any]
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """(target, label refusal) for a guarded call that activates an element."""
    if not action_log.needs_label_check(tool_name, call_args):
        return None, None
    tree = _trusted_tree(device)
    if isinstance(tree, dict):
        return None, tree
    target = action_log.tap_target(tool_name, call_args, tree)
    return target, action_log.activation_refusal(guard, tool_name, call_args, tree)


def _action_context(
    guard: action_log.GuardConfig | None, tool_name: str, device: str, call_args: dict[str, Any]
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """(element under the point, guard refusal) before an action runs.

    Unguarded, only a tap / long press reads the tree (to annotate the log
    line) and a failed read just leaves ``target`` None.
    """
    if guard is not None:
        return _guarded_context(guard, tool_name, device, call_args)
    if tool_name not in action_log.TARGET_TOOLS:
        return None, None
    read = _read_tree(device, guarded=False)
    target = (
        None if isinstance(read, str) else action_log.tap_target(tool_name, call_args, read[0])
    )
    return target, None


def _foreground_package(device: str) -> str | None:
    """Foreground package for the guard: None if the platform can't tell, "" if unreadable.

    A platform without ``get_foreground`` (iOS today) skips the foreground
    check; an unresolvable serial, or a read that fails on a platform that has
    one, counts as unknown and is refused (fail closed).
    """
    try:
        dev, platform = _resolve_device(device)
        if dev is None:
            return ""
        reader = getattr(_get_controller(platform), "get_foreground", None)
        if reader is None:
            return None
        return str(reader(device).get("package") or "")
    except Exception as exc:
        log.warning("guard_foreground_unreadable", serial=device, error=str(exc))
        return ""


def _guard_refusal(
    guard: action_log.GuardConfig | None, tool_name: str, device: str, call_args: dict[str, Any]
) -> dict[str, Any] | None:
    """Package + foreground checks of a guarded session (label check runs later)."""
    if guard is None:
        return None
    refusal = _app_refusal(guard, tool_name, device, call_args)
    if refusal is not None or not action_log.needs_foreground(tool_name, call_args):
        return refusal
    package = _foreground_package(device)
    return None if package is None else action_log.foreground_refusal(guard, package)


def _app_refusal(
    guard: action_log.GuardConfig, tool_name: str, device: str, call_args: dict[str, Any]
) -> dict[str, Any] | None:
    """Package check, then a deep-link launch the platform would not confine to the target."""
    refusal = action_log.package_refusal(guard, tool_name, call_args)
    if refusal is not None or not action_log.is_uri_launch(tool_name, call_args):
        return refusal
    _, platform = _resolve_device(device)
    return action_log.uri_launch_refusal(guard, platform)


def _refuse(device: str, tool_name: str, refusal: dict[str, Any]) -> dict[str, Any]:
    """Record a guard refusal in the action log and return it as the tool result."""
    action_log.REGISTRY.record_guard(device, tool_name, refusal)
    return refusal


def _run_in_session(
    fn: Any,
    tool_name: str,
    session: action_log.ActionLogSession,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Guard (when the session names a target app), then run *fn* with logging."""
    device = session.serial
    call_args = _call_args(inspect.signature(fn), args, kwargs)
    refusal = _guard_refusal(session.guard, tool_name, device, call_args)
    if refusal is not None:
        return _refuse(device, tool_name, refusal)
    target, refusal = _action_context(session.guard, tool_name, device, call_args)
    if refusal is not None:
        return _refuse(device, tool_name, refusal)
    return _call_with_action_log(fn, tool_name, device, (args, kwargs), call_args, target)


def _call_with_action_log(
    fn: Any,
    tool_name: str,
    device: str,
    call: tuple[tuple[Any, ...], dict[str, Any]],
    call_args: dict[str, Any],
    target: dict[str, str] | None,
) -> Any:
    """Run *fn* and append one action-log entry for it (result returned unchanged)."""
    args, kwargs = call
    with_target = tool_name in action_log.TARGET_TOOLS
    rec_started = action_log.REGISTRY.recording_started(device)
    t0 = action_log._clock()
    ok, error = False, "interrupted"
    try:
        result = fn(*args, **kwargs)
        ok, error = action_log.outcome_of(result)
        return result
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        entry = action_log.build_action_entry(
            tool_name,
            call_args,
            ok=ok,
            error=error,
            duration_ms=(action_log._clock() - t0) * 1000,
            readonly=tool_name in action_log.READONLY_TOOLS,
            target=target,
            with_target=with_target,
        )
        action_log.REGISTRY.append(device, entry, at=t0, rec_started=rec_started)


def _with_action_log(fn: Any, tool_name: str) -> Any:
    """Thin sync wrapper that logs *fn* calls while an action log is active.

    With no session on the call's device it calls *fn* directly — one dict
    lookup of extra work. A session with a target app is guarded first
    (``_run_in_session``) — a safety rail for agents, not a security
    boundary. ``functools.wraps`` keeps the name, docstring and
    (via ``__wrapped__``) the signature that CLI reflection inspects. The
    MCP, daemon and CLI paths all call this wrapper, so each call logs once.
    """
    if tool_name in action_log.UNLOGGED_TOOLS:
        return fn

    @wraps(fn)
    def logged(*args: Any, **kwargs: Any) -> Any:
        device = _device_arg(args, kwargs)
        session = action_log.REGISTRY.active(device) if isinstance(device, str) else None
        if session is None:
            return fn(*args, **kwargs)
        return _run_in_session(fn, tool_name, session, args, kwargs)

    return logged


def device_tool(fn: Any) -> Any:
    """Register a sync device function as an MCP tool.

    The sync function must accept ``device: str`` as its first parameter.
    A per-device semaphore + thread-dispatched async wrapper is auto-registered
    with FastMCP. Returned is a thin sync wrapper around the function (same
    name, docstring and signature) that records action-log entries while a
    session is active on the device and is otherwise a direct call — tests
    and the daemon import and call it like the original.
    """
    tool_name = _record_tool(fn, "device")
    logged = _with_action_log(fn, tool_name)

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        device = args[0] if args else kwargs["device"]
        t0 = time.monotonic()
        error_msg = None
        validation_only = False
        try:
            result = await _run_on_device(device, partial(logged, *args, **kwargs))
            if isinstance(result, dict) and "error" in result:
                error_msg = str(result["error"])
                validation_only = bool(result.get("validation_error"))
                return result
            if isinstance(result, dict):
                result = _apply_agent_image_budget(result, kwargs)
                if _INLINE_IMAGE_KEY in result:
                    return _wrap_inline_image(result)
            return result
        except Exception as exc:
            error_msg = str(exc)
            raise
        finally:
            duration_ms = (time.monotonic() - t0) * 1000
            # Validation errors (bad app_id, unknown route, etc.) are user-input
            # mistakes — not tool failures. Don't pollute error_rate with them.
            success = error_msg is None or validation_only
            if _metrics is not None:
                _metrics.record_tool_call(tool_name, success)
            log_mcp_call("linc-nerve", tool_name, kwargs, duration_ms, success, error_msg)

    _register_tool(wrapper, fn, tool_name)
    return logged


def global_tool(fn: Any) -> Any:
    """Register a sync non-device function as an MCP tool.

    No device serial, no per-device semaphore — just thread dispatch.
    """
    tool_name = _record_tool(fn, "global")

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.monotonic()
        error_msg = None
        validation_only = False
        try:
            result = await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))
            if isinstance(result, dict) and "error" in result:
                error_msg = str(result["error"])
                validation_only = bool(result.get("validation_error"))
            return result
        except Exception as exc:
            error_msg = str(exc)
            raise
        finally:
            duration_ms = (time.monotonic() - t0) * 1000
            success = error_msg is None or validation_only
            if _metrics is not None:
                _metrics.record_tool_call(tool_name, success)
            log_mcp_call("linc-nerve", tool_name, kwargs, duration_ms, success, error_msg)

    _register_tool(wrapper, fn, tool_name)
    return fn


# ---------------------------------------------------------------------------
# Initialization / shutdown
# ---------------------------------------------------------------------------


def _are_singletons_initialized() -> bool:
    return (
        _android is not None
        and _ios is not None
        and _state_mgr is not None
        and _metrics is not None
        and _device_mgr is not None
        and _emulator is not None
    )


def _init_singletons() -> None:
    global _android, _ios, _state_mgr, _metrics, _device_mgr, _emulator
    if _metrics is None:
        _metrics = MetricsCollector()
    if _android is None:
        _android = AndroidController()
        _android._metrics = _metrics
    if _ios is None:
        _ios = IOSController()
    if _state_mgr is None:
        _state_mgr = StateSessionManager()
    if _device_mgr is None:
        _device_mgr = DeviceManager()
    if _emulator is None:
        _emulator = EmulatorManager()
    atexit.register(_shutdown_cleanup)


def _ensure_initialized() -> tuple[AndroidController, IOSController]:
    """Lazy-init controllers (thread-safe)."""
    if not _are_singletons_initialized():
        with _init_lock:
            _init_singletons()
    return require(_android, "Android controller"), require(_ios, "iOS controller")


_cleanup_done: bool = False


def _shutdown_cleanup() -> None:
    """Stop scrcpy streams on exit to prevent zombie adb processes."""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    if _android is not None:
        scrcpy = _android._get_scrcpy()
        if scrcpy is not None:
            with contextlib.suppress(Exception):
                scrcpy.stop_all()


def _get_state_mgr() -> StateSessionManager:
    """Get the state session manager (initializes if needed)."""
    _ensure_initialized()
    return require(_state_mgr, "state session manager")


# ---------------------------------------------------------------------------
# Device cache helpers
# ---------------------------------------------------------------------------


def _refresh_cache() -> list[NerveDevice]:
    """Refresh the device cache from both controllers (thread-safe).

    Uses a non-blocking lock to prevent thread pile-up: if another thread
    is already refreshing, callers return the stale cache instead of blocking.
    """
    global _cached_devices, _cache_timestamp
    acquired = _cache_lock.acquire(blocking=False)
    if not acquired:
        return _cached_devices
    try:
        android, ios = _ensure_initialized()
        t0 = _clock()
        devices = android.discover() + ios.discover()
        _cached_devices = devices
        _cache_timestamp = _clock()
        _stale_confirmed.clear()
        log.debug(
            "device_cache_refreshed",
            duration_ms=(_cache_timestamp - t0) * 1000,
            device_count=len(devices),
        )
        return devices
    finally:
        _cache_lock.release()


def _get_cached_devices() -> list[NerveDevice]:
    """Get devices, refreshing cache if TTL expired."""
    if _clock() - _cache_timestamp > _CACHE_TTL:
        return _refresh_cache()
    return _cached_devices


def _invalidate_cache() -> None:
    """Force cache invalidation (e.g., after errors)."""
    global _cache_timestamp
    with _cache_lock:
        _cache_timestamp = _INVALIDATED
        _stale_confirmed.clear()


# ---------------------------------------------------------------------------
# Device resolution helpers
# ---------------------------------------------------------------------------


def _resolve_device(serial: str) -> tuple[NerveDevice | None, str]:
    """Resolve a device serial to a NerveDevice and platform.

    Uses cached device list. An expired cache still resolves a known Android
    serial after a cheap adb liveness check (``_stale_ready_android``); on a
    cache miss it forces a full refresh before giving up.
    Returns (device, platform_str) or (None, error_message).
    """
    _ensure_initialized()
    dev = (
        _stale_ready_android(serial)
        or _find_serial(_get_cached_devices(), serial)
        or _find_serial(_refresh_cache(), serial)
    )
    if dev is None:
        return None, f"Device '{serial}' not found. Run nerve_discover to list devices."
    return dev, dev.platform.value


def _find_serial(devices: list[NerveDevice], serial: str) -> NerveDevice | None:
    return next((d for d in devices if d.serial == serial), None)


def _cache_expired_not_invalidated() -> bool:
    """True when the cache is past its TTL but was not explicitly invalidated."""
    return _cache_timestamp != _INVALIDATED and _clock() - _cache_timestamp > _CACHE_TTL


def _is_stale_confirmed(known: NerveDevice) -> bool:
    """True if *known* (this exact cached entry) was confirmed ready within _CACHE_TTL."""
    with _cache_lock:
        memo = _stale_confirmed.get(known.serial)
    return memo is not None and memo[1] is known and _clock() - memo[0] <= _CACHE_TTL


def _memo_stale_confirmed(known: NerveDevice) -> None:
    """Record that adb just confirmed the cached entry *known* as ready."""
    with _cache_lock:
        _stale_confirmed[known.serial] = (_clock(), known)


def _stale_ready_android(serial: str) -> NerveDevice | None:
    """Resolve *serial* from an EXPIRED cache if adb still lists it as ready.

    A full ``_refresh_cache`` on the request path runs three ``getprop``
    shells per connected Android device — each under that device's lock, so
    it queues behind another lane's ``uiautomator dump`` — plus
    ``ios.discover()`` (linc-nerve-fv0, measured on the Tab Active3: ~8.6 s
    of a ~9.2 s ``key BACK`` in the darwin-background launchd daemon). A
    serial the cache already knows only needs one ``host:devices`` round
    trip to confirm it is still connected; its model/type do not change.
    Android only (iOS has no equally cheap probe); any error, or a serial
    adb no longer lists, returns None so the caller does the full refresh.
    An explicit ``_invalidate_cache()`` is never answered from the stale
    entry (a restarted AVD can reuse ``emulator-5554`` with a new model).
    A successful check is memoised per serial for ``_CACHE_TTL`` so a busy
    lane on a known serial does not pay the probe on every call; the memo
    is cleared by any refresh or invalidation and never caches a failure.
    """
    if not _cache_expired_not_invalidated():
        return None  # fresh (lookup already free) or invalidated (must re-read)
    known = _find_serial(_cached_devices, serial)
    if known is None or known.platform is not Platform.ANDROID:
        return None
    return known if _is_stale_confirmed(known) or _probe_ready(known) else None


def _probe_ready(known: NerveDevice) -> bool:
    """One adb liveness check for *known*; memoise only a definite True."""
    try:
        ready = _ensure_initialized()[0].is_ready_serial(known.serial)
    except Exception as exc:
        log.debug("stale_device_check_failed", serial=known.serial, error=str(exc))
        return False
    if ready is True:
        _memo_stale_confirmed(known)
    return ready is True


def _get_controller(platform: str) -> AndroidController | IOSController:
    """Get the controller for a platform."""
    android, ios = _ensure_initialized()
    if platform == "ios":
        return ios
    return android


def _check_guard(serial: str) -> dict[str, str] | None:
    """Check if a device is reserved by another session.

    Returns an error dict if blocked, None if access is allowed.
    """
    if _device_mgr is None:
        return None
    error = _device_mgr.guard(serial)
    if error:
        return {"error": error}
    return None


# ---------------------------------------------------------------------------
# Output path helper
# ---------------------------------------------------------------------------


def _next_output_path(serial: str, tool: str, ext: str = "png") -> str:
    """Generate a unique output file path for a device tool result.

    Pattern: <tmp>/nerve-output-<random>/<serial>_<counter>_<tool>.<ext>
    Counter prevents collisions from rapid sequential calls.
    """
    global _file_counter
    safe_serial = serial.replace("/", "_").replace(":", "_")
    with _counter_lock:
        _file_counter += 1
        counter = _file_counter
    return os.path.join(_output_dir(), f"{safe_serial}_{counter:04d}_{tool}.{ext}")


def _output_dir() -> str:
    """Return this process's private output dir, creating it on first use."""
    global _OUTPUT_DIR
    with _output_dir_lock:
        if _OUTPUT_DIR is None:
            _OUTPUT_DIR = tempfile.mkdtemp(prefix="nerve-output-")
        return _OUTPUT_DIR
