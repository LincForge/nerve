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
import os
import re
import signal
from collections.abc import Collection
from typing import Any

from . import scrcpy as _scrcpy
from .procs import pid_alive

_NERVE_FORWARD_REMOTE_RE = re.compile(r"localabstract:scrcpy_([0-9a-f]{8})")
_SCID_HEX_RE = re.compile(r"[0-9a-f]{8}")


def _nerve_client_pattern() -> str:
    """Return regex matching only nerve's own adb scrcpy-server client processes."""
    return rf"(?:^\S*/|^)adb(?:\s+.*|\s+){re.escape(_scrcpy._DEVICE_PATH)}"


def _extract_scid_tokens(args: str) -> list[str]:
    """Return all ``scid=...`` values from a process command line."""
    return [tok.removeprefix("scid=") for tok in args.split() if tok.startswith("scid=")]


def _parse_ps_nerve_line(line: str) -> tuple[int, int, int] | str | None:
    """Classify one ``ps -axo pid=,ppid=,args=`` line.

    Returns:
        - ``(pid, ppid, scid)`` for a well-formed nerve scrcpy-server adb client line.
        - ``None`` for a well-formed non-nerve process line.
        - ``"skip"`` for an unparseable/hostile line (non-ASCII, non-numeric pid/ppid,
          missing or non-hex scid on a nerve client line).
    """
    if not line.isascii():
        return "skip"
    parts = line.split(None, 2)
    if len(parts) != 3:
        return "skip"
    pid_str, ppid_str, args = parts
    if not pid_str.isdigit() or not ppid_str.isdigit():
        return "skip"
    pid = int(pid_str)
    ppid = int(ppid_str)
    if pid <= 0:
        return "skip"
    if re.search(_nerve_client_pattern(), args) is None:
        return None
    scid_values = _extract_scid_tokens(args)
    if len(scid_values) != 1 or _SCID_HEX_RE.fullmatch(scid_values[0]) is None:
        return "skip"
    return pid, ppid, int(scid_values[0], 16)


def _parse_ps_nerve_clients(stdout: str) -> tuple[list[tuple[int, int, int]], int]:
    """Parse ``ps -axo pid=,ppid=,args=`` output into ``(clients, skipped_count)``."""
    clients: list[tuple[int, int, int]] = []
    skipped = 0
    for line in stdout.splitlines():
        verdict = _parse_ps_nerve_line(line)
        if verdict == "skip":
            skipped += 1
        elif isinstance(verdict, tuple):
            clients.append(verdict)
    return clients, skipped


def _parent_gone(ppid: int) -> bool:
    """Return True when a client's parent process is gone (re-parented to 1 or dead)."""
    return ppid <= 1 or not pid_alive(ppid)


def _terminate_pids(pids: list[int]) -> None:
    """Send SIGTERM to each pid, tolerating a pid that already exited."""
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


def _parse_nerve_forward(line: str) -> tuple[str, str, int] | None:
    """Return ``(serial, local, scid)`` when ``line`` is a nerve scrcpy forward, else ``None``."""
    if not line.isascii():
        return None
    parts = line.split()
    if len(parts) != 3:
        return None
    serial, local, remote = parts
    match = _NERVE_FORWARD_REMOTE_RE.fullmatch(remote)
    if match is None:
        return None
    scid = int(match.group(1), 16)
    if scid != _scrcpy.nerve_scid(scid):
        return None
    return serial, local, scid


def _parse_nerve_forward_line(line: str) -> tuple[str, str] | None:
    """Return ``(serial, local)`` when ``line`` is a nerve scrcpy forward, else ``None``."""
    parsed = _parse_nerve_forward(line)
    if parsed is None:
        return None
    serial, local, _scid = parsed
    return serial, local


class ScrcpyManager:
    """Manages scrcpy streams for multiple devices."""

    def __init__(self) -> None:
        self._streams: dict[str, _scrcpy.ScrcpyStream] = {}
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._sweep_orphan_forwards(self._cleanup_orphans())

    @staticmethod
    def _cleanup_orphans() -> set[int] | None:
        """Reap orphaned nerve scrcpy-server adb clients and record surviving scids.

        Enumerates ``ps -axo pid=,ppid=,args=`` for adb client processes whose
        argv contains nerve's versioned device jar path. Keeps every client
        whose parent process is alive and not pid 1 (recording its
        ``scid=<8 hex>``), and sends ``SIGTERM`` only to clients whose parent
        is gone. Construction of a ``ScrcpyManager`` never signals a process
        whose parent is alive and never matches upstream
        ``/data/local/tmp/scrcpy-server.jar`` sessions. Unparseable ``ps``
        lines are skipped and counted in ``scrcpy_ps_lines_skipped``. If
        process enumeration fails or times out — or the adb path contains
        whitespace, so no ``ps`` argv could ever match it — reaps nothing and
        returns ``None`` so ``_sweep_orphan_forwards`` removes nothing (fail safe).
        """
        if any(ch.isspace() for ch in _scrcpy.ADB_PATH):
            _scrcpy.logger.warning("scrcpy_orphan_scan_skipped", reason="adb_path_whitespace")
            return None
        try:
            result = _scrcpy.subprocess.run(
                ["ps", "-axo", "pid=,ppid=,args="],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (_scrcpy.subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            _scrcpy.logger.warning("scrcpy_orphan_scan_skipped", reason=type(exc).__name__)
            return None
        if result.returncode != 0:
            _scrcpy.logger.warning(
                "scrcpy_orphan_scan_skipped", reason="ps_failed", rc=result.returncode
            )
            return None

        stdout = result.stdout if isinstance(result.stdout, str) else ""
        clients, skipped = _parse_ps_nerve_clients(stdout)
        if skipped:
            _scrcpy.logger.info("scrcpy_ps_lines_skipped", count=skipped)

        surviving_scids: set[int] = set()
        orphan_pids: list[int] = []
        for pid, ppid, scid in clients:
            if _parent_gone(ppid):
                orphan_pids.append(pid)
            else:
                surviving_scids.add(scid)

        if orphan_pids:
            _terminate_pids(orphan_pids)
            _scrcpy.logger.info("scrcpy_orphans_cleaned", count=len(orphan_pids))
        return surviving_scids

    @staticmethod
    def _sweep_orphan_forwards(surviving: Collection[int] | None) -> None:
        """Remove nerve scrcpy_* adb forwards whose scid no surviving client uses.

        Runs at manager init with the ``scid`` set ``_cleanup_orphans`` returned
        for all surviving clients (whose parent process is still alive). The set
        is passed explicitly — never through shared state — so a concurrent
        manager construction cannot change what this sweep keeps. Removes
        a nerve-range ``localabstract:scrcpy_6e65xxxx`` forward only when its
        ``scid`` is not in that surviving set — construction of a
        ``ScrcpyManager`` never removes a forward a surviving client uses.
        ``surviving=None`` (process enumeration failed) removes nothing (fail safe).
        Foreign forwards (upstream scrcpy, non-scrcpy, or unparseable lines)
        are skipped and counted in ``scrcpy_foreign_forwards_kept``.
        """
        if surviving is None:
            return
        if _scrcpy.adb_server_config_error() is not None:
            return
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
        kept = 0
        for line in (result.stdout or "").splitlines():
            target = _parse_nerve_forward(line)
            if target is None:
                kept += 1
                continue
            serial, local, scid = target
            if scid in surviving:
                continue
            with contextlib.suppress(Exception):
                _scrcpy.subprocess.run(
                    [_scrcpy.ADB_PATH, "-s", serial, "forward", "--remove", local],
                    capture_output=True,
                    timeout=5,
                )
                removed += 1
        if removed:
            _scrcpy.logger.info("scrcpy_orphan_forwards_removed", count=removed)
        if kept:
            _scrcpy.logger.info("scrcpy_foreign_forwards_kept", count=kept)

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
