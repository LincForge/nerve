"""Tests for MCP server tool definitions."""

from __future__ import annotations

import io
import os
import signal
import time
from typing import Any
from unittest.mock import MagicMock

import anyio
import pytest
from PIL import Image

from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.emulator import EmulatorInfo, EmulatorManager
from linc_nerve.core.ios import IOSController
from linc_nerve.core.metrics import CommandMetric, MetricsCollector
from linc_nerve.mcp import server as mcp_mod


def _reset_singletons(tmp_path: Any = None) -> None:
    """Reset module-level singletons and cache between tests."""
    # Close existing device manager if any
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


def _make_png(color: tuple[int, int, int] = (128, 128, 128)) -> bytes:
    """Create a solid-color PNG for testing."""
    img = Image.new("RGB", (64, 64), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _phone() -> NerveDevice:
    return NerveDevice(
        serial="PIXEL8PRO",
        platform=Platform.ANDROID,
        device_type=DeviceType.PHONE,
        model="Pixel 8 Pro",
        os_version="14",
    )


def _simulator() -> NerveDevice:
    return NerveDevice(
        serial="UUID-123",
        platform=Platform.IOS,
        device_type=DeviceType.SIMULATOR,
        model="iPhone 15 Pro",
        os_version="17.5",
    )


class TestNerveDiscover:
    def test_discover_returns_all_devices(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = [_simulator()]
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_discover_sync()

        assert result["count"] == 2
        assert result["android"] == 1
        assert result["ios"] == 1
        assert len(result["devices"]) == 2

    def test_discover_empty(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_discover_sync()
        assert result["count"] == 0


class TestNerveScreenshot:
    def test_screenshot_android(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.screenshot.return_value = _make_png()
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_screenshot_sync("PIXEL8PRO")
        assert isinstance(result, dict)
        assert "screenshot" in result
        assert result["screenshot"].endswith(".png")
        assert os.path.exists(result["screenshot"])

    def test_screenshot_device_not_found(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_screenshot_sync("NONEXISTENT")
        assert "error" in result

    def test_screenshot_annotate_degrades_when_element_dump_fails(self) -> None:
        """annotate=True with a failed element dump still delivers the raw
        screenshot, carrying annotate_error instead of failing the call
        (reactor 8bba730a)."""
        from linc_nerve.core.device import UiHierarchyUnavailableError

        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.screenshot.return_value = _make_png()
        mock_android.elements.side_effect = UiHierarchyUnavailableError("dump Killed")
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_screenshot_sync("PIXEL8PRO", annotate=True)
        assert "error" not in result
        assert "screenshot" in result
        assert "Killed" in result["annotate_error"]
        assert "elements" not in result

    def test_screenshot_annotate_success_includes_elements(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.screenshot.return_value = _make_png()
        mock_android.elements.return_value = [
            {"text": "OK", "bounds": "[0,0][100,50]", "clickable": True}
        ]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_screenshot_sync("PIXEL8PRO", annotate=True)
        assert "error" not in result
        assert result["element_count"] == 1
        assert "annotate_error" not in result


class TestNerveTap:
    def test_tap_success(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_tap_sync("PIXEL8PRO", 100, 200)
        assert result["success"] is True
        mock_android.tap.assert_called_once_with("PIXEL8PRO", 100, 200)

    def test_tap_ios_not_implemented(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = [_simulator()]
        mock_ios.tap.side_effect = NotImplementedError("Phase 3")
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_tap_sync("UUID-123", 100, 200)
        assert "error" in result
        assert "Phase 3" in result["error"]


class TestNerveLongPress:
    def test_long_press_default_duration(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_long_press_sync("PIXEL8PRO", 100, 200)
        assert result["success"] is True
        assert result["action"] == "long_press"
        assert result["duration_ms"] == 500
        mock_android.long_press.assert_called_once_with("PIXEL8PRO", 100, 200, 500)

    def test_long_press_custom_duration(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_long_press_sync("PIXEL8PRO", 50, 75, 1000)
        assert result["success"] is True
        assert result["duration_ms"] == 1000
        mock_android.long_press.assert_called_once_with("PIXEL8PRO", 50, 75, 1000)

    def test_long_press_device_not_found(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_long_press_sync("NONEXISTENT", 100, 200)
        assert "error" in result

    def test_long_press_exception(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.long_press.side_effect = RuntimeError("ADB down")
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_long_press_sync("PIXEL8PRO", 100, 200)
        assert "error" in result
        assert "Long press failed" in result["error"]


class TestNerveDensity:
    def test_density_success(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.get_density.return_value = {"density_dpi": 420, "density_factor": 2.625}
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_density_sync("PIXEL8PRO")
        assert result["platform"] == "android"
        assert result["density_dpi"] == 420
        assert result["density_factor"] == 2.625
        mock_android.get_density.assert_called_once_with("PIXEL8PRO")

    def test_density_device_not_found(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_density_sync("NONEXISTENT")
        assert "error" in result


class TestNerveElements:
    def test_elements_android(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.elements.return_value = [{"id": "btn", "text": "OK", "clickable": True}]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_elements_sync("PIXEL8PRO")
        assert result["count"] == 1
        assert result["elements"][0]["text"] == "OK"

    def test_elements_dump_failure_is_loud_error(self) -> None:
        """A failed hierarchy dump must surface as an error dict, never as
        elements: [] / count: 0 (reactor 8bba730a)."""
        from linc_nerve.core.device import UiHierarchyUnavailableError

        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.elements.side_effect = UiHierarchyUnavailableError(
            "uiautomator dump failed on PIXEL8PRO (output: 'Killed')"
        )
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_elements_sync("PIXEL8PRO")
        assert "error" in result
        assert "Killed" in result["error"]
        assert "elements" not in result

    def test_elements_ios_via_wda(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = [_simulator()]
        mock_ios.elements.return_value = [{"text": "OK", "class": "Button"}]
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_elements_sync("UUID-123")
        assert "elements" in result
        assert result["count"] == 1

    def test_elements_ios_watch_not_supported(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = [_simulator()]
        mock_ios.elements.side_effect = NotImplementedError("WDA not supported on watchOS")
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_elements_sync("UUID-123")
        assert "error" in result
        assert "watchOS" in result["error"]

    def test_scroll_depth_discovers_offscreen_elements(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        # First call: initial dump with scrollable container + 2 items
        initial_elements = [
            {
                "id": "list",
                "class": "ListView",
                "bounds": "[0,100][1080,2000]",
                "scrollable": True,
            },
            {"id": "item1", "text": "Item 1", "bounds": "[0,100][1080,200]"},
            {"id": "item2", "text": "Item 2", "bounds": "[0,200][1080,300]"},
        ]
        # Second call: after scroll, new item appears (item2 shifts bounds)
        scrolled_elements = [
            {
                "id": "list",
                "class": "ListView",
                "bounds": "[0,100][1080,2000]",
                "scrollable": True,
            },
            {"id": "item2", "text": "Item 2", "bounds": "[0,100][1080,200]"},
            {"id": "item3", "text": "Item 3", "bounds": "[0,200][1080,300]"},
        ]
        mock_android.elements.side_effect = [initial_elements, scrolled_elements]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_elements_sync("PIXEL8PRO", scroll_depth=1)
        assert result["scroll_steps"] == 1
        # Dedup key is (id, text, bounds). item2 has different bounds after scroll
        # so it appears twice: list + item1 + item2@old_bounds + item2@new_bounds + item3 = 5
        assert result["count"] == 5
        mock_android.swipe.assert_called_once()

    def test_scroll_depth_early_termination(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        elements = [
            {"id": "list", "bounds": "[0,100][1080,2000]", "scrollable": True},
            {"id": "item1", "text": "Item 1", "bounds": "[0,100][1080,200]"},
        ]
        # Return same elements on scroll (no new items = end of list)
        mock_android.elements.side_effect = [elements, elements]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_elements_sync("PIXEL8PRO", scroll_depth=3)
        assert result["scroll_steps"] == 1  # Stopped after first scroll (no new elements)
        assert result["count"] == 2

    def test_scroll_depth_no_scrollable_container(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.elements.return_value = [
            {"id": "btn", "text": "OK", "bounds": "[0,0][100,50]"},
        ]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_elements_sync("PIXEL8PRO", scroll_depth=2)
        assert result["scroll_steps"] == 0
        assert "no scrollable container" in result["note"]

    def test_diff_only_and_scroll_depth_mutually_exclusive(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_elements_sync("PIXEL8PRO", diff_only=True, scroll_depth=1)
        assert "error" in result
        assert "mutually exclusive" in result["error"]


class TestNerveAppLifecycle:
    def test_launch(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_launch_sync("PIXEL8PRO", "com.lincmobile.level")
        assert result["success"] is True
        mock_android.launch.assert_called_once_with(
            "PIXEL8PRO", "com.lincmobile.level", uri="", extras=None
        )

    def test_launch_unknown_device_returns_error(self) -> None:
        """Launching an unresolved device returns an error (covers dev-None path)."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_launch_sync("UNKNOWN", "com.lincmobile.level")
        assert "error" in result
        mock_android.launch.assert_not_called()

    def test_launch_blocked_when_reserved(self, tmp_path: Any) -> None:
        """A device reserved by another session blocks launch (covers guard path)."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        db = tmp_path / "test.db"
        mgr1 = DeviceManager(db_path=db)
        mgr1.reserve("PIXEL8PRO", purpose="other session work")
        mgr2 = DeviceManager(db_path=db)
        mcp_mod._device_mgr = mgr2
        try:
            result = mcp_mod._nerve_launch_sync("PIXEL8PRO", "com.lincmobile.level")
            assert "error" in result
            mock_android.launch.assert_not_called()
        finally:
            mgr1.close()
            mgr2.close()

    def test_launch_with_extras(self) -> None:
        """Intent extras forward to the controller and are echoed in the result."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_launch_sync(
            "PIXEL8PRO", "com.lincmobile.level", extras={"SKIP_CONSENT": True}
        )
        assert result["success"] is True
        assert result["extras"] == {"SKIP_CONSENT": True}
        mock_android.launch.assert_called_once_with(
            "PIXEL8PRO", "com.lincmobile.level", uri="", extras={"SKIP_CONSENT": True}
        )

    def test_launch_without_extras_has_no_extras_key(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_launch_sync("PIXEL8PRO", "com.lincmobile.level")
        assert "extras" not in result

    def test_launch_records_metric_when_collector_present(self) -> None:
        """A configured metrics collector records the launch (covers the metrics branch)."""
        from linc_nerve.mcp import _core

        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios
        mock_metrics = MagicMock()
        _core._metrics = mock_metrics
        try:
            result = mcp_mod._nerve_launch_sync("PIXEL8PRO", "com.lincmobile.level")
        finally:
            _core._metrics = None
        assert result["success"] is True
        mock_metrics.record_launch.assert_called_once_with("PIXEL8PRO")

    def test_launch_invalid_extras_returns_error(self) -> None:
        """A bad extras value type surfaces as an error dict, not a crash."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.launch.side_effect = ValueError(
            "Unsupported intent extras value type for 'BAD': float"
        )
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_launch_sync(
            "PIXEL8PRO", "com.lincmobile.level", extras={"BAD": 3.14}
        )
        assert "error" in result
        assert "extras" in result["error"]

    def test_launch_with_uri(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_launch_sync(
            "PIXEL8PRO", "com.lincmobile.level", uri="precisionlevel://settings"
        )
        assert result["success"] is True
        assert result["uri"] == "precisionlevel://settings"
        mock_android.launch.assert_called_once_with(
            "PIXEL8PRO", "com.lincmobile.level", uri="precisionlevel://settings", extras=None
        )

    def test_launch_without_uri_has_no_uri_key(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_launch_sync("PIXEL8PRO", "com.lincmobile.level")
        assert result["success"] is True
        assert "uri" not in result

    def test_navigate_known_route(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_navigate_sync("PIXEL8PRO", "precisionlevel", "settings")
        assert result["success"] is True
        assert result["uri"] == "precisionlevel://screen/settings"
        mock_android.launch.assert_called_once_with(
            "PIXEL8PRO",
            "com.lincmobile.level",
            uri="precisionlevel://screen/settings",
            extras=None,
        )

    def test_navigate_unknown_app(self) -> None:
        result = mcp_mod._nerve_navigate_sync("PIXEL8PRO", "nonexistent", "home")
        assert "error" in result
        assert "nonexistent" in result["error"]
        assert "precisionlevel" in result["error"]

    def test_navigate_unknown_route(self) -> None:
        result = mcp_mod._nerve_navigate_sync("PIXEL8PRO", "precisionlevel", "nonexistent")
        assert "error" in result
        assert "nonexistent" in result["error"]
        assert "settings" in result["error"]

    def test_navigate_screenshot_happy_path(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.screenshot.return_value = _make_png()
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_navigate_screenshot_sync("PIXEL8PRO", "precisionlevel", "settings")
        assert result["success"] is True
        assert result["action"] == "navigate_screenshot"
        assert result["uri"] == "precisionlevel://screen/settings"
        assert result["screenshot"]  # non-empty path
        assert result["size_bytes"] > 0
        assert result["settle_ms"] == 500
        mock_android.launch.assert_called_once()
        # screenshot called during stability polling (last_frame reused, no re-capture)
        assert mock_android.screenshot.call_count >= 1

    def test_navigate_screenshot_nav_failure(self) -> None:
        result = mcp_mod._nerve_navigate_screenshot_sync("PIXEL8PRO", "nonexistent_app", "home")
        assert "error" in result
        assert "nonexistent_app" in result["error"]

    def test_navigate_screenshot_screenshot_failure(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.screenshot.side_effect = RuntimeError("device disconnected")
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_navigate_screenshot_sync("PIXEL8PRO", "precisionlevel", "settings")
        assert "error" in result
        assert "Screenshot failed" in result["error"]

    def test_navigate_screenshot_custom_settle(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.screenshot.return_value = _make_png()
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_navigate_screenshot_sync(
            "PIXEL8PRO", "precisionlevel", "settings", settle_ms=100
        )
        assert result["success"] is True
        assert result["settle_ms"] == 100

    def test_kill(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_kill_sync("PIXEL8PRO", "com.app")
        assert result["success"] is True

    def test_install(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_install_sync("PIXEL8PRO", "/path/app.apk")
        assert result["success"] is True

    def test_apps(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.list_apps.return_value = ["com.app.one", "com.app.two"]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_apps_sync("PIXEL8PRO")
        assert result["count"] == 2

    def test_uninstall(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_uninstall_sync("PIXEL8PRO", "com.app", confirm=True)
        assert result["success"] is True

    def test_clear_app_data(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.clear_app_data.return_value = False
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_clear_app_data_sync("PIXEL8PRO", "com.app", confirm=True)
        assert result["success"] is True
        assert result["action"] == "clear_app_data"
        assert result["needs_reinstall"] is False
        mock_android.clear_app_data.assert_called_once_with("PIXEL8PRO", "com.app")

    def test_clear_app_data_ios_physical_surfaces_error(self) -> None:
        """Physical iOS has no wipe primitive — the tool surfaces the guard
        as {"error": ...} with no needs_reinstall key (contract layer pin)."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = [_simulator()]
        mock_ios.clear_app_data.side_effect = NotImplementedError(
            "clear_app_data on physical iOS is not supported"
        )
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_clear_app_data_sync("UUID-123", "com.app", confirm=True)
        assert "error" in result
        assert "physical iOS" in result["error"]
        assert "success" not in result
        assert "needs_reinstall" not in result

    def test_clear_app_data_ios_sim_needs_reinstall(self) -> None:
        """iOS sim clear_app_data is uninstall-based → needs_reinstall=True."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = [_simulator()]
        mock_ios.clear_app_data.return_value = True
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_clear_app_data_sync("UUID-123", "com.app", confirm=True)
        assert result["success"] is True
        assert result["action"] == "clear_app_data"
        assert result["needs_reinstall"] is True
        mock_ios.clear_app_data.assert_called_once_with("UUID-123", "com.app")

    def test_clear_app_data_error(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.clear_app_data.side_effect = RuntimeError("adb gone")
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_clear_app_data_sync("PIXEL8PRO", "com.app", confirm=True)
        assert "error" in result
        assert "success" not in result

    def test_grant_permission(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_grant_permission_sync(
            "PIXEL8PRO", "com.app", "android.permission.CAMERA", confirm=True
        )
        assert result["success"] is True
        assert result["permission"] == "android.permission.CAMERA"
        mock_android.grant_permission.assert_called_once_with(
            "PIXEL8PRO", "com.app", "android.permission.CAMERA"
        )


class TestNerveDestructiveConfirmationGate:
    """Issue #94 gap 1: nerve_uninstall / nerve_clear_app_data /
    nerve_grant_permission are destructive and irreversible — they must
    require an explicit ``confirm=True`` before touching the controller.
    """

    TOOLS = ("uninstall", "clear_app_data", "grant_permission")

    def _setup(self) -> MagicMock:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios
        return mock_android

    def _invoke(
        self, tool: str, mock_android: MagicMock, *, pass_confirm: bool, confirm: Any = None
    ) -> tuple[dict[str, Any], MagicMock]:
        """Call the named tool, either with a confirm kwarg or fully omitted."""
        if tool == "uninstall":
            fn, method, args = (
                mcp_mod._nerve_uninstall_sync,
                mock_android.uninstall,
                (
                    "PIXEL8PRO",
                    "com.app",
                ),
            )
        elif tool == "clear_app_data":
            fn, method, args = (
                mcp_mod._nerve_clear_app_data_sync,
                mock_android.clear_app_data,
                (
                    "PIXEL8PRO",
                    "com.app",
                ),
            )
        elif tool == "grant_permission":
            fn, method, args = (
                mcp_mod._nerve_grant_permission_sync,
                mock_android.grant_permission,
                ("PIXEL8PRO", "com.app", "android.permission.CAMERA"),
            )
        else:
            raise ValueError(tool)

        result = fn(*args, confirm=confirm) if pass_confirm else fn(*args)
        return result, method

    @pytest.mark.parametrize("tool", TOOLS)
    def test_confirm_omitted_blocks(self, tool: str) -> None:
        mock_android = self._setup()
        result, method = self._invoke(tool, mock_android, pass_confirm=False)
        assert result == {"success": False, "error": "confirmation_required"}
        method.assert_not_called()

    @pytest.mark.parametrize("tool", TOOLS)
    def test_confirm_explicit_false_blocks(self, tool: str) -> None:
        mock_android = self._setup()
        result, method = self._invoke(tool, mock_android, pass_confirm=True, confirm=False)
        assert result == {"success": False, "error": "confirmation_required"}
        method.assert_not_called()

    @pytest.mark.parametrize("tool", TOOLS)
    def test_confirm_stringly_typed_false_blocks(self, tool: str) -> None:
        """JSON-RPC callers may pass confirm as the literal string "false"."""
        mock_android = self._setup()
        result, method = self._invoke(tool, mock_android, pass_confirm=True, confirm="false")
        assert result == {"success": False, "error": "confirmation_required"}
        method.assert_not_called()

    @pytest.mark.parametrize("tool", TOOLS)
    def test_confirm_true_proceeds(self, tool: str) -> None:
        mock_android = self._setup()
        result, method = self._invoke(tool, mock_android, pass_confirm=True, confirm=True)
        assert result["success"] is True
        method.assert_called_once()


class TestNerveState:
    def test_state_android(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.get_foreground.return_value = {
            "package": "com.lincmobile.level",
            "activity": ".MainActivity",
        }
        mock_android.get_orientation.return_value = "portrait"
        mock_android.get_screen_size.return_value = {"width": 1080, "height": 2400}
        mock_android.elements.return_value = [{"text": "OK"}]
        mock_android.screenshot.return_value = _make_png()
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_state_sync("PIXEL8PRO")
        # Returns dict with screenshot file path
        assert isinstance(result, dict)
        assert result["platform"] == "android"
        assert result["foreground"]["package"] == "com.lincmobile.level"
        assert result["orientation"] == "portrait"
        assert result["element_count"] == 1
        assert "screenshot" in result
        assert result["screenshot"].endswith(".png")

    def test_state_android_elements_failure_reports_error_not_zero(self) -> None:
        """When the element dump fails, nerve_state must carry elements_error
        and OMIT element_count — element_count: 0 would read as an empty
        screen and push agents into screenshot-only vision (reactor 8bba730a).
        The screenshot must still ship."""
        from linc_nerve.core.device import UiHierarchyUnavailableError

        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.get_foreground.return_value = {"package": "p", "activity": "a"}
        mock_android.get_orientation.return_value = "portrait"
        mock_android.get_screen_size.return_value = {"width": 1080, "height": 2400}
        mock_android.elements.side_effect = UiHierarchyUnavailableError(
            "uiautomator dump failed (output: 'Killed'); reboot the device"
        )
        mock_android.screenshot.return_value = _make_png()
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_state_sync("PIXEL8PRO")
        assert "Killed" in result["elements_error"]
        assert "element_count" not in result
        assert "elements" not in result
        assert "screenshot" in result


class TestNerveLogcat:
    def test_logcat_android(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_android.logcat.return_value = "line1\nline2\nline3"
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", lines=10)
        assert result["count"] == 3

    def test_logcat_ios_simulator(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = [_simulator()]
        mock_ios.get_logs.return_value = "ios line1\nios line2"
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_logcat_sync("UUID-123", lines=10)
        assert result["count"] == 2
        assert result["platform"] == "ios"
        assert result["lines"] == ["ios line1", "ios line2"]

    def test_logcat_ios_bundle_id_error(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = [_simulator()]
        mock_ios.get_logs.side_effect = ValueError(
            "No installed app matches bundle ID 'com.bad.app'.",
        )
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_logcat_sync("UUID-123", tag="com.bad.app")
        assert "error" in result
        assert "No installed app matches" in result["error"]


class TestDeviceCache:
    def test_cache_avoids_repeated_discover(self) -> None:
        """Sequential tool calls should use cached devices, not re-discover."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        # First call populates cache
        mcp_mod._nerve_tap_sync("PIXEL8PRO", 10, 20)
        # Second call should use cache
        mcp_mod._nerve_tap_sync("PIXEL8PRO", 30, 40)

        # discover() called once for initial cache, not twice
        assert mock_android.discover.call_count == 1

    def test_cache_refreshes_after_ttl(self) -> None:
        """Cache should refresh when TTL expires."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        # First call populates cache
        mcp_mod._nerve_tap_sync("PIXEL8PRO", 10, 20)
        assert mock_android.discover.call_count == 1

        # Expire cache by backdating timestamp
        mcp_mod._cache_timestamp = time.monotonic() - mcp_mod._CACHE_TTL - 1

        # Next call should refresh
        mcp_mod._nerve_tap_sync("PIXEL8PRO", 10, 20)
        assert mock_android.discover.call_count == 2

    def test_cache_miss_forces_refresh(self) -> None:
        """Looking up an unknown device forces a cache refresh."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        # Populate cache with phone
        mcp_mod._nerve_tap_sync("PIXEL8PRO", 10, 20)
        assert mock_android.discover.call_count == 1

        # Look up unknown device — forces refresh + retry
        result = mcp_mod._nerve_tap_sync("UNKNOWN", 10, 20)
        assert "error" in result
        # discover was called once more for the forced refresh
        assert mock_android.discover.call_count == 2

    def test_discover_always_refreshes(self) -> None:
        """nerve_discover should always refresh, ignoring cache."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        mcp_mod._nerve_discover_sync()
        mcp_mod._nerve_discover_sync()

        # Each discover call refreshes
        assert mock_android.discover.call_count == 2

    def test_invalidate_cache(self) -> None:
        """_invalidate_cache forces next lookup to refresh."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        mcp_mod._nerve_tap_sync("PIXEL8PRO", 10, 20)
        assert mock_android.discover.call_count == 1

        mcp_mod._invalidate_cache()
        mcp_mod._nerve_tap_sync("PIXEL8PRO", 10, 20)
        assert mock_android.discover.call_count == 2


def _setup_android_mocks(
    screenshot_data: bytes | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Helper to set up standard android mock pair."""
    _reset_singletons()
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = [_phone()]
    if screenshot_data is not None:
        mock_android.screenshot.return_value = screenshot_data
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    mcp_mod._android = mock_android
    mcp_mod._ios = mock_ios
    return mock_android, mock_ios


class TestNerveScreenshotDelta:
    def test_skip_if_unchanged_first_call_returns_image(self) -> None:
        png = _make_png()
        _setup_android_mocks(png)

        result = mcp_mod._nerve_screenshot_sync("PIXEL8PRO", skip_if_unchanged=True)
        assert isinstance(result, dict)
        assert "screenshot" in result

    def test_skip_if_unchanged_same_image(self) -> None:
        png = _make_png()
        _setup_android_mocks(png)

        # First call — returns file path
        r1 = mcp_mod._nerve_screenshot_sync("PIXEL8PRO", skip_if_unchanged=True)
        assert isinstance(r1, dict)
        assert "screenshot" in r1

        # Second call — same image, should be unchanged
        r2 = mcp_mod._nerve_screenshot_sync("PIXEL8PRO", skip_if_unchanged=True)
        assert r2["status"] == "unchanged"
        assert r2["frames_since_change"] == 1

    def test_skip_if_unchanged_different_image(self) -> None:
        mock_android, _ = _setup_android_mocks()
        png1 = _make_png((0, 0, 0))
        png2 = _make_png((255, 255, 255))
        mock_android.screenshot.side_effect = [png1, png2]

        mcp_mod._nerve_screenshot_sync("PIXEL8PRO", skip_if_unchanged=True)
        r2 = mcp_mod._nerve_screenshot_sync("PIXEL8PRO", skip_if_unchanged=True)
        assert isinstance(r2, dict) and "screenshot" in r2  # Changed, so file path returned

    def test_default_no_skip(self) -> None:
        """Default behavior (skip_if_unchanged=False) always returns image."""
        png = _make_png()
        _setup_android_mocks(png)

        r1 = mcp_mod._nerve_screenshot_sync("PIXEL8PRO")
        r2 = mcp_mod._nerve_screenshot_sync("PIXEL8PRO")
        assert isinstance(r1, dict) and "screenshot" in r1
        assert isinstance(r2, dict) and "screenshot" in r2


class TestNerveElementsDiff:
    def test_diff_only_first_call_returns_full(self) -> None:
        """First diff_only call should return elements (no prior state)."""
        mock_android, _ = _setup_android_mocks()
        mock_android.elements.return_value = [
            {"id": "btn", "text": "OK", "bounds": "[0,0][100,50]"}
        ]

        # First call with diff_only — no prior, returns None diff → "unchanged"
        # since update_elements returns None on first call (no prior data)
        result = mcp_mod._nerve_elements_sync("PIXEL8PRO", diff_only=True)
        assert result["status"] == "unchanged"

    def test_diff_only_unchanged_second_call(self) -> None:
        mock_android, _ = _setup_android_mocks()
        elems = [{"id": "btn", "text": "OK", "bounds": "[0,0][100,50]"}]
        mock_android.elements.return_value = elems

        mcp_mod._nerve_elements_sync("PIXEL8PRO", diff_only=True)
        result = mcp_mod._nerve_elements_sync("PIXEL8PRO", diff_only=True)
        assert result["status"] == "unchanged"

    def test_diff_only_changed(self) -> None:
        mock_android, _ = _setup_android_mocks()
        elems1 = [{"id": "btn", "text": "OK", "bounds": "[0,0][100,50]"}]
        elems2 = [{"id": "btn", "text": "Done", "bounds": "[0,0][100,50]"}]
        mock_android.elements.side_effect = [elems1, elems2]

        mcp_mod._nerve_elements_sync("PIXEL8PRO", diff_only=True)
        result = mcp_mod._nerve_elements_sync("PIXEL8PRO", diff_only=True)
        assert "diff" in result
        assert len(result["diff"]["changed"]) == 1

    def test_default_no_diff(self) -> None:
        """Default behavior always returns full elements."""
        mock_android, _ = _setup_android_mocks()
        mock_android.elements.return_value = [{"id": "btn", "text": "OK"}]

        r1 = mcp_mod._nerve_elements_sync("PIXEL8PRO")
        r2 = mcp_mod._nerve_elements_sync("PIXEL8PRO")
        assert "elements" in r1
        assert "elements" in r2


class TestNerveDiff:
    def test_diff_first_call_establishes_baseline_silently(self) -> None:
        """First nerve_diff with no prior baseline must NOT phantom-flip changes.

        Bug 2b2dbef6: previously returned any_change=True with hamming=0 and
        unchanged foreground/orientation marked as changed. With no baseline
        there is no "change" to report — establish baseline silently.
        """
        mock_android, _ = _setup_android_mocks(_make_png())
        mock_android.get_foreground.return_value = {"package": "com.app", "activity": ".Main"}
        mock_android.get_orientation.return_value = "portrait"
        mock_android.elements.return_value = [{"id": "btn", "text": "OK"}]

        result = mcp_mod._nerve_diff_sync("PIXEL8PRO")
        assert isinstance(result, dict)
        assert result["any_change"] is False
        assert result["changes"]["screenshot"] is False
        assert result["changes"]["foreground"] is False
        assert result["changes"]["orientation"] is False
        assert result["changes"]["elements"] is False
        assert result["hamming_distance"] == 0

    def test_diff_first_call_with_empty_foreground(self) -> None:
        """Empty foreground on first call doesn't lie about a change (regression)."""
        mock_android, _ = _setup_android_mocks(_make_png())
        mock_android.get_foreground.return_value = {"package": "", "activity": ""}
        mock_android.get_orientation.return_value = "portrait"
        mock_android.elements.return_value = []

        result = mcp_mod._nerve_diff_sync("PIXEL8PRO")
        assert result["any_change"] is False
        assert result["changes"]["foreground"] is False
        assert result["changes"]["orientation"] is False

    def test_diff_elements_failure_reports_error(self) -> None:
        """A failed element dump during nerve_diff surfaces as elements_error
        instead of being swallowed (reactor 8bba730a)."""
        from linc_nerve.core.device import UiHierarchyUnavailableError

        mock_android, _ = _setup_android_mocks(_make_png())
        mock_android.get_foreground.return_value = {"package": "com.app", "activity": ".Main"}
        mock_android.get_orientation.return_value = "portrait"
        mock_android.elements.side_effect = UiHierarchyUnavailableError("dump Killed")

        result = mcp_mod._nerve_diff_sync("PIXEL8PRO")
        assert "dump Killed" in result["elements_error"]
        assert "elements" not in result["changes"]

    def test_diff_no_change(self) -> None:
        png = _make_png()
        mock_android, _ = _setup_android_mocks(png)
        mock_android.get_foreground.return_value = {"package": "com.app", "activity": ".Main"}
        mock_android.get_orientation.return_value = "portrait"
        elems = [{"id": "btn", "text": "OK", "bounds": "[0,0][50,50]"}]
        mock_android.elements.return_value = elems

        # First call seeds state
        mcp_mod._nerve_diff_sync("PIXEL8PRO")
        # Second call — nothing changed
        result = mcp_mod._nerve_diff_sync("PIXEL8PRO")
        assert isinstance(result, dict)  # No image, just dict
        assert result["any_change"] is False
        assert result["frames_since_change"] == 1

    def test_diff_foreground_change(self) -> None:
        png = _make_png()
        mock_android, _ = _setup_android_mocks(png)
        mock_android.get_foreground.side_effect = [
            {"package": "com.app1", "activity": ".Main"},
            {"package": "com.app2", "activity": ".Main"},
        ]
        mock_android.get_orientation.return_value = "portrait"
        mock_android.elements.return_value = []

        mcp_mod._nerve_diff_sync("PIXEL8PRO")
        result = mcp_mod._nerve_diff_sync("PIXEL8PRO")
        # Screenshot unchanged but foreground changed — no image, just dict
        assert isinstance(result, dict)
        assert result["changes"]["foreground"] is True
        assert result["foreground"]["package"] == "com.app2"

    def test_diff_device_not_found(self) -> None:
        _setup_android_mocks()
        result = mcp_mod._nerve_diff_sync("NONEXISTENT")
        assert "error" in result


class TestNerveStateTracking:
    def test_state_track_changes(self) -> None:
        mock_android, _ = _setup_android_mocks(_make_png())
        mock_android.get_foreground.return_value = {"package": "com.app"}
        mock_android.get_orientation.return_value = "portrait"
        mock_android.get_screen_size.return_value = {"width": 1080, "height": 2400}
        mock_android.elements.return_value = []

        result = mcp_mod._nerve_state_sync("PIXEL8PRO", track_changes=True)
        # Returns dict with screenshot path
        assert isinstance(result, dict)
        assert "changes" in result
        # First call establishes baseline silently — no phantom changes (bug 2b2dbef6)
        assert result["changes"]["screenshot"] is False
        assert result["changes"]["foreground"] is False
        # Screenshot file is still saved on first call
        assert "screenshot" in result

        # Second call with identical state: still no change
        result2 = mcp_mod._nerve_state_sync("PIXEL8PRO", track_changes=True)
        assert result2["changes"]["screenshot"] is False
        assert result2["changes"]["foreground"] is False

    def test_state_no_tracking_by_default(self) -> None:
        mock_android, _ = _setup_android_mocks(_make_png())
        mock_android.get_foreground.return_value = {"package": "com.app"}
        mock_android.get_orientation.return_value = "portrait"
        mock_android.get_screen_size.return_value = {"width": 1080, "height": 2400}
        mock_android.elements.return_value = []

        result = mcp_mod._nerve_state_sync("PIXEL8PRO")
        # Returns dict with screenshot path
        assert isinstance(result, dict)
        assert "changes" not in result


class TestNerveSessionStats:
    def test_session_stats_empty(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_session_stats_sync()
        assert result["count"] == 0

    def test_session_stats_after_screenshot(self) -> None:
        _setup_android_mocks(_make_png())

        mcp_mod._nerve_screenshot_sync("PIXEL8PRO", skip_if_unchanged=True)
        result = mcp_mod._nerve_session_stats_sync()
        assert result["count"] == 1
        assert "PIXEL8PRO" in result["sessions"]


class TestNerveMetrics:
    def test_metrics_empty(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_metrics_sync()
        assert result["total_recorded"] == 0
        assert result["stats"] == {}
        assert result["slow_commands"] == []
        assert result["recent"] == []

    def test_metrics_with_data(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        # Pre-populate metrics
        mc = MetricsCollector()
        mc.record(CommandMetric(command="input", serial="A", duration_ms=10.0, success=True))
        mc.record(
            CommandMetric(
                command="screenshot",
                serial="A",
                duration_ms=3000.0,
                success=True,
            )
        )
        mcp_mod._metrics = mc

        result = mcp_mod._nerve_metrics_sync()
        assert result["total_recorded"] == 2
        assert "input" in result["stats"]
        assert "screenshot" in result["stats"]
        assert len(result["slow_commands"]) == 1  # screenshot > 2000ms
        assert len(result["recent"]) == 2

    def test_metrics_filter_by_command(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        mc = MetricsCollector()
        mc.record(CommandMetric(command="input", serial="A", duration_ms=10.0, success=True))
        mc.record(CommandMetric(command="screenshot", serial="A", duration_ms=50.0, success=True))
        mcp_mod._metrics = mc

        result = mcp_mod._nerve_metrics_sync(command="screenshot")
        assert "screenshot" in result["stats"]
        assert "input" not in result["stats"]

    def test_metrics_returns_expected_format(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        mc = MetricsCollector()
        for i in range(10):
            mc.record(
                CommandMetric(command="input", serial="A", duration_ms=float(i * 10), success=True)
            )
        mcp_mod._metrics = mc

        result = mcp_mod._nerve_metrics_sync()
        assert "stats" in result
        assert "slow_commands" in result
        assert "recent" in result
        assert "total_recorded" in result

        s = result["stats"]["input"]
        assert "p50" in s
        assert "p95" in s
        assert "p99" in s
        assert "max" in s
        assert "count" in s
        assert "error_rate" in s

    def test_metrics_response_includes_tool_stats(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        mc = MetricsCollector()
        mc.record_tool_call("nerve_screenshot", True)
        mc.record_reserve("ABC123")
        mc.record_launch("ABC123")
        mcp_mod._metrics = mc

        result = mcp_mod._nerve_metrics_sync()
        assert "tool_stats" in result
        assert "reservation_warnings" in result
        assert "launch_to_screenshot" in result
        assert result["tool_stats"]["nerve_screenshot"]["success"] == 1

    def test_metrics_accepts_device_kwarg(self) -> None:
        # Regression: agents reflexively pass `device=` (every other nerve tool
        # takes it). Before the fix this raised TypeError and the tool logged
        # 655 calls at 0% success. It must now accept device gracefully.
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        mc = MetricsCollector()
        mc.record(CommandMetric(command="input", serial="A", duration_ms=10.0, success=True))
        mcp_mod._metrics = mc

        result = mcp_mod._nerve_metrics_sync(device="A")
        assert result["device"] == "A"
        assert "input" in result["stats"]

    def test_metrics_accepts_package_kwarg(self) -> None:
        # Regression: Crucible's NerveClient passes `package=` during BVT.
        # Must accept package gracefully without TypeError.
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        mc = MetricsCollector()
        mcp_mod._metrics = mc

        result = mcp_mod._nerve_metrics_sync(device="A", package="com.app")
        assert result["device"] == "A"

    def test_metrics_filter_by_device(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        mc = MetricsCollector()
        mc.record(CommandMetric(command="input", serial="A", duration_ms=10.0, success=True))
        mc.record(CommandMetric(command="screenshot", serial="B", duration_ms=50.0, success=True))
        mcp_mod._metrics = mc

        result = mcp_mod._nerve_metrics_sync(device="A")
        assert "input" in result["stats"]
        assert "screenshot" not in result["stats"]
        assert all(r["serial"] == "A" for r in result["recent"])


class TestNerveEmulatorList:
    def test_emulator_list(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mcp_mod._android = mock_android

        from linc_nerve.core.device import DeviceType, NerveDevice, Platform

        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mock_ios._discover_simulators.return_value = [
            NerveDevice(
                serial="4C215E09-3AAF-498F-B8AF-FE9F8B3B5177",
                platform=Platform.IOS,
                device_type=DeviceType.PHONE,
                model="iPhone 15 Pro",
                state="online",
            )
        ]
        mcp_mod._ios = mock_ios

        mock_emu = MagicMock(spec=EmulatorManager)
        mock_emu.list_avds.return_value = [
            EmulatorInfo(avd_name="Pixel_7_API_34", state="stopped"),
            EmulatorInfo(
                avd_name="Pixel_Watch",
                serial="emulator-5554",
                port=5554,
                state="running",
            ),
        ]
        mcp_mod._emulator = mock_emu

        result = mcp_mod._nerve_emulator_list_sync()
        assert result["count"] == 3
        assert result["running"] == 2
        assert result["stopped"] == 1
        assert "virtual_devices" in result
        assert len(result["virtual_devices"]) == 3

        # Regression: inbox f41a355d. The legacy `avds` key duplicated
        # `virtual_devices` — confusing for new consumers reading the
        # response shape, and zero downstream code keys off `avds`.
        assert "avds" not in result

        # Verify iOS properties are mapped correctly
        ios_dev = next(d for d in result["virtual_devices"] if d["platform"] == "ios")
        assert ios_dev["name"] == "iPhone 15 Pro"
        assert ios_dev["id"] == "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"
        assert ios_dev["state"] == "running"

    def test_emulator_list_reports_unresolved_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When resolve_emulator_path() can't find the emulator binary, the response
        still returns iOS sims but flags android_sdk_resolved=false with a warning
        naming the probed locations — never a bare iOS-only success (linc-nerve#118).
        Fails pre-fix: response dict has only virtual_devices/count/running/stopped
        keys with no indication Android was silently empty."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mcp_mod._android = mock_android

        from linc_nerve.core.device import DeviceType, NerveDevice, Platform

        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mock_ios._discover_simulators.return_value = [
            NerveDevice(
                serial="4C215E09-3AAF-498F-B8AF-FE9F8B3B5177",
                platform=Platform.IOS,
                device_type=DeviceType.PHONE,
                model="iPhone 15 Pro",
                state="online",
            )
        ]
        mcp_mod._ios = mock_ios

        mock_emu = MagicMock(spec=EmulatorManager)
        mock_emu.list_avds.return_value = []
        mcp_mod._emulator = mock_emu

        monkeypatch.setattr("linc_nerve.mcp.emulator_tools.resolve_emulator_path", lambda: None)

        result = mcp_mod._nerve_emulator_list_sync()

        assert result["android_sdk_resolved"] is False
        assert "emulator binary not found" in result["android_warning"]
        # Still a success response with the iOS sim present, not an error.
        assert "error" not in result
        assert result["count"] == 1
        assert result["virtual_devices"][0]["platform"] == "ios"

    def test_emulator_list_resolved_sdk_no_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the emulator binary DOES resolve, android_sdk_resolved is True and
        no android_warning key is present (no noise on the happy path)."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mcp_mod._android = mock_android

        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mock_ios._discover_simulators.return_value = []
        mcp_mod._ios = mock_ios

        mock_emu = MagicMock(spec=EmulatorManager)
        mock_emu.list_avds.return_value = [
            EmulatorInfo(avd_name="Pixel_7_API_34", state="stopped"),
        ]
        mcp_mod._emulator = mock_emu

        monkeypatch.setattr(
            "linc_nerve.mcp.emulator_tools.resolve_emulator_path",
            lambda: "/opt/homebrew/share/android-commandlinetools/emulator/emulator",
        )

        result = mcp_mod._nerve_emulator_list_sync()

        assert result["android_sdk_resolved"] is True
        assert "android_warning" not in result


class TestNerveEmulatorStart:
    def test_emulator_start_ios(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mcp_mod._android = mock_android

        mock_emu = MagicMock(spec=EmulatorManager)
        mcp_mod._emulator = mock_emu

        from linc_nerve.core.device import DeviceType, NerveDevice, Platform

        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mock_ios._discover_simulators.return_value = [
            NerveDevice(
                serial="4C215E09-3AAF-498F-B8AF-FE9F8B3B5177",
                platform=Platform.IOS,
                device_type=DeviceType.PHONE,
                model="iPhone 15 Pro",
                state="offline",
            )
        ]
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_emulator_start_sync("iPhone 15 Pro")

        assert result["success"] is True
        assert result["emulator"]["name"] == "iPhone 15 Pro"
        assert result["emulator"]["platform"] == "ios"

        # Ensure it called iOS boot, not Android start
        mock_ios.boot.assert_called_once_with("4C215E09-3AAF-498F-B8AF-FE9F8B3B5177")
        mock_emu.start.assert_not_called()


class TestNerveEmulatorStop:
    def test_stops_ios_simulator(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []

        from linc_nerve.core.device import DeviceType, NerveDevice, Platform

        mock_ios = MagicMock(spec=IOSController)
        mock_ios._discover_simulators.return_value = [
            NerveDevice(
                serial="4C215E09-3AAF-498F-B8AF-FE9F8B3B5177",
                platform=Platform.IOS,
                device_type=DeviceType.PHONE,
                model="iPhone 15 Pro",
                state="online",
            )
        ]

        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios
        mock_emu = MagicMock(spec=EmulatorManager)
        mcp_mod._emulator = mock_emu

        # Pass valid iOS UDID
        result = mcp_mod._nerve_emulator_stop_sync("4C215E09-3AAF-498F-B8AF-FE9F8B3B5177")

        assert result["success"] is True
        assert result["device"] == "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"
        mock_ios.shutdown.assert_called_once_with("4C215E09-3AAF-498F-B8AF-FE9F8B3B5177")
        mock_emu.stop.assert_not_called()

    def test_rejects_non_emulator(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        mock_emu = MagicMock(spec=EmulatorManager)
        mcp_mod._emulator = mock_emu

        result = mcp_mod._nerve_emulator_stop_sync("UNKNOWN_SERIAL_123")
        assert "error" in result
        assert "not an Android emulator serial or iOS Simulator UDID" in result["error"]
        mock_emu.stop.assert_not_called()
        mock_ios.shutdown.assert_not_called()

    def test_stops_emulator(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        mock_emu = MagicMock(spec=EmulatorManager)
        mock_emu.stop.return_value = True
        mcp_mod._emulator = mock_emu

        result = mcp_mod._nerve_emulator_stop_sync("emulator-5554")
        assert result["success"] is True
        mock_emu.stop.assert_called_once_with("emulator-5554")


class TestNerveReserveAndRelease:
    def test_reserve_and_release(self, tmp_path: Any) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        mgr = DeviceManager(db_path=tmp_path / "test.db")
        mcp_mod._device_mgr = mgr

        try:
            result = mcp_mod._nerve_reserve_sync("PIXEL8PRO", purpose="testing")
            assert result["success"] is True
            assert result["device"] == "PIXEL8PRO"
            assert result["purpose"] == "testing"

            pool = mcp_mod._nerve_pool_sync()
            assert pool["count"] == 1
            assert pool["reservations"][0]["device_serial"] == "PIXEL8PRO"
            assert pool["reservations"][0]["held_by"].startswith("held by ")

            release = mcp_mod._nerve_release_sync("PIXEL8PRO")
            assert release["success"] is True

            pool2 = mcp_mod._nerve_pool_sync()
            assert pool2["count"] == 0
        finally:
            mgr.close()


class TestNervePoolEmpty:
    def test_pool_empty(self, tmp_path: Any) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = []
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        mgr = DeviceManager(db_path=tmp_path / "test.db")
        mcp_mod._device_mgr = mgr

        try:
            result = mcp_mod._nerve_pool_sync()
            assert result["count"] == 0
            assert result["reservations"] == []
        finally:
            mgr.close()


class TestNerveGuardBlocksTool:
    def test_guard_blocks_tool(self, tmp_path: Any) -> None:
        """Reserve from one manager, verify a tool call returns error."""
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        db = tmp_path / "test.db"
        # mgr1 reserves the device (simulates another session)
        mgr1 = DeviceManager(db_path=db)
        mgr1.reserve("PIXEL8PRO", purpose="other session work")

        # mgr2 is the "current" session's device manager
        mgr2 = DeviceManager(db_path=db)
        mcp_mod._device_mgr = mgr2

        try:
            # Tool should be blocked
            result = mcp_mod._nerve_tap_sync("PIXEL8PRO", 100, 200)
            assert "error" in result
            assert "held by" in result["error"]
            mock_android.tap.assert_not_called()
        finally:
            mgr1.close()
            mgr2.close()


class TestDeviceSemaphore:
    """Per-device semaphore serializes same-device calls but allows
    different-device and non-device calls to run concurrently."""

    def test_same_device_serializes(self) -> None:
        """Two calls to the same device must not overlap."""
        timeline: list[tuple[str, str]] = []  # (event, device)

        async def run() -> None:
            mcp_mod._device_semaphores.clear()

            def slow_work() -> str:
                timeline.append(("start", "A"))
                time.sleep(0.15)
                timeline.append(("end", "A"))
                return "ok"

            def fast_work() -> str:
                timeline.append(("start", "A2"))
                timeline.append(("end", "A2"))
                return "ok"

            async with anyio.create_task_group() as tg:
                tg.start_soon(mcp_mod._run_on_device, "DEV-A", slow_work)
                await anyio.sleep(0.02)  # Ensure slow_work starts first
                tg.start_soon(mcp_mod._run_on_device, "DEV-A", fast_work)

        anyio.run(run)
        # slow_work must finish before fast_work starts
        assert timeline == [
            ("start", "A"),
            ("end", "A"),
            ("start", "A2"),
            ("end", "A2"),
        ]

    def test_different_devices_concurrent(self) -> None:
        """Calls to different devices must overlap."""
        timeline: list[tuple[str, str]] = []

        async def run() -> None:
            mcp_mod._device_semaphores.clear()

            def work_a() -> str:
                timeline.append(("start", "A"))
                time.sleep(0.15)
                timeline.append(("end", "A"))
                return "ok"

            def work_b() -> str:
                timeline.append(("start", "B"))
                time.sleep(0.15)
                timeline.append(("end", "B"))
                return "ok"

            async with anyio.create_task_group() as tg:
                tg.start_soon(mcp_mod._run_on_device, "DEV-A", work_a)
                tg.start_soon(mcp_mod._run_on_device, "DEV-B", work_b)

        anyio.run(run)
        # Both should start before either ends
        starts = [e for e in timeline if e[0] == "start"]
        ends = [e for e in timeline if e[0] == "end"]
        assert len(starts) == 2
        assert len(ends) == 2
        # First two events should both be starts (concurrent)
        assert timeline[0][0] == "start"
        assert timeline[1][0] == "start"


class TestShutdownCleanup:
    def test_shutdown_cleanup_calls_scrcpy_stop_all(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_scrcpy = MagicMock()
        mock_android._get_scrcpy.return_value = mock_scrcpy
        mcp_mod._android = mock_android

        mcp_mod._shutdown_cleanup()

        mock_scrcpy.stop_all.assert_called_once()

    def test_shutdown_cleanup_noop_when_no_android(self) -> None:
        _reset_singletons()
        mcp_mod._android = None

        mcp_mod._shutdown_cleanup()  # Should not raise

    def test_shutdown_cleanup_noop_when_no_scrcpy(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android._get_scrcpy.return_value = None
        mcp_mod._android = mock_android

        mcp_mod._shutdown_cleanup()  # Should not raise

    def test_shutdown_cleanup_suppresses_exceptions(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_scrcpy = MagicMock()
        mock_scrcpy.stop_all.side_effect = RuntimeError("boom")
        mock_android._get_scrcpy.return_value = mock_scrcpy
        mcp_mod._android = mock_android

        mcp_mod._shutdown_cleanup()  # Should not raise

    def test_shutdown_cleanup_idempotent(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_scrcpy = MagicMock()
        mock_android._get_scrcpy.return_value = mock_scrcpy
        mcp_mod._android = mock_android

        mcp_mod._shutdown_cleanup()
        mcp_mod._shutdown_cleanup()

        mock_scrcpy.stop_all.assert_called_once()

    def test_sigint_handler_registered(self) -> None:
        assert signal.getsignal(signal.SIGINT) is mcp_mod._sigterm_handler


class TestNerveStreamLogs:
    def test_stream_logs_android(self) -> None:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios

        result = mcp_mod._nerve_stream_logs_sync("PIXEL8PRO", filter_str="com.app")
        assert "url" in result
        assert "device=PIXEL8PRO" in result["url"]
        assert "filter=com.app" in result["url"]
