"""scrcpy-server integration for fast screen capture via H264 decoding.

Pushes the scrcpy-server binary to the device, establishes an ADB forward tunnel,
and decodes H264 frames using PyAV. Provides ~35-70ms screenshots vs ~200-500ms
for screencap.
"""

from __future__ import annotations

import contextlib
import io
import socket
import subprocess
import threading
import time
from pathlib import Path

import structlog
from PIL import Image

from .adb_path import ADB_PATH

logger = structlog.get_logger().bind(domain="device_control")

# scrcpy-server version — must match the binary exactly
SCRCPY_VERSION = "3.3.4"

# Default server binary location (bundled with linc-nerve)
_SERVER_DIR = Path(__file__).parent.parent / "vendor"
_SERVER_FILENAME = f"scrcpy-server-v{SCRCPY_VERSION}"
_DEVICE_PATH = "/data/local/tmp/scrcpy-server.jar"

# Connection defaults
_BASE_PORT = 27183
_CONNECT_TIMEOUT = 5.0  # seconds to wait for server startup
# Seconds to wait for the stream's first decoded keyframe before declaring it a
# 0-frame stream. Kept well under android.py's 15s screenshot hard-timeout so a
# stuck stream falls back to screencap fast instead of burning the whole budget
# (a 0-frame stream + restart could otherwise stack two 15s timeouts — see
# reactor e0262e7c / inbox 351a08d6).
_KEYFRAME_TIMEOUT = 3.0
_CONNECT_RETRY_INTERVAL = 0.05  # seconds between connection attempts
_MAX_SIZE = 1080  # max dimension for captured frames
_MAX_FPS = 10
_BIT_RATE = 4_000_000
# Stop decoding H264 after this long with no screenshot read (mirrors
# wda_mjpeg.py's _MJPEG_IDLE_TIMEOUT / PR #135 — same defect, same fix, #138).
_SCRCPY_IDLE_TIMEOUT = 60.0

# Circuit breaker for scrcpy failures
_SCRCPY_CB_THRESHOLD = 3  # consecutive failures before circuit opens
_SCRCPY_CB_COOLDOWN = 300.0  # 5 minutes


class ScrcpyStreamStartError(RuntimeError):
    """A scrcpy stream started but decoded no frames within the keyframe window.

    Raised by ``ScrcpyManager.start_stream`` so callers skip the in-scrcpy
    restart-and-retry (which would stack another hard-timeout) and fall back to
    screencap immediately. Subclasses ``RuntimeError`` so existing
    ``except Exception`` fallbacks keep working.
    """


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from socket."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed while reading")
        buf += chunk
    return buf


def _recv_exact_into(sock: socket.socket, n: int, buf: bytearray) -> bytes:
    """Read exactly n bytes into ``buf``, resuming from whatever it already holds.

    Unlike ``_recv_exact``, a timeout (``TimeoutError`` — e.g. raised by a
    socket with ``settimeout()`` set) is allowed to propagate to the caller
    without losing progress: bytes already read before the timeout stay in
    ``buf``, so calling this again with the SAME ``buf`` resumes exactly
    where the previous call left off instead of discarding the partial read
    and desyncing the stream (round-2 review finding 1 on #138/PR #139 — a
    plain ``_recv_exact`` retried after a timeout starts a fresh local
    buffer each call, so a partial 12-byte header that times out mid-read
    loses its already-read bytes and the next successful read unpacks
    garbage as the frame size).

    Raises ``ConnectionError`` if the socket closes mid-read. Returns the
    completed ``n``-byte payload as ``bytes`` once ``buf`` reaches length
    ``n`` — the caller is responsible for clearing ``buf`` before reusing it
    for the next read.
    """
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed while reading")
        buf.extend(chunk)
    return bytes(buf)


# Imported after the patched module-globals above are defined; the mixin
# resolves them through this module at call-time (see scrcpy_stream_server.py).
from .scrcpy_stream_server import _StreamServerMixin  # noqa: E402


class ScrcpyStream(_StreamServerMixin):
    """Manages a persistent scrcpy-server connection for a single device.

    Runs a background thread that continuously decodes H264 frames.
    The latest frame is always available via ``latest_frame``.

    Server push, tunnel/socket connect, and the H264 read-loop are inherited
    from ``_StreamServerMixin`` (scrcpy_stream_server.py, split in #53).
    """

    def __init__(
        self,
        serial: str,
        *,
        max_size: int = _MAX_SIZE,
        max_fps: int = _MAX_FPS,
        bit_rate: int = _BIT_RATE,
        port: int = 0,
        idle_timeout_s: float = _SCRCPY_IDLE_TIMEOUT,
    ) -> None:
        self.serial = serial
        self.max_size = max_size
        self.max_fps = max_fps
        self.bit_rate = bit_rate
        self.idle_timeout_s = idle_timeout_s
        self._port = port or _allocate_port()

        self._server_proc: subprocess.Popen[bytes] | None = None
        self._sock: socket.socket | None = None
        self._control_sock: socket.socket | None = None
        self._control_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self._last_access = 0.0

        # Latest decoded frame
        self._latest_frame: Image.Image | None = None
        self._frame_count = 0
        self._started_at: float = 0.0

        # Stream metadata
        self.device_width = 0
        self.device_height = 0

    @property
    def latest_frame(self) -> Image.Image | None:
        """Get the most recently decoded frame (thread-safe)."""
        with self._lock:
            return self._latest_frame

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def has_control(self) -> bool:
        """True if the control socket is connected and usable."""
        return self._control_sock is not None

    def start(self) -> None:
        """Push server, establish tunnel, start background frame reader."""
        if self._running:
            return

        self._ensure_server_pushed()
        try:
            self._start_server()
            self._connect()
        except Exception:
            # _start_server() creates the adb forward before _connect() can
            # fail — tear down here or every failed start leaks one forward
            # into the host adb server (improvement 29d723d7).
            self._teardown_transport()
            raise
        # With control=true, server waits for both connections before streaming.
        # Connect the control socket before starting the read loop.
        try:
            self._connect_control()
        except ConnectionError:
            logger.warning("scrcpy_control_connect_failed", serial=self.serial)
        self._running = True
        self._started_at = time.monotonic()
        self._last_access = self._started_at
        self._thread = threading.Thread(
            target=self._read_loop, daemon=True, name=f"scrcpy-{self.serial}"
        )
        self._thread.start()
        logger.info(
            "scrcpy_stream_started",
            serial=self.serial,
            port=self._port,
            max_size=self.max_size,
        )

    def _teardown_transport(self) -> None:
        """Close sockets, kill the server process, and remove the adb forward.

        Idempotent and best-effort — shared by stop() and the failed-start
        cleanup path so the forward is removed on every exit route.
        """
        if self._control_sock:
            with contextlib.suppress(OSError):
                self._control_sock.close()
            self._control_sock = None
        if self._sock:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        if self._server_proc:
            try:
                self._server_proc.kill()
                self._server_proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self._server_proc = None
        with contextlib.suppress(subprocess.TimeoutExpired, FileNotFoundError, OSError):
            subprocess.run(
                [ADB_PATH, "-s", self.serial, "forward", "--remove", f"tcp:{self._port}"],
                capture_output=True,
                timeout=5,
            )

    def stop(self) -> None:
        """Stop the stream and clean up resources."""
        self._running = False
        self._teardown_transport()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        logger.info("scrcpy_stream_stopped", serial=self.serial, frames=self._frame_count)

    def screenshot_pil(self) -> Image.Image:
        """Get the latest frame as a PIL Image (no PNG encode).

        Thread-safe: returns a copy of the shared _latest_frame.

        Waits for a frame strictly newer than the one observed on entry
        — without this, consecutive callers within the H264-decode latency
        window all see the same cached frame (inbox 35a9c61f). If
        `_latest_frame is None`, waits for the read_loop to produce the
        first frame (original behavior). If the deadline passes without a
        fresher frame, returns the last available one rather than raising
        — degraded but compatible with the pre-fix established-stream
        behavior. Only raises when no frame was ever produced.
        """
        self._last_access = time.monotonic()
        # Checked under `self._lock` — the SAME lock `_reap()` now holds for
        # its whole flag-flip + teardown + frame-clear sequence (round-2
        # review finding 2 on #138/PR #139). Both threads therefore
        # serialize on this lock: by the time this read observes
        # `_running is False`, any in-flight reap has either not started yet
        # or has FULLY finished tearing down — never caught mid-teardown.
        # Without this, a screenshot landing in that window could see
        # `_running is False`, call `start()` to stand up a brand-new
        # transport, and then have the stale reap's `_teardown_transport()`
        # tear that new transport down right after.
        with self._lock:
            needs_start = not self._running
        if needs_start:
            self.start()

        entry_count = self._frame_count
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        while (
            self._latest_frame is None or self._frame_count <= entry_count
        ) and time.monotonic() < deadline:
            time.sleep(0.02)

        frame = self.latest_frame
        if frame is None:
            raise RuntimeError(f"No frame received from scrcpy on {self.serial}")

        return frame.copy()

    def screenshot(self) -> bytes:
        """Get the latest frame as PNG bytes.

        Delegates to screenshot_pil() and encodes to PNG.
        """
        frame = self.screenshot_pil()
        buf = io.BytesIO()
        frame.save(buf, format="PNG")
        return buf.getvalue()

    def wait_for_first_frame(self, timeout: float) -> bool:
        """Block until the stream decodes its first frame, or ``timeout``.

        Returns True once a frame has been decoded (frame_count > 0). Returns
        False if the stream stops or produces no frame within ``timeout`` — a
        0-frame stream that will never serve a screenshot. Used as a keyframe
        gate so a stuck stream is detected at startup rather than during a
        later screenshot hard-timeout.
        """
        deadline = time.monotonic() + timeout
        while self._frame_count == 0 and time.monotonic() < deadline:
            if not self._running:
                return False
            time.sleep(0.02)
        return self._frame_count > 0


def _allocate_port() -> int:
    """Allocate a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def server_available() -> bool:
    """Check if the scrcpy-server binary is available."""
    return (_SERVER_DIR / _SERVER_FILENAME).exists()


# ScrcpyManager lives in scrcpy_manager.py (split out in #53 to stay under the
# hot-file LOC ceiling) but is re-exported here so its public import path
# ``from linc_nerve.core.scrcpy import ScrcpyManager`` is preserved. The import
# is at module end so ``ScrcpyStream`` / ``_allocate_port`` / constants are
# already defined when scrcpy_manager (which references them via this module)
# is imported.
from .scrcpy_manager import ScrcpyManager  # noqa: E402

__all__ = [
    "SCRCPY_VERSION",
    "ScrcpyManager",
    "ScrcpyStream",
    "ScrcpyStreamStartError",
    "_allocate_port",
    "_recv_exact",
    "_recv_exact_into",
    "server_available",
]
