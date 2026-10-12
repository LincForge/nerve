"""Tests for the first-class crash-retrieval path — nerve_inspect's crash_signature.

LincForge/linc-nerve#98: on mobile-agent-bench's crash-repro task (b5), agents
driving nerve had to hand-mine raw `nerve_logcat` output to find a crash
signature, while mobile-mcp surfaced it directly. Investigation showed
nerve_inspect ALREADY runs crash detection (inspect_helpers._detect_crashes)
on a device's logcat buffer via the "crashes" field — the gap was not a
missing capability, it was that `_detect_crashes` returns one record per
matched log LINE (the "FATAL EXCEPTION: main" marker and the
"java.lang.XException: ..." message two lines below it land as two separate
records), so a caller still had to read every record's message/context
themselves to answer "what crashed and why". Per the issue's decision gate,
this extends nerve_inspect (not a parallel nerve_crash tool) with
build_crash_signature(), which consolidates the existing detection output
into one named structure. These tests pin its output against literal values.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from linc_nerve.core.inspect_helpers import _detect_crashes, build_crash_signature

# Seeded fixture: a realistic Android FATAL EXCEPTION logcat capture where the
# exception class and app stack frame land on lines AFTER the marker line —
# exactly the shape that defeated naive single-line crash detection.
_SEEDED_CRASH_LOGCAT = [
    "I/System: some normal log",
    "E/AndroidRuntime: FATAL EXCEPTION: main",
    "E/AndroidRuntime: Process: com.example.app, PID: 12345",
    "E/AndroidRuntime: java.lang.IllegalStateException: Something bad happened",
    "E/AndroidRuntime: \tat com.example.app.MainActivity.onCreate(MainActivity.java:42)",
    "E/AndroidRuntime: \tat android.app.ActivityThread.performLaunchActivity",
]

_CLEAN_LOGCAT = [
    "I/System: App started",
    "D/MainActivity: onCreate called",
]


class TestBuildCrashSignatureUnit:
    """Pure-function tests against inspect_helpers.build_crash_signature."""

    def test_names_exception_class_and_package_from_seeded_fixture(self):
        crashes = _detect_crashes(_SEEDED_CRASH_LOGCAT, "android")
        sig = build_crash_signature(crashes, package="com.example.app")

        assert sig["exception_class"] == "java.lang.IllegalStateException"
        assert sig["package"] == "com.example.app"
        assert "com.example.app.MainActivity.onCreate" in sig["first_app_frame"]

    def test_no_crash_found_is_explicit_literal_status(self):
        crashes = _detect_crashes(_CLEAN_LOGCAT, "android")
        sig = build_crash_signature(crashes, package="com.example.app")

        assert sig == {"status": "no_crash_found"}

    def test_empty_crashes_list_is_no_crash_found(self):
        assert build_crash_signature([], package="com.example.app") == {"status": "no_crash_found"}

    def test_falls_back_to_pattern_type_when_no_exception_class_present(self):
        # ANR has no "...Exception" token anywhere in the line — exception_class
        # must fall back to the raw _detect_crashes type name, not crash.
        lines = [
            "I/ActivityManager: ANR in com.example.app (com.example.app/.MainActivity)",
            "I/ActivityManager: Reason: Input dispatching timed out",
        ]
        crashes = _detect_crashes(lines, "android")
        sig = build_crash_signature(crashes, package="com.example.app")

        assert sig["exception_class"] == "ANR"
        assert sig["package"] == "com.example.app"

    def test_first_app_frame_falls_back_when_no_frame_names_package(self):
        lines = [
            "E/AndroidRuntime: FATAL EXCEPTION: main",
            "E/AndroidRuntime: java.lang.RuntimeException: framework failure",
            "E/AndroidRuntime: \tat android.app.ActivityThread.performLaunchActivity",
        ]
        crashes = _detect_crashes(lines, "android")
        sig = build_crash_signature(crashes, package="com.example.app")

        assert sig["exception_class"] == "java.lang.RuntimeException"
        # No frame mentions com.example.app, so the first frame at all is used.
        assert "android.app.ActivityThread.performLaunchActivity" in sig["first_app_frame"]


class TestNerveInspectCrashSignature:
    """Integration: crash_signature is wired into the nerve_inspect MCP tool."""

    def test_nerve_inspect_returns_crash_signature_for_seeded_crash(self):
        from linc_nerve.mcp.diagnostic_tools import _nerve_inspect_sync

        with patch("linc_nerve.mcp.diagnostic_tools._core") as core:
            android = MagicMock()
            ios = MagicMock()
            core._ensure_initialized.return_value = (android, ios)
            core._resolve_device.return_value = (MagicMock(), "android")
            core._check_guard.return_value = None

            def shell_side_effect(serial, cmd, timeout=10):
                if "pidof" in cmd:
                    return "12345"
                if "logcat" in cmd:
                    return "\n".join(_SEEDED_CRASH_LOGCAT)
                return ""

            android._shell.side_effect = shell_side_effect

            result = _nerve_inspect_sync(device="ABC123", package="com.example.app")

        assert result["crash_signature"]["exception_class"] == ("java.lang.IllegalStateException")
        assert result["crash_signature"]["package"] == "com.example.app"
        assert (
            "com.example.app.MainActivity.onCreate"
            in (result["crash_signature"]["first_app_frame"])
        )

    def test_nerve_inspect_no_crash_found_literal(self):
        from linc_nerve.mcp.diagnostic_tools import _nerve_inspect_sync

        with patch("linc_nerve.mcp.diagnostic_tools._core") as core:
            android = MagicMock()
            ios = MagicMock()
            core._ensure_initialized.return_value = (android, ios)
            core._resolve_device.return_value = (MagicMock(), "android")
            core._check_guard.return_value = None

            def shell_side_effect(serial, cmd, timeout=10):
                if "pidof" in cmd:
                    return "12345"
                if "logcat" in cmd:
                    return "\n".join(_CLEAN_LOGCAT)
                return ""

            android._shell.side_effect = shell_side_effect

            result = _nerve_inspect_sync(device="ABC123", package="com.example.app")

        assert result["crash_signature"] == {"status": "no_crash_found"}
