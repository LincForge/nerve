"""Tests for nerve launch --uri, validate_launch_uri, controller/router guards, and CLI parity.

Acceptance contract:
- validate_launch_uri accepts valid RFC 3986 URIs with query params, percent-encoding, anchors.
- validate_launch_uri rejects empty, whitespace, newlines, control characters, leading-dash,
  missing scheme, quotes, backticks, backslashes, length > 2048, NUL bytes.
- AndroidController.launch with uri executes am start with VIEW intent and shlex-quoted URI.
- _nerve_launch_sync rejects bad uri with "Launch failed: ..." without calling controller
  (Android & iOS).
- Router _reject_bad_uri rejects bad uri with 400, passes valid / empty / multi-param URIs.
- CLI nerve launch sends uri in payload only when non-empty.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from linc_nerve.cli.main import main
from linc_nerve.core.android import AndroidController, validate_launch_uri
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.daemon.http_router import _reject_bad_uri, route_request
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


# ---------------------------------------------------------------------------
# 1. validate_launch_uri accept / reject
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valid_uri",
    [
        "market://details?id=net.sourceforge.opencamera",
        "market://details?id=org.x&referrer=utm_source%3Dbench",
        "https://www.traeger.com/recipes?q=a%20b",
        "precisionlevel://settings",
        "sniperpulse://session/42#top",
    ],
)
def test_validate_launch_uri_accepts_valid(valid_uri: str) -> None:
    validate_launch_uri(valid_uri)


@pytest.mark.parametrize(
    "invalid_uri",
    [
        "",
        "market://x y",
        "market://x\n",
        "market://x\nreboot",
        "-d evil",
        "//no-scheme",
        "1abc://x",
        "https://x/`id`",
        'https://x/"q"',
        "https://x/\\y",
        "https://example.com/" + "a" * (2049 - len("https://example.com/")),
        "https://example.com/\x00evil",
    ],
)
def test_validate_launch_uri_rejects_invalid(invalid_uri: str) -> None:
    with pytest.raises(ValueError, match="RFC 3986"):
        validate_launch_uri(invalid_uri)


# ---------------------------------------------------------------------------
# 2. AndroidController.launch with fake _shell
# ---------------------------------------------------------------------------


def test_android_controller_launch_valid_uri() -> None:
    ctrl = AndroidController()
    shell_calls: list[tuple[str, str]] = []
    ctrl._shell = lambda serial, cmd: shell_calls.append((serial, cmd)) or ""  # type: ignore[assignment]

    ctrl.launch("S", "com.android.vending", uri="market://details?id=org.x")

    assert shell_calls == [
        (
            "S",
            (
                "am start -a android.intent.action.VIEW "
                "-d 'market://details?id=org.x' com.android.vending"
            ),
        )
    ]


def test_android_controller_launch_rejected_uri_raises_and_never_calls_shell() -> None:
    ctrl = AndroidController()
    shell_calls: list[tuple[str, str]] = []
    ctrl._shell = lambda serial, cmd: shell_calls.append((serial, cmd)) or ""  # type: ignore[assignment]

    with pytest.raises(ValueError):
        ctrl.launch("S", "com.android.vending", uri="market://x\nreboot")

    assert shell_calls == []


# ---------------------------------------------------------------------------
# 3. _nerve_launch_sync bad uri returns error without calling controller
# ---------------------------------------------------------------------------


def test_nerve_launch_sync_bad_uri_android() -> None:
    mock_android = MagicMock()
    mock_android.discover.return_value = [
        NerveDevice(
            serial="android-dev",
            platform=Platform.ANDROID,
            model="Pixel",
            device_type=DeviceType.PHONE,
            os_version="14",
        )
    ]
    mcp_mod._android = mock_android
    mcp_mod._ios = MagicMock()
    mcp_mod._ios.discover.return_value = []

    res = _nerve_launch_sync("android-dev", "com.android.vending", uri="market://x\nreboot")

    assert "error" in res
    assert res["error"].startswith("Launch failed")
    mock_android.launch.assert_not_called()


def test_nerve_launch_sync_bad_uri_ios() -> None:
    mock_ios = MagicMock()
    mock_ios.discover.return_value = [
        NerveDevice(
            serial="ios-dev",
            platform=Platform.IOS,
            model="iPhone",
            device_type=DeviceType.PHONE,
            os_version="17",
        )
    ]
    mcp_mod._ios = mock_ios
    mcp_mod._android = MagicMock()
    mcp_mod._android.discover.return_value = []

    res = _nerve_launch_sync("ios-dev", "com.example.ios", uri="market://x\nreboot")

    assert "error" in res
    assert res["error"].startswith("Launch failed")
    mock_ios.launch.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Router _reject_bad_uri
# ---------------------------------------------------------------------------


def test_router_reject_bad_uri() -> None:
    rejection = _reject_bad_uri("nerve_launch", {"uri": "market://x\nreboot"})
    assert rejection is not None
    assert rejection["status"] == 400
    assert rejection["body"] == {"error": "Request body contains a disallowed uri value"}

    # passes navigate shape
    nav_shape = {"package": "", "uri": "precisionlevel://settings"}
    assert _reject_bad_uri("nerve_launch", nav_shape) is None

    # passes multi-param & uri
    assert (
        _reject_bad_uri(
            "nerve_launch",
            {"uri": "market://details?id=org.x&referrer=utm_source%3Dbench"},
        )
        is None
    )

    # ignores body with no uri
    assert _reject_bad_uri("nerve_launch", {"device": "S", "package": "com.x"}) is None


def test_route_request_runs_uri_guard_before_dispatch() -> None:
    """The guard is wired into the pre-dispatch chain: a bad uri never reaches the tool."""
    calls: list[dict[str, object]] = []

    def fake_launch(device: str = "", package: str = "", uri: str = "") -> dict[str, object]:
        calls.append({"device": device, "package": package, "uri": uri})
        return {"launched": package}

    registry = {"nerve_launch": fake_launch}
    bad = route_request(
        "POST",
        "/tools/nerve_launch",
        {"device": "S", "package": "com.x", "uri": "market://x\nreboot"},
        registry,
    )
    assert bad["status"] == 400
    assert bad["body"] == {"error": "Request body contains a disallowed uri value"}
    assert calls == []

    ok = route_request(
        "POST",
        "/tools/nerve_launch",
        {"device": "S", "package": "", "uri": "market://details?id=org.x&referrer=a%3Db"},
        registry,
    )
    assert ok["status"] == 200
    assert calls == [
        {"device": "S", "package": "", "uri": "market://details?id=org.x&referrer=a%3Db"}
    ]


# ---------------------------------------------------------------------------
# 5. CLI nerve launch sends uri in payload only when non-empty
# ---------------------------------------------------------------------------


def test_cli_launch_with_uri() -> None:
    runner = CliRunner()
    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {"success": True}
        result = runner.invoke(
            main,
            ["launch", "S", "com.android.vending", "--uri", "market://details?id=org.x"],
        )
        assert result.exit_code == 0
        mock_call.assert_called_once_with(
            "nerve_launch",
            {"device": "S", "package": "com.android.vending", "uri": "market://details?id=org.x"},
        )


def test_cli_launch_without_uri() -> None:
    runner = CliRunner()
    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {"success": True}
        result = runner.invoke(main, ["launch", "S", "com.x"])
        assert result.exit_code == 0
        mock_call.assert_called_once_with("nerve_launch", {"device": "S", "package": "com.x"})
