"""Integration tests for nerve_inspect MCP tool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_core():
    with patch("linc_nerve.mcp.diagnostic_tools._core") as core:
        android = MagicMock()
        ios = MagicMock()
        core._ensure_initialized.return_value = (android, ios)
        core._resolve_device.return_value = (MagicMock(), "android")
        core._check_guard.return_value = None
        yield core, android, ios


@pytest.fixture
def mock_core_ios():
    with patch("linc_nerve.mcp.diagnostic_tools._core") as core:
        android = MagicMock()
        ios = MagicMock()
        core._ensure_initialized.return_value = (android, ios)
        core._resolve_device.return_value = (MagicMock(), "ios")
        core._check_guard.return_value = None
        yield core, android, ios


# ---------------------------------------------------------------------------
# Android tests
# ---------------------------------------------------------------------------


def test_nerve_inspect_android_running(mock_core):
    """Full schema: PID found, activity state, meminfo, logs, crashes."""
    from linc_nerve.mcp.diagnostic_tools import _nerve_inspect_sync

    _core, android, _ios = mock_core

    def shell_side_effect(serial, cmd, timeout=10):
        if "pidof" in cmd:
            return "12345"
        if "ps -A" in cmd:
            return ""
        if "dumpsys activity activities" in cmd:
            return "  mResumedActivity: ActivityRecord{abc com.example/.MainActivity}"
        if "dumpsys meminfo" in cmd:
            return (
                "  TOTAL PSS:   12345\n"
                "  Java Heap:   4000\n"
                "  Native Heap: 3000\n"
                "  Graphics:    2000\n"
            )
        if "logcat" in cmd:
            return "\n".join(f"line {i}" for i in range(50))
        return ""

    android._shell.side_effect = shell_side_effect

    result = _nerve_inspect_sync(device="ABC123", package="com.example")

    assert result["device"] == "ABC123"
    assert result["platform"] == "android"
    assert result["package"] == "com.example"
    assert result["process"]["pid"] == 12345
    assert result["process"]["running"] is True
    assert result["activity_state"] == "RESUMED"
    assert result["memory"]["total_pss_kb"] == 12345
    assert result["memory"]["java_heap_kb"] == 4000
    assert isinstance(result["recent_logs"], list)
    assert isinstance(result["crashes"], list)
    assert "timestamp" in result


def test_nerve_inspect_android_not_running(mock_core):
    """Process not running: no PID, no memory, still has logs."""
    from linc_nerve.mcp.diagnostic_tools import _nerve_inspect_sync

    _core, android, _ios = mock_core

    def shell_side_effect(serial, cmd, timeout=10):
        if "pidof" in cmd:
            return ""
        if "ps -A" in cmd:
            return "  PID NAME\n  999 com.other\n"
        if "dumpsys activity activities" in cmd:
            return ""
        if "dumpsys meminfo" in cmd:
            return ""
        return "some log line\n"

    android._shell.side_effect = shell_side_effect
    android.logcat.return_value = "background log line 1\nbackground log line 2\n"

    result = _nerve_inspect_sync(device="ABC123", package="com.example")

    assert result["process"]["pid"] is None
    assert result["process"]["running"] is False
    assert "memory" not in result
    assert isinstance(result["recent_logs"], list)


def test_nerve_inspect_android_unknown_device(mock_core):
    """Unknown device returns error."""
    from linc_nerve.mcp.diagnostic_tools import _nerve_inspect_sync

    core, _android, _ios = mock_core
    core._resolve_device.return_value = (None, "Device 'UNKNOWN' not found")

    result = _nerve_inspect_sync(device="UNKNOWN", package="com.example")

    assert "error" in result
    assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# iOS tests
# ---------------------------------------------------------------------------


def test_nerve_inspect_ios_running(mock_core_ios):
    """iOS simulator: PID found via launchctl, memory via ps, logs via ios ctrl."""
    from linc_nerve.mcp.diagnostic_tools import _nerve_inspect_sync

    _core, _android, ios = mock_core_ios

    launchctl_output = (
        "-\t0\tcom.apple.other\n5678\t0\tcom.example.app\n-\t0\tcom.apple.something\n"
    )

    ps_output = "  8192 16384"

    with patch("linc_nerve.mcp.diagnostic_tools.subprocess") as mock_subprocess:
        # launchctl list
        launchctl_result = MagicMock()
        launchctl_result.stdout = launchctl_output
        # ps for memory
        ps_result = MagicMock()
        ps_result.stdout = ps_output

        mock_subprocess.run.side_effect = [launchctl_result, ps_result]

        ios.get_logs.return_value = "iOS log line 1\niOS log line 2\n"

        result = _nerve_inspect_sync(
            device="AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE", package="com.example.app"
        )

    assert result["platform"] == "ios"
    assert result["process"]["pid"] == 5678
    assert result["process"]["running"] is True
    assert result["memory"]["rss_kb"] == 8192
    assert result["memory"]["vsz_kb"] == 16384
    assert len(result["recent_logs"]) == 2
    assert isinstance(result["crashes"], list)


def test_nerve_inspect_ios_not_running(mock_core_ios):
    """iOS: process not found in launchctl list."""
    from linc_nerve.mcp.diagnostic_tools import _nerve_inspect_sync

    _core, _android, ios = mock_core_ios

    with patch("linc_nerve.mcp.diagnostic_tools.subprocess") as mock_subprocess:
        launchctl_result = MagicMock()
        launchctl_result.stdout = "-\t0\tcom.apple.other\n"
        mock_subprocess.run.return_value = launchctl_result

        ios.get_logs.return_value = ""

        result = _nerve_inspect_sync(
            device="AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE", package="com.example.app"
        )

    assert result["process"]["pid"] is None
    assert result["process"]["running"] is False
    assert "memory" not in result


def test_nerve_inspect_ios_unknown_device(mock_core_ios):
    """iOS unknown device returns error."""
    from linc_nerve.mcp.diagnostic_tools import _nerve_inspect_sync

    core, _android, _ios = mock_core_ios
    core._resolve_device.return_value = (None, "Device 'SIM-MISSING' not found")

    result = _nerve_inspect_sync(device="SIM-MISSING", package="com.example.app")

    assert "error" in result
    assert "not found" in result["error"]


def test_nerve_inspect_ios_physical_pid_via_devicectl(mock_core_ios):
    """Physical iOS: PID detected via devicectl apps+processes JSON."""
    from linc_nerve.mcp.diagnostic_tools import _nerve_inspect_sync

    _core, _android, ios = mock_core_ios

    # Physical device serial (25+ chars with dash = physical)
    physical_serial = "00008110-0000000000000001"

    with patch("linc_nerve.mcp.diagnostic_tools.subprocess") as mock_sub:
        # simctl fails on physical
        simctl_result = MagicMock()
        simctl_result.stdout = ""
        simctl_result.returncode = 1

        mock_sub.run.return_value = simctl_result

        # devicectl calls write JSON to temp files — we need to mock that
        # The implementation will use --json-output <path>, so we patch
        # the file reads instead
        with patch("linc_nerve.mcp.diagnostic_tools._devicectl_pid") as mock_dctl:
            mock_dctl.return_value = 418
            ios.get_logs.return_value = ""

            result = _nerve_inspect_sync(device=physical_serial, package="com.example.app")

    assert result["process"]["running"] is True
    assert result["process"]["pid"] == 418
