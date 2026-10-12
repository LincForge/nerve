"""UI hierarchy, state, and diff tools.

Navigation tools (nerve_navigate / nerve_navigate_screenshot) and their
stability helpers live in ``navigation_tools.py`` + ``_stability.py`` (split
out in Batch 4 N5 to keep this file under the 400-line advisory).
"""

from __future__ import annotations

import re
from typing import Any

from linc_nerve.mcp import _core
from linc_nerve.mcp._core import device_tool

# ---------------------------------------------------------------------------
# Scroll-and-merge helper (used by nerve_elements with scroll_depth)
# ---------------------------------------------------------------------------


def _scroll_and_merge(
    ctrl: Any,
    device: str,
    elements: list[dict],
    scroll_depth: int,
) -> dict[str, Any]:
    bounds_re = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
    scroll_bounds = None
    for el in elements:
        if el.get("scrollable"):
            m = bounds_re.match(el.get("bounds", ""))
            if m:
                scroll_bounds = (
                    int(m.group(1)),
                    int(m.group(2)),
                    int(m.group(3)),
                    int(m.group(4)),
                )
                break

    if scroll_bounds is None:
        return {
            "elements": elements,
            "count": len(elements),
            "device": device,
            "scroll_steps": 0,
            "note": "no scrollable container found",
        }

    x1, y1, x2, y2 = scroll_bounds
    cx = (x1 + x2) // 2
    margin = (y2 - y1) // 10
    swipe_from_y = y2 - margin
    swipe_to_y = y1 + margin

    seen: set[tuple[str, str, str]] = set()
    merged: list[dict] = []
    for el in elements:
        key = (el.get("id", ""), el.get("text", ""), el.get("bounds", ""))
        if key not in seen:
            seen.add(key)
            merged.append(el)

    actual_steps = 0
    for _ in range(scroll_depth):
        ctrl.swipe(device, cx, swipe_from_y, cx, swipe_to_y, 300)
        new_elements = ctrl.elements(device)
        new_count = 0
        for el in new_elements:
            key = (el.get("id", ""), el.get("text", ""), el.get("bounds", ""))
            if key not in seen:
                seen.add(key)
                merged.append(el)
                new_count += 1
        actual_steps += 1
        if new_count == 0:
            break

    return {
        "elements": merged,
        "count": len(merged),
        "device": device,
        "scroll_steps": actual_steps,
    }


# ---------------------------------------------------------------------------
# UI Hierarchy
# ---------------------------------------------------------------------------


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

        return {
            "elements": elements,
            "count": len(elements),
            "device": device,
        }
    except NotImplementedError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Element dump failed: {exc}"}


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
