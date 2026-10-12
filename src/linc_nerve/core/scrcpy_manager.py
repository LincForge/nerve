"""Multi-device scrcpy stream manager.

Split out of ``scrcpy.py`` (#53) to keep both files under the hot-file LOC
ceiling. ``ScrcpyManager`` is re-exported from ``scrcpy`` for backward
compatibility — import it as ``from linc_nerve.core.scrcpy import ScrcpyManager``.

Module-level globals that the test suite patches (``ScrcpyStream``, ``time``,
``_MAX_SIZE``, ``_allocate_port``, circuit-breaker constants, ``logger`` ...)
live on the ``scrcpy`` module. This module references them through the parent
module (``_scrcpy.<name>``) so ``patch("linc_nerve.core.scrcpy.<name>")`` keeps
taking effect after the move.
"""

from __future__ import annotations

import contextlib
from typing import Any

from . import scrcpy as _scrcpy


class ScrcpyManager:
    """Manages scrcpy streams for multiple devices."""

    def __init__(self) -> None:
        self._streams: dict[str, _scrcpy.ScrcpyStream] = {}
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._cleanup_orphans()
        self._sweep_orphan_forwards()

    @staticmethod
    def _cleanup_orphans() -> None:
        """Kill orphaned scrcpy-server adb processes from previous sessions.

        When the MCP server restarts, old ``adb shell ... scrcpy-server``
        processes may linger, exhausting device capacity. We kill them on init.
        """
        try:
            result = _scrcpy.subprocess.run(
                ["pkill", "-f", "adb.*scrcpy-server"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                _scrcpy.logger.info("scrcpy_orphans_cleaned")
        except (_scrcpy.subprocess.TimeoutExpired, FileNotFoundError):
            pass

    @staticmethod
    def _sweep_orphan_forwards() -> None:
        """Remove scrcpy_* adb forwards left behind by dead sessions (29d723d7).

        Runs at manager init, right after ``_cleanup_orphans`` has killed any
        orphaned scrcpy-server processes — so every ``localabstract:scrcpy_*``
        forward still in the adb server's table at this point belongs to a
        dead stream. Forwards live in the host adb server and survive process
        death; without this sweep they accumulate (~230 observed after one
        bench night) and degrade other adb consumers.
        """
        try:
            result = _scrcpy.subprocess.run(
                [_scrcpy.ADB_PATH, "forward", "--list"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (_scrcpy.subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return
        removed = 0
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) != 3 or not parts[2].startswith("localabstract:scrcpy_"):
                continue
            serial, local, _remote = parts
            with contextlib.suppress(Exception):
                _scrcpy.subprocess.run(
                    [_scrcpy.ADB_PATH, "-s", serial, "forward", "--remove", local],
                    capture_output=True,
                    timeout=5,
                )
                removed += 1
        if removed:
            _scrcpy.logger.info("scrcpy_orphan_forwards_removed", count=removed)

    def is_open(self, serial: str) -> bool:
        """Return True if the circuit breaker is open (scrcpy bypassed) for this device."""
        deadline = self._open_until.get(serial)
        if deadline is None:
            return False
        if _scrcpy.time.monotonic() >= deadline:
            # Cooldown expired — auto-clear
            del self._open_until[serial]
            self._failures.pop(serial, None)
            return False
        return True

    def record_failure(self, serial: str) -> None:
        """Record a scrcpy failure; open circuit at threshold."""
        count = self._failures.get(serial, 0) + 1
        self._failures[serial] = count
        if count >= _scrcpy._SCRCPY_CB_THRESHOLD:
            self._open_until[serial] = _scrcpy.time.monotonic() + _scrcpy._SCRCPY_CB_COOLDOWN
            _scrcpy.logger.warning(
                "scrcpy_circuit_opened",
                serial=serial,
                failures=count,
                cooldown_s=_scrcpy._SCRCPY_CB_COOLDOWN,
            )

    def record_success(self, serial: str) -> None:
        """Clear failure count on success."""
        self._failures.pop(serial, None)
        self._open_until.pop(serial, None)

    def get_stream(self, serial: str) -> _scrcpy.ScrcpyStream | None:
        """Get an existing stream for a device."""
        stream = self._streams.get(serial)
        if stream and not stream.is_running:
            # Dead stream — clean up
            del self._streams[serial]
            return None
        return stream

    def start_stream(
        self,
        serial: str,
        *,
        max_size: int = _scrcpy._MAX_SIZE,
        max_fps: int = _scrcpy._MAX_FPS,
    ) -> _scrcpy.ScrcpyStream:
        """Start or get an existing stream for a device."""
        existing = self.get_stream(serial)
        if existing:
            return existing

        stream = _scrcpy.ScrcpyStream(
            serial, max_size=max_size, max_fps=max_fps, port=_scrcpy._allocate_port()
        )
        stream.start()
        # Gate on the first keyframe: a stream that decodes 0 frames will never
        # serve a screenshot, and retrying it just stacks another hard-timeout.
        # Reject it here so screenshot() skips the retry and falls back fast.
        if not stream.wait_for_first_frame(_scrcpy._KEYFRAME_TIMEOUT):
            frames = stream.frame_count
            stream.stop()
            self.record_failure(serial)
            _scrcpy.logger.warning(
                "scrcpy_stream_zero_frames",
                serial=serial,
                frames=frames,
                timeout_s=_scrcpy._KEYFRAME_TIMEOUT,
            )
            raise _scrcpy.ScrcpyStreamStartError(
                f"scrcpy stream for {serial} decoded 0 frames in {_scrcpy._KEYFRAME_TIMEOUT}s"
            )
        self._streams[serial] = stream
        return stream

    def stop_stream(self, serial: str) -> None:
        """Stop the stream for a device."""
        stream = self._streams.pop(serial, None)
        if stream:
            stream.stop()

    def stop_all(self) -> None:
        """Stop all active streams."""
        for stream in list(self._streams.values()):
            stream.stop()
        self._streams.clear()

    def screenshot(self, serial: str) -> bytes:
        """Get a screenshot using scrcpy, starting a stream if needed.

        Includes recovery logic: if the stream is stale (dead but still
        registered), stop it and start a fresh one.  Circuit breaker
        prevents repeated attempts after consecutive failures.
        """
        if self.is_open(serial):
            raise RuntimeError(f"scrcpy circuit open for {serial}")

        stream = self.start_stream(serial)
        try:
            data = stream.screenshot()
            self.record_success(serial)
            return data
        except RuntimeError:
            # No frame received — stream may be stale. Force restart once.
            _scrcpy.logger.info("scrcpy_screenshot_retry", serial=serial)
            self.stop_stream(serial)
            try:
                stream = self.start_stream(serial)
                data = stream.screenshot()
                self.record_success(serial)
                return data
            except Exception:
                self.record_failure(serial)
                raise

    @property
    def active_streams(self) -> dict[str, dict[str, Any]]:
        """Get info about active streams."""
        return {
            serial: {
                "frames": stream.frame_count,
                "width": stream.device_width,
                "height": stream.device_height,
                "running": stream.is_running,
            }
            for serial, stream in self._streams.items()
        }
