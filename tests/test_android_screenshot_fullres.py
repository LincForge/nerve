"""full_resolution=True must skip the 1080-capped scrcpy fast path entirely
and go straight to the screencap slow path (reactor 1cac8e2f)."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from PIL import Image as PILImage

from linc_nerve.core.android import AndroidController


def _make_controller() -> AndroidController:
    ctrl = AndroidController()
    ctrl._scrcpy_available = False  # scrcpy availability probe not exercised here
    return ctrl


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), "teal").save(buf, format="PNG")
    return buf.getvalue()


class TestFullResolutionSkipsScrcpy:
    def test_full_resolution_skips_scrcpy_and_returns_uncapped_frame(self) -> None:
        ctrl = _make_controller()

        capped = _png(485, 1080)  # scrcpy's 1080-max frame
        uncapped = _png(1008, 2244)  # true device resolution

        fake_scrcpy = MagicMock()
        fake_scrcpy.is_open.return_value = False
        fake_scrcpy.screenshot.return_value = capped

        with (
            patch.object(ctrl, "_get_scrcpy", return_value=fake_scrcpy) as get_scrcpy,
            patch.object(ctrl, "_screencap_screenshot", return_value=uncapped),
        ):
            data = ctrl.screenshot("S", full_resolution=True)

        img = PILImage.open(io.BytesIO(data))
        assert (img.width, img.height) == (1008, 2244)
        fake_scrcpy.screenshot.assert_not_called()
        # The fast path is skipped entirely — _get_scrcpy is never consulted.
        get_scrcpy.assert_not_called()

    def test_default_still_prefers_scrcpy(self) -> None:
        """Pin: without full_resolution, the fast path is tried first."""
        ctrl = _make_controller()

        capped = _png(485, 1080)
        uncapped = _png(1008, 2244)

        fake_scrcpy = MagicMock()
        fake_scrcpy.is_open.return_value = False
        fake_scrcpy.screenshot.return_value = capped

        with (
            patch.object(ctrl, "_get_scrcpy", return_value=fake_scrcpy),
            patch.object(ctrl, "_screencap_screenshot", return_value=uncapped),
        ):
            data = ctrl.screenshot("S")

        fake_scrcpy.screenshot.assert_called_once()
        img = PILImage.open(io.BytesIO(data))
        assert (img.width, img.height) == (485, 1080)
