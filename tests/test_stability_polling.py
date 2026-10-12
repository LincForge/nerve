"""Tests for UI stability polling wired into MCP tools.

TDD RED phase: verify navigate_screenshot uses wait_for_stable_ui
instead of fixed time.sleep().
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from PIL import Image

from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.emulator import EmulatorManager
from linc_nerve.core.ios import IOSController
from linc_nerve.core.metrics import MetricsCollector
from linc_nerve.core.state_engine import StateSessionManager
from linc_nerve.mcp import _core
from linc_nerve.mcp.navigation_tools import _nerve_navigate_screenshot_sync


def _reset_singletons() -> None:
    import contextlib

    if _core._device_mgr is not None:
        with contextlib.suppress(Exception):
            _core._device_mgr.close()
    _core._android = None
    _core._ios = None
    _core._state_mgr = None
    _core._metrics = None
    _core._device_mgr = None
    _core._emulator = None
    _core._cached_devices = []
    _core._cache_timestamp = 0.0
    _core._cleanup_done = False


def _phone() -> NerveDevice:
    return NerveDevice(
        serial="PIXEL8PRO",
        platform=Platform.ANDROID,
        device_type=DeviceType.PHONE,
        model="Pixel 8 Pro",
        os_version="14",
    )


def _make_png(
    width: int = 540,
    height: int = 960,
    color: tuple[int, int, int] = (128, 128, 128),
) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _setup_mocks() -> None:
    _reset_singletons()
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = [_phone()]
    mock_android.screenshot.return_value = _make_png()
    mock_android.elements.return_value = []
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    _core._android = mock_android
    _core._ios = mock_ios
    _core._state_mgr = MagicMock(spec=StateSessionManager)
    _core._metrics = MagicMock(spec=MetricsCollector)
    _core._device_mgr = MagicMock(spec=DeviceManager)
    _core._device_mgr.guard.return_value = None
    _core._emulator = MagicMock(spec=EmulatorManager)


class TestNavigateScreenshotStability:
    """navigate_screenshot should use stability polling instead of fixed sleep."""

    def test_calls_wait_for_stable_ui(self) -> None:
        """Should call wait_for_stable_ui instead of time.sleep."""
        _setup_mocks()
        try:
            with (
                patch(
                    "linc_nerve.mcp.app_tools._nerve_launch_sync",
                    return_value={"success": True, "uri": "app://test/home"},
                ),
                patch(
                    "linc_nerve.core.state_engine.wait_for_stable_ui",
                    return_value={
                        "stable": True,
                        "elapsed_ms": 150.0,
                        "polls": 4,
                        "last_frame": _make_png(),
                    },
                ) as mock_stable,
            ):
                result = _nerve_navigate_screenshot_sync(
                    "PIXEL8PRO",
                    "precisionlevel",
                    "home",
                )
                mock_stable.assert_called_once()
                assert result["success"] is True
        finally:
            _reset_singletons()

    def test_does_not_call_time_sleep(self) -> None:
        """time.sleep should NOT be called for settling — stability polling replaces it."""
        _setup_mocks()
        try:
            with (
                patch(
                    "linc_nerve.mcp.app_tools._nerve_launch_sync",
                    return_value={"success": True, "uri": "app://test/home"},
                ),
                patch(
                    "linc_nerve.core.state_engine.wait_for_stable_ui",
                    return_value={
                        "stable": True,
                        "elapsed_ms": 150.0,
                        "polls": 4,
                        "last_frame": _make_png(),
                    },
                ),
                patch("linc_nerve.mcp._stability.time.sleep") as mock_sleep,
            ):
                _nerve_navigate_screenshot_sync(
                    "PIXEL8PRO",
                    "precisionlevel",
                    "home",
                )
                mock_sleep.assert_not_called()
        finally:
            _reset_singletons()

    def test_stability_info_in_response(self) -> None:
        """Response should include stability polling metadata."""
        _setup_mocks()
        try:
            with (
                patch(
                    "linc_nerve.mcp.app_tools._nerve_launch_sync",
                    return_value={"success": True, "uri": "app://test/home"},
                ),
                patch(
                    "linc_nerve.core.state_engine.wait_for_stable_ui",
                    return_value={
                        "stable": True,
                        "elapsed_ms": 250.0,
                        "polls": 6,
                        "last_frame": _make_png(),
                    },
                ),
            ):
                result = _nerve_navigate_screenshot_sync(
                    "PIXEL8PRO",
                    "precisionlevel",
                    "home",
                )
                assert result["stable"] is True
                assert result["stability_ms"] == 250.0
                assert result["stability_polls"] == 6
        finally:
            _reset_singletons()

    def test_settle_ms_used_as_timeout(self) -> None:
        """settle_ms should be passed as the timeout to wait_for_stable_ui."""
        _setup_mocks()
        try:
            with (
                patch(
                    "linc_nerve.mcp.app_tools._nerve_launch_sync",
                    return_value={"success": True, "uri": "app://test/home"},
                ),
                patch(
                    "linc_nerve.core.state_engine.wait_for_stable_ui",
                    return_value={
                        "stable": True,
                        "elapsed_ms": 100.0,
                        "polls": 3,
                        "last_frame": _make_png(),
                    },
                ) as mock_stable,
            ):
                _nerve_navigate_screenshot_sync(
                    "PIXEL8PRO",
                    "precisionlevel",
                    "home",
                    settle_ms=1000,
                )
                call_kwargs = mock_stable.call_args
                # settle_ms should map to timeout_ms
                assert call_kwargs.kwargs.get("timeout_ms") == 1000 or (
                    len(call_kwargs.args) > 1 and call_kwargs.args[1] == 1000
                )
        finally:
            _reset_singletons()


class TestNavigateScreenshotLastFrame:
    """navigate_screenshot should reuse the last frame from stability polling."""

    def test_reuses_last_frame_skips_recapture(self) -> None:
        """When last_frame is present, should NOT call screenshot again."""
        _setup_mocks()
        frame_bytes = _make_png(color=(200, 200, 200))
        try:
            with (
                patch(
                    "linc_nerve.mcp.app_tools._nerve_launch_sync",
                    return_value={"success": True, "uri": "app://test/home"},
                ),
                patch(
                    "linc_nerve.core.state_engine.wait_for_stable_ui",
                    return_value={
                        "stable": True,
                        "elapsed_ms": 150.0,
                        "polls": 4,
                        "last_frame": frame_bytes,
                    },
                ),
            ):
                result = _nerve_navigate_screenshot_sync(
                    "PIXEL8PRO",
                    "precisionlevel",
                    "home",
                )
                assert result["success"] is True
                assert "screenshot" in result
                # The screenshot file should contain the last_frame bytes
                with open(result["screenshot"], "rb") as f:
                    saved = f.read()
                assert saved == frame_bytes
                # Controller screenshot should NOT have been called again
                _core._android.screenshot.assert_not_called()
        finally:
            _reset_singletons()

    def test_falls_back_without_last_frame(self) -> None:
        """If last_frame is missing (exception path), should call screenshot_sync."""
        _setup_mocks()
        try:
            with (
                patch(
                    "linc_nerve.mcp.app_tools._nerve_launch_sync",
                    return_value={"success": True, "uri": "app://test/home"},
                ),
                patch(
                    "linc_nerve.core.state_engine.wait_for_stable_ui",
                    return_value={
                        "stable": False,
                        "elapsed_ms": 500.0,
                        "polls": 0,
                        # No last_frame key
                    },
                ),
            ):
                result = _nerve_navigate_screenshot_sync(
                    "PIXEL8PRO",
                    "precisionlevel",
                    "home",
                )
                assert result["success"] is True
                # Should have called screenshot via the fallback path
                _core._android.screenshot.assert_called()
        finally:
            _reset_singletons()
