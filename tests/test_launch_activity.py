"""nerve_launch ``activity``: pick one launcher when a package has several.

A debug build with a second launcher activity (SniperPulse's Shot Lab) makes a
package-only launch fail with "has 2 launcher activities ... pass activity= to
choose one", and the ``nerve_launch`` tool had no way to pass one, so crucible's
cold_start BVT could not launch the app at all.

Contract:
- ``activity`` reaches the controller and is echoed in the result.
- Omitting it keeps the old controller call (no ``activity`` kwarg).
- A hostile activity is rejected before any device shell command (input boundary).
- The CLI sends ``activity`` only when given.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from linc_nerve.cli.main import main
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.mcp import server as mcp_mod
from linc_nerve.mcp.app_tools import _nerve_launch_sync

pytestmark = pytest.mark.usefixtures("restore_signal_handlers")


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
    _reset_singletons()
    yield
    _reset_singletons()


def _android_with(controller: MagicMock | AndroidController) -> None:
    device = NerveDevice(
        serial="android-dev",
        platform=Platform.ANDROID,
        model="Galaxy",
        device_type=DeviceType.PHONE,
        os_version="16",
    )
    if isinstance(controller, MagicMock):
        controller.discover.return_value = [device]
    else:
        controller.discover = lambda: [device]  # type: ignore[method-assign]
    mcp_mod._android = controller
    mcp_mod._ios = MagicMock()
    mcp_mod._ios.discover.return_value = []


def test_contract_activity_reaches_controller_and_is_echoed() -> None:
    ctrl = MagicMock()
    _android_with(ctrl)

    res = _nerve_launch_sync(
        "android-dev", "com.sniperpulse.app.dev", activity="com.sniperpulse.app.MainActivity"
    )

    assert res["success"] is True
    assert res["activity"] == "com.sniperpulse.app.MainActivity"
    ctrl.launch.assert_called_once_with(
        "android-dev",
        "com.sniperpulse.app.dev",
        activity="com.sniperpulse.app.MainActivity",
        uri="",
        extras=None,
    )


def test_contract_no_activity_keeps_old_controller_call() -> None:
    ctrl = MagicMock()
    _android_with(ctrl)

    res = _nerve_launch_sync("android-dev", "com.example.app")

    assert res["success"] is True
    assert "activity" not in res
    ctrl.launch.assert_called_once_with("android-dev", "com.example.app", uri="", extras=None)


def test_contract_activity_launches_explicit_component() -> None:
    ctrl = AndroidController()
    shell_calls: list[str] = []
    ctrl._shell = lambda serial, cmd: shell_calls.append(cmd) or ""  # type: ignore[assignment]
    _android_with(ctrl)

    res = _nerve_launch_sync(
        "android-dev", "com.sniperpulse.app.dev", activity="com.sniperpulse.app.MainActivity"
    )

    assert res["success"] is True
    assert shell_calls == ["am start -n com.sniperpulse.app.dev/com.sniperpulse.app.MainActivity"]


@pytest.mark.parametrize(
    "hostile",
    [
        "Main;reboot",
        "Main\nreboot",
        "Main Activity",
        "$(reboot)",
        "-n com.evil/.X",
        "Main`id`",
    ],
)
def test_contract_hostile_activity_rejected_before_any_shell(hostile: str) -> None:
    ctrl = AndroidController()
    shell_calls: list[str] = []
    ctrl._shell = lambda serial, cmd: shell_calls.append(cmd) or ""  # type: ignore[assignment]
    _android_with(ctrl)

    res = _nerve_launch_sync("android-dev", "com.example.app", activity=hostile)

    assert res["error"].startswith("Launch failed")
    assert shell_calls == []


def test_cli_launch_sends_activity_only_when_given() -> None:
    runner = CliRunner()
    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {"success": True}
        result = runner.invoke(main, ["launch", "S", "com.x", "--activity", "com.x.MainActivity"])
        assert result.exit_code == 0
        mock_call.assert_called_once_with(
            "nerve_launch",
            {"device": "S", "package": "com.x", "activity": "com.x.MainActivity"},
        )
