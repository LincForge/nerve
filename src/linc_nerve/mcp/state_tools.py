"""nerve_ensure_ready — device-readiness contract MCP tool — and nerve_network."""

from __future__ import annotations

from typing import Any

from linc_nerve.core.device_state import (
    NetworkInfo,
    apply_network_requirement,
    unknown_network,
)
from linc_nerve.mcp import _core
from linc_nerve.mcp._core import device_tool


@device_tool
def _nerve_ensure_ready_sync(device: str = "", require_network: bool = False) -> dict[str, Any]:
    """Ensure a device is awake + unlocked before UI interaction, and report readiness.

    Wakes an asleep/dozing Android device (KEYCODE_WAKEUP), dismisses a non-secure
    keyguard (wm dismiss-keyguard), extends the screen timeout, then returns the gate
    verdict. Callers SHOULD skip a run when ``ready`` is false rather than produce
    corrupt artifacts (lock-screen screenshots, no-op taps, 0-byte recordings).

    USE FOR: pre-flighting a device before a BVT/acceptance run; recovering an asleep
        bench device; deciding whether to SKIP a device in an unattended sweep.
    NOT FOR: unlocking a PIN-secured device (never attempted — provision bench devices
        lock-free instead), or iOS physical devices (report-only, can't unlock).
    TIP: ``ready``/``reason`` encode the policy; just check ``ready`` and log ``reason``.
        Returns ~60-120 tokens.

    Args:
        device: Device serial (optional when the caller holds a single reservation).
        require_network: Also gate ``ready`` on a default network with validated
            internet (Android only). Off by default: ``ready``/``reason`` are then
            unchanged and ``network`` is report-only. When on and the device is
            otherwise ready, offline -> reason ``no_network``, unreadable ->
            ``network_unknown``. Ignored on iOS. nerve never joins networks.

    Returns:
        {awake, unlocked, battery_pct, charging, ready, reason, actions_taken, platform,
        network}. ``network`` is {transport, default_network, validated, reachable,
        online} (no ping here; reachable stays null). iOS physical also carries
        report_only=True with null state fields; on iOS ``network`` is all-unknown
        with note "not checked on iOS" and require_network is ignored.
    """
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    if platform == "ios":
        return {**_ios_verdict(device), "network": _ios_network()}

    guard = _core._check_guard(device)
    if guard:
        return guard
    ctrl = _core._get_controller(platform)
    try:
        verdict = ctrl.ensure_ready(device)
    except Exception as exc:
        return {"error": f"ensure_ready failed: {exc}"}
    return _attach_network(ctrl, device, verdict, require_network)


_IOS_NOTE = "not checked on iOS"


def _ios_network() -> dict[str, Any]:
    return {**unknown_network().as_dict(), "note": _IOS_NOTE}


def _ios_verdict(device: str) -> dict[str, Any]:
    """iOS readiness — report-only on physical devices, always ready on simulators."""
    _, ios = _core._ensure_initialized()
    if ios._is_physical(device):
        return {
            "ready": True,
            "report_only": True,
            "reason": "ios_physical_state_not_detectable",
            "platform": "ios",
            "awake": None,
            "unlocked": None,
            "battery_pct": None,
            "charging": None,
            "actions_taken": [],
        }
    return {
        "ready": True,
        "reason": "simulator (no lock)",
        "platform": "ios",
        "awake": True,
        "unlocked": True,
        "battery_pct": None,
        "charging": True,
        "actions_taken": [],
    }


def _read_network(ctrl: Any, device: str, probe: bool = False) -> NetworkInfo:
    """``ctrl.network_info`` behind a never-raises seam (anything odd -> all-unknown)."""
    try:
        info = ctrl.network_info(device, probe=probe)
    except Exception:
        return unknown_network()
    return info if isinstance(info, NetworkInfo) else unknown_network()


def _attach_network(
    ctrl: Any, device: str, verdict: dict[str, Any], require: bool
) -> dict[str, Any]:
    """Add ``network`` to an Android verdict; no read on an error or adb_unauthorized."""
    if "error" in verdict:
        return verdict
    if verdict.get("reason") == "adb_unauthorized":
        network = unknown_network()
    else:
        network = _read_network(ctrl, device)
    return apply_network_requirement(verdict, network, require)


@device_tool
def _nerve_network_sync(device: str, probe: bool = False) -> dict[str, Any]:
    """Report whether a device is online — default network transport + validated internet.

    Read-only: one ``dumpsys connectivity`` read (plus one ICMP ping with probe=True).
    No side effects — it does NOT wake, unlock or touch the device's settings.

    USE FOR: checking a device is online before or during a run; deciding to stop a
        case whose device lost network mid-run.
    NOT FOR: joining or configuring a network — nerve never touches Wi-Fi settings or
        credentials. An offline device is an operator problem: stop and report it.
        Waking/unlocking (use nerve_ensure_ready, which also reports ``network``).
    TIP: ``online`` false means stop; null means the state could not be read.
        ``reachable`` is only set with probe=True and can be false on networks that
        drop ICMP even when ``validated`` is true. Android only; iOS returns
        all-unknown with a note. Returns ~40 tokens.

    Args:
        device: Device serial.
        probe: Also send one ping (8.8.8.8, 3 s cap) to set ``reachable``; only runs
            when the device has a default network.

    Returns:
        {device, platform, transport, default_network, validated, reachable, online};
        iOS adds note "not checked on iOS"; {"error": ...} for an unknown device.
    """
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}
    if platform == "ios":
        return {"device": device, "platform": "ios", **_ios_network()}
    ctrl = _core._get_controller(platform)
    network = _read_network(ctrl, device, probe=probe)
    return {"device": device, "platform": platform, **network.as_dict()}
