"""UI hierarchy, state, and diff tools.

Navigation tools (nerve_navigate / nerve_navigate_screenshot) and their
stability helpers live in ``navigation_tools.py`` + ``_stability.py`` (split
out in Batch 4 N5 to keep this file under the 400-line advisory).
"""

from __future__ import annotations

from typing import Any

from linc_nerve.core.grounding import (
    VISUAL_GROUNDING_HINT,
    ground_visual_targets,
    render_grounded_overlay,
    should_suggest_visual_grounding,
)
from linc_nerve.mcp import _core
from linc_nerve.mcp._core import device_tool
from linc_nerve.mcp._ui_helpers import _scroll_and_merge


@device_tool
def _nerve_elements_sync(
    device: str,
    diff_only: bool = False,
    scroll_depth: int = 0,
) -> dict[str, Any]:
    """Return the UI element tree only (clickable + text widgets) — no image bytes.

    PRIMARY perception tool for structural questions — reach for this (or
    nerve_state) first; the element tree answers structural queries at
    ~30-150 tokens with no image cost.

    USE FOR: scripted assertions against the UI element tree, listing
        clickable widgets, token-saving structure reads without image data.
    NOT FOR: full state bundled with a screenshot (nerve_state); poll-loop
        delta detection (nerve_diff); genuinely needing pixels
        (nerve_screenshot, the visual fallback).

    Args:
        device: Device serial.
        diff_only: Only elements changed since last call.
        scroll_depth: Scroll-and-dump passes, 0-5.

    Returns:
        elements dict with the UI tree only.
    """
    _core._ensure_initialized()
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    if diff_only and scroll_depth > 0:
        return {"error": "diff_only and scroll_depth are mutually exclusive."}

    guard = _core._check_guard(device)
    if guard:
        return guard

    scroll_depth = min(scroll_depth, 5)

    try:
        ctrl = _core._get_controller(platform)
        elements = ctrl.elements(device)

        if diff_only:
            session = _core._get_state_mgr().get(device)
            diff = session.update_elements(elements)
            if diff is None:
                return {
                    "status": "unchanged",
                    "count": len(elements),
                    "device": device,
                }
            return {
                "diff": diff,
                "count": len(elements),
                "device": device,
            }

        if scroll_depth > 0:
            return _scroll_and_merge(ctrl, device, elements, scroll_depth)

        out: dict[str, Any] = {
            "elements": elements,
            "count": len(elements),
            "device": device,
        }
        if should_suggest_visual_grounding(elements):
            out["visual_grounding_hint"] = VISUAL_GROUNDING_HINT
        return out
    except NotImplementedError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {
            "error": f"Element dump failed: {exc}",
            "visual_grounding_hint": VISUAL_GROUNDING_HINT,
        }


# ---------------------------------------------------------------------------
# State collection helpers
# ---------------------------------------------------------------------------


def _collect_android_state(
    android: Any,
    device: str,
    state: dict[str, Any],
    session: Any | None,
    changes: dict[str, bool],
) -> bytes | None:
    try:
        fg = android.get_foreground(device)
        state["foreground"] = fg
        if session:
            changes["foreground"] = session.update_foreground(fg)
            changes["foreground_changed_due_to_crash"] = session.foreground_changed_due_to_crash
    except Exception:
        state["foreground"] = {}

    try:
        orientation = android.get_orientation(device)
        state["orientation"] = orientation
        if session:
            changes["orientation"] = session.update_orientation(orientation)
    except Exception:
        state["orientation"] = "unknown"

    try:
        state["screen_size"] = android.get_screen_size(device)
    except Exception:
        state["screen_size"] = {}

    try:
        elements = android.elements(device)
        state["elements"] = elements[:30]
        state["element_count"] = len(elements)
        if session:
            diff = session.update_elements(elements)
            changes["elements"] = diff is not None
    except Exception as exc:
        # Never report element_count: 0 for a failed dump — an agent reads
        # that as "empty screen" and falls back to screenshot-only vision
        # (reactor 8bba730a). The screenshot below still ships.
        state["elements_error"] = str(exc)

    try:
        data = android.screenshot(device)
        state["screenshot_size"] = len(data)
        if session:
            changed, distance = session.check_screenshot(data)
            changes["screenshot"] = changed
            state["hamming_distance"] = distance
        return data
    except Exception:
        return None


def _collect_ios_state(
    ios: Any,
    device: str,
    state: dict[str, Any],
    session: Any | None,
    changes: dict[str, bool],
) -> bytes | None:
    try:
        data = ios.screenshot(device)
        state["screenshot_size"] = len(data)
        if session:
            changed, distance = session.check_screenshot(data)
            changes["screenshot"] = changed
            state["hamming_distance"] = distance
        return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Composite State
# ---------------------------------------------------------------------------


@device_tool
def _nerve_state_sync(device: str, track_changes: bool = False) -> Any:
    """Capture full device state in one call (screenshot + UI elements + foreground).

    PRIMARY perception tool — the bundled element tree is the main signal;
    the screenshot rides along for visual verification.

    USE FOR: cold-start grabs and after-navigation snapshots bundling
        screenshot pixels, the UI element tree, and the foreground
        app/activity in one call.
    NOT FOR: a one-off image with no tree/foreground (nerve_screenshot);
        tree-only reads when token budget matters (nerve_elements); poll
        loops since the last snapshot (nerve_diff).

    Args:
        device: device serial.
        track_changes: Include change tracking info.

    Returns:
        state dict with elements + screenshot path + foreground. If the tree
        could not be dumped, the dict carries ``elements_error`` INSTEAD of
        elements/element_count — "tree unavailable", never "empty screen".
    """
    android, ios = _core._ensure_initialized()
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    state: dict[str, Any] = {"device": device, "platform": platform}
    session = _core._get_state_mgr().get(device) if track_changes else None
    changes: dict[str, bool] = {}
    screenshot_bytes: bytes | None = None

    if platform == "android":
        screenshot_bytes = _collect_android_state(android, device, state, session, changes)
    else:
        screenshot_bytes = _collect_ios_state(ios, device, state, session, changes)

    if track_changes:
        state["changes"] = changes

    if screenshot_bytes is not None:
        path = _core._next_output_path(device, "state")
        with open(path, "wb") as f:
            f.write(screenshot_bytes)
        state["screenshot"] = path
        state.update(_core._screenshot_geometry(screenshot_bytes, state.get("screen_size", {})))

    if should_suggest_visual_grounding(state.get("elements"), state.get("elements_error")):
        state["visual_grounding_hint"] = VISUAL_GROUNDING_HINT

    return state


# ---------------------------------------------------------------------------
# Delta Detection
# ---------------------------------------------------------------------------


@device_tool
def _nerve_diff_sync(device: str) -> Any:
    """Return UI changes since the last nerve_state snapshot on this device.

    PRIMARY perception tool for poll loops — returns only the delta, far
    cheaper than re-capturing on every tick.

    USE FOR: poll loops watching for UI updates after an action.
    NOT FOR: cold-start / no-prior-snapshot contexts (call nerve_state first
        to establish a baseline).

    Args:
        device: device serial.

    Returns:
        diff dict with added / removed / changed elements.
    """
    android, _ios = _core._ensure_initialized()
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    session = _core._get_state_mgr().get(device)
    result: dict[str, Any] = {"device": device, "platform": platform}
    changes: dict[str, bool] = {}

    # Screenshot delta
    ctrl = _core._get_controller(platform)
    screenshot_bytes: bytes | None = None
    try:
        data = ctrl.screenshot(device)
        changed, distance = session.check_screenshot(data)
        changes["screenshot"] = changed
        result["hamming_distance"] = distance
        if changed:
            screenshot_bytes = data
            result["screenshot_size"] = len(data)
    except Exception as exc:
        result["screenshot_error"] = str(exc)

    # Android-specific diffs
    if platform == "android":
        try:
            fg = android.get_foreground(device)
            fg_changed = session.update_foreground(fg)
            changes["foreground"] = fg_changed
            changes["foreground_changed_due_to_crash"] = session.foreground_changed_due_to_crash
            if fg_changed:
                result["foreground"] = fg
        except Exception:
            pass

        try:
            orientation = android.get_orientation(device)
            orient_changed = session.update_orientation(orientation)
            changes["orientation"] = orient_changed
            if orient_changed:
                result["orientation"] = orientation
        except Exception:
            pass

        try:
            elements = android.elements(device)
            diff = session.update_elements(elements)
            changes["elements"] = diff is not None
            if diff is not None:
                result["element_diff"] = diff
                result["element_count"] = len(elements)
        except Exception as exc:
            result["elements_error"] = str(exc)

    result["changes"] = changes
    result["any_change"] = any(changes.values())
    result["frames_since_change"] = session.frames_since_change

    if screenshot_bytes is not None:
        path = _core._next_output_path(device, "diff")
        with open(path, "wb") as f:
            f.write(screenshot_bytes)
        result["screenshot"] = path

    return result


@device_tool
def _nerve_ground_sync(
    device: str,
    prompt: str,
    annotate: bool = False,
) -> dict[str, Any]:
    """Locate UI controls visually by natural-language concept prompt using Meta SAM 3.1.

    USE FOR: grounding visual UI elements when nerve_elements / nerve_state
        return an empty or unlabelled hierarchy (uiautomator dump killed,
        custom Compose/Flutter/WebView canvas, or unlabelled icon buttons).
    NOT FOR: standard native screens where nerve_elements already returns
        labelled bounds (reach for nerve_elements / nerve_state first).
    TIP: returns physical device coordinates ready for nerve_tap(x, y), or
        pass prompt directly to nerve_tap(device, prompt=...).

    Args:
        device: Device serial.
        prompt: Natural-language description of the visual UI target to segment.
        annotate: Save and return a green mask/centroid overlay PNG when True.

    Returns:
        Dict with count, targets (bounds, center, confidence), and top_target.
    """
    _core._ensure_initialized()
    if not isinstance(prompt, str) or not prompt.strip():
        return {"error": "prompt must be a non-empty string", "validation_error": True}

    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    ctrl = _core._get_controller(platform)
    try:
        png_bytes = ctrl.screenshot(device)
        screen_size = ctrl.get_screen_size(device) if hasattr(ctrl, "get_screen_size") else None
        targets = ground_visual_targets(png_bytes, prompt.strip(), screen_size=screen_size)
        out: dict[str, Any] = {
            "device": device,
            "prompt": prompt.strip(),
            "count": len(targets),
            "targets": targets,
            "top_target": targets[0] if targets else None,
        }
        if screen_size:
            out["screen_size"] = screen_size
        if annotate:
            overlay_bytes = render_grounded_overlay(png_bytes, targets, screen_size=screen_size)
            path = _core._next_output_path(device, "grounded")
            with open(path, "wb") as f:
                f.write(overlay_bytes)
            out["screenshot"] = path
            out[_core._INLINE_IMAGE_KEY] = overlay_bytes
        return out
    except Exception as exc:
        return {"error": f"Visual grounding failed: {exc}"}
