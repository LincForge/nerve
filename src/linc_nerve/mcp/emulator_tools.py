"""Emulator lifecycle tools."""

from __future__ import annotations

from typing import Any

from linc_nerve.core.adb_path import describe_emulator_probe_locations, resolve_emulator_path
from linc_nerve.core.contracts import require
from linc_nerve.core.emulator_console import validate_sensor_type
from linc_nerve.mcp import _core
from linc_nerve.mcp._core import device_tool, global_tool


def _android_sdk_status_fields() -> dict[str, Any]:
    """Build the ``android_sdk_resolved``/``android_warning`` fields for
    ``nerve_emulator_list`` (linc-nerve#118) — distinguishes "no Android AVDs
    exist" from "the emulator binary couldn't be resolved so we never even
    asked" (reactor 8d140900). A caller must never read an unresolved SDK as a
    bare iOS-only success.

    Extracted from ``_nerve_emulator_list_sync`` so that function's own
    complexity stays at its pre-fix level.
    """
    emulator_path = resolve_emulator_path()
    fields: dict[str, Any] = {"android_sdk_resolved": emulator_path is not None}
    if emulator_path is None:
        probed = ", ".join(describe_emulator_probe_locations())
        fields["android_warning"] = (
            f"emulator binary not found on PATH or at any known SDK location "
            f"(probed: {probed}) — Android AVDs cannot be listed or started "
            f"until ANDROID_SDK_ROOT/ANDROID_HOME is set or the binary is "
            f"installed at one of the probed locations."
        )
    return fields


@global_tool
def _nerve_emulator_list_sync() -> dict[str, Any]:
    """List available Android emulators (AVDs) and iOS Simulators, and their running state.

    USE FOR: Finding which emulators/simulators are available, checking which are running.
    NOT FOR: Real device discovery (use nerve_discover).
    TIP: Shows both installed AVDs/Simulators and their running status. Use nerve_emulator_start
    to boot a stopped emulator.
    """
    _core._ensure_initialized()
    emulator = require(_core._emulator, "emulator manager")
    ios = require(_core._ios, "iOS controller")

    try:
        # Android AVDs
        avds = emulator.list_avds()
        virtual_devices = []
        for a in avds:
            virtual_devices.append(
                {
                    "name": a.avd_name,
                    "id": a.serial or a.avd_name,
                    "platform": "android",
                    # Android avd list doesn't easily expose form factor
                    "device_type": "emulator",
                    "state": a.state,
                }
            )

        # iOS Simulators
        simulators = ios._discover_simulators()
        for sim in simulators:
            virtual_devices.append(
                {
                    "name": sim.model,
                    "id": sim.serial,
                    "platform": "ios",
                    "device_type": sim.device_type.value,
                    "state": "running" if sim.state == "online" else "stopped",
                }
            )

        response: dict[str, Any] = {
            "virtual_devices": virtual_devices,
            "count": len(virtual_devices),
            "running": sum(1 for d in virtual_devices if d["state"] == "running"),
            "stopped": sum(1 for d in virtual_devices if d["state"] == "stopped"),
        }

        response.update(_android_sdk_status_fields())

        return response
    except Exception as exc:
        return {"error": f"Failed to list virtual devices: {exc}"}


@global_tool
def _nerve_emulator_start_sync(
    name_or_id: str, snapshot: str = "", headless: bool = False, timeout: int = 120
) -> dict[str, Any]:
    """Start an Android emulator (by AVD name) or iOS Simulator (by UDID/Name).

    USE FOR: Spinning up an emulator/simulator for testing or development.
    NOT FOR: Starting real devices (they must be physically connected).
    TIP: For Android, pass AVD name. For iOS, pass the Simulator UDID or exact Name.
    The emulator will appear as 'online' in nerve_discover once booted.

    Args:
        name_or_id: AVD name (Android) or UDID/Name (iOS).
        snapshot: Optional snapshot name for fast boot (Android only).
        headless: Run without display window (Android only).
        timeout: Boot timeout in seconds (default 120).
    """
    _core._ensure_initialized()
    emulator = require(_core._emulator, "emulator manager")
    ios = require(_core._ios, "iOS controller")

    # Check if it matches an iOS simulator (by UDID or exact Name)
    simulators = ios._discover_simulators()
    ios_sim = next(
        (s for s in simulators if s.serial == name_or_id or s.model == name_or_id), None
    )

    if ios_sim:
        try:
            ios.boot(ios_sim.serial)
            _core._invalidate_cache()
            return {
                "success": True,
                "emulator": {"id": ios_sim.serial, "name": ios_sim.model, "platform": "ios"},
            }
        except Exception as exc:
            return {"error": f"iOS Simulator boot failed: {exc}"}

    # Fallback to Android AVD
    try:
        info = emulator.start(
            avd_name=name_or_id,
            snapshot=snapshot or None,
            headless=headless,
            timeout=timeout,
        )
        _core._invalidate_cache()  # New emulator should appear in device list
        return {
            "success": True,
            "emulator": info.to_dict(),
        }
    except ValueError as exc:
        return {"error": str(exc)}
    except TimeoutError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Emulator start failed: {exc}"}


@device_tool
def _nerve_emulator_stop_sync(device: str) -> dict[str, Any]:
    """Stop a running Android emulator or iOS Simulator.

    USE FOR: Shutting down emulators/simulators to free resources.
    NOT FOR: Stopping real devices or killing apps (use nerve_kill).

    Args:
        device: Emulator serial (e.g., "emulator-5554") or iOS Simulator UDID.
    """
    _core._ensure_initialized()
    emulator = require(_core._emulator, "emulator manager")
    ios = require(_core._ios, "iOS controller")

    if device.startswith("emulator-"):
        # Android path
        try:
            stopped = emulator.stop(device)
            if stopped:
                _core._invalidate_cache()  # Emulator gone from device list
                return {"success": True, "device": device, "action": "stopped"}
            return {"error": f"Failed to stop Android emulator {device} within timeout."}
        except Exception as exc:
            return {"error": f"Emulator stop failed: {exc}"}

    # Verify it matches an iOS simulator
    simulators = ios._discover_simulators()
    ios_sim = next((s for s in simulators if s.serial == device or s.model == device), None)

    if ios_sim:
        try:
            ios.shutdown(ios_sim.serial)
            _core._invalidate_cache()
            return {"success": True, "device": ios_sim.serial, "action": "stopped"}
        except Exception as exc:
            return {"error": f"iOS Simulator shutdown failed: {exc}"}

    return {
        "error": f"'{device}' is not an Android emulator serial or iOS Simulator UDID. "
        "Emulator serials start with 'emulator-' (e.g., emulator-5554)."
    }


@device_tool
def _nerve_inject_sensor_sync(
    device: str,
    sensor_type: str = "acceleration",
    x: float = 0.0,
    y: float = 0.0,
    z: float = 9.81,
) -> dict[str, Any]:
    """Inject sensor data into an Android emulator via its telnet console.

    USE FOR: Simulating accelerometer, gyroscope, or magnetic-field readings on emulators.
    NOT FOR: Physical devices or iOS simulators (emulators only).
    TIP: Default values simulate a phone face-up (gravity on Z axis). Common sensor
    types: acceleration, gyroscope, magnetic-field, orientation, temperature, proximity.

    Args:
        device: Emulator serial (e.g., "emulator-5554").
        sensor_type: Sensor name (default "acceleration").
        x: X-axis value (default 0.0).
        y: Y-axis value (default 0.0).
        z: Z-axis value (default 9.81 for gravity).
    """
    validate_sensor_type(sensor_type)  # security review M1 — before any device I/O
    _core._ensure_initialized()
    android = require(_core._android, "Android controller")

    if not device.startswith("emulator-"):
        return {
            "error": f"'{device}' is not an emulator. "
            "nerve_inject_sensor only works on Android emulators "
            "(serials starting with 'emulator-')."
        }

    return android.inject_sensor(
        serial=device,
        sensor_type=sensor_type,
        x=x,
        y=y,
        z=z,
    )
