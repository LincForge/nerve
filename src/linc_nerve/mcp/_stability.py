"""UI-stability + screenshot-resolution helpers for navigation tools.

Private module (no ``_tools`` suffix) — these are internal helpers, not MCP
tools, so they are deliberately NOT auto-discovered by
``sse_server._discover_tool_modules``. Split out of ``ui_tools.py`` (Batch 4 N5)
to keep both files under the 400-line advisory. Consumed by
``navigation_tools._nerve_navigate_screenshot_sync``.
"""

from __future__ import annotations

import time
from typing import Any


def _check_pil_support(ctrl: Any, device: str, platform: str) -> bool:
    """Check if PIL fast-path screenshots are available (scrcpy Android only).

    Internal helper — not an MCP tool. The ``ctrl`` arg is resolved by callers
    via ``_core._get_controller(platform)`` so it stays out of the public
    schema.
    """
    if platform != "android":
        return False
    try:
        return ctrl.screenshot_pil(device) is not None
    except Exception:
        return False


def _poll_stability(
    ctrl: Any,
    device: str,
    settle_ms: int,
    use_pil: bool,
) -> dict[str, Any]:
    """Wait for UI to stabilize, falling back to fixed sleep on failure."""
    try:
        from linc_nerve.core.state_engine import wait_for_stable_ui

        if use_pil:
            return wait_for_stable_ui(
                lambda: ctrl.screenshot_pil(device),
                timeout_ms=settle_ms,
                pil_mode=True,
            )
        return wait_for_stable_ui(
            lambda: ctrl.screenshot(device),
            timeout_ms=settle_ms,
        )
    except Exception:
        time.sleep(settle_ms / 1000.0)
        return {"stable": False, "elapsed_ms": settle_ms, "polls": 0}


def _resolve_screenshot_data(
    stability: dict[str, Any],
    annotate: bool,
    ctrl: Any,
    device: str,
) -> tuple[bytes | None, list[dict] | None]:
    """Extract screenshot bytes from stability result (PIL or raw path)."""
    elements: list[dict] | None = None

    # Path 1: PIL fast path — annotate as PIL, single PNG encode at end.
    last_pil = stability.get("last_frame_pil")
    if last_pil is not None:
        import io as _io

        if annotate:
            from linc_nerve.core.annotator import annotate_screenshot_pil

            try:
                elements = ctrl.elements(device)
                last_pil = annotate_screenshot_pil(last_pil, elements)
            except Exception:
                # Annotation is best-effort — deliver the raw frame; the
                # dump failure itself surfaces via nerve_state/nerve_elements.
                elements = None

        buf = _io.BytesIO()
        last_pil.save(buf, format="PNG")
        return buf.getvalue(), elements

    # Path 2: Bytes path — reuse last_frame from stability polling.
    last_frame = stability.get("last_frame")
    if last_frame is not None:
        if annotate:
            from linc_nerve.core.annotator import annotate_screenshot

            try:
                elements = ctrl.elements(device)
                last_frame = annotate_screenshot(last_frame, elements)
            except Exception:
                elements = None
        return last_frame, elements

    return None, None


def _build_nav_screenshot_result(
    app_id: str,
    route_id: str,
    nav_result: dict[str, Any],
    screenshot_path: str,
    size_bytes: int,
    settle_ms: int,
    stability: dict[str, Any],
    annotate: bool,
    elements: list[dict] | None,
) -> dict[str, Any]:
    """Build the standard navigate_screenshot response dict."""
    result: dict[str, Any] = {
        "success": True,
        "action": "navigate_screenshot",
        "app_id": app_id,
        "route_id": route_id,
        "uri": nav_result.get("uri", ""),
        "screenshot": screenshot_path,
        "size_bytes": size_bytes,
        "settle_ms": settle_ms,
        "stable": stability.get("stable", False),
        "stability_ms": stability.get("elapsed_ms", 0),
        "stability_polls": stability.get("polls", 0),
    }
    if annotate and elements is not None:
        result["elements"] = elements
        result["element_count"] = len(elements)
    return result
