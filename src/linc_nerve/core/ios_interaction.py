"""iOS screenshot + input — simctl/WDA screenshot fast path and tap/swipe/keys.

Split out of ``ios.py`` (#53) as a mixin to keep both files under the hot-file
LOC ceiling. ``logger`` is referenced through the parent module (``_ios.logger``)
so ``patch("linc_nerve.core.ios.logger")`` keeps working; ``subprocess`` is used
directly (the suite patches the global ``subprocess.run``).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import threading

from . import ios as _ios
from .android import AndroidController
from .device import DeviceType
from .wda import WDASession


class _InteractionMixin:
    """Screenshot + input methods for IOSController."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._device_type_cache: dict[str, DeviceType] = {}
        self._device_type_cache_lock = threading.Lock()

    @property
    def _device_type_cache(self) -> dict[str, DeviceType]:
        if not hasattr(self, "_device_type_cache_store"):
            self._device_type_cache_store: dict[str, DeviceType] = {}
        return self._device_type_cache_store

    @_device_type_cache.setter
    def _device_type_cache(self, val: dict[str, DeviceType]) -> None:
        self._device_type_cache_store = val

    @property
    def _device_type_cache_lock(self) -> threading.Lock:
        if not hasattr(self, "_device_type_cache_lock_store"):
            self._device_type_cache_lock_store = threading.Lock()
        return self._device_type_cache_lock_store

    @_device_type_cache_lock.setter
    def _device_type_cache_lock(self, val: threading.Lock) -> None:
        self._device_type_cache_lock_store = val

    # --- Screenshots ---

    def screenshot(self, serial: str, full_resolution: bool = False) -> bytes:
        """Capture a PNG screenshot from a simulator or physical device.

        Simulators try a warm WDA MJPEG stream first (15.8ms p50 vs ~212ms
        simctl), falling back to simctl. Physical devices use the WDA screenshot
        endpoint (MJPEG over a USB tunnel is a tracked follow-up).
        """
        if self._is_physical(serial):
            with self._device_lock_timeout(serial, timeout=30.0):
                return self._screenshot_physical(serial)
        return self._screenshot_simulator(serial, full_resolution)

    def _get_device_type(self, serial: str) -> DeviceType | None:
        """Lookup and cache device type for a serial via discover().

        Memoizes only resolved lookups. Misses are not cached so transient
        discovery gaps (e.g. boot-time or simctl hiccups) re-query on the next call.
        The populate step is guarded by self._device_type_cache_lock to prevent duplicate
        discover() invocations across concurrent threads.
        """
        if serial in self._device_type_cache:
            return self._device_type_cache[serial]

        with self._device_type_cache_lock:
            if serial in self._device_type_cache:
                return self._device_type_cache[serial]
            for dev in self.discover():
                if dev.serial and dev.device_type is not None:
                    self._device_type_cache[dev.serial] = dev.device_type
            return self._device_type_cache.get(serial)

    def _screenshot_simulator(self, serial: str, full_resolution: bool) -> bytes:
        """Simulator screenshot: warm MJPEG fast path, else simctl fallback."""
        if self._get_device_type(serial) != DeviceType.WATCH:
            data = self._try_mjpeg_screenshot(serial, full_resolution)
            if data is not None:
                return data
        with self._device_lock_timeout(serial, timeout=30.0):
            return self._screenshot_simctl(serial)

    def _try_mjpeg_screenshot(self, serial: str, full_resolution: bool) -> bytes | None:
        """Warm WDA MJPEG frame for a simulator, or None to fall back to simctl.

        No device lock — the stream has its own internal lock, mirroring the
        Android/scrcpy fast path.
        """
        session = self._get_wda(serial)
        port = getattr(session, "mjpeg_port", None)
        if port is None or self._mjpeg.is_open(serial):
            return None
        try:
            data = self._mjpeg.screenshot(serial, port)
        except Exception as exc:
            _ios.logger.debug("ios_mjpeg_fallback", serial=serial[:12], error=str(exc))
            return None
        return data if full_resolution else AndroidController._downscale_if_needed(data)

    def _screenshot_simctl(self, serial: str) -> bytes:
        """Capture a simulator screenshot to a temp file.

        iOS 26 sims reject `simctl io screenshot -` (stdout: 'volume is read only'),
        so we write to a real path, read it, and unlink. (Bug abf68b2c.)
        """
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            res = self._run_binary("io", serial, "screenshot", "--type=png", path)
            returncode = getattr(res, "returncode", None)
            if isinstance(returncode, int) and returncode != 0:
                raise RuntimeError(
                    f"simctl screenshot failed for {serial} with exit code {returncode}"
                )
            with open(path, "rb") as f:
                data = f.read()
            if not data:
                raise RuntimeError(f"simctl screenshot produced empty output for {serial}")
            return data
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    def _screenshot_physical(self, serial: str) -> bytes:
        """Capture screenshot from physical device via WDA."""
        import base64

        self._require_wda(serial)
        session = self._get_wda(serial)
        resp = session._request("GET", "/screenshot")
        resp.raise_for_status()
        data = resp.json()
        b64 = data.get("value", "")
        return base64.b64decode(b64)

    # --- Input ---

    def _require_wda(self, serial: str) -> None:
        """Guard: WDA is not supported on watchOS devices."""
        if self._get_device_type(serial) == DeviceType.WATCH:
            raise NotImplementedError(
                f"WDA not supported on watchOS device {serial}: screenshot/type/key "
                "unsupported for a physical Apple Watch — use a watchOS simulator or "
                "`xcrun devicectl` app-level checks."
            )

    def _get_wda(self, serial: str) -> WDASession:
        """Get or start a WDA session, routing physical vs simulator."""
        return self._wda.get_or_start(serial, is_physical=self._is_physical(serial))

    def tap(self, serial: str, x: int, y: int) -> None:
        """Tap at coordinates via WDA."""
        with self._device_lock(serial):
            self._require_wda(serial)
            session = self._get_wda(serial)
            session.tap(x, y)

    def long_press(self, serial: str, x: int, y: int, duration_ms: int = 500) -> None:
        """Long press at coordinates via WDA."""
        with self._device_lock(serial):
            self._require_wda(serial)
            session = self._get_wda(serial)
            session.long_press(x, y, duration_ms)

    def swipe(
        self,
        serial: str,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 300,
    ) -> None:
        """Swipe from one point to another via WDA."""
        with self._device_lock(serial):
            self._require_wda(serial)
            session = self._get_wda(serial)
            session.swipe(start_x, start_y, end_x, end_y, duration_ms)

    def type_text(self, serial: str, text: str) -> None:
        """Type text into the focused field."""
        with self._device_lock(serial):
            if self._is_physical(serial):
                self._require_wda(serial)
                session = self._get_wda(serial)
                session._request("POST", "/wda/keys", json={"value": list(text)})
            else:
                self._run("io", serial, "input", "text", text)

    def press_key(self, serial: str, key: str) -> None:
        """Press a key on an iOS device.

        Simulators: HOME via notifyutil, other keys via simctl io input.
        Physical devices: HOME via WDA pressButton, others limited.
        """
        with self._device_lock(serial):
            upper = key.upper()
            if self._is_physical(serial):
                self._require_wda(serial)
                if upper == "HOME":
                    session = self._get_wda(serial)
                    session._request("POST", "/wda/pressButton", json={"name": "home"})
                else:
                    _ios.logger.debug("physical_key_not_supported", key=key)
                return

            if upper == "HOME":
                self._run(
                    "spawn",
                    serial,
                    "notifyutil",
                    "-p",
                    "com.apple.springboard.simulateHomeButtonPress",
                )
                return
            key_map = {
                "BACK": "shake",
            }
            mapped = key_map.get(upper, key.lower())
            try:
                self._run("io", serial, "input", mapped)
            except subprocess.CalledProcessError:
                _ios.logger.debug("ios_key_not_supported", key=key)
