"""nerve_screenshot inline MCP image content (linc-nerve#97).

TDD coverage for two pieces:

1. ``linc_nerve.core.android_screenshot.encode_inline_screenshot`` — the
   size-budget algorithm (downscale to a 1280px long edge, fall back to
   JPEG quality 80 if still over budget).
2. ``linc_nerve.mcp._core._wrap_inline_image`` — the MCP-boundary hook that
   turns a dict carrying raw screenshot bytes (``_nerve_screenshot_sync``'s
   ``_INLINE_IMAGE_KEY`` sentinel) into ``[dict, Image]`` content blocks, so
   FastMCP emits an inline ``ImageContent`` block alongside the existing
   text/JSON result — without changing the plain-dict return contract that
   direct callers (this repo's own tests, ``navigation_tools.py``'s
   fallback path) rely on.

AUDIT RECEIPT (size-budget fixture, pasted into the PR description per the
issue's audit-receipt AC): a synthetic 2400x3200 photo-like PNG (random
per-pixel noise + a light Gaussian blur, so it defeats both PNG's delta
compression and doesn't trivially flatten under JPEG) encodes to a ~12.4 MB
source PNG. ``encode_inline_screenshot`` downscales it to a 1280px long
edge, and — because the downscaled PNG re-encode is still over budget —
falls back to JPEG quality 80, landing at a base64 payload of ~353 KB,
comfortably under the 768 KiB (786432 byte) hard budget.
"""

from __future__ import annotations

import base64
import io
import random

from PIL import Image as PILImage
from PIL import ImageFilter

# Must be imported before android_screenshot: android.py's own import of
# _ScreenshotMixin from android_screenshot needs to finish first, or a
# direct `import linc_nerve.core.android_screenshot` triggers a circular
# partial-init ImportError (android_screenshot -> android -> android_screenshot).
from linc_nerve.core.android import AndroidController
from linc_nerve.core.android_screenshot import (
    _INLINE_MAX_B64_BYTES,
    encode_inline_screenshot,
)
from linc_nerve.mcp import _core

_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_JPEG_HEADER = b"\xff\xd8\xff"


def _make_png(width: int, height: int, color: tuple[int, int, int] = (120, 40, 200)) -> bytes:
    img = PILImage.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_oversized_photo_like_png(width: int = 2400, height: int = 3200) -> bytes:
    """A synthetic screenshot large enough (>> 768 KB) to force the fallback path.

    Random per-pixel noise + a light blur mimics photo/gradient-heavy screen
    content (per the issue: "a raw 1-4 MB device PNG") — large as PNG,
    unlike a flat-color fixture which PNG would compress away to nothing.
    """
    random.seed(1234)
    raw = random.randbytes(width * height * 3)
    img = PILImage.frombytes("RGB", (width, height), raw)
    img = img.filter(ImageFilter.GaussianBlur(radius=1.5))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestEncodeInlineScreenshot:
    """Size-budget algorithm: downscale to 1280px long edge, else JPEG q80."""

    def test_small_screenshot_passes_through_unchanged_as_png(self) -> None:
        png = _make_png(540, 960)
        encoded, fmt = encode_inline_screenshot(png)
        assert fmt == "png"
        assert encoded == png

    def test_oversized_screenshot_stays_within_size_budget(self) -> None:
        """[test] SIZE BUDGET (hard AC): len(base64 payload) <= 786432."""
        png = _make_oversized_photo_like_png()
        assert len(base64.b64encode(png)) > _INLINE_MAX_B64_BYTES, (
            "fixture must actually exceed the budget pre-encode to exercise the fallback"
        )

        encoded, fmt = encode_inline_screenshot(png)
        b64_payload = base64.b64encode(encoded)

        assert len(b64_payload) <= 786432
        assert fmt in ("png", "jpeg")
        if fmt == "png":
            assert encoded[:8] == _PNG_HEADER
        else:
            assert encoded[:3] == _JPEG_HEADER

    def test_oversized_screenshot_downscaled_to_max_dimension(self) -> None:
        png = _make_oversized_photo_like_png()
        encoded, _fmt = encode_inline_screenshot(png)
        img = PILImage.open(io.BytesIO(encoded))
        assert max(img.width, img.height) <= 1280


class TestWrapInlineImage:
    """_core._wrap_inline_image: the MCP-boundary [dict, Image] conversion."""

    def _result_with_inline_bytes(self, png: bytes) -> dict:
        return {
            "screenshot": "/tmp/nerve-output/PIXEL8PRO_screenshot_1.png",
            "device": "PIXEL8PRO",
            "size_bytes": len(png),
            _core._INLINE_IMAGE_KEY: png,
        }

    def test_response_content_list_contains_image_block(self) -> None:
        """[test] response content includes an inline ImageContent block

        whose data decodes to a valid PNG header, pinned to mime 'image/png'.
        """
        png = _make_png(540, 960)
        content = _core._wrap_inline_image(self._result_with_inline_bytes(png))

        assert len(content) == 2
        image_content = content[1].to_image_content()
        assert image_content.mimeType == "image/png"
        decoded = base64.b64decode(image_content.data)
        assert decoded[:8] == _PNG_HEADER

    def test_path_field_remains_present_alongside_image_block(self) -> None:
        """[test] backward compatibility: the path field survives alongside the image block."""
        png = _make_png(540, 960)
        content = _core._wrap_inline_image(self._result_with_inline_bytes(png))

        dict_block = content[0]
        assert dict_block["screenshot"] == "/tmp/nerve-output/PIXEL8PRO_screenshot_1.png"
        assert dict_block["device"] == "PIXEL8PRO"
        # Sentinel must not leak into the wire response.
        assert _core._INLINE_IMAGE_KEY not in dict_block

    def test_oversized_result_produces_budgeted_jpeg_or_png_block(self) -> None:
        png = _make_oversized_photo_like_png()
        content = _core._wrap_inline_image(self._result_with_inline_bytes(png))

        image_content = content[1].to_image_content()
        assert image_content.mimeType in ("image/png", "image/jpeg")
        assert len(image_content.data) <= 786432
        decoded = base64.b64decode(image_content.data)
        assert decoded[:8] == _PNG_HEADER or decoded[:3] == _JPEG_HEADER


class TestDeviceToolWrapperInlineImageBranch:
    """Drives the ``device_tool`` async ``wrapper`` (not ``_wrap_inline_image``
    directly) end-to-end through FastMCP's ``server.call_tool``, so the
    wrapper's own ``if isinstance(result, dict) and _INLINE_IMAGE_KEY in
    result`` branch (both sides) gets exercised — closing the CRAP-ratchet
    coverage gap ``TestWrapInlineImage`` above left on ``device_tool`` itself.
    """

    def _reset_and_setup(self, png: bytes) -> tuple:
        import contextlib
        from unittest.mock import MagicMock

        from linc_nerve.core.device import DeviceType, NerveDevice, Platform
        from linc_nerve.core.device_manager import DeviceManager
        from linc_nerve.core.emulator import EmulatorManager
        from linc_nerve.core.ios import IOSController
        from linc_nerve.core.metrics import MetricsCollector
        from linc_nerve.core.state_engine import StateSessionManager

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

        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [
            NerveDevice(
                serial="PIXEL8PRO",
                platform=Platform.ANDROID,
                device_type=DeviceType.PHONE,
                model="Pixel 8 Pro",
                os_version="14",
            )
        ]
        mock_android.screenshot.return_value = png
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        _core._android = mock_android
        _core._ios = mock_ios
        _core._state_mgr = StateSessionManager()
        _core._metrics = MagicMock(spec=MetricsCollector)
        _core._device_mgr = MagicMock(spec=DeviceManager)
        _core._device_mgr.guard.return_value = None
        _core._emulator = MagicMock(spec=EmulatorManager)
        return mock_android, mock_ios

    def test_wrapper_returns_inline_image_content_when_sentinel_present(self) -> None:
        """[test] sentinel branch: nerve_screenshot's dict result carries
        ``_INLINE_IMAGE_KEY`` (set by ``_nerve_screenshot_sync``), so the
        ``device_tool`` wrapper itself — not just ``_wrap_inline_image`` in
        isolation — must route into the inline-image conversion and hand
        FastMCP a ``[dict, Image]`` pair that converts to a text block plus
        an ``ImageContent`` block.
        """
        import asyncio

        # Registers nerve_screenshot with FastMCP (decorator runs at import
        # time) — needed when this file runs standalone, independent of test
        # execution order within the file/class below.
        from linc_nerve.mcp.screenshot_tools import _nerve_screenshot_sync  # noqa: F401

        png = _make_png(540, 960)
        self._reset_and_setup(png)

        content = asyncio.run(
            _core.server.call_tool("nerve_screenshot", {"device": "PIXEL8PRO"})
        )

        assert len(content) == 2
        text_block, image_block = content
        assert getattr(image_block, "type", None) == "image"
        decoded = base64.b64decode(image_block.data)
        assert decoded[:8] == _PNG_HEADER
        # Sentinel must never leak onto the wire — only checkable via the
        # text/JSON block since the sentinel key isn't a valid dict anymore
        # post-conversion; assert the surviving fields are present instead.
        assert "screenshot" in text_block.text
        assert _core._INLINE_IMAGE_KEY not in text_block.text

    def test_wrapper_returns_plain_result_when_sentinel_absent(self) -> None:
        """[test] negative branch: a device_tool result dict WITHOUT the
        sentinel (and without "error", so the earlier error-branch return
        doesn't short-circuit first) must pass through the wrapper's
        sentinel check unchanged — the other side of the new ``if`` at
        ``device_tool``'s line 208. ``nerve_tap`` (a different @device_tool)
        returns a plain ``{"success": True, ...}`` dict on the happy path,
        which is exactly this case.
        """
        import asyncio

        # Registers nerve_tap with FastMCP (decorator runs at import time) —
        # needed when this file runs standalone, without another test module
        # having already imported linc_nerve.mcp.server (which eagerly
        # imports every tool module).
        from linc_nerve.mcp.input_tools import _nerve_tap_sync  # noqa: F401

        png = _make_png(540, 960)
        mock_android, _ = self._reset_and_setup(png)
        mock_android.tap.return_value = None

        content = asyncio.run(
            _core.server.call_tool("nerve_tap", {"device": "PIXEL8PRO", "x": 10, "y": 20})
        )
        # _nerve_tap_sync is annotated ``-> dict[str, Any]``, so FastMCP infers
        # an output schema and convert_result returns
        # (unstructured_content, structured_content) instead of a bare list
        # (unlike nerve_screenshot's ``-> Any`` above). Either way, the
        # unstructured half is what the wrapper's sentinel branch touches.
        unstructured = content[0] if isinstance(content, tuple) else content

        # No inline image block was added — just the plain text/JSON block.
        assert len(unstructured) == 1
        assert "\"success\": true" in unstructured[0].text.lower()
        assert _core._INLINE_IMAGE_KEY not in unstructured[0].text


class TestNerveScreenshotSyncSetsSentinel:
    """The dict-returning sync function stays dict-shaped for direct callers,

    but carries the sentinel the MCP wrapper needs to build the inline block.
    """

    def test_direct_call_result_carries_inline_image_sentinel(self) -> None:
        from unittest.mock import MagicMock

        from linc_nerve.core.device import DeviceType, NerveDevice, Platform
        from linc_nerve.core.device_manager import DeviceManager
        from linc_nerve.core.emulator import EmulatorManager
        from linc_nerve.core.ios import IOSController
        from linc_nerve.core.metrics import MetricsCollector
        from linc_nerve.core.state_engine import StateSessionManager
        from linc_nerve.mcp.screenshot_tools import _nerve_screenshot_sync

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

        png = _make_png(540, 960)
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [
            NerveDevice(
                serial="PIXEL8PRO",
                platform=Platform.ANDROID,
                device_type=DeviceType.PHONE,
                model="Pixel 8 Pro",
                os_version="14",
            )
        ]
        mock_android.screenshot.return_value = png
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        _core._android = mock_android
        _core._ios = mock_ios
        _core._state_mgr = MagicMock(spec=StateSessionManager)
        _core._metrics = MagicMock(spec=MetricsCollector)
        _core._device_mgr = MagicMock(spec=DeviceManager)
        _core._device_mgr.guard.return_value = None
        _core._emulator = MagicMock(spec=EmulatorManager)

        result = _nerve_screenshot_sync("PIXEL8PRO")

        # Direct callers still see a plain dict with the usual keys...
        assert isinstance(result, dict)
        assert "screenshot" in result
        # ...plus the sentinel the MCP wrapper consumes to build the inline block.
        assert result[_core._INLINE_IMAGE_KEY] == png

        # And _wrap_inline_image can consume it end-to-end.
        content = _core._wrap_inline_image(dict(result))
        assert content[1].to_image_content().mimeType == "image/png"
        assert "screenshot" in content[0]
        assert _core._INLINE_IMAGE_KEY not in content[0]
