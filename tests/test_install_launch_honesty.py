"""Tests for nerve install path validation and launch of uninstalled packages.

Contract:
- nerve_install rejects relative APK paths without calling controller.install.
- nerve_install names missing file on daemon host when controller.install raises.
- AndroidController.launch raises RuntimeError when package is not installed
  (monkey fallback aborts).
- nerve_launch maps that RuntimeError to an error response.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.mcp import server as mcp_mod
from linc_nerve.mcp.app_tools import (
    _install_checked,
    _install_failure,
    _install_path_error,
    _nerve_install_sync,
    _nerve_launch_sync,
)


def _reset_singletons() -> None:
    if mcp_mod._device_mgr is not None:
        mcp_mod._device_mgr.close()
    mcp_mod._android = None
    mcp_mod._ios = None
    mcp_mod._state_mgr = None
    mcp_mod._metrics = None
    mcp_mod._device_mgr = None
    mcp_mod._emulator = None
    mcp_mod._cached_devices = []
    mcp_mod._cache_timestamp = 0.0
    mcp_mod._cleanup_done = False


@pytest.fixture(autouse=True)
def reset_singletons() -> Iterator[None]:
    """Reset the server singletons before AND after each test.

    Resetting only before let the last test's mocked ``_device_mgr`` (whose
    ``guard`` refuses every serial) leak into whichever module ran next in the
    same process — mutmut's clean run then 409'd ``test_http_router``.
    """
    _reset_singletons()
    yield
    _reset_singletons()


def _phone(serial: str = "ABC") -> NerveDevice:
    return NerveDevice(
        serial=serial,
        platform=Platform.ANDROID,
        device_type=DeviceType.PHONE,
        model="Test Phone",
        os_version="14",
    )


def test_install_rejects_relative_path() -> None:
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = [_phone("ABC")]
    mcp_mod._android = mock_android
    mcp_mod._ios = MagicMock()
    mcp_mod._ios.discover.return_value = []

    result = _nerve_install_sync("ABC", "build/app.apk")

    assert "error" in result
    assert "absolute" in result["error"]
    assert os.path.abspath("build/app.apk") in result["error"]
    mock_android.install.assert_not_called()


def test_install_names_missing_file_on_failure() -> None:
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = [_phone("ABC")]
    mock_android.install.side_effect = RuntimeError("APK file error")
    mcp_mod._android = mock_android
    mcp_mod._ios = MagicMock()
    mcp_mod._ios.discover.return_value = []

    result = _nerve_install_sync("ABC", "/nonexistent/x.apk")

    assert "error" in result
    assert result["error"].startswith("Install failed")
    assert "does not exist" in result["error"]


def test_android_controller_launch_missing_package_raises() -> None:
    ctrl = AndroidController()
    mock_device = MagicMock()
    mock_client = MagicMock()
    mock_client.device.return_value = mock_device
    ctrl._client = mock_client

    mock_device.shell.side_effect = [
        "nosdcard,default\n",
        "Error: No activity found\n",
        "** No activities found to run, monkey aborted.\n",
    ]

    with pytest.raises(RuntimeError) as exc_info:
        ctrl.launch("ABC", "com.missing")

    assert "not installed" in str(exc_info.value)


def test_nerve_launch_sync_missing_package_returns_error() -> None:
    ctrl = AndroidController()
    mock_device = MagicMock()
    mock_client = MagicMock()
    mock_client.device.return_value = mock_device
    ctrl._client = mock_client

    mock_device.shell.side_effect = [
        "nosdcard,default\n",
        "Error: No activity found\n",
        "** No activities found to run, monkey aborted.\n",
    ]

    ctrl.discover = MagicMock(return_value=[_phone("ABC")])  # type: ignore[method-assign]
    mcp_mod._android = ctrl
    mcp_mod._ios = MagicMock()
    mcp_mod._ios.discover.return_value = []

    result = _nerve_launch_sync("ABC", "com.missing")

    assert "error" in result
    assert result["error"].startswith("Launch failed:")
    assert "not installed" in result["error"]


# ---------------------------------------------------------------------------
# Helper coverage — the branching lives in small helpers so _nerve_install_sync
# keeps its baseline CRAP score.
# ---------------------------------------------------------------------------


def test_install_path_error_accepts_absolute_path() -> None:
    assert _install_path_error("/abs/app.apk") is None


def test_install_path_error_rejects_relative_path() -> None:
    result = _install_path_error("app.apk")

    assert result is not None
    assert result["error"].startswith("Install failed: path must be absolute")
    assert os.path.abspath("app.apk") in result["error"]
    assert '"$(pwd)/app.apk"' in result["error"]


def test_install_failure_names_missing_path() -> None:
    result = _install_failure("/nonexistent/x.apk", RuntimeError("boom"))

    assert result == {
        "error": "Install failed: /nonexistent/x.apk does not exist on the daemon host (boom)"
    }


def test_install_failure_existing_path_keeps_plain_message(tmp_path) -> None:  # type: ignore[no-untyped-def]
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"x")

    result = _install_failure(str(apk), RuntimeError("INSTALL_FAILED_OLDER_SDK"))

    assert result == {"error": "Install failed: INSTALL_FAILED_OLDER_SDK"}


def test_install_checked_success(tmp_path) -> None:  # type: ignore[no-untyped-def]
    mock_android = MagicMock(spec=AndroidController)
    mcp_mod._android = mock_android
    apk = str(tmp_path / "app.apk")

    result = _install_checked("android", "ABC", apk)

    assert result == {"success": True, "action": "install", "path": apk}
    mock_android.install.assert_called_once_with("ABC", apk)


def test_install_checked_relative_path_skips_controller() -> None:
    mock_android = MagicMock(spec=AndroidController)
    mcp_mod._android = mock_android

    result = _install_checked("android", "ABC", "rel/app.apk")

    assert "absolute" in result["error"]
    mock_android.install.assert_not_called()


def test_install_checked_controller_failure() -> None:
    mock_android = MagicMock(spec=AndroidController)
    mock_android.install.side_effect = RuntimeError("adb gone")
    mcp_mod._android = mock_android

    result = _install_checked("android", "ABC", "/nonexistent/x.apk")

    assert result["error"].startswith("Install failed: /nonexistent/x.apk does not exist")
    assert "adb gone" in result["error"]


def test_install_unknown_device_returns_error() -> None:
    mcp_mod._android = MagicMock(spec=AndroidController)
    mcp_mod._android.discover.return_value = []
    mcp_mod._ios = MagicMock()
    mcp_mod._ios.discover.return_value = []

    result = _nerve_install_sync("NOPE", "/abs/app.apk")

    assert "error" in result
    mcp_mod._android.install.assert_not_called()


def test_install_blocked_by_reservation_guard() -> None:
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = [_phone("ABC")]
    mcp_mod._android = mock_android
    mcp_mod._ios = MagicMock()
    mcp_mod._ios.discover.return_value = []
    mgr = MagicMock()
    mgr.guard.return_value = "reserved by someone else"
    mcp_mod._device_mgr = mgr

    result = _nerve_install_sync("ABC", "/abs/app.apk")

    assert result == {"error": "reserved by someone else"}
    mock_android.install.assert_not_called()


# ---------------------------------------------------------------------------
# _launch_phone_package monkey fallback — pin each abort marker on its own,
# the exact monkey command, and the exact error message.
# ---------------------------------------------------------------------------


def _monkey_ctrl(monkey_output: str) -> tuple[AndroidController, MagicMock]:
    """A controller whose launcher resolution fails, so the monkey fallback runs."""
    ctrl = AndroidController()
    ctrl._resolve_launcher_activity = MagicMock(return_value="")  # type: ignore[method-assign]
    shell = MagicMock(return_value=monkey_output)
    ctrl._shell = shell  # type: ignore[method-assign]
    return ctrl, shell


@pytest.mark.parametrize(
    "monkey_output",
    [
        "** No activities found to run\n",
        "** monkey aborted.\n",
    ],
)
def test_launch_phone_package_raises_on_either_abort_marker(monkey_output: str) -> None:
    ctrl, _ = _monkey_ctrl(monkey_output)

    with pytest.raises(RuntimeError, match="not installed"):
        ctrl._launch_phone_package("ABC", "com.missing")


def test_launch_phone_package_error_names_package_serial_and_first_line() -> None:
    ctrl, _ = _monkey_ctrl("\n** No activities found to run, monkey aborted.\ntrailer\n")

    with pytest.raises(RuntimeError) as exc_info:
        ctrl._launch_phone_package("ABC", "com.missing")

    assert str(exc_info.value) == (
        "package com.missing is not installed (or has no launcher activity) "
        "on ABC: ** No activities found to run, monkey aborted."
    )


def test_launch_phone_package_monkey_success_runs_exact_command() -> None:
    ctrl, shell = _monkey_ctrl("Events injected: 1\n## Network stats: elapsed time=12ms\n")

    ctrl._launch_phone_package("ABC", "com.present")

    shell.assert_called_once_with(
        "ABC", "monkey -p com.present -c android.intent.category.LAUNCHER 1"
    )


def test_install_checked_routes_ios_to_ios_controller() -> None:
    mcp_mod._android = MagicMock(spec=AndroidController)
    mcp_mod._ios = MagicMock()

    result = _install_checked("ios", "SIM-UDID", "/abs/App.app")

    assert result == {"success": True, "action": "install", "path": "/abs/App.app"}
    mcp_mod._ios.install.assert_called_once_with("SIM-UDID", "/abs/App.app")
    mcp_mod._android.install.assert_not_called()
