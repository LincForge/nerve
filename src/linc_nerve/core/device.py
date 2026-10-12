"""Device data models for linc-nerve."""

from __future__ import annotations

from dataclasses import dataclass, field

# Canonical enums + shared failure base from device-kit — re-exported for backward compat
from linc_device_kit.exceptions import DeviceKitError
from linc_device_kit.models import DeviceType, Platform

__all__ = [
    "DeviceError",
    "DeviceType",
    "ElementNode",
    "NerveDevice",
    "Platform",
    "UiHierarchyUnavailableError",
]


# nerve's device-error base IS device-kit's shared base (alias, not subclass).
# Aliasing gives cross-repo catch symmetry: a DeviceKitError raised by another
# consumer (e.g. crucible) is caught by nerve's `except DeviceError`, and vice
# versa. A subclass would split type identity across the stack. The richer leaf
# taxonomy (BuildFailed/InstallFailed/...) stays a documented extension seam in
# device-kit, deferred until a caller actually discriminates by failure type —
# see docs spec 2026-06-30-device-kit-taxonomy-adopt-design.md.
DeviceError = DeviceKitError


class UiHierarchyUnavailableError(DeviceError):
    """The UI element tree could not be dumped — NOT an empty screen.

    Raised when `uiautomator dump` fails (e.g. SIGKILLed because another
    process — a leaked instrumentation driver — holds the device's single
    UiAutomation connection). Callers must surface this as an error, never
    as `element_count: 0`; a benchmark run showed the silent-empty fallback
    forced agents into screenshot-only vision (reactor 8bba730a/280bbee7).
    """


@dataclass
class NerveDevice:
    """A device known to Nerve."""

    serial: str
    platform: Platform
    device_type: DeviceType
    model: str = ""
    os_version: str = ""
    state: str = "online"

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "serial": self.serial,
            "platform": self.platform.value,
            "device_type": self.device_type.value,
            "model": self.model,
            "os_version": self.os_version,
            "state": self.state,
        }


@dataclass
class ElementNode:
    """A UI element from the view hierarchy."""

    index: int = 0
    resource_id: str = ""
    text: str = ""
    content_desc: str = ""
    class_name: str = ""
    bounds: str = ""
    clickable: bool = False
    enabled: bool = True
    checked: bool = False
    focused: bool = False
    scrollable: bool = False
    children: list[ElementNode] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to compact dictionary (omit falsy fields)."""
        d: dict = {}
        if self.resource_id:
            d["id"] = self.resource_id
        if self.text:
            d["text"] = self.text
        if self.content_desc:
            d["desc"] = self.content_desc
        if self.class_name:
            d["class"] = self.class_name
        if self.bounds:
            d["bounds"] = self.bounds
        if self.clickable:
            d["clickable"] = True
        if self.checked:
            d["checked"] = True
        if self.focused:
            d["focused"] = True
        if self.scrollable:
            d["scrollable"] = True
        if not self.enabled:
            d["enabled"] = False
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d
