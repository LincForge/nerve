"""WDA UI-source tree parsing.

Split out of ``wda.py`` (#53) to keep both files under the hot-file LOC ceiling.
Converts the WDA ``/source`` JSON tree into the flat ElementNode dict list shared
with Android's uiautomator output. Pure functions — no process/HTTP I/O and no
module-globals that the test suite patches — so they move cleanly. ``parse_wda_source``
is re-exported from ``wda`` to preserve ``from linc_nerve.core.wda import parse_wda_source``.
"""

from __future__ import annotations

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

_FRAME_RE = re.compile(r"[\d.]+")  # Extract numbers from "{{x, y}, {w, h}}"


def parse_wda_source(
    tree: dict,
    max_depth: int = 10,
    max_elements: int = 80,
) -> list[dict]:
    """Convert WDA source JSON tree to flat ElementNode dict list.

    Matches the format produced by Android's uiautomator → ElementNode.to_dict().
    """
    out: list[dict] = []
    _walk(tree, out, depth=0, max_depth=max_depth, max_elements=max_elements)
    return out


def _parse_frame(frame_str: str) -> str:
    """Convert WDA frame string '{{x, y}, {w, h}}' to bounds '[x,y][x+w,y+h]'."""
    if not frame_str:
        return ""
    nums = _FRAME_RE.findall(frame_str)
    if len(nums) >= 4:
        x, y, w, h = (
            int(float(nums[0])),
            int(float(nums[1])),
            int(float(nums[2])),
            int(float(nums[3])),
        )
        return f"[{x},{y}][{x + w},{y + h}]"
    # Also handle dict-style rect (some WDA versions)
    return ""


def _parse_rect(rect: dict) -> str:
    """Convert rect dict {x, y, width, height} to bounds string."""
    if not rect:
        return ""
    x = int(rect.get("x", 0))
    y = int(rect.get("y", 0))
    w = int(rect.get("width", 0))
    h = int(rect.get("height", 0))
    return f"[{x},{y}][{x + w},{y + h}]"


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
    out: list[dict],
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
        bounds = _parse_frame(str(node.get("frame", "")))
        if not bounds:
            bounds = _parse_rect(node.get("rect") or {})

        content_desc = identifier if identifier != text else ""
        elem = ElementNode(
            index=len(out),
            resource_id=identifier,
            text=text,
            content_desc=content_desc,
            class_name=element_type,
            bounds=bounds,
            clickable=clickable,
            enabled=is_enabled,
        )
        out.append(elem.to_dict())

    for child in node.get("children", []):
        if len(out) >= max_elements:
            break
        if isinstance(child, dict):
            _walk(child, out, depth + 1, max_depth, max_elements)
