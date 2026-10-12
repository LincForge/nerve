"""iOS stream-tool routing + status aggregation (mocked controllers)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from linc_nerve.mcp import screenshot_tools


def test_stream_screenshot_routes_ios_to_mjpeg():
    ios_ctrl = MagicMock()
    ios_ctrl._mjpeg.screenshot.return_value = b"MJPEGPNG"
    session = MagicMock(mjpeg_port=9100)
    ios_ctrl._get_wda.return_value = session
    with (
        patch.object(
            screenshot_tools._core,
            "_ensure_initialized",
            return_value=(MagicMock(), MagicMock()),
        ),
        patch.object(screenshot_tools._core, "_resolve_device", return_value=(object(), "ios")),
        patch.object(screenshot_tools._core, "_check_guard", return_value=None),
        patch.object(screenshot_tools._core, "_get_controller", return_value=ios_ctrl),
        patch.object(screenshot_tools._core, "_metrics", None),
    ):
        result = screenshot_tools._nerve_stream_screenshot_sync("sim-serial", inline=True)
    assert "screenshot_b64" in result
    ios_ctrl._mjpeg.screenshot.assert_called_once_with("sim-serial", 9100)


def test_status_aggregates_both_managers():
    android = MagicMock()
    android._get_scrcpy.return_value.active_streams = {"andser": {"transport": "scrcpy"}}
    ios_ctrl = MagicMock()
    ios_ctrl._mjpeg.active_streams = {"iosser": {"transport": "wda-mjpeg"}}
    with (
        patch.object(
            screenshot_tools._core,
            "_ensure_initialized",
            return_value=(android, MagicMock()),
        ),
        patch.object(screenshot_tools._core, "_get_controller", return_value=ios_ctrl),
    ):
        result = screenshot_tools._nerve_stream_status_sync()
    assert "andser" in result["streams"]
    assert "iosser" in result["streams"]


# --- direct helper coverage (keeps the tool functions simple) ---


def _patch_ios(ios_ctrl):
    return patch.object(screenshot_tools._core, "_get_controller", return_value=ios_ctrl)


def test_ios_mjpeg_port_reads_session():
    ios_ctrl = MagicMock()
    ios_ctrl._get_wda.return_value = MagicMock(mjpeg_port=9101)
    with _patch_ios(ios_ctrl):
        assert screenshot_tools._ios_mjpeg_port("dev") == 9101


def test_ios_stream_start_ok():
    ios_ctrl = MagicMock()
    ios_ctrl._get_wda.return_value = MagicMock(mjpeg_port=9100)
    with _patch_ios(ios_ctrl):
        out = screenshot_tools._ios_stream_start("dev")
    assert out["success"] is True
    assert out["port"] == 9100
    ios_ctrl._mjpeg.start_stream.assert_called_once_with("dev", 9100)


def test_ios_stream_start_no_port():
    ios_ctrl = MagicMock()
    ios_ctrl._get_wda.return_value = MagicMock(mjpeg_port=None)
    with _patch_ios(ios_ctrl):
        out = screenshot_tools._ios_stream_start("dev")
    assert "MJPEG not available" in out["error"]


def test_ios_stream_start_raises_returns_error():
    ios_ctrl = MagicMock()
    ios_ctrl._get_wda.return_value = MagicMock(mjpeg_port=9100)
    ios_ctrl._mjpeg.start_stream.side_effect = RuntimeError("boom")
    with _patch_ios(ios_ctrl):
        out = screenshot_tools._ios_stream_start("dev")
    assert "Stream start failed" in out["error"]


def test_ios_stream_stop_stops_matching_serial():
    ios_ctrl = MagicMock()
    ios_ctrl._mjpeg._streams = {("dev", 9100): object(), ("other", 9101): object()}
    with _patch_ios(ios_ctrl):
        out = screenshot_tools._ios_stream_stop("dev")
    assert out["success"] is True
    ios_ctrl._mjpeg.stop_stream.assert_called_once_with("dev", 9100)


def test_ios_stream_frame_ok():
    ios_ctrl = MagicMock()
    ios_ctrl._get_wda.return_value = MagicMock(mjpeg_port=9100)
    ios_ctrl._mjpeg.screenshot.return_value = b"PNG"
    with _patch_ios(ios_ctrl):
        assert screenshot_tools._ios_stream_frame("dev") == b"PNG"


def test_ios_stream_frame_no_port_raises():
    import pytest

    ios_ctrl = MagicMock()
    ios_ctrl._get_wda.return_value = MagicMock(mjpeg_port=None)
    with _patch_ios(ios_ctrl), pytest.raises(RuntimeError, match="MJPEG not available"):
        screenshot_tools._ios_stream_frame("dev")


def test_ios_active_streams_swallows_errors():
    with patch.object(screenshot_tools._core, "_get_controller", side_effect=RuntimeError):
        assert screenshot_tools._ios_active_streams() == {}


# --- width/height on the screenshot result (reactor 1cac8e2f) ---


def test_screenshot_sync_full_resolution_reports_width_height():
    import io

    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", (1008, 2244), "teal").save(buf, format="PNG")
    uncapped = buf.getvalue()

    android_ctrl = MagicMock()
    android_ctrl.screenshot.return_value = uncapped

    with (
        patch.object(
            screenshot_tools._core,
            "_ensure_initialized",
            return_value=(android_ctrl, MagicMock()),
        ),
        patch.object(
            screenshot_tools._core, "_resolve_device", return_value=(object(), "android")
        ),
        patch.object(screenshot_tools._core, "_check_guard", return_value=None),
        patch.object(screenshot_tools._core, "_get_controller", return_value=android_ctrl),
        patch.object(screenshot_tools._core, "_metrics", None),
    ):
        result = screenshot_tools._nerve_screenshot_sync("S", full_resolution=True)

    assert result["width"] == 1008
    assert result["height"] == 2244
    assert result["screenshot"]
    assert result["device"] == "S"
    assert result["size_bytes"] == len(uncapped)
    android_ctrl.screenshot.assert_called_once_with("S", full_resolution=True)
