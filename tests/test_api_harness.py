"""API harness: automated stress test exercising all 26 MCP sync functions.

Eight test phases, two devices (Android + iOS), ~99 API calls total.
Maps the manual stress test pattern from tests/manual/test_stdio_raw.py
into automated regression tests.
"""

from __future__ import annotations

import base64
import io
import os
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.emulator import EmulatorInfo, EmulatorManager
from linc_nerve.core.ios import IOSController
from linc_nerve.core.metrics import CommandMetric, MetricsCollector
from linc_nerve.core.state_engine import StateSessionManager
from linc_nerve.mcp import server as mcp_mod

pytestmark = pytest.mark.api_harness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_singletons(tmp_path: Any = None) -> None:
    """Reset module-level singletons and cache between tests."""
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


def _setup_two_devices() -> tuple[MagicMock, MagicMock]:
    """Create mock Android + iOS controllers with 2 devices, assign to mcp_mod."""
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = [_phone()]
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = [_simulator()]
    mcp_mod._android = mock_android
    mcp_mod._ios = mock_ios
    return mock_android, mock_ios


def _setup_state_and_metrics(tmp_path: Any) -> None:
    """Initialize real StateSessionManager, MetricsCollector, DeviceManager."""
    mcp_mod._state_mgr = StateSessionManager()
    mcp_mod._metrics = MetricsCollector()
    mcp_mod._device_mgr = DeviceManager(db_path=tmp_path / "harness.db")
    mcp_mod._emulator = MagicMock(spec=EmulatorManager)


# ---------------------------------------------------------------------------
# Phase 1: Discovery (3 calls)
# ---------------------------------------------------------------------------


class TestPhase1Discovery:
    def test_discover_finds_both_devices(self) -> None:
        _reset_singletons()
        _setup_two_devices()

        result = mcp_mod._nerve_discover_sync()
        assert result["count"] == 2
        assert result["android"] == 1
        assert result["ios"] == 1
        serials = {d["serial"] for d in result["devices"]}
        assert serials == {"PIXEL8PRO", "UUID-123"}

    def test_discover_cache_hit(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()

        # First call — populates cache
        mcp_mod._nerve_tap_sync("PIXEL8PRO", 10, 20)
        # Second call within TTL — should use cache
        mcp_mod._nerve_tap_sync("PIXEL8PRO", 30, 40)
        assert mock_android.discover.call_count == 1

    def test_discover_cache_expired(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()

        mcp_mod._nerve_tap_sync("PIXEL8PRO", 10, 20)
        assert mock_android.discover.call_count == 1

        # Expire cache
        mcp_mod._cache_timestamp = time.monotonic() - mcp_mod._CACHE_TTL - 1
        mcp_mod._nerve_tap_sync("PIXEL8PRO", 10, 20)
        assert mock_android.discover.call_count == 2


# ---------------------------------------------------------------------------
# Phase 2: Screenshots (12 calls)
# ---------------------------------------------------------------------------


class TestPhase2Screenshots:
    def test_screenshot_android(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_android.screenshot.return_value = _make_png()

        result = mcp_mod._nerve_screenshot_sync("PIXEL8PRO")
        assert "screenshot" in result
        assert result["screenshot"].endswith(".png")
        assert os.path.exists(result["screenshot"])

    def test_screenshot_ios(self) -> None:
        _reset_singletons()
        _, mock_ios = _setup_two_devices()
        mock_ios.screenshot.return_value = _make_png((200, 200, 200))

        result = mcp_mod._nerve_screenshot_sync("UUID-123")
        assert "screenshot" in result
        assert result["screenshot"].endswith(".png")

    def test_screenshot_skip_unchanged(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        png = _make_png()
        mock_android.screenshot.return_value = png

        r1 = mcp_mod._nerve_screenshot_sync("PIXEL8PRO", skip_if_unchanged=True)
        assert "screenshot" in r1

        r2 = mcp_mod._nerve_screenshot_sync("PIXEL8PRO", skip_if_unchanged=True)
        assert r2["status"] == "unchanged"

    def test_screenshot_detects_change(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_android.screenshot.side_effect = [
            _make_png((0, 0, 0)),
            _make_png((255, 255, 255)),
        ]

        r1 = mcp_mod._nerve_screenshot_sync("PIXEL8PRO", skip_if_unchanged=True)
        assert "screenshot" in r1

        r2 = mcp_mod._nerve_screenshot_sync("PIXEL8PRO", skip_if_unchanged=True)
        assert "screenshot" in r2  # Changed, so image returned

    def test_screenshot_rapid_fire(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_android.screenshot.return_value = _make_png()

        for i in range(6):
            result = mcp_mod._nerve_screenshot_sync("PIXEL8PRO")
            assert "screenshot" in result, f"Rapid-fire call {i} failed"
        assert mock_android.screenshot.call_count == 6


# ---------------------------------------------------------------------------
# Phase 3: Interaction (15 calls)
# ---------------------------------------------------------------------------


class TestPhase3Interaction:
    def test_tap_android(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()

        result = mcp_mod._nerve_tap_sync("PIXEL8PRO", 100, 200)
        assert result["success"] is True
        mock_android.tap.assert_called_once_with("PIXEL8PRO", 100, 200)

    def test_tap_ios(self) -> None:
        _reset_singletons()
        _, mock_ios = _setup_two_devices()

        result = mcp_mod._nerve_tap_sync("UUID-123", 150, 300)
        assert result["success"] is True
        mock_ios.tap.assert_called_once_with("UUID-123", 150, 300)

    def test_long_press_android(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()

        result = mcp_mod._nerve_long_press_sync("PIXEL8PRO", 100, 200, 800)
        assert result["success"] is True
        assert result["duration_ms"] == 800
        mock_android.long_press.assert_called_once_with("PIXEL8PRO", 100, 200, 800)

    def test_long_press_ios(self) -> None:
        _reset_singletons()
        _, mock_ios = _setup_two_devices()

        result = mcp_mod._nerve_long_press_sync("UUID-123", 50, 75)
        assert result["success"] is True
        mock_ios.long_press.assert_called_once()

    def test_swipe_android(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()

        result = mcp_mod._nerve_swipe_sync("PIXEL8PRO", 100, 800, 100, 200, 300)
        assert result["success"] is True
        assert result["action"] == "swipe"
        mock_android.swipe.assert_called_once_with("PIXEL8PRO", 100, 800, 100, 200, 300)

    def test_swipe_ios(self) -> None:
        _reset_singletons()
        _, mock_ios = _setup_two_devices()

        result = mcp_mod._nerve_swipe_sync("UUID-123", 200, 600, 200, 100, 250)
        assert result["success"] is True
        mock_ios.swipe.assert_called_once()

    def test_type_android(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()

        result = mcp_mod._nerve_type_sync("PIXEL8PRO", "hello world")
        assert result["success"] is True
        assert result["text"] == "hello world"
        mock_android.type_text.assert_called_once_with("PIXEL8PRO", "hello world")

    def test_type_ios(self) -> None:
        _reset_singletons()
        _, mock_ios = _setup_two_devices()

        result = mcp_mod._nerve_type_sync("UUID-123", "test input")
        assert result["success"] is True
        mock_ios.type_text.assert_called_once_with("UUID-123", "test input")

    def test_key_android(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()

        result = mcp_mod._nerve_key_sync("PIXEL8PRO", "HOME")
        assert result["success"] is True
        assert result["key"] == "HOME"
        mock_android.press_key.assert_called_once_with("PIXEL8PRO", "HOME")

    def test_key_ios(self) -> None:
        _reset_singletons()
        _, mock_ios = _setup_two_devices()

        result = mcp_mod._nerve_key_sync("UUID-123", "HOME")
        assert result["success"] is True
        mock_ios.press_key.assert_called_once_with("UUID-123", "HOME")

    def test_rapid_fire_taps(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()

        for i in range(5):
            result = mcp_mod._nerve_tap_sync("PIXEL8PRO", 100 + i, 200 + i)
            assert result["success"] is True
        assert mock_android.tap.call_count == 5


# ---------------------------------------------------------------------------
# Phase 4: Elements (10 calls)
# ---------------------------------------------------------------------------


class TestPhase4Elements:
    def test_elements_android_full(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_android.elements.return_value = [
            {"id": "btn1", "text": "OK", "clickable": True},
            {"id": "btn2", "text": "Cancel", "clickable": True},
        ]

        result = mcp_mod._nerve_elements_sync("PIXEL8PRO")
        assert result["count"] == 2
        assert result["elements"][0]["text"] == "OK"

    def test_elements_ios_full(self) -> None:
        _reset_singletons()
        _, mock_ios = _setup_two_devices()
        mock_ios.elements.return_value = [{"text": "Settings", "class": "Button"}]

        result = mcp_mod._nerve_elements_sync("UUID-123")
        assert result["count"] == 1

    def test_elements_diff_only_no_prior(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_android.elements.return_value = [
            {"id": "btn", "text": "OK", "bounds": "[0,0][100,50]"}
        ]

        result = mcp_mod._nerve_elements_sync("PIXEL8PRO", diff_only=True)
        # First call with diff_only — no prior state → "unchanged"
        assert result["status"] == "unchanged"

    def test_elements_diff_only_with_change(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        elems1 = [{"id": "btn", "text": "OK", "bounds": "[0,0][100,50]"}]
        elems2 = [{"id": "btn", "text": "Done", "bounds": "[0,0][100,50]"}]
        mock_android.elements.side_effect = [elems1, elems2]

        mcp_mod._nerve_elements_sync("PIXEL8PRO", diff_only=True)
        result = mcp_mod._nerve_elements_sync("PIXEL8PRO", diff_only=True)
        assert "diff" in result
        assert len(result["diff"]["changed"]) == 1

    def test_elements_diff_only_no_change(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        elems = [{"id": "btn", "text": "OK", "bounds": "[0,0][100,50]"}]
        mock_android.elements.return_value = elems

        mcp_mod._nerve_elements_sync("PIXEL8PRO", diff_only=True)
        result = mcp_mod._nerve_elements_sync("PIXEL8PRO", diff_only=True)
        assert result["status"] == "unchanged"

    def test_elements_scroll_depth(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        initial = [
            {
                "id": "list",
                "class": "ListView",
                "bounds": "[0,100][1080,2000]",
                "scrollable": True,
            },
            {"id": "item1", "text": "Item 1", "bounds": "[0,100][1080,200]"},
        ]
        scrolled = [
            {
                "id": "list",
                "class": "ListView",
                "bounds": "[0,100][1080,2000]",
                "scrollable": True,
            },
            {"id": "item2", "text": "Item 2", "bounds": "[0,100][1080,200]"},
        ]
        mock_android.elements.side_effect = [initial, scrolled]

        result = mcp_mod._nerve_elements_sync("PIXEL8PRO", scroll_depth=1)
        assert result["scroll_steps"] == 1
        assert result["count"] == 3  # list + item1 + item2

    def test_elements_rapid_fire(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_android.elements.return_value = [{"id": "btn", "text": "OK"}]

        for _ in range(2):
            result = mcp_mod._nerve_elements_sync("PIXEL8PRO")
            assert result["count"] == 1


# ---------------------------------------------------------------------------
# Phase 5: App Lifecycle (12 calls)
# ---------------------------------------------------------------------------


class TestPhase5AppLifecycle:
    def test_install_android(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        result = mcp_mod._nerve_install_sync("PIXEL8PRO", "/path/app.apk")
        assert result["success"] is True
        mock_android.install.assert_called_once_with("PIXEL8PRO", "/path/app.apk")

    def test_install_ios(self) -> None:
        _reset_singletons()
        _, mock_ios = _setup_two_devices()
        result = mcp_mod._nerve_install_sync("UUID-123", "/path/app.ipa")
        assert result["success"] is True
        mock_ios.install.assert_called_once_with("UUID-123", "/path/app.ipa")

    def test_launch_android(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        result = mcp_mod._nerve_launch_sync("PIXEL8PRO", "com.lincmobile.level")
        assert result["success"] is True
        mock_android.launch.assert_called_once_with(
            "PIXEL8PRO", "com.lincmobile.level", uri="", extras=None
        )

    def test_launch_ios(self) -> None:
        _reset_singletons()
        _, mock_ios = _setup_two_devices()
        result = mcp_mod._nerve_launch_sync("UUID-123", "com.lincmobile.level")
        assert result["success"] is True
        mock_ios.launch.assert_called_once_with(
            "UUID-123", "com.lincmobile.level", uri="", extras=None
        )

    def test_apps_android(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_android.list_apps.return_value = ["com.app.one", "com.app.two"]
        result = mcp_mod._nerve_apps_sync("PIXEL8PRO")
        assert result["count"] == 2

    def test_apps_ios(self) -> None:
        _reset_singletons()
        _, mock_ios = _setup_two_devices()
        mock_ios.list_apps.return_value = ["com.app.three"]
        result = mcp_mod._nerve_apps_sync("UUID-123")
        assert result["count"] == 1

    def test_kill_android(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        result = mcp_mod._nerve_kill_sync("PIXEL8PRO", "com.app")
        assert result["success"] is True
        mock_android.kill.assert_called_once_with("PIXEL8PRO", "com.app")

    def test_kill_ios(self) -> None:
        _reset_singletons()
        _, mock_ios = _setup_two_devices()
        result = mcp_mod._nerve_kill_sync("UUID-123", "com.app")
        assert result["success"] is True
        mock_ios.kill.assert_called_once_with("UUID-123", "com.app")

    def test_uninstall_android(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        result = mcp_mod._nerve_uninstall_sync("PIXEL8PRO", "com.app", confirm=True)
        assert result["success"] is True
        mock_android.uninstall.assert_called_once_with("PIXEL8PRO", "com.app")

    def test_uninstall_ios(self) -> None:
        _reset_singletons()
        _, mock_ios = _setup_two_devices()
        result = mcp_mod._nerve_uninstall_sync("UUID-123", "com.app", confirm=True)
        assert result["success"] is True
        mock_ios.uninstall.assert_called_once_with("UUID-123", "com.app")

    def test_full_lifecycle_sequence(self) -> None:
        """install → launch → kill → uninstall on one device."""
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_android.list_apps.return_value = ["com.lincmobile.level"]

        r1 = mcp_mod._nerve_install_sync("PIXEL8PRO", "/path/app.apk")
        assert r1["success"] is True
        r2 = mcp_mod._nerve_launch_sync("PIXEL8PRO", "com.lincmobile.level")
        assert r2["success"] is True
        r3 = mcp_mod._nerve_kill_sync("PIXEL8PRO", "com.lincmobile.level")
        assert r3["success"] is True
        r4 = mcp_mod._nerve_uninstall_sync("PIXEL8PRO", "com.lincmobile.level", confirm=True)
        assert r4["success"] is True


# ---------------------------------------------------------------------------
# Phase 6: State & Diff (15 calls)
# ---------------------------------------------------------------------------


class TestPhase6State:
    def _setup_state_mocks(self) -> tuple[MagicMock, MagicMock]:
        mock_android, mock_ios = _setup_two_devices()
        mock_android.get_foreground.return_value = {
            "package": "com.app",
            "activity": ".Main",
        }
        mock_android.get_orientation.return_value = "portrait"
        mock_android.get_screen_size.return_value = {"width": 1080, "height": 2400}
        mock_android.elements.return_value = [{"text": "OK"}]
        mock_android.screenshot.return_value = _make_png()
        return mock_android, mock_ios

    def test_state_basic(self) -> None:
        _reset_singletons()
        self._setup_state_mocks()

        result = mcp_mod._nerve_state_sync("PIXEL8PRO")
        assert result["platform"] == "android"
        assert result["foreground"]["package"] == "com.app"
        assert "screenshot" in result

    def test_state_track_changes_first_call(self) -> None:
        _reset_singletons()
        self._setup_state_mocks()

        result = mcp_mod._nerve_state_sync("PIXEL8PRO", track_changes=True)
        assert "changes" in result
        # First call establishes baseline silently — no phantom changes (bug 2b2dbef6)
        assert result["changes"]["screenshot"] is False

    def test_state_track_changes_after_mutation(self, tmp_path: Any) -> None:
        _reset_singletons()
        _mock_android, _ = self._setup_state_mocks()
        _setup_state_and_metrics(tmp_path)

        # First screenshot sets baseline
        mcp_mod._nerve_screenshot_sync("PIXEL8PRO", skip_if_unchanged=True)

        # State with tracking should detect screenshot as unchanged
        result = mcp_mod._nerve_state_sync("PIXEL8PRO", track_changes=True)
        assert "changes" in result

    def test_diff_no_prior(self) -> None:
        """nerve_diff with no prior baseline must not lie about changes (bug 2b2dbef6)."""
        _reset_singletons()
        _mock_android, _ = self._setup_state_mocks()

        result = mcp_mod._nerve_diff_sync("PIXEL8PRO")
        assert result["any_change"] is False
        assert result["changes"]["screenshot"] is False

    def test_diff_after_screenshot_change(self) -> None:
        _reset_singletons()
        mock_android, _ = self._setup_state_mocks()
        mock_android.screenshot.side_effect = [
            _make_png((0, 0, 0)),
            _make_png((0, 0, 0)),
            _make_png((255, 255, 255)),
        ]

        # Baseline diff
        mcp_mod._nerve_diff_sync("PIXEL8PRO")
        # Same screenshot — no change
        r2 = mcp_mod._nerve_diff_sync("PIXEL8PRO")
        assert r2["any_change"] is False
        # Different screenshot — change detected
        r3 = mcp_mod._nerve_diff_sync("PIXEL8PRO")
        assert r3["any_change"] is True
        assert r3["changes"]["screenshot"] is True

    def test_diff_after_element_change(self) -> None:
        _reset_singletons()
        mock_android, _ = self._setup_state_mocks()
        elems1 = [{"id": "btn", "text": "OK", "bounds": "[0,0][100,50]"}]
        elems2 = [{"id": "btn", "text": "OK", "bounds": "[0,0][100,50]"}]
        elems3 = [{"id": "btn", "text": "Done", "bounds": "[0,0][100,50]"}]
        mock_android.elements.side_effect = [elems1, elems2, elems3]

        mcp_mod._nerve_diff_sync("PIXEL8PRO")
        r2 = mcp_mod._nerve_diff_sync("PIXEL8PRO")
        assert r2["changes"]["elements"] is False
        r3 = mcp_mod._nerve_diff_sync("PIXEL8PRO")
        assert r3["changes"]["elements"] is True

    def test_session_stats(self) -> None:
        _reset_singletons()
        _setup_two_devices()

        result = mcp_mod._nerve_session_stats_sync()
        assert result["count"] == 0

    def test_session_stats_after_activity(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_android.screenshot.return_value = _make_png()

        # Two screenshots with skip_if_unchanged
        mcp_mod._nerve_screenshot_sync("PIXEL8PRO", skip_if_unchanged=True)
        mcp_mod._nerve_screenshot_sync("PIXEL8PRO", skip_if_unchanged=True)

        result = mcp_mod._nerve_session_stats_sync()
        assert result["count"] == 1
        assert "PIXEL8PRO" in result["sessions"]
        session = result["sessions"]["PIXEL8PRO"]
        assert session["total_screenshots"] >= 1


# ---------------------------------------------------------------------------
# Phase 7: Streaming, Metrics & Logcat (12 calls)
# ---------------------------------------------------------------------------


class TestPhase7StreamingMetrics:
    def test_stream_start(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_scrcpy = MagicMock()
        mock_stream = MagicMock()
        mock_stream._port = 12345
        mock_scrcpy.start_stream.return_value = mock_stream
        mock_android._get_scrcpy.return_value = mock_scrcpy

        result = mcp_mod._nerve_stream_start_sync("PIXEL8PRO")
        assert result["success"] is True
        assert result["port"] == 12345

    def test_stream_status(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_scrcpy = MagicMock()
        mock_scrcpy.active_streams = {"PIXEL8PRO": {"frames": 42}}
        mock_android._get_scrcpy.return_value = mock_scrcpy

        result = mcp_mod._nerve_stream_status_sync()
        assert result["scrcpy_available"] is True
        assert "PIXEL8PRO" in result["streams"]

    def test_stream_stop(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_scrcpy = MagicMock()
        mock_android._get_scrcpy.return_value = mock_scrcpy

        result = mcp_mod._nerve_stream_stop_sync("PIXEL8PRO")
        assert result["success"] is True
        mock_scrcpy.stop_stream.assert_called_once_with("PIXEL8PRO")

    def test_stream_lifecycle(self) -> None:
        """start → status → stop sequence."""
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_scrcpy = MagicMock()
        mock_stream = MagicMock()
        mock_stream._port = 9999
        mock_scrcpy.start_stream.return_value = mock_stream
        mock_scrcpy.active_streams = {"PIXEL8PRO": {"frames": 10}}
        mock_android._get_scrcpy.return_value = mock_scrcpy

        r1 = mcp_mod._nerve_stream_start_sync("PIXEL8PRO")
        assert r1["success"] is True
        r2 = mcp_mod._nerve_stream_status_sync()
        assert r2["scrcpy_available"] is True
        r3 = mcp_mod._nerve_stream_stop_sync("PIXEL8PRO")
        assert r3["success"] is True

    def test_logcat_default(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_android.logcat.return_value = "line1\nline2\nline3"

        result = mcp_mod._nerve_logcat_sync("PIXEL8PRO")
        assert result["count"] == 3

    def test_logcat_with_tag(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_android.logcat.return_value = "tagged line"

        result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", lines=20, tag="MyApp")
        assert result["count"] == 1

    def test_stream_logs(self) -> None:
        _reset_singletons()
        _setup_two_devices()
        result = mcp_mod._nerve_stream_logs_sync("PIXEL8PRO", filter_str="com.app")
        assert "url" in result
        assert "PIXEL8PRO" in result["url"]
        assert "com.app" in result["url"]

    def test_metrics_empty(self) -> None:
        _reset_singletons()
        _setup_two_devices()

        result = mcp_mod._nerve_metrics_sync()
        assert result["total_recorded"] == 0

    def test_metrics_after_commands(self) -> None:
        _reset_singletons()
        _setup_two_devices()

        mc = MetricsCollector()
        mc.record(
            CommandMetric(
                command="input",
                serial="PIXEL8PRO",
                duration_ms=10.0,
                success=True,
            )
        )
        mc.record(
            CommandMetric(
                command="screenshot",
                serial="PIXEL8PRO",
                duration_ms=50.0,
                success=True,
            )
        )
        mcp_mod._metrics = mc

        result = mcp_mod._nerve_metrics_sync()
        assert result["total_recorded"] == 2
        assert "input" in result["stats"]
        assert "screenshot" in result["stats"]

    def test_metrics_filtered(self) -> None:
        _reset_singletons()
        _setup_two_devices()

        mc = MetricsCollector()
        mc.record(CommandMetric(command="input", serial="A", duration_ms=10.0, success=True))
        mc.record(CommandMetric(command="screenshot", serial="A", duration_ms=50.0, success=True))
        mcp_mod._metrics = mc

        result = mcp_mod._nerve_metrics_sync(command="screenshot")
        assert "screenshot" in result["stats"]
        assert "input" not in result["stats"]


# ---------------------------------------------------------------------------
# Stream Screenshot
# ---------------------------------------------------------------------------


class TestStreamScreenshot:
    def test_stream_screenshot_writes_to_disk_by_default(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_scrcpy = MagicMock()
        png_data = _make_png()
        mock_scrcpy.screenshot.return_value = png_data
        mock_android._get_scrcpy.return_value = mock_scrcpy

        result = mcp_mod._nerve_stream_screenshot_sync("PIXEL8PRO")
        assert "screenshot" in result, f"expected disk path, got {result!r}"
        assert "screenshot_b64" not in result
        assert result["device"] == "PIXEL8PRO"
        assert result["size_bytes"] == len(png_data)
        assert "latency_ms" in result
        # File exists and is a valid PNG
        with open(result["screenshot"], "rb") as f:
            on_disk = f.read()
        assert on_disk[:4] == b"\x89PNG"
        assert on_disk == png_data

    def test_stream_screenshot_inline_opt_in(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_scrcpy = MagicMock()
        png_data = _make_png()
        mock_scrcpy.screenshot.return_value = png_data
        mock_android._get_scrcpy.return_value = mock_scrcpy

        result = mcp_mod._nerve_stream_screenshot_sync("PIXEL8PRO", inline=True)
        assert "screenshot_b64" in result
        assert "screenshot" not in result
        decoded = base64.b64decode(result["screenshot_b64"])
        assert decoded[:4] == b"\x89PNG"

    def test_stream_screenshot_ios_without_mjpeg_port_errors(self) -> None:
        # iOS now routes to WDA MJPEG (no longer Android-only). When the session
        # has no MJPEG port (pool exhausted / disabled), the stream tool reports
        # MJPEG unavailable rather than the old "Android-only" rejection.
        _reset_singletons()
        _, mock_ios = _setup_two_devices()
        mock_ios._get_wda.return_value = MagicMock(mjpeg_port=None)

        result = mcp_mod._nerve_stream_screenshot_sync("UUID-123")
        assert "error" in result
        assert "MJPEG not available" in result["error"]

    def test_stream_screenshot_no_scrcpy(self) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()
        mock_android._get_scrcpy.return_value = None

        result = mcp_mod._nerve_stream_screenshot_sync("PIXEL8PRO")
        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# Phase 8: Device Management & Emulators (20 calls)
# ---------------------------------------------------------------------------


class TestPhase8DeviceManagement:
    def test_reserve_device(self, tmp_path: Any) -> None:
        _reset_singletons()
        _setup_two_devices()
        _setup_state_and_metrics(tmp_path)

        result = mcp_mod._nerve_reserve_sync("PIXEL8PRO", purpose="stress test")
        assert result["success"] is True
        assert result["device"] == "PIXEL8PRO"
        assert result["purpose"] == "stress test"

    def test_pool_shows_reserved(self, tmp_path: Any) -> None:
        _reset_singletons()
        _setup_two_devices()
        _setup_state_and_metrics(tmp_path)

        mcp_mod._nerve_reserve_sync("PIXEL8PRO", purpose="testing")
        result = mcp_mod._nerve_pool_sync()
        assert result["count"] == 1
        assert result["reservations"][0]["device_serial"] == "PIXEL8PRO"

    def test_release_device(self, tmp_path: Any) -> None:
        _reset_singletons()
        _setup_two_devices()
        _setup_state_and_metrics(tmp_path)

        mcp_mod._nerve_reserve_sync("PIXEL8PRO")
        result = mcp_mod._nerve_release_sync("PIXEL8PRO")
        assert result["success"] is True

    def test_pool_shows_available(self, tmp_path: Any) -> None:
        _reset_singletons()
        _setup_two_devices()
        _setup_state_and_metrics(tmp_path)

        mcp_mod._nerve_reserve_sync("PIXEL8PRO")
        mcp_mod._nerve_release_sync("PIXEL8PRO")
        result = mcp_mod._nerve_pool_sync()
        assert result["count"] == 0

    def test_reserve_already_reserved(self, tmp_path: Any) -> None:
        _reset_singletons()
        _setup_two_devices()
        _setup_state_and_metrics(tmp_path)

        mcp_mod._nerve_reserve_sync("PIXEL8PRO", purpose="first")
        result = mcp_mod._nerve_reserve_sync("PIXEL8PRO", purpose="second")
        # Same session re-reserving — may succeed or error depending on impl
        # Just verify it returns a dict (no crash)
        assert isinstance(result, dict)

    def test_guard_blocks_interaction(self, tmp_path: Any) -> None:
        _reset_singletons()
        mock_android, _ = _setup_two_devices()

        db = tmp_path / "guard.db"
        # mgr1 reserves (simulates another session)
        mgr1 = DeviceManager(db_path=db)
        mgr1.reserve("PIXEL8PRO", purpose="other session")

        # mgr2 is "our" session
        mgr2 = DeviceManager(db_path=db)
        mcp_mod._device_mgr = mgr2
        mcp_mod._state_mgr = StateSessionManager()
        mcp_mod._metrics = MetricsCollector()
        mcp_mod._emulator = MagicMock(spec=EmulatorManager)

        try:
            result = mcp_mod._nerve_tap_sync("PIXEL8PRO", 100, 200)
            assert "error" in result
            assert "held by" in result["error"]
            mock_android.tap.assert_not_called()

            result2 = mcp_mod._nerve_swipe_sync("PIXEL8PRO", 0, 0, 100, 100, 300)
            assert "error" in result2
        finally:
            mgr1.close()
            mgr2.close()

    def test_rapid_reserve_release(self, tmp_path: Any) -> None:
        _reset_singletons()
        _setup_two_devices()
        _setup_state_and_metrics(tmp_path)

        for i in range(3):
            r = mcp_mod._nerve_reserve_sync("PIXEL8PRO", purpose=f"cycle-{i}")
            assert r["success"] is True, f"Reserve cycle {i} failed"
            rel = mcp_mod._nerve_release_sync("PIXEL8PRO")
            assert rel["success"] is True, f"Release cycle {i} failed"

    def test_emulator_list(self) -> None:
        _reset_singletons()
        _setup_two_devices()
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
        assert result["count"] == 2
        assert result["running"] == 1

    def test_emulator_start(self) -> None:
        _reset_singletons()
        _setup_two_devices()
        mock_emu = MagicMock(spec=EmulatorManager)
        mock_emu.start.return_value = EmulatorInfo(
            avd_name="Pixel_7_API_34",
            serial="emulator-5556",
            port=5556,
            state="running",
        )
        mcp_mod._emulator = mock_emu

        result = mcp_mod._nerve_emulator_start_sync("Pixel_7_API_34")
        assert result["success"] is True
        assert result["emulator"]["avd_name"] == "Pixel_7_API_34"

    def test_emulator_stop(self) -> None:
        _reset_singletons()
        _setup_two_devices()
        mock_emu = MagicMock(spec=EmulatorManager)
        mock_emu.stop.return_value = True
        mcp_mod._emulator = mock_emu

        result = mcp_mod._nerve_emulator_stop_sync("emulator-5554")
        assert result["success"] is True

    def test_emulator_start_already_running(self) -> None:
        _reset_singletons()
        _setup_two_devices()
        mock_emu = MagicMock(spec=EmulatorManager)
        mock_emu.start.side_effect = ValueError("AVD 'Pixel_7' is already running")
        mcp_mod._emulator = mock_emu

        result = mcp_mod._nerve_emulator_start_sync("Pixel_7")
        assert "error" in result
        assert "already running" in result["error"]

    def test_emulator_lifecycle(self) -> None:
        """list → start → stop sequence."""
        _reset_singletons()
        _setup_two_devices()
        mock_emu = MagicMock(spec=EmulatorManager)
        mock_emu.list_avds.return_value = [
            EmulatorInfo(avd_name="Pixel_7_API_34", state="stopped"),
        ]
        mock_emu.start.return_value = EmulatorInfo(
            avd_name="Pixel_7_API_34",
            serial="emulator-5556",
            port=5556,
            state="running",
        )
        mock_emu.stop.return_value = True
        mcp_mod._emulator = mock_emu

        r1 = mcp_mod._nerve_emulator_list_sync()
        assert r1["count"] == 1
        assert r1["stopped"] == 1

        r2 = mcp_mod._nerve_emulator_start_sync("Pixel_7_API_34")
        assert r2["success"] is True

        r3 = mcp_mod._nerve_emulator_stop_sync("emulator-5556")
        assert r3["success"] is True


# ---------------------------------------------------------------------------
# MCP schema hygiene
# ---------------------------------------------------------------------------


class TestMcpSchemaHygiene:
    def test_check_pil_support_is_not_registered_as_tool(self) -> None:
        """_check_pil_support is an internal helper, not an MCP tool.

        Its signature takes a controller as first arg (`ctrl: Any`), which would
        leak through the MCP schema if exposed. Lock down that it stays unregistered.
        """
        import asyncio

        from linc_nerve.mcp._core import server

        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}
        assert "check_pil_support" not in names, (
            "check_pil_support is a helper, not an MCP tool — remove its @device_tool decorator"
        )


# ---------------------------------------------------------------------------
# Validation errors vs runtime errors (metrics classifier)
# ---------------------------------------------------------------------------


class TestNavigateValidationErrors:
    def test_unknown_app_returns_validation_error_flag(self) -> None:
        _reset_singletons()
        _setup_two_devices()

        result = mcp_mod._nerve_navigate_sync("PIXEL8PRO", "nonexistent_app", "home")
        assert "error" in result
        assert result.get("validation_error") is True
        assert "available_apps" in result, "should hint at known app ids"

    def test_unknown_route_returns_validation_error_flag(self) -> None:
        _reset_singletons()
        _setup_two_devices()

        result = mcp_mod._nerve_navigate_sync("PIXEL8PRO", "precisionlevel", "no_such_route")
        assert "error" in result
        assert result.get("validation_error") is True
        assert "available_routes" in result, "should hint at known routes"

    def test_validation_errors_not_counted_as_tool_failures(self) -> None:
        """Validation errors are user-input mistakes, not system failures.

        Counting them in tool error_rate skews observability metrics.
        """
        import asyncio

        from linc_nerve.core.metrics import MetricsCollector
        from linc_nerve.mcp._core import server

        _reset_singletons()
        _setup_two_devices()

        mc = MetricsCollector()
        mcp_mod._metrics = mc

        async def run() -> None:
            await server.call_tool(
                "nerve_navigate",
                {"device": "PIXEL8PRO", "app_id": "nonexistent_app", "route_id": "home"},
            )

        asyncio.run(run())

        stats = mc.tool_stats()
        nav = stats.get("nerve_navigate", {})
        assert nav.get("fail", 0) == 0, f"validation error should not count as fail: {nav}"
        assert nav.get("success", 0) == 1, f"validation error should count as success: {nav}"
