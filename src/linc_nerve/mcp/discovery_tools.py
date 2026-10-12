"""Discovery and pool status tools."""

from __future__ import annotations

from typing import Any

from linc_nerve.core.contracts import require
from linc_nerve.mcp import _core
from linc_nerve.mcp._core import global_tool


@global_tool
def _nerve_discover_sync() -> dict[str, Any]:
    """List all connected devices (Android + iOS).

    USE FOR: Finding available devices, checking device state, getting serials.
    NOT FOR: Deploying apps (use crucible_pour), debugging (use spectra).
    TIP: Call this first to get device serials for other nerve tools.
    """
    _core._ensure_initialized()
    all_devices = _core._refresh_cache()
    devices: list[dict] = [d.to_dict() for d in all_devices]
    return {
        "devices": devices,
        "count": len(devices),
        "android": sum(1 for d in devices if d["platform"] == "android"),
        "ios": sum(1 for d in devices if d["platform"] == "ios"),
    }


@global_tool
def _nerve_capabilities_sync() -> dict[str, Any]:
    """Per-platform support matrix for nerve's capability groups.

    USE FOR: Checking whether a capability (input, streaming, sensor injection,
    cold_start, ...) is supported on a platform BEFORE picking a device;
    discovering known gaps (explicit not_supported entries with notes).
    NOT FOR: Live device state (use nerve_discover / nerve_state).
    TIP: tool_count and the matrix are derived from the tool registry —
    always current, never hand-counted.
    """
    from linc_nerve.core.capabilities import get_capabilities

    return get_capabilities()


@global_tool
def _nerve_pool_sync() -> dict[str, Any]:
    """Show device pool status — who has what reserved.

    USE FOR: Checking device availability, debugging reservation conflicts.
    NOT FOR: Device discovery (use nerve_discover for device details).
    TIP: Shows all active reservations with session IDs, timestamps, and purposes.
    """
    _core._ensure_initialized()
    device_mgr = require(_core._device_mgr, "device manager")

    reservations = device_mgr.pool()
    return {
        "reservations": reservations,
        "count": len(reservations),
        "session_id": device_mgr.session_id,
    }
