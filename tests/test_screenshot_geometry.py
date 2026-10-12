"""_image_size / _screenshot_geometry — never-raising pixel-geometry helpers
(reactor 1cac8e2f)."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from PIL import Image as PILImage

from linc_nerve.mcp import ui_tools
from linc_nerve.mcp._core import _image_size, _screenshot_geometry


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), "orange").save(buf, format="PNG")
    return buf.getvalue()


class TestImageSize:
    def test_undecodable_bytes_return_none(self) -> None:
        assert _image_size(b"not a png") is None

    def test_decodes_png_dimensions(self) -> None:
        assert _image_size(_png(300, 600)) == (300, 600)


class TestScreenshotGeometry:
    def test_scale_computed_from_screen_size(self) -> None:
        geometry = _screenshot_geometry(_png(540, 1200), {"width": 1080, "height": 2400})
        assert geometry == {
            "screenshot_width": 540,
            "screenshot_height": 1200,
            "scale": 2.0,
        }

    def test_no_scale_key_when_screen_size_empty(self) -> None:
        geometry = _screenshot_geometry(_png(540, 1200), {})
        assert "scale" not in geometry
        assert geometry["screenshot_width"] == 540
        assert geometry["screenshot_height"] == 1200

    def test_no_scale_key_when_screen_size_width_not_positive(self) -> None:
        geometry = _screenshot_geometry(_png(540, 1200), {"width": 0, "height": 2400})
        assert "scale" not in geometry

    def test_undecodable_data_yields_none_dimensions_and_no_scale(self) -> None:
        geometry = _screenshot_geometry(b"not a png", {"width": 1080, "height": 2400})
        assert geometry == {"screenshot_width": None, "screenshot_height": None}


class TestNerveStateGeometry:
    """Integration: _nerve_state_sync surfaces screenshot geometry + scale."""

    def test_state_reports_scale_and_screenshot_dimensions(self) -> None:
        screenshot = _png(540, 1200)
        android = MagicMock()
        android.get_foreground.return_value = {"package": "p", "activity": "a"}
        android.get_orientation.return_value = "portrait"
        android.get_screen_size.return_value = {"width": 1080, "height": 2400}
        android.elements.return_value = []
        android.screenshot.return_value = screenshot

        with (
            patch.object(
                ui_tools._core, "_ensure_initialized", return_value=(android, MagicMock())
            ),
            patch.object(ui_tools._core, "_resolve_device", return_value=(object(), "android")),
            patch.object(ui_tools._core, "_check_guard", return_value=None),
        ):
            result = ui_tools._nerve_state_sync("S")

        assert result["scale"] == 2.0
        assert result["screenshot_width"] == 540
        assert result["screenshot_height"] == 1200
        assert result["screenshot_size"] == len(screenshot)  # unchanged shape
