"""Screenshot and scrcpy streaming tools."""

from __future__ import annotations

import base64
import time
from typing import Any

from linc_nerve.core.android import AndroidController
from linc_nerve.mcp import _core
from linc_nerve.mcp._core import device_tool, global_tool

# --- iOS MJPEG routing helpers (extracted to keep the tool functions simple
# and the CRAP ratchet happy — each is small and directly unit-tested). ---


def _ios_mjpeg_port(device: str) -> int | None:
    """The WDA MJPEG port for an iOS sim session, or None if MJPEG is unavailable."""
    session = _core._get_controller("ios")._get_wda(device)
    return getattr(session, "mjpeg_port", None)


def _ios_stream_start(device: str) -> dict[str, Any]:
    """Open an iOS WDA MJPEG stream for a device."""
    port = _ios_mjpeg_port(device)
    if port is None:
        return {"error": "MJPEG not available for this iOS session."}
    try:
        _core._get_controller("ios")._mjpeg.start_stream(device, port)
    except Exception as exc:
        return {"error": f"Stream start failed: {exc}"}
    return {"success": True, "device": device, "port": port, "transport": "wda-mjpeg"}


def _ios_stream_stop(device: str) -> dict[str, Any]:
    """Stop every iOS MJPEG stream registered for a device serial."""
    mgr = _core._get_controller("ios")._mjpeg
    for s, p in [k for k in mgr._streams if k[0] == device]:
        mgr.stop_stream(s, p)
    return {"success": True, "device": device}


def _ios_stream_frame(device: str) -> bytes:
    """Pull one PNG frame from an iOS sim's warm MJPEG stream (raises on failure)."""
    port = _ios_mjpeg_port(device)
    if port is None:
        raise RuntimeError("MJPEG not available for this iOS session")
    return _core._get_controller("ios")._mjpeg.screenshot(device, port)


def _ios_active_streams() -> dict[str, Any]:
    """Active iOS MJPEG streams (empty if the iOS controller is unavailable)."""
    try:
        return dict(_core._get_controller("ios")._mjpeg.active_streams)
    except Exception:
        return {}


def _android_stream_stop(android: AndroidController, device: str) -> dict[str, Any]:
    """Stop an Android scrcpy stream for a device."""
    scrcpy = android._get_scrcpy()
    if scrcpy is None:
        return {"error": "scrcpy not available."}
    scrcpy.stop_stream(device)
    return {"success": True, "device": device}


@device_tool
def _nerve_screenshot_sync(
    device: str,
    skip_if_unchanged: bool = False,
    full_resolution: bool = False,
    annotate: bool = False,
) -> Any:
    """Capture one screenshot — a stateless PNG of the current device display.

    VISUAL FALLBACK — nerve_state / nerve_elements are primary; use this
    when you need pixels.

    USE FOR: a single one-off screenshot right now — one PNG file of the
        current screen with no streaming session and no setup needed — BVT
        golden fixtures, stateless visual diffs, confirming the UI looks
        right.
    NOT FOR: structural questions (nerve_state / nerve_elements first); a
        frame off a running stream (nerve_stream_screenshot); video
        (nerve_record_start).

    TIP: image arrives inline (~300k-px budget; full_resolution=true =
        raw); canonical PNG saved alongside.

    Args:
        device: Device serial.
        skip_if_unchanged: Return "unchanged" when static.
        full_resolution: Raw capture, no downscaling.
        annotate: numbered element boxes + elements.

    Returns:
        dict with screenshot path + inline image.
    """
    _core._ensure_initialized()
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    if _core._metrics is not None:
        _core._metrics.record_screenshot(device)

    guard = _core._check_guard(device)
    if guard:
        return guard

    ctrl = _core._get_controller(platform)
    try:
        data = ctrl.screenshot(device, full_resolution=full_resolution)

        if skip_if_unchanged:
            session = _core._get_state_mgr().get(device)
            # First observation always returns the image — no prior frame to
            # compare against. Only skip when a baseline existed AND it matches.
            had_baseline = session.last_screenshot_hash is not None
            changed, distance = session.check_screenshot(data)
            if had_baseline and not changed:
                return {
                    "status": "unchanged",
                    "frames_since_change": session.frames_since_change,
                    "hamming_distance": distance,
                    "device": device,
                }

        annotate_extras: dict[str, Any] = {}
        if annotate:
            data, annotate_extras = _annotate_best_effort(ctrl, device, data)

        path = _core._next_output_path(device, "screenshot")
        with open(path, "wb") as f:
            f.write(data)

        result = _build_screenshot_result(path, device, data, annotate_extras)
        # Carries the captured PNG bytes so the MCP tool wrapper (_core.py)
        # can deliver an inline, size-budgeted ImageContent block alongside
        # this dict — see linc-nerve#97. Stripped before the wire response;
        # invisible to direct callers of this function (tests,
        # navigation_tools.py's fallback path).
        result[_core._INLINE_IMAGE_KEY] = data
        return result
    except NotImplementedError as exc:
        # This serial cannot be serviced at all (e.g. a physical Apple Watch
        # has no WDA support) — distinct from a transient failure so callers
        # and the :8111 daemon don't retry a request that can never succeed.
        return {"error": f"unsupported: {exc}", "device": device, "unsupported": True}
    except Exception as exc:
        return {"error": f"Screenshot failed: {exc}", "device": device}


def _build_screenshot_result(
    path: str, device: str, data: bytes, annotate_extras: dict[str, Any]
) -> dict[str, Any]:
    """Assemble the nerve_screenshot success dict — path, size, pixel geometry.

    Pulled out of ``_nerve_screenshot_sync`` so that function's CRAP score
    does not rise (reactor 1cac8e2f).
    """
    size = _core._image_size(data)
    return {
        "screenshot": path,
        "device": device,
        "size_bytes": len(data),
        "width": size[0] if size else None,
        "height": size[1] if size else None,
        **annotate_extras,
    }


def _annotate_best_effort(ctrl: Any, device: str, data: bytes) -> tuple[bytes, dict[str, Any]]:
    """Overlay the element tree on a screenshot, best-effort.

    Returns (image, result-extras): elements + element_count on success,
    annotate_error on a failed dump. A failed element dump must not fail
    the whole screenshot call — the raw frame passes through and the
    extras say why the overlay is missing (reactor 8bba730a).
    """
    from linc_nerve.core.annotator import annotate_screenshot

    try:
        elements = ctrl.elements(device)
        return annotate_screenshot(data, elements), {
            "elements": elements,
            "element_count": len(elements),
        }
    except Exception as exc:
        return data, {"annotate_error": str(exc)}


@device_tool
def _nerve_stream_start_sync(
    device: str, max_size: int = 1080, max_fps: int = 10
) -> dict[str, Any]:
    """Open a live screen-mirror session for warming up fast screenshots.

    Persistent scrcpy stream (iOS sims: WDA MJPEG) — an in-memory frame
    source, no on-disk artifact.

    USE FOR: opening a live screen-mirror session, warming a scrcpy stream
        before a burst of captures, amortising screenshot latency.
    NOT FOR: a durable MP4 (nerve_record_start / nerve_record_fixed); a
        one-off capture (nerve_screenshot); fetching a frame off a running
        stream (nerve_stream_screenshot).

    TIP: ~35-70ms per frame vs 200-500ms screencap; needs the scrcpy-server
        binary in the vendor directory.

    Args:
        device: Device serial. Android (scrcpy) or iOS simulator (WDA MJPEG).
        max_size: Max dimension px (default 1080). Android only.
        max_fps: Max frame rate (default 10). Android only.

    Returns:
        dict with success, device, port, max_size, max_fps — or error.
    """
    android, _ = _core._ensure_initialized()
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    if platform == "ios":
        return _ios_stream_start(device)

    scrcpy = android._get_scrcpy()
    if scrcpy is None:
        return {
            "error": "scrcpy-server binary not found. "
            "Download from https://github.com/Genymobile/scrcpy/releases "
            "and place in linc-nerve's vendor directory."
        }

    try:
        stream = scrcpy.start_stream(device, max_size=max_size, max_fps=max_fps)
        return {
            "success": True,
            "device": device,
            "port": stream._port,
            "max_size": max_size,
            "max_fps": max_fps,
        }
    except Exception as exc:
        return {"error": f"Stream start failed: {exc}"}


@device_tool
def _nerve_stream_stop_sync(device: str) -> dict[str, Any]:
    """Tear down the live mirror session opened by nerve_stream_start.

    Closes the persistent scrcpy stream and releases the device-side server
    socket, the host-side TCP forward, and the background frame-decoder
    thread. The stream is discarded entirely — there is no pause primitive,
    so a re-open via nerve_stream_start is required to resume fast captures.
    No-op if no stream is active for this device.

    USE FOR: tearing down the live mirror session when fast captures are no
        longer needed, closing the persistent scrcpy stream to release
        device-side resources, ending a streaming session before
        disconnecting the device.
    NOT FOR: pausing the stream — there is no pause; re-open with
        nerve_stream_start when needed; stopping a durable on-disk
        recording (use nerve_record_stop, which finalises an MP4 file
        rather than discarding in-memory frames).

    TIP: idempotent — safe to call when no stream is active; returns
        success either way.

    Args:
        device: Device serial. Android (scrcpy) or iOS simulator (WDA MJPEG).

    Returns:
        dict with success flag and device — or error if scrcpy unavailable.
    """
    android, _ = _core._ensure_initialized()
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    if platform == "ios":
        return _ios_stream_stop(device)
    return _android_stream_stop(android, device)


@global_tool
def _nerve_stream_status_sync() -> dict[str, Any]:
    """Report status and health of active screen-mirror streaming sessions.

    Returns whether the stream is alive, its uptime, and bandwidth stats —
    without fetching any image data. Use this when an agent wants to know
    whether a streaming session is still running before pulling captures from
    it.

    USE FOR: stream health checks, uptime and stats queries, verifying a
        streaming session is still alive before attempting captures.
    NOT FOR: fetching the latest captured image off an active stream (use
        nerve_stream_screenshot for that); a one-off PNG with no stream setup
        (use nerve_screenshot); device discovery (use nerve_discover).

    TIP: returns <1KB JSON status report, no image bytes.

    Args:
        (none — global tool, queries all active streams)

    Returns:
        dict with active streams metadata and scrcpy_available flag.
    """
    android, _ = _core._ensure_initialized()
    scrcpy = android._get_scrcpy()
    streams = dict(scrcpy.active_streams) if scrcpy is not None else {}
    streams.update(_ios_active_streams())

    return {
        "streams": streams,
        "scrcpy_available": scrcpy is not None,
    }


@device_tool
def _nerve_stream_screenshot_sync(device: str, inline: bool = False) -> dict[str, Any]:
    """Pull the current frame from an active screen-mirror streaming session.

    Requires an active stream; lower-latency than nerve_screenshot when one
    is already open.

    USE FOR: reading the most recent frame off an existing screen-mirror
        stream, low-latency frame fetch during a streaming session.
    NOT FOR: a one-off capture without setup (nerve_screenshot); stream
        health checks (nerve_stream_status); producing video
        (nerve_record_start).

    TIP: inline=True embeds base64 in JSON text, billed as TEXT tokens
        (tens of thousands per frame even after budgeting) — prefer the
        default path response unless a disk hop is impossible.

    Args:
        device: Device serial. Android (scrcpy) or iOS simulator (WDA MJPEG).
        inline: Return base64 in screenshot_b64 instead of writing to disk.

    Returns:
        frame dict with screenshot path (default) or screenshot_b64 (inline).
    """
    t0 = time.monotonic()
    android, _ = _core._ensure_initialized()
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    if _core._metrics is not None:
        _core._metrics.record_screenshot(device)

    guard = _core._check_guard(device)
    if guard:
        return guard

    try:
        if platform == "ios":
            data = _ios_stream_frame(device)
        else:
            scrcpy = android._get_scrcpy()
            if scrcpy is None:
                return {"error": "scrcpy-server binary not found."}
            data = scrcpy.screenshot(device)
        data = AndroidController._downscale_if_needed(data)
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        if inline:
            from linc_nerve.mcp._core import _rescale_for_agent

            data = _rescale_for_agent(data)
            b64 = base64.b64encode(data).decode("ascii")
            return {
                "screenshot_b64": b64,
                "size_bytes": len(data),
                "device": device,
                "latency_ms": latency_ms,
            }
        path = _core._next_output_path(device, "stream_screenshot")
        with open(path, "wb") as f:
            f.write(data)
        return {
            "screenshot": path,
            "size_bytes": len(data),
            "device": device,
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        return {"error": f"Stream screenshot failed: {exc}", "device": device}
