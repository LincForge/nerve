"""Tests for annotated screenshot wiring — annotate=True on MCP tools.

TDD RED phase: tests for the integration between annotator and MCP tools.
"""

from __future__ import annotations

import io
import os
from unittest.mock import MagicMock

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
from linc_nerve.mcp.screenshot_tools import _nerve_screenshot_sync


def _reset_singletons() -> None:
    """Reset module-level singletons between tests."""
    if _core._device_mgr is not None:
        import contextlib

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


def _setup_android_mock(
    elements: list[dict] | None = None,
    screenshot_color: tuple[int, int, int] = (128, 128, 128),
) -> None:
    """Set up mocked Android controller with screenshot + elements."""
    _reset_singletons()
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = [_phone()]
    mock_android.screenshot.return_value = _make_png(color=screenshot_color)
    mock_android.elements.return_value = elements or [
        {"id": "btn_ok", "text": "OK", "bounds": "[100,200][300,400]", "clickable": True},
        {"id": "tv_title", "text": "Hello", "bounds": "[50,50][500,100]"},
    ]
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    _core._android = mock_android
    _core._ios = mock_ios
    _core._state_mgr = MagicMock(spec=StateSessionManager)
    _core._metrics = MagicMock(spec=MetricsCollector)
    _core._device_mgr = MagicMock(spec=DeviceManager)
    _core._device_mgr.guard.return_value = None
    _core._emulator = MagicMock(spec=EmulatorManager)


class TestScreenshotAnnotate:
    """nerve_screenshot with annotate=True."""

    def test_annotate_returns_file_path(self) -> None:
        _setup_android_mock()
        result = _nerve_screenshot_sync("PIXEL8PRO", annotate=True)
        assert "screenshot" in result
        assert result["screenshot"].endswith(".png")
        assert os.path.exists(result["screenshot"])

    def test_annotate_includes_elements_in_response(self) -> None:
        _setup_android_mock()
        result = _nerve_screenshot_sync("PIXEL8PRO", annotate=True)
        assert "elements" in result
        assert len(result["elements"]) == 2
        assert result["elements"][0]["id"] == "btn_ok"

    def test_annotate_includes_element_count(self) -> None:
        _setup_android_mock()
        result = _nerve_screenshot_sync("PIXEL8PRO", annotate=True)
        assert result["element_count"] == 2

    def test_annotate_false_does_not_fetch_elements(self) -> None:
        """Default annotate=False should NOT call elements()."""
        _setup_android_mock()
        result = _nerve_screenshot_sync("PIXEL8PRO")
        assert "elements" not in result
        _core._android.elements.assert_not_called()

    def test_annotated_image_differs_from_original(self) -> None:
        """The saved file should be the annotated version, not the raw screenshot."""
        _setup_android_mock(screenshot_color=(0, 0, 0))
        result = _nerve_screenshot_sync("PIXEL8PRO", annotate=True)
        with open(result["screenshot"], "rb") as f:
            img = Image.open(io.BytesIO(f.read()))
        # Box should be drawn at [100,200][300,400] — check a box edge pixel
        # Image is downscaled to max 1280, but 540x960 < 1280 so no scaling
        px = img.getpixel((100, 200))
        assert px != (0, 0, 0), "Annotated image should have bounding box drawn"


class TestNavigateScreenshotAnnotate:
    """nerve_navigate_screenshot with annotate=True."""

    def _setup_with_nav(self) -> None:
        """Set up mocks including navigation (launch) support."""
        _setup_android_mock()
        # Mock the launch call that navigate uses under the hood
        from unittest.mock import patch

        self._launch_patcher = patch(
            "linc_nerve.mcp.app_tools._nerve_launch_sync",
            return_value={"success": True, "uri": "app://test/home"},
        )
        self._launch_patcher.start()

    def _teardown(self) -> None:
        self._launch_patcher.stop()
        _reset_singletons()

    def test_annotate_param_accepted(self) -> None:
        """navigate_screenshot should accept annotate as a keyword argument."""
        self._setup_with_nav()
        try:
            result = _nerve_navigate_screenshot_sync(
                "PIXEL8PRO",
                "precisionlevel",
                "home",
                annotate=True,
            )
            assert "error" not in result
        finally:
            self._teardown()

    def test_annotate_returns_elements(self) -> None:
        """With annotate=True, response should include elements and element_count."""
        self._setup_with_nav()
        try:
            result = _nerve_navigate_screenshot_sync(
                "PIXEL8PRO",
                "precisionlevel",
                "home",
                annotate=True,
            )
            assert "elements" in result, "annotate=True should include elements"
            assert result["element_count"] == 2
        finally:
            self._teardown()

    def test_annotate_false_no_elements(self) -> None:
        """Default (annotate=False) should NOT include elements."""
        self._setup_with_nav()
        try:
            result = _nerve_navigate_screenshot_sync(
                "PIXEL8PRO",
                "precisionlevel",
                "home",
            )
            assert "elements" not in result
        finally:
            self._teardown()

    def test_annotated_image_has_bounding_boxes(self) -> None:
        """The screenshot file should be annotated when annotate=True."""
        _setup_android_mock(screenshot_color=(0, 0, 0))
        from unittest.mock import patch

        with patch(
            "linc_nerve.mcp.app_tools._nerve_launch_sync",
            return_value={"success": True, "uri": "app://test/home"},
        ):
            result = _nerve_navigate_screenshot_sync(
                "PIXEL8PRO",
                "precisionlevel",
                "home",
                annotate=True,
            )
            with open(result["screenshot"], "rb") as f:
                img = Image.open(io.BytesIO(f.read()))
            px = img.getpixel((100, 200))
            assert px != (0, 0, 0), "Annotated image should have bounding box"
        _reset_singletons()


def test_nerve_navigate_screenshot_is_registered_mcp_tool() -> None:
    """nerve_navigate_screenshot must be exposed via FastMCP.

    Regression test for a3fc16ca: commit 9ceba05 (CRAP refactor) accidentally
    dropped the @device_tool decorator from _nerve_navigate_screenshot_sync,
    causing the tool to disappear from the MCP registry while remaining in the
    docs. This test fails if the decorator is missing again.
    """
    from linc_nerve.mcp import _core, server  # noqa: F401  (triggers registration)

    tool_names = [t.name for t in _core.server._tool_manager.list_tools()]
    assert "nerve_navigate_screenshot" in tool_names, (
        f"nerve_navigate_screenshot not registered; have {sorted(tool_names)}"
    )


class TestAnnotateBestEffort:
    """A failed element dump degrades to the raw frame, never fails the call
    (reactor 8bba730a). Extracted helper — both branches pinned here."""

    def test_failure_returns_raw_frame_with_error(self) -> None:
        from linc_nerve.core.device import UiHierarchyUnavailableError
        from linc_nerve.mcp.screenshot_tools import _annotate_best_effort

        ctrl = MagicMock()
        ctrl.elements.side_effect = UiHierarchyUnavailableError("dump Killed")

        data, extras = _annotate_best_effort(ctrl, "SER", b"rawpng")
        assert data == b"rawpng"
        assert "Killed" in extras["annotate_error"]
        assert "elements" not in extras

    def test_success_returns_annotated_frame(self) -> None:
        from unittest.mock import patch

        from linc_nerve.mcp.screenshot_tools import _annotate_best_effort

        ctrl = MagicMock()
        ctrl.elements.return_value = [{"text": "OK"}]
        with patch(
            "linc_nerve.core.annotator.annotate_screenshot", return_value=b"annotated"
        ):
            data, extras = _annotate_best_effort(ctrl, "SER", b"rawpng")
        assert data == b"annotated"
        assert extras["elements"] == [{"text": "OK"}]
        assert extras["element_count"] == 1
        assert "annotate_error" not in extras
