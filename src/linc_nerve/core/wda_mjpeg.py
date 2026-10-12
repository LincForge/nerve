"""WDA MJPEG stream client + pure multipart parser (productionized from T13 spike).

Mirrors core/scrcpy.py: a background thread holds one streaming HTTP connection to
the WDA MJPEG server and decodes frames into a timestamped latest_frame; the manager
(WdaMjpegManager) layers a warming cache + circuit breaker on top.
"""

from __future__ import annotations

import io
import re
import threading
import time
from collections.abc import Iterable, Iterator

import httpx
import structlog
from PIL import Image

logger = structlog.get_logger().bind(domain="device_control")

_MJPEG_CB_THRESHOLD = 3  # consecutive failures before circuit opens
_MJPEG_CB_COOLDOWN = 30.0  # seconds the circuit stays open
_KEYFRAME_TIMEOUT = 1.5  # first-frame gate: fall back to simctl fast
_MJPEG_IDLE_TIMEOUT = 60.0  # stop decoding after this long with no screenshot read

_CONTENT_LENGTH_RE = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)
_HEADER_END = b"\r\n\r\n"


def _try_extract_one(buf: bytearray, boundary: bytes) -> tuple[bytes | None, int]:
    """Try to pull one JPEG payload off the front of buf.

    Returns (frame, consumed). frame is None when more bytes are needed; consumed
    is how many leading bytes of buf the caller should drop once a frame is taken.
    """
    start = buf.find(boundary)
    if start == -1:
        return None, 0
    header_start = start + len(boundary)
    he = buf.find(_HEADER_END, header_start)
    if he == -1:
        return None, 0
    headers = bytes(buf[header_start:he])
    body_start = he + len(_HEADER_END)

    m = _CONTENT_LENGTH_RE.search(headers)
    if m:
        length = int(m.group(1))
        end = body_start + length
        if len(buf) < end:
            return None, 0
        return bytes(buf[body_start:end]), end

    # No Content-Length: the frame runs up to the next boundary.
    nb = buf.find(boundary, body_start)
    if nb == -1:
        return None, 0
    frame = bytes(buf[body_start:nb])
    if frame.endswith(b"\r\n"):
        frame = frame[:-2]
    return frame, nb


def iter_mjpeg_frames(chunks: Iterable[bytes], boundary: bytes) -> Iterator[bytes]:
    """Yield JPEG payloads from an MJPEG multipart/x-mixed-replace byte stream.

    boundary: the boundary token WITHOUT leading CRLF, e.g. b'--BoundaryString'.
    Tolerates chunk splits at arbitrary byte offsets and both Content-Length and
    pure boundary-delimited framing.
    """
    buf = bytearray()
    for chunk in chunks:
        buf.extend(chunk)
        while True:
            frame, consumed = _try_extract_one(buf, boundary)
            if frame is None:
                break
            del buf[:consumed]
            yield frame


class StaleFrameError(RuntimeError):
    """Raised when the freshest available frame is older than the stale threshold."""


def _boundary_from_content_type(content_type: str) -> bytes:
    """Extract the multipart boundary token (with leading --) from a Content-Type."""
    # e.g. 'multipart/x-mixed-replace; boundary=--BoundaryString'
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("boundary="):
            tok = part.split("=", 1)[1].strip().strip('"')
            return tok.encode() if tok.startswith("--") else b"--" + tok.encode()
    return b"--BoundaryString"  # WDA default


class WdaMjpegStream:
    """Persistent WDA MJPEG client — mirrors core/scrcpy.py:ScrcpyStream.

    A background thread holds one streaming HTTP connection to the WDA MJPEG server
    and decodes frames into a timestamped ``latest_frame``. ``screenshot()`` returns
    the freshest frame. Reconnects with backoff on any stream failure (a frozen
    stream is an invisible failure mode that would silently serve stale frames).
    """

    def __init__(
        self,
        url: str,
        *,
        stale_after_s: float = 0.5,
        connect_timeout_s: float = 1.5,
        idle_timeout_s: float = _MJPEG_IDLE_TIMEOUT,
    ) -> None:
        self.url = url  # e.g. http://localhost:9110
        self.stale_after_s = stale_after_s
        self.connect_timeout_s = connect_timeout_s
        self.idle_timeout_s = idle_timeout_s
        self._last_access = 0.0
        self._lock = threading.Lock()
        self._latest_frame: Image.Image | None = None
        self._latest_ts: float = 0.0
        self._frame_count = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._started_at = 0.0

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def is_running(self) -> bool:
        """False once the read loop has stopped — including a self-reap on idle."""
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._started_at = time.monotonic()
        self._last_access = self._started_at
        self._thread = threading.Thread(target=self._read_loop, daemon=True, name="wda-mjpeg")
        self._thread.start()
        # Block until the first frame or timeout (the keyframe gate: callers treat
        # frame_count == 0 after start() as a fast-fail → fall back to simctl).
        deadline = time.monotonic() + self.connect_timeout_s
        while self._frame_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _read_loop(self) -> None:
        backoff = 0.1
        while self._running:
            try:
                timeout = httpx.Timeout(self.connect_timeout_s, read=None)
                with (
                    httpx.Client(timeout=timeout) as client,
                    client.stream("GET", self.url) as resp,
                ):
                    boundary = _boundary_from_content_type(resp.headers.get("content-type", ""))
                    backoff = 0.1  # reset on a successful connect
                    for frame in iter_mjpeg_frames(resp.iter_bytes(), boundary):
                        if not self._running:
                            break
                        idle_for = time.monotonic() - self._last_access
                        if idle_for > self.idle_timeout_s:
                            logger.info(
                                "wda_mjpeg_idle_reaped",
                                url=self.url,
                                idle_s=round(idle_for, 1),
                                frames=self._frame_count,
                            )
                            self._running = False
                            break
                        img = Image.open(io.BytesIO(frame))
                        img.load()
                        with self._lock:
                            self._latest_frame = img
                            self._latest_ts = time.monotonic()
                            self._frame_count += 1
            except Exception:
                if not self._running:
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, 2.0)

    def screenshot_pil(self) -> Image.Image:
        """Return the freshest frame, waiting for one strictly newer than on entry."""
        self._last_access = time.monotonic()
        entry = self._frame_count
        deadline = time.monotonic() + self.connect_timeout_s
        while self._frame_count <= entry and time.monotonic() < deadline:
            time.sleep(0.005)
        with self._lock:
            frame, ts = self._latest_frame, self._latest_ts
        if frame is None:
            raise RuntimeError(f"No MJPEG frame received from {self.url}")
        age = time.monotonic() - ts
        if age > self.stale_after_s:
            raise StaleFrameError(f"Freshest frame is {age:.2f}s old")
        return frame.copy()

    def screenshot(self) -> bytes:
        buf = io.BytesIO()
        self.screenshot_pil().save(buf, format="PNG")
        return buf.getvalue()


class WdaMjpegManager:
    """Warming cache + circuit breaker for WDA MJPEG streams (mirrors ScrcpyManager).

    Streams are keyed by (serial, mjpeg_port): a WDASession restart draws a new
    port, so a serial-only key would serve a dead port until the breaker tripped.
    On a port mismatch we stop the stale stream and start a fresh one.
    """

    def __init__(self) -> None:
        self._streams: dict[tuple[str, int], WdaMjpegStream] = {}
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def is_open(self, serial: str) -> bool:
        deadline = self._open_until.get(serial)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            # Cooldown elapsed — half-open: clear state so the next call re-probes.
            del self._open_until[serial]
            self._failures.pop(serial, None)
            return False
        return True

    def record_failure(self, serial: str) -> None:
        count = self._failures.get(serial, 0) + 1
        self._failures[serial] = count
        if count >= _MJPEG_CB_THRESHOLD:
            self._open_until[serial] = time.monotonic() + _MJPEG_CB_COOLDOWN
            logger.warning("wda_mjpeg_circuit_opened", serial=serial[:12], failures=count)

    def record_success(self, serial: str) -> None:
        self._failures.pop(serial, None)
        self._open_until.pop(serial, None)

    def get_stream(self, serial: str, port: int) -> WdaMjpegStream | None:
        """Return the cached stream iff its port matches; else invalidate + drop."""
        for (s, p), stream in list(self._streams.items()):
            if s != serial:
                continue
            if p == port:
                if not stream.is_running:
                    # Self-reaped after idle — drop it so a fresh stream is started.
                    stream.stop()
                    del self._streams[(s, p)]
                    return None
                return stream
            # Stale: session restarted on a new port — reap the old stream/thread.
            stream.stop()
            del self._streams[(s, p)]
        return None

    def start_stream(self, serial: str, port: int) -> WdaMjpegStream:
        existing = self.get_stream(serial, port)
        if existing is not None:
            return existing
        stream = WdaMjpegStream(f"http://localhost:{port}", connect_timeout_s=_KEYFRAME_TIMEOUT)
        stream.start()
        if stream.frame_count == 0:
            stream.stop()
            self.record_failure(serial)
            raise RuntimeError(
                f"WDA MJPEG stream for {serial[:12]} decoded 0 frames in {_KEYFRAME_TIMEOUT}s"
            )
        self._streams[(serial, port)] = stream
        return stream

    def screenshot(self, serial: str, port: int) -> bytes:
        if self.is_open(serial):
            raise RuntimeError(f"WDA MJPEG circuit open for {serial[:12]}")
        stream = self.start_stream(serial, port)
        try:
            data = stream.screenshot()
            self.record_success(serial)
            return data
        except RuntimeError:
            self.stop_stream(serial, port)
            self.record_failure(serial)
            raise

    def stop_stream(self, serial: str, port: int) -> None:
        stream = self._streams.pop((serial, port), None)
        if stream:
            stream.stop()

    def stop_all(self) -> None:
        for stream in list(self._streams.values()):
            stream.stop()
        self._streams.clear()

    @property
    def active_streams(self) -> dict[str, dict[str, object]]:
        return {
            serial: {"port": port, "frames": stream.frame_count, "transport": "wda-mjpeg"}
            for (serial, port), stream in self._streams.items()
        }
