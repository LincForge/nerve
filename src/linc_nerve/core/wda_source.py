"""WDA UI-source tree parsing.

Split out of ``wda.py`` (#53) to keep both files under the hot-file LOC ceiling.
Converts the WDA ``/source`` JSON tree into the flat ElementNode dict list shared
with Android's uiautomator output. Pure functions — no process/HTTP I/O and no
module-globals that the test suite patches — so they move cleanly. ``parse_wda_source``
is re-exported from ``wda`` to preserve ``from linc_nerve.core.wda import parse_wda_source``.
"""

from __future__ import annotations

import math
import re

from .device import ElementNode

# XCUIElement types considered clickable/tappable.
# WDA returns short names (e.g., "Button") not full names ("XCUIElementTypeButton").
_TAPPABLE_TYPES = frozenset(
    {
        "Button",
        "Cell",
        "Link",
        "Switch",
        "Toggle",
        "SegmentedControl",
        "Tab",
        "TextField",
        "SecureTextField",
        "SearchField",
        # Also match full XCUIElementType prefix form (varies by WDA version)
        "XCUIElementTypeButton",
        "XCUIElementTypeCell",
        "XCUIElementTypeLink",
        "XCUIElementTypeSwitch",
        "XCUIElementTypeToggle",
        "XCUIElementTypeSegmentedControl",
        "XCUIElementTypeTab",
        "XCUIElementTypeTextField",
        "XCUIElementTypeSecureTextField",
        "XCUIElementTypeSearchField",
    }
)

# Types to skip in element listing (container types with no useful content)
_CONTAINER_TYPES = frozenset(
    {
        "Application",
        "Window",
        "Other",
        "XCUIElementTypeApplication",
        "XCUIElementTypeWindow",
        "XCUIElementTypeOther",
    }
)

_FRAME_RE = re.compile(r"-?\d+(?:\.\d+)?")  # Extract signed numbers from "{{x, y}, {w, h}}"
_BOUNDS_RE = re.compile(r"\A\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]\Z")
_MAX_SCANNED_NODES = 2500


def parse_wda_source(
    tree: dict,
    max_depth: int = 10,
    max_elements: int = 80,
) -> list[dict]:
    """Convert WDA source JSON tree to flat ElementNode dict list.

    Matches the format produced by Android's uiautomator → ElementNode.to_dict().
    """
    if not isinstance(tree, dict) or max_elements <= 0:
        return []
    candidates: list[ElementNode] = []
    _walk(tree, candidates, depth=0, max_depth=max_depth, max_elements=_MAX_SCANNED_NODES)
    ordered = _prioritise_viewport(candidates, _extract_viewport(tree))
    result: list[dict] = []
    for idx, elem in enumerate(ordered[:max_elements]):
        elem.index = idx
        item = elem.to_dict()
        item["index"] = elem.index
        result.append(item)
    return result


def _finite_ints(
    raw_x: object,
    raw_y: object,
    raw_w: object,
    raw_h: object,
) -> tuple[int, int, int, int] | None:
    """Convert four coordinate tokens to finite ints ``(x, y, w, h)`` or ``None``."""
    try:
        coords = (
            float(raw_x),  # type: ignore[arg-type]
            float(raw_y),  # type: ignore[arg-type]
            float(raw_w),  # type: ignore[arg-type]
            float(raw_h),  # type: ignore[arg-type]
        )
    except (ValueError, TypeError, OverflowError):
        return None
    if not all(math.isfinite(c) for c in coords):
        return None
    x, y, w, h = (int(c) for c in coords)
    return x, y, w, h


def _parse_frame(frame_str: str) -> str:
    """Convert WDA frame string '{{x, y}, {w, h}}' to bounds '[x,y][x+w,y+h]'."""
    if not isinstance(frame_str, str) or not frame_str:
        return ""
    nums = _FRAME_RE.findall(frame_str)
    if len(nums) < 4:
        return ""
    xywh = _finite_ints(nums[0], nums[1], nums[2], nums[3])
    if xywh is None:
        return ""
    x, y, w, h = xywh
    return f"[{x},{y}][{x + w},{y + h}]"


def _parse_rect(rect: dict) -> str:
    """Convert rect dict {x, y, width, height} to bounds string."""
    if not isinstance(rect, dict) or not rect:
        return ""
    xywh = _finite_ints(
        rect.get("x", 0),
        rect.get("y", 0),
        rect.get("width", 0),
        rect.get("height", 0),
    )
    if xywh is None:
        return ""
    x, y, w, h = xywh
    return f"[{x},{y}][{x + w},{y + h}]"


def _node_bounds(node: dict) -> str:
    """Extract bounds string from a node's frame or rect."""
    raw_frame = node.get("frame")
    bounds = _parse_frame(raw_frame) if isinstance(raw_frame, str) else ""
    if not bounds:
        raw_rect = node.get("rect")
        bounds = _parse_rect(raw_rect) if isinstance(raw_rect, dict) else ""
    return bounds


def _parse_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    """Parse '[x1,y1][x2,y2]' into (x1, y1, x2, y2), or None if unparseable."""
    m = _BOUNDS_RE.fullmatch(bounds)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def _extract_viewport(root: dict) -> tuple[int, int] | None:
    """Extract positive viewport (win_w, win_h) from the root node's frame or rect."""
    coords = _parse_bounds(_node_bounds(root))
    if coords is None:
        return None
    x1, y1, x2, y2 = coords
    win_w, win_h = x2 - x1, y2 - y1
    if win_w > 0 and win_h > 0:
        return win_w, win_h
    return None


def _intersects_viewport(bounds: str, win_w: int, win_h: int) -> bool:
    """True when bounds has positive area and intersects (0, 0, win_w, win_h)."""
    coords = _parse_bounds(bounds)
    if coords is None:
        return False
    x1, y1, x2, y2 = coords
    return x2 > x1 and y2 > y1 and x2 > 0 and x1 < win_w and y2 > 0 and y1 < win_h


def _prioritise_viewport(
    candidates: list[ElementNode],
    viewport: tuple[int, int] | None,
) -> list[ElementNode]:
    """Partition viewport-intersecting candidates ahead of off-screen candidates."""
    if viewport is None:
        return candidates
    win_w, win_h = viewport
    onscreen: list[ElementNode] = []
    offscreen: list[ElementNode] = []
    for elem in candidates:
        if _intersects_viewport(elem.bounds, win_w, win_h):
            onscreen.append(elem)
        else:
            offscreen.append(elem)
    return onscreen + offscreen


def _extract_identifier(node: dict) -> str:
    return str(node.get("rawIdentifier") or node.get("name") or "")


def _extract_text(node: dict) -> str:
    label = node.get("label")
    if label:
        return str(label)
    return str(node.get("value") or "")


def _is_clickable(element_type: str, is_enabled: bool) -> bool:
    return element_type in _TAPPABLE_TYPES and is_enabled


def _walk(
    node: dict,
    out: list[ElementNode],
    depth: int,
    max_depth: int,
    max_elements: int,
) -> None:
    """Recursively walk WDA source tree, collecting meaningful elements."""
    if depth > max_depth or len(out) >= max_elements:
        return

    element_type = str(node.get("type", ""))
    identifier = _extract_identifier(node)
    text = _extract_text(node)

    is_enabled_raw = node.get("isEnabled", True)
    is_enabled = is_enabled_raw not in (False, "0", 0)

    clickable = _is_clickable(element_type, is_enabled)
    is_container = element_type in _CONTAINER_TYPES

    has_content = bool(text) or bool(identifier) or clickable

    if has_content and not is_container:
        bounds = _node_bounds(node)
        content_desc = identifier if identifier != text else ""
        out.append(
            ElementNode(
                resource_id=identifier,
                text=text,
                content_desc=content_desc,
                class_name=element_type,
                bounds=bounds,
                clickable=clickable,
                enabled=is_enabled,
            )
        )

    children = node.get("children")
    if not isinstance(children, list):
        return
    for child in children:
        if len(out) >= max_elements:
            break
        if isinstance(child, dict):
            _walk(child, out, depth + 1, max_depth, max_elements)
