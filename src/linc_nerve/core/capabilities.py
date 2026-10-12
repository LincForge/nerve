"""Platform-support capability matrix — the agent-facing capability surface.

Declares, per platform, which major capability groups linc-nerve actually
supports, including explicit ``not_supported`` entries for known gaps
(capability honesty — issue #81):

- motion/sensor injection on iOS (Android emulator console only)
- watchOS WDA (no UI automation channel on Apple Watch)
- physical-iOS cold_start (no clean wipe primitive; ios-sim cold_start IS
  supported — uninstall-based ``clear_app_data`` with the needs_reinstall
  contract; ``grant_permission`` stays Android-only)
- iOS physical-device MJPEG/video streaming (WDA MJPEG is simulator-only;
  scrcpy is Android-only)

Each group lists the MCP tools that provide it. Tests assert every listed
tool is a real registration (``linc_nerve.mcp._core.TOOL_REGISTRY``), so this
matrix cannot silently reference tools that don't exist.

This module mirrors the shape of ``IDeviceIO.capabilities()`` from
linc-device-kit (a per-adapter capability set) but at the fleet/platform
altitude an agent needs before picking a device.
"""

from __future__ import annotations

from typing import Any

PLATFORMS: tuple[str, ...] = (
    "android",
    "android-watch",
    "ios-sim",
    "ios-physical",
    "watchos",
)

STATUS_SUPPORTED = "supported"
STATUS_PARTIAL = "partial"
STATUS_NOT_SUPPORTED = "not_supported"


def _support(
    overrides: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    """Build a per-platform support map, defaulting to supported."""
    result: dict[str, dict[str, str]] = {
        platform: {"status": STATUS_SUPPORTED} for platform in PLATFORMS
    }
    if overrides:
        result.update(overrides)
    return result


def _ok(note: str) -> dict[str, str]:
    return {"status": STATUS_SUPPORTED, "note": note}


def _no(note: str) -> dict[str, str]:
    return {"status": STATUS_NOT_SUPPORTED, "note": note}


def _partial(note: str) -> dict[str, str]:
    return {"status": STATUS_PARTIAL, "note": note}


_WATCHOS_WDA = "watchOS has no WDA — no UI automation channel on Apple Watch"

# The matrix. Keys are capability groups; each entry lists the provider tools
# and the per-platform support status. Non-supported entries carry a note.
CAPABILITY_MATRIX: dict[str, dict[str, Any]] = {
    "discovery": {
        "tools": ["nerve_discover", "nerve_pool", "nerve_ensure_ready"],
        "support": _support(
            {
                "watchos": _partial(
                    "Apple Watch is only visible via its paired iPhone (devicectl); "
                    "no direct control channel"
                ),
            }
        ),
    },
    "screenshot": {
        "tools": ["nerve_screenshot", "nerve_navigate_screenshot"],
        "support": _support(
            {
                "watchos": _no(_WATCHOS_WDA),
            }
        ),
    },
    "video_stream": {
        "tools": [
            "nerve_stream_start",
            "nerve_stream_stop",
            "nerve_stream_status",
            "nerve_stream_screenshot",
        ],
        "support": _support(
            {
                "android": _ok("scrcpy H264"),
                "android-watch": _partial("scrcpy on Wear OS: works but low fps"),
                "ios-sim": _partial("WDA MJPEG stream, not scrcpy"),
                "ios-physical": _no(
                    "iOS physical MJPEG is not supported — WDA MJPEG is "
                    "simulator-only; scrcpy is Android-only"
                ),
                "watchos": _no(_WATCHOS_WDA),
            }
        ),
    },
    "ui_elements": {
        "tools": ["nerve_elements", "nerve_state", "nerve_diff", "nerve_density"],
        "support": _support(
            {
                "ios-sim": _partial("via WDA (requires WDA_PROJECT_DIR)"),
                "ios-physical": _partial("via WDA (requires WDA_TEAM_ID code signing)"),
                "watchos": _no(_WATCHOS_WDA),
            }
        ),
    },
    "input": {
        "tools": [
            "nerve_tap",
            "nerve_long_press",
            "nerve_swipe",
            "nerve_type",
            "nerve_key",
        ],
        "support": _support(
            {
                "ios-sim": _partial("via WDA (requires WDA_PROJECT_DIR)"),
                "ios-physical": _partial("via WDA (requires WDA_TEAM_ID code signing)"),
                "watchos": _no(_WATCHOS_WDA),
            }
        ),
    },
    "deep_link_navigation": {
        "tools": ["nerve_navigate", "nerve_navigate_screenshot", "nerve_launch"],
        "support": _support(
            {
                "watchos": _no("no direct launch/deep-link channel on Apple Watch"),
            }
        ),
    },
    "app_management": {
        "tools": [
            "nerve_install",
            "nerve_launch",
            "nerve_kill",
            "nerve_apps",
            "nerve_uninstall",
        ],
        "support": _support(
            {
                "watchos": _no("no direct install/launch channel — deploy via the paired iPhone"),
            }
        ),
    },
    "cold_start": {
        "tools": ["nerve_clear_app_data", "nerve_grant_permission"],
        "support": _support(
            {
                "android": _ok("pm clear / pm grant"),
                "android-watch": _ok("pm clear / pm grant"),
                "ios-sim": _ok(
                    "uninstall-based: clear_app_data runs simctl uninstall and "
                    "returns needs_reinstall=true — reinstall the app before the "
                    "next launch; grant_permission stays Android-only"
                ),
                "ios-physical": _no(
                    "iOS cold_start is not supported on physical devices — no "
                    "clean wipe primitive; use a simulator"
                ),
                "watchos": _no("no package-manager channel on Apple Watch"),
            }
        ),
    },
    "sensor_injection": {
        "tools": ["nerve_inject_sensor"],
        "support": _support(
            {
                "android": _partial("Android emulator console only — no physical devices"),
                "android-watch": _partial("Wear OS emulator console only"),
                "ios-sim": _no(
                    "motion/sensor injection on iOS is not supported — "
                    "simctl has no accelerometer/gyro injection"
                ),
                "ios-physical": _no("motion/sensor injection on iOS is not supported"),
                "watchos": _no("motion/sensor injection on watchOS is not supported"),
            }
        ),
    },
    "screen_recording": {
        "tools": ["nerve_record_start", "nerve_record_stop", "nerve_record_fixed"],
        "support": _support(
            {
                "ios-sim": _ok("simctl io recordVideo"),
                "ios-physical": _no(
                    "recording uses simctl io recordVideo, which is simulator-only"
                ),
                "watchos": _no("no recording channel on Apple Watch"),
            }
        ),
    },
    "logs": {
        "tools": ["nerve_logcat", "nerve_stream_logs", "nerve_inspect"],
        "support": _support(
            {
                "watchos": _no("no direct log channel — logs surface via the paired iPhone"),
            }
        ),
    },
    "emulator_control": {
        "tools": [
            "nerve_emulator_list",
            "nerve_emulator_start",
            "nerve_emulator_stop",
        ],
        "support": _support(
            {
                "ios-physical": _no("physical devices cannot be booted/stopped remotely"),
                "watchos": _no("watchOS simulators are not managed by nerve"),
            }
        ),
    },
    "device_reservation": {
        "tools": ["nerve_reserve", "nerve_release", "nerve_pool"],
        "support": _support(),
    },
    "diagnostics": {
        "tools": ["nerve_metrics", "nerve_session_stats", "nerve_usage_stats"],
        "support": _support(),
    },
}


def get_capabilities() -> dict[str, Any]:
    """Return the full capability payload for the nerve_capabilities tool.

    ``tool_count`` is derived from the registration decorators (the single
    source), never hand-counted.
    """
    # Imported here to avoid a core → mcp import cycle at module load.
    from linc_nerve.mcp import _core

    return {
        "platforms": list(PLATFORMS),
        "tool_count": len(_core.TOOL_REGISTRY),
        "capabilities": CAPABILITY_MATRIX,
    }
