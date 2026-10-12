"""Android screenshot capture — scrcpy fast path, screencap, emulator fallback, downscale.

Split from android.py (#53) as a mixin to keep both files under the hot-file LOC
ceiling. References to test-patched / android.py module-globals (``subprocess``,
``time``, ``server_available``, ``ScrcpyManager``, ``ADB_PATH``, ``logger``,
constants) are routed through the parent module (``_android.<name>``) so patches
on ``linc_nerve.core.android.*`` keep working after the move.
"""

from __future__ import annotations

from typing import Any

from . import android as _android

# Inline MCP image-content size budget (linc-nerve#97): the caller-visible
# base64 payload must stay under this so nerve_screenshot's inline
# ImageContent block never blows the MCP result cap. 768 KiB.
_INLINE_MAX_B64_BYTES = 768 * 1024
_INLINE_MAX_DIM = 1280  # long-edge px cap before a PNG re-encode is attempted
_INLINE_JPEG_QUALITY = 80


def _b64_len(raw: bytes) -> int:
    return len(_android.base64.b64encode(raw))


def encode_inline_screenshot(data: bytes) -> tuple[bytes, str]:
    """Size-budget a screenshot for inline MCP delivery.

    Returns ``(encoded_bytes, format)`` where ``format`` is ``"png"`` or
    ``"jpeg"`` and the base64-encoded payload is guaranteed to be
    <= ``_INLINE_MAX_B64_BYTES``.

    Algorithm (linc-nerve#97): if the source PNG already fits the budget,
    return it unchanged. Otherwise downscale to a ``_INLINE_MAX_DIM``-px long
    edge and re-encode as PNG. If that still exceeds budget, fall back to
    JPEG at ``_INLINE_JPEG_QUALITY``.
    """
    if _b64_len(data) <= _INLINE_MAX_B64_BYTES:
        return data, "png"

    from PIL import Image as PILImage

    img = PILImage.open(_android.io.BytesIO(data))
    long_edge = max(img.width, img.height)
    if long_edge > _INLINE_MAX_DIM:
        scale = _INLINE_MAX_DIM / long_edge
        new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        img = img.resize(new_size, PILImage.LANCZOS)

    buf = _android.io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()
    if _b64_len(png_bytes) <= _INLINE_MAX_B64_BYTES:
        return png_bytes, "png"

    buf2 = _android.io.BytesIO()
    img.convert("RGB").save(buf2, format="JPEG", quality=_INLINE_JPEG_QUALITY)
    return buf2.getvalue(), "jpeg"


class _ScreenshotMixin:
    """Screenshot capture methods for AndroidController."""

    def _get_scrcpy(self) -> Any:
        """Get the scrcpy manager, checking availability once."""
        if self._scrcpy_available is None:
            self._scrcpy_available = _android.server_available()
            if self._scrcpy_available:
                self._scrcpy = _android.ScrcpyManager()
                _android.logger.info("scrcpy_available")
            else:
                _android.logger.info("scrcpy_not_available", hint="screencap fallback active")
        return self._scrcpy

    def screenshot(self, serial: str, *, full_resolution: bool = False) -> bytes:
        """Capture a PNG screenshot.

        Tries scrcpy (fast, ~35-70ms) first **without** the device lock
        (scrcpy reads a cached frame via its own internal lock).  Falls back
        to adbutils screencap under the device lock.  On emulators, detects
        transparent/blank captures (<20KB) and falls back to
        ``adb emu screenrecord screenshot``.

        By default, large screenshots are downscaled to keep MCP responses
        under ~1 MB (prevents Claude Code client stalls).  Set
        *full_resolution=True* to return the original capture unmodified.

        scrcpy frames are capped at ``scrcpy._MAX_SIZE`` (1080px) regardless
        of the true device resolution, so *full_resolution=True* skips the
        scrcpy fast path entirely and goes straight to the screencap slow
        path — otherwise "full resolution" would silently mean "1080px max"
        (reactor 1cac8e2f).
        """
        t0 = _android.time.monotonic()

        # --- Fast path: scrcpy (no device lock) — skipped for full_resolution ---
        if not full_resolution:
            scrcpy = self._get_scrcpy()
            if scrcpy is not None and not scrcpy.is_open(serial):
                try:
                    data = self._hard_timeout(
                        scrcpy.screenshot,
                        args=(serial,),
                        timeout=15,
                        label=f"scrcpy-{serial[:8]}",
                    )
                    self._record_metric("screenshot", serial, t0, success=True)
                    return self._downscale_if_needed(data)
                except Exception as exc:
                    _android.logger.debug(
                        "scrcpy_screenshot_failed",
                        serial=serial,
                        error=str(exc),
                    )
                    # Fall through to screencap

        # --- Slow path: screencap (needs device lock) ---
        try:
            with self._device_lock_timeout(serial, timeout=30):
                data = self._screencap_screenshot(serial)
                self._record_metric("screenshot", serial, t0, success=True)
                return data if full_resolution else self._downscale_if_needed(data)
        except Exception as exc:
            self._record_metric("screenshot", serial, t0, success=False, error=exc)
            raise

    def _screencap_screenshot(self, serial: str) -> bytes:
        """Screenshot via adbutils screencap (legacy path).

        Uses ``_hard_timeout`` to prevent ``d.screenshot()`` from hanging
        indefinitely. Falls back to raw screencap on timeout.
        """
        d = self._get_device(serial)
        try:
            img = self._hard_timeout(d.screenshot, timeout=15, label=f"screencap-{serial[:8]}")
            buf = _android.io.BytesIO()
            img.save(buf, format="PNG")
            data = buf.getvalue()
        except TimeoutError:
            _android.logger.warning("screencap_timeout", serial=serial)
            data = self._raw_screencap(serial)
        except Exception:
            data = self._raw_screencap(serial)

        # Emulator transparency fallback
        if serial.startswith("emulator-") and len(data) < _android._MIN_SCREENSHOT_SIZE:
            _android.logger.info("screencap_transparent_fallback", serial=serial, size=len(data))
            fallback = self._emu_screenshot(serial)
            if fallback and len(fallback) > len(data):
                data = fallback

        return data

    @staticmethod
    def _get_scaled_image(data: bytes) -> bytes:
        from PIL import Image

        img = Image.open(_android.io.BytesIO(data))
        scale = 0.5
        while True:
            new_w = int(img.width * scale)
            new_h = int(img.height * scale)
            resized = img.resize((new_w, new_h), Image.LANCZOS)
            buf = _android.io.BytesIO()
            resized.save(buf, format="PNG", optimize=True)
            result = buf.getvalue()
            if len(result) <= _android._MAX_SCREENSHOT_BYTES or scale < 0.2:
                _android.logger.debug(
                    "screenshot_downscaled",
                    original_bytes=len(data),
                    scaled_bytes=len(result),
                    scale=round(scale, 2),
                    dimensions=f"{new_w}x{new_h}",
                )
                return result
            scale *= 0.75

    @staticmethod
    def _downscale_if_needed(data: bytes) -> bytes:
        """Downscale a PNG screenshot if it exceeds _MAX_SCREENSHOT_BYTES."""
        if len(data) <= _android._MAX_SCREENSHOT_BYTES:
            return data
        return _android.AndroidController._get_scaled_image(data)

    def _raw_screencap(self, serial: str) -> bytes:
        """Raw screencap via subprocess."""
        cmd = [_android.ADB_PATH, "-s", serial, "exec-out", "screencap", "-p"]
        result = _android.subprocess.run(cmd, capture_output=True, check=True, timeout=15)
        return result.stdout

    def _emu_screenshot(self, serial: str) -> bytes | None:
        """Emulator console screenshot fallback."""
        device_path = "/data/local/tmp/nerve_screenshot.png"
        try:
            _android.subprocess.run(
                [
                    _android.ADB_PATH,
                    "-s",
                    serial,
                    "emu",
                    "screenrecord",
                    "screenshot",
                    device_path,
                ],
                capture_output=True,
                check=True,
                timeout=10,
            )
            result = _android.subprocess.run(
                [_android.ADB_PATH, "-s", serial, "exec-out", "cat", device_path],
                capture_output=True,
                check=True,
                timeout=10,
            )
            return result.stdout
        except (_android.subprocess.CalledProcessError, _android.subprocess.TimeoutExpired) as exc:
            _android.logger.debug("emu_screenshot_fallback_failed", error=str(exc))
            return None

    def screenshot_pil(self, serial: str) -> Any:
        """Try to get a PIL Image directly from scrcpy (fast path, no PNG encode).

        Returns None if scrcpy is unavailable or fails — caller should
        fall back to screenshot() bytes path.
        """
        scrcpy = self._get_scrcpy()
        if scrcpy is None or scrcpy.is_open(serial):
            return None
        try:
            stream = scrcpy.start_stream(serial)
            return stream.screenshot_pil()
        except Exception:
            return None
