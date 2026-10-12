"""Device polling loop for the nerve daemon."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger().bind(domain="daemon_polling")


@dataclass
class DeviceDiff:
    """Result of one polling cycle.

    ``raw_devices`` holds the device objects each controller's ``discover()``
    returned this tick, keyed by platform ("android"/"ios"), so callers (e.g.
    the WDA reconcile in ``_poll_cycle``) can reuse them without a second
    discovery pass. A platform whose discovery raised is ABSENT from the dict
    — distinct from a successful empty discovery (``[]``).
    """

    connected: dict[str, dict[str, Any]] = field(default_factory=dict)
    disconnected: set[str] = field(default_factory=set)
    all_serials: set[str] = field(default_factory=set)
    raw_devices: dict[str, list[Any]] = field(default_factory=dict)


def poll_devices(
    android_ctrl: Any,
    ios_ctrl: Any,
    known_serials: set[str],
) -> DeviceDiff:
    """Poll connected devices and return a diff against known_serials.

    Args:
        android_ctrl: AndroidController instance (or mock).
        ios_ctrl: IOSController instance (or mock).
        known_serials: Set of serials seen in the previous polling cycle.

    Returns:
        DeviceDiff with connected (new), disconnected (gone), all_serials, and
        raw_devices (per-platform discover() results for caller reuse).
    """
    current: dict[str, dict[str, Any]] = {}
    raw_devices: dict[str, list[Any]] = {}

    for ctrl, platform in [(android_ctrl, "android"), (ios_ctrl, "ios")]:
        try:
            devices = list(ctrl.discover())
            for d in devices:
                current[d.serial] = {
                    "serial": d.serial,
                    "model": d.model,
                    "platform": (
                        d.platform.value if hasattr(d.platform, "value") else platform
                    ),
                    "device_type": (
                        d.device_type.value
                        if hasattr(d.device_type, "value")
                        else "unknown"
                    ),
                    "os_version": d.os_version,
                }
            raw_devices[platform] = devices
        except Exception as e:
            log.debug("poll_discover_error", platform=platform, error=str(e))

    current_serials = set(current.keys())
    connected = {s: current[s] for s in current_serials - known_serials}
    disconnected = known_serials - current_serials

    return DeviceDiff(
        connected=connected,
        disconnected=disconnected,
        all_serials=current_serials,
        raw_devices=raw_devices,
    )
