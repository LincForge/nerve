"""scrcpy-server lifecycle + socket I/O for ``ScrcpyStream``.

Split out of ``scrcpy.py`` (#53) as a mixin to keep both files under the
hot-file LOC ceiling. ``_StreamServerMixin`` holds the methods that push the
server binary, open the ADB tunnel, connect the video/control sockets, and run
the H264 read loop.

Patched module-globals (``subprocess``, ``time``, ``_SERVER_DIR``,
``_CONNECT_TIMEOUT`` ...) live on the ``scrcpy`` module; this mixin reaches them
through the parent module (``_scrcpy.<name>``) so
``patch("linc_nerve.core.scrcpy.<name>")`` keeps working. Pure-stdlib names that
the suite never patches (``contextlib``, ``struct``, ``socket``) are imported
directly here.
"""

from __future__ import annotations

import contextlib
import socket
import struct

from . import scrcpy as _scrcpy
from .contracts import require


class _StreamServerMixin:
    """Server push, tunnel/socket connect, and frame read-loop for ScrcpyStream."""

    def _connect_control(self) -> None:
        """Connect the control socket (second connection after video).

        In forward mode (tunnel_forward=true), the dummy byte is only sent on
        the first socket (video). The control socket connects without a
        handshake — just a raw TCP connection to the same forwarded port.
        """
        deadline = _scrcpy.time.monotonic() + _scrcpy._CONNECT_TIMEOUT
        last_error: Exception | None = None

        while _scrcpy.time.monotonic() < deadline:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect(("127.0.0.1", self._port))
                # No dummy byte on control socket in forward mode.
                # Verify connection is alive by setting TCP keepalive.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                self._control_sock = sock
                _scrcpy.logger.info(
                    "scrcpy_control_connected", serial=self.serial, port=self._port
                )
                return
            except (ConnectionRefusedError, ConnectionError, OSError) as exc:
                last_error = exc
                with contextlib.suppress(OSError):
                    sock.close()
                _scrcpy.time.sleep(_scrcpy._CONNECT_RETRY_INTERVAL)

        raise ConnectionError(
            f"Could not connect control socket on port {self._port}: {last_error}"
        )

    def send_control(self, data: bytes) -> None:
        """Send a control message to the device.

        Thread-safe — serializes writes via _control_lock.
        Raises RuntimeError if control socket is not connected.
        """
        with self._control_lock:
            if self._control_sock is None:
                raise RuntimeError("Control socket not connected")
            self._control_sock.sendall(data)

    # NOTE: scrcpy-control input (tap/long_press/swipe via pack_touch_event over
    # the control socket) was reverted in #33 — Android input now routes through
    # `adb shell input` (android.py) and iOS through WDA. The control-socket
    # connect/send plumbing above (_connect_control, send_control) is kept for a
    # possible future re-enable; the message-packing layer was removed (T12).

    def _ensure_server_pushed(self) -> None:
        """Push scrcpy-server binary to device if not already present."""
        server_path = _scrcpy._SERVER_DIR / _scrcpy._SERVER_FILENAME
        if not server_path.exists():
            raise FileNotFoundError(
                f"scrcpy-server binary not found at {server_path}. "
                f"Download from https://github.com/Genymobile/scrcpy/releases "
                f"and place at {server_path}"
            )

        # Check if already pushed (by file size)
        local_size = server_path.stat().st_size
        try:
            result = _scrcpy.subprocess.run(
                [_scrcpy.ADB_PATH, "-s", self.serial, "shell", f"wc -c < {_scrcpy._DEVICE_PATH}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            remote_size = int(result.stdout.strip())
            if remote_size == local_size:
                return  # Already pushed
        except (
            _scrcpy.subprocess.TimeoutExpired,
            ValueError,
            _scrcpy.subprocess.CalledProcessError,
        ):
            pass  # Need to push

        _scrcpy.subprocess.run(
            [_scrcpy.ADB_PATH, "-s", self.serial, "push", str(server_path), _scrcpy._DEVICE_PATH],
            capture_output=True,
            check=True,
            timeout=30,
        )
        _scrcpy.logger.info("scrcpy_server_pushed", serial=self.serial, size=local_size)

    def _start_server(self) -> None:
        """Start the scrcpy-server process on the device."""
        scid = self._port  # Use port as scid for uniqueness
        scid_hex = format(scid, "08d")  # Server uses zero-padded decimal, not hex

        # Set up ADB forward tunnel
        _scrcpy.subprocess.run(
            [
                _scrcpy.ADB_PATH,
                "-s",
                self.serial,
                "forward",
                f"tcp:{self._port}",
                f"localabstract:scrcpy_{scid_hex}",
            ],
            capture_output=True,
            check=True,
            timeout=5,
        )

        # Start server
        self._server_proc = _scrcpy.subprocess.Popen(
            [
                _scrcpy.ADB_PATH,
                "-s",
                self.serial,
                "shell",
                f"CLASSPATH={_scrcpy._DEVICE_PATH}",
                "app_process",
                "/",
                "com.genymobile.scrcpy.Server",
                _scrcpy.SCRCPY_VERSION,
                f"scid={scid}",
                "tunnel_forward=true",
                "audio=false",
                "control=true",
                "send_device_meta=false",
                "send_frame_meta=true",
                "send_codec_meta=true",
                "send_dummy_byte=true",
                "video_codec=h264",
                f"max_size={self.max_size}",
                f"max_fps={self.max_fps}",
                f"video_bit_rate={self.bit_rate}",
                "log_level=info",
            ],
            stdout=_scrcpy.subprocess.DEVNULL,
            stderr=_scrcpy.subprocess.PIPE,
        )

    def _extract_stderr(self) -> str:
        import select

        server_proc = require(self._server_proc, "scrcpy server process")
        readable, _, _ = select.select([server_proc.stderr], [], [], 0.5)
        if not readable:
            return ""
        data = server_proc.stderr.read1(4096)  # type: ignore[union-attr]
        return data.decode("utf-8", errors="replace").strip()

    def _read_server_stderr(self) -> str:
        """Read any available stderr output from the scrcpy-server process."""
        if self._server_proc is None or self._server_proc.stderr is None:
            return ""
        try:
            return self._extract_stderr()
        except Exception:
            return ""

    def _attempt_connection(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(2.0)
            sock.connect(("127.0.0.1", self._port))
            dummy = sock.recv(1)
            if not dummy:
                raise ConnectionError("Server not ready (connection closed)")
            return sock
        except Exception:
            with contextlib.suppress(OSError):
                sock.close()
            raise

    def _connect(self) -> None:
        """Connect to the scrcpy-server's video socket.

        The ADB forward tunnel accepts TCP connections immediately, then
        forwards to the device abstract socket. If the server hasn't opened
        its socket yet, the tunnel closes the connection — which looks like
        a successful connect followed by an immediate close, not a refused
        connection. We verify the connection by reading the dummy byte.
        """
        deadline = _scrcpy.time.monotonic() + _scrcpy._CONNECT_TIMEOUT
        last_error: Exception | None = None

        while _scrcpy.time.monotonic() < deadline:
            try:
                self._sock = self._attempt_connection()
                self._dummy_received = True
                return
            except (ConnectionRefusedError, ConnectionError, OSError) as exc:
                last_error = exc
                _scrcpy.time.sleep(_scrcpy._CONNECT_RETRY_INTERVAL)

        raise ConnectionError(
            f"Could not connect to scrcpy-server on port {self._port} "
            f"after {_scrcpy._CONNECT_TIMEOUT}s: {last_error}"
        )

    def _reap(self, idle_for: float) -> None:
        """Stop decoding, release the transport, and drop the cached frame.

        Shared by both idle-check sites in ``_read_loop`` so an abandoned
        stream (idle detected between packets) and a quiet-device stream
        (idle detected on a socket timeout, #138 finding 2) tear down
        identically. Clearing ``_latest_frame`` here closes finding 3: without
        it, ``screenshot_pil()`` restarting a reaped stream in place could
        hand back a frame at least ``idle_timeout_s`` stale, presented as
        current.

        The flag flip, transport teardown, and frame clear all happen under
        ``self._lock`` (round-2 review finding 2 on #138/PR #139): previously
        ``self._running = False`` was published BEFORE ``_teardown_transport()``
        ran, so a ``screenshot_pil()`` call landing in that window saw
        ``_running is False``, called ``start()`` (restart-in-place), and
        stood up a brand-new transport — which this reap then tore down
        anyway, leaving the stream flagged running with a dead transport.
        ``screenshot_pil()`` now checks ``_running`` under the SAME lock, so
        it can only ever observe this reap as either not-yet-started or
        fully finished, never mid-teardown.
        """
        _scrcpy.logger.info(
            "scrcpy_stream_idle_reaped",
            serial=self.serial,
            idle_s=round(idle_for, 1),
            frames=self._frame_count,
        )
        with self._lock:
            self._running = False
            self._teardown_transport()
            self._latest_frame = None

    def _read_loop(self) -> None:
        """Background thread: read and decode H264 frames continuously."""
        try:
            import av
        except ImportError:
            _scrcpy.logger.error("pyav_not_installed", hint="pip install av")
            self._running = False
            return

        sock = self._sock
        if sock is None:
            return

        # Bound EVERY read (handshake, header, and packet body) so a
        # quiet/static device screen (scrcpy-server emits a frame only on
        # screen change) still reaches the idle check below instead of
        # blocking `_recv_exact_into` forever — without this, the idle check
        # only ran AFTER a header had already arrived, so an abandoned
        # stream against an unchanging screen never reaped: thread, both
        # sockets, the server subprocess, and the adb forward all leaked
        # (round-1 review finding 2 on #138/PR #139). Reads use
        # `_recv_exact_into` with a persistent buffer so a timeout
        # mid-accumulation resumes on retry instead of discarding the
        # partial read and desyncing the stream (round-2 review finding 1's
        # second defect).
        sock.settimeout(min(1.0, self.idle_timeout_s))

        # Tracks whether THIS invocation already reaped (and therefore
        # already tore down the transport) so the `finally` block below
        # doesn't redundantly re-run `_teardown_transport()` on the normal
        # idle-reap path — it still must run on every OTHER exit (a real
        # disconnect, or any other exception), which previously leaked the
        # transport because only `_reap()`'s two deliberate call sites tore
        # it down (round-2 review finding 1).
        reaped = False

        def _idle_reap_if_due() -> bool:
            nonlocal reaped
            idle_for = _scrcpy.time.monotonic() - self._last_access
            if idle_for > self.idle_timeout_s:
                self._reap(idle_for)
                reaped = True
                return True
            return False

        handshake_buf = bytearray()
        header_buf = bytearray()
        body_buf = bytearray()

        try:
            # 1. Dummy byte already consumed in _connect() for connection validation

            # 2. Read codec metadata (12 bytes): codec_id(u32) + width(u32) +
            # height(u32). Resumable across a timeout like the header/body
            # reads below — a slow handshake must re-evaluate idle rather
            # than escape to the broad except handler with no teardown.
            while True:
                try:
                    codec_meta = _scrcpy._recv_exact_into(sock, 12, handshake_buf)
                    break
                except TimeoutError:
                    if _idle_reap_if_due():
                        return
            _codec_id, width, height = struct.unpack(">III", codec_meta)
            self.device_width = width
            self.device_height = height

            # 3. Set up H264 decoder
            codec_ctx = av.CodecContext.create("h264", "r")

            # 4. Frame read loop
            while self._running:
                # Read 12-byte frame header — bounded by the socket timeout
                # set above. On a quiet device no bytes ever arrive, so this
                # raises TimeoutError instead of blocking forever; re-evaluate
                # idle on that path exactly as on the between-packets path
                # below, so a quiet device reaps on schedule too. Bytes
                # already read before a timeout stay in `header_buf` so a
                # partial header resumes instead of desyncing the next
                # unpack (round-2 finding 1).
                try:
                    header = _scrcpy._recv_exact_into(sock, 12, header_buf)
                except TimeoutError:
                    if _idle_reap_if_due():
                        break
                    continue
                header_buf.clear()

                pts_flags = struct.unpack(">Q", header[0:8])[0]
                packet_size = struct.unpack(">I", header[8:12])[0]

                is_config = bool(pts_flags & (1 << 63))

                # Idle check BEFORE reading/decoding the packet body — an
                # abandoned stream must do no H264 decode or frame.to_image()
                # work at all (mirrors wda_mjpeg.py's idle reap, #138/PR #135).
                if _idle_reap_if_due():
                    break

                # Read packet data. A stall here used to be UNGUARDED — only
                # the header read above had a try/except TimeoutError, so
                # any stall past the socket's 1.0s bound (a large keyframe
                # over a hiccupping adb forward, a device sleeping
                # mid-frame) escaped straight to the broad
                # `except (ConnectionError, OSError)` handler below with no
                # teardown, leaking the sockets/process/adb forward — a
                # fresh instance of the AC5 leak this PR exists to prevent
                # (round-2 review finding 1). Retry from `body_buf` instead,
                # the same resumable pattern as the header read, so the loop
                # re-evaluates idle rather than escaping.
                body_complete = False
                while self._running and not body_complete:
                    try:
                        data = _scrcpy._recv_exact_into(sock, packet_size, body_buf)
                        body_complete = True
                    except TimeoutError:
                        if _idle_reap_if_due():
                            break
                if not body_complete:
                    break
                body_buf.clear()

                # Parse and decode
                packets = codec_ctx.parse(data)
                for pkt in packets:
                    frames = codec_ctx.decode(pkt)
                    for frame in frames:
                        if not is_config:
                            img = frame.to_image()
                            with self._lock:
                                self._latest_frame = img
                            self._frame_count += 1

        except (ConnectionError, OSError) as exc:
            if self._running:
                server_err = self._read_server_stderr()
                _scrcpy.logger.debug(
                    "scrcpy_stream_disconnected",
                    serial=self.serial,
                    error=str(exc),
                    server_stderr=server_err or "(empty)",
                )
        except Exception as exc:
            if self._running:
                server_err = self._read_server_stderr()
                _scrcpy.logger.warning(
                    "scrcpy_read_error",
                    serial=self.serial,
                    error=str(exc),
                    server_stderr=server_err or "(empty)",
                )
        finally:
            # Any fatal exit from this loop — not just a deliberate
            # `_reap()` — must release the transport, or a real disconnect
            # (ConnectionError/OSError escaping the retry loops above, or an
            # unexpected decode exception) leaks the sockets, scrcpy server
            # process, and adb forward exactly like the reap gap this PR
            # fixes elsewhere (round-2 review finding 1). Skipped when this
            # invocation already reaped — `_teardown_transport()` is
            # idempotent either way, this just avoids a redundant adb call
            # on the common idle-reap path.
            #
            # Generation guard (round-3 review finding on PR #139): once this
            # invocation's reap has released `self._lock`, a `screenshot_pil()`
            # may already have restarted the stream in place — new socket,
            # `_running = True`, new read thread. An unconditional
            # `self._running = False` here (outside the lock) clobbered that
            # restart, and the new read thread then exited and tore down the
            # fresh transport. So: after a reap, touch nothing (`_reap()`
            # already flipped the flag and tore down under the lock); on any
            # other exit, flip + tear down under the SAME lock, and only if
            # `self._sock` is still the socket THIS generation read from — a
            # newer transport installed by a restart belongs to its own
            # generation's read loop.
            if not reaped:
                with self._lock:
                    if self._sock is sock:
                        self._running = False
                        self._teardown_transport()
