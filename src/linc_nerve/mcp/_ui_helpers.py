"""Internal UI hierarchy helpers for ``ui_tools.py``."""

from __future__ import annotations

import re
from typing import Any


def _scroll_and_merge(
    ctrl: Any,
    device: str,
    elements: list[dict[str, Any]],
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
    merged: list[dict[str, Any]] = []
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
