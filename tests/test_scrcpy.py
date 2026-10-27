"""Tests for scrcpy stream integration."""

from __future__ import annotations

import socket
import struct
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from linc_nerve.core.scrcpy import (
    _SCRCPY_CB_COOLDOWN,
    _SCRCPY_CB_THRESHOLD,
    SCRCPY_VERSION,
    ScrcpyManager,
    ScrcpyStream,
    _allocate_port,
    _recv_exact,
    _recv_exact_into,
    server_available,
)


@pytest.fixture(autouse=True)
def _no_leaked_scrcpy_threads():
    """Fail any test that leaves a new ``scrcpy*`` thread alive on exit."""
    before = set(threading.enumerate())
    yield

    def _leaked() -> list[threading.Thread]:
        return [
            t
            for t in threading.enumerate()
            if t not in before and t.name.startswith("scrcpy") and t.is_alive()
        ]

    deadline = time.monotonic() + 3.0
    while _leaked() and time.monotonic() < deadline:
        time.sleep(0.02)

    leaked = _leaked()
    if leaked:
        names = ", ".join(t.name for t in leaked)
        pytest.fail(f"leaked thread {names}")


class TestRecvExact:
    def test_reads_exact_bytes(self) -> None:
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.bind(("127.0.0.1", 0))
        port = server_sock.getsockname()[1]
        server_sock.listen(1)

        def send_data() -> None:
            conn, _ = server_sock.accept()
            conn.sendall(b"hello world")
            conn.close()

        t = threading.Thread(target=send_data)
        t.start()

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect(("127.0.0.1", port))
        data = _recv_exact(client, 5)
        assert data == b"hello"
        data2 = _recv_exact(client, 6)
        assert data2 == b" world"
        client.close()
        server_sock.close()
        t.join()

    def test_raises_on_closed_socket(self) -> None:
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.bind(("127.0.0.1", 0))
        port = server_sock.getsockname()[1]
        server_sock.listen(1)

        def close_immediately() -> None:
            conn, _ = server_sock.accept()
            conn.sendall(b"hi")
            conn.close()

        t = threading.Thread(target=close_immediately)
        t.start()

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect(("127.0.0.1", port))
        with pytest.raises(ConnectionError):
            _recv_exact(client, 100)  # Ask for more than available
        client.close()
        server_sock.close()
        t.join()


class TestAllocatePort:
    def test_returns_different_ports(self) -> None:
        p1 = _allocate_port()
        p2 = _allocate_port()
        assert isinstance(p1, int)
        assert isinstance(p2, int)
        assert p1 > 0
        assert p2 > 0


class TestServerAvailable:
    def test_unavailable_when_no_binary(self) -> None:
        with patch("linc_nerve.core.scrcpy._SERVER_DIR", Path("/nonexistent")):
            assert server_available() is False


class TestScrcpyStream:
    def test_init_defaults(self) -> None:
        stream = ScrcpyStream("PIXEL8PRO", port=12345)
        assert stream.serial == "PIXEL8PRO"
        assert stream.max_size == 1080
        assert stream.max_fps == 10
        assert stream.is_running is False
        assert stream.latest_frame is None
        assert stream.frame_count == 0

    def test_latest_frame_thread_safe(self) -> None:
        stream = ScrcpyStream("ABC", port=12345)
        img = Image.new("RGB", (100, 100), "red")
        stream._latest_frame = img
        assert stream.latest_frame is img

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_ensure_server_pushed_skips_if_same_size(
        self, mock_popen: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        # Create a fake server binary
        server_file = tmp_path / f"scrcpy-server-v{SCRCPY_VERSION}"
        server_file.write_bytes(b"fake_server_data_1234")
        file_size = server_file.stat().st_size

        with patch("linc_nerve.core.scrcpy._SERVER_DIR", tmp_path):
            stream = ScrcpyStream("DEV1", port=12345)
            # Mock wc -c returning same size
            mock_run.return_value = MagicMock(stdout=str(file_size), returncode=0)
            stream._ensure_server_pushed()

        # Should only have called the size check, not push
        assert mock_run.call_count == 1
        args = mock_run.call_args[0][0]
        assert "wc" in " ".join(args)

    @patch("subprocess.run")
    def test_ensure_server_pushed_pushes_if_different_size(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        server_file = tmp_path / f"scrcpy-server-v{SCRCPY_VERSION}"
        server_file.write_bytes(b"fake_server_data_1234")

        with patch("linc_nerve.core.scrcpy._SERVER_DIR", tmp_path):
            stream = ScrcpyStream("DEV1", port=12345)
            # Mock wc -c returning different size
            mock_run.return_value = MagicMock(stdout="0", returncode=0)
            stream._ensure_server_pushed()

        # Should have called wc -c check + push
        assert mock_run.call_count == 2

    def test_ensure_server_pushed_raises_if_no_binary(self) -> None:
        with patch("linc_nerve.core.scrcpy._SERVER_DIR", Path("/nonexistent")):
            stream = ScrcpyStream("DEV1", port=12345)
            with pytest.raises(FileNotFoundError, match="scrcpy-server binary not found"):
                stream._ensure_server_pushed()

    def test_stop_when_not_started(self) -> None:
        """Stopping an unstarted stream should not raise."""
        stream = ScrcpyStream("DEV1", port=12345)
        stream.stop()  # Should not raise

    def test_screenshot_pil_returns_image(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """screenshot_pil() should return a PIL Image directly (no PNG encode)."""
        monkeypatch.setattr("linc_nerve.core.scrcpy._CONNECT_TIMEOUT", 0.1)
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True
        stream._latest_frame = Image.new("RGB", (100, 200), "blue")
        img = stream.screenshot_pil()
        assert isinstance(img, Image.Image)
        assert img.size == (100, 200)

    def test_screenshot_pil_copies_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """screenshot_pil() should return a copy, not the shared _latest_frame."""
        monkeypatch.setattr("linc_nerve.core.scrcpy._CONNECT_TIMEOUT", 0.1)
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True
        original = Image.new("RGB", (50, 50), "red")
        stream._latest_frame = original
        result = stream.screenshot_pil()
        assert result is not original

    def test_screenshot_pil_raises_when_no_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("linc_nerve.core.scrcpy._CONNECT_TIMEOUT", 0.1)
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True
        stream._latest_frame = None
        with pytest.raises(RuntimeError, match="No frame"):
            stream.screenshot_pil()

    def test_screenshot_pil_waits_for_fresh_frame_between_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: inbox 35a9c61f. Each call must wait for the read_loop
        to produce a frame newer than the one observed on entry, not just
        return whatever's already in `_latest_frame`."""
        import threading

        monkeypatch.setattr("linc_nerve.core.scrcpy._CONNECT_TIMEOUT", 2.0)
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True
        stream._latest_frame = Image.new("RGB", (10, 10), "red")
        stream._frame_count = 1

        # Background "decoder" advances frame_count after a short delay
        def advance() -> None:
            import time as _t

            _t.sleep(0.1)
            stream._frame_count = 2
            stream._latest_frame = Image.new("RGB", (10, 10), "green")

        t = threading.Thread(target=advance, daemon=True)
        t.start()

        img = stream.screenshot_pil()
        t.join(timeout=1.0)
        # Second call should observe the newer frame (green), not the stale (red).
        assert stream._frame_count >= 2
        # The returned image is decoded from the post-advance frame
        assert img.getpixel((5, 5)) == (0, 128, 0) or img.getpixel((5, 5)) == (0, 255, 0)

    def test_screenshot_pil_returns_stale_frame_when_no_advance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: inbox 35a9c61f compatible-degraded-mode. If the
        read_loop never produces a new frame within the deadline, return
        the last available frame rather than raising (the old behavior
        for an established stream)."""
        import time as _t

        monkeypatch.setattr("linc_nerve.core.scrcpy._CONNECT_TIMEOUT", 0.2)
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True
        stale = Image.new("RGB", (10, 10), "red")
        stream._latest_frame = stale
        stream._frame_count = 5  # never increments below

        t0 = _t.monotonic()
        img = stream.screenshot_pil()
        elapsed = _t.monotonic() - t0
        # Should block for ~deadline then return the stale frame
        assert elapsed >= 0.15
        assert img is not stale  # must be a copy
        assert img.getpixel((5, 5)) == (255, 0, 0)

    def test_screenshot_pil_returns_first_frame_when_initially_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When latest_frame starts None, the first call must wait for it
        to be populated (existing behavior must be preserved)."""
        import threading

        monkeypatch.setattr("linc_nerve.core.scrcpy._CONNECT_TIMEOUT", 1.0)
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True
        stream._latest_frame = None
        stream._frame_count = 0

        def populate() -> None:
            import time as _t

            _t.sleep(0.05)
            stream._latest_frame = Image.new("RGB", (10, 10), "blue")
            stream._frame_count = 1

        t = threading.Thread(target=populate, daemon=True)
        t.start()
        img = stream.screenshot_pil()
        t.join(timeout=1.0)
        assert img.getpixel((5, 5)) == (0, 0, 255)


class TestScrcpyManager:
    def test_get_stream_returns_none_for_unknown(self) -> None:
        mgr = ScrcpyManager()
        assert mgr.get_stream("UNKNOWN") is None

    def test_get_stream_cleans_dead_streams(self) -> None:
        mgr = ScrcpyManager()
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = False  # Dead stream
        mgr._streams["DEV1"] = stream

        assert mgr.get_stream("DEV1") is None
        assert "DEV1" not in mgr._streams

    def test_active_streams_empty(self) -> None:
        mgr = ScrcpyManager()
        assert mgr.active_streams == {}

    def test_active_streams_info(self) -> None:
        mgr = ScrcpyManager()
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True
        stream._frame_count = 42
        stream.device_width = 1080
        stream.device_height = 1920
        mgr._streams["DEV1"] = stream

        info = mgr.active_streams
        assert "DEV1" in info
        assert info["DEV1"]["frames"] == 42
        assert info["DEV1"]["width"] == 1080
        assert info["DEV1"]["running"] is True

    def test_stop_all(self) -> None:
        mgr = ScrcpyManager()
        stream1 = MagicMock()
        stream2 = MagicMock()
        mgr._streams = {"A": stream1, "B": stream2}

        mgr.stop_all()

        stream1.stop.assert_called_once()
        stream2.stop.assert_called_once()
        assert mgr._streams == {}

    def test_stop_stream_unknown(self) -> None:
        """Stopping an unknown stream should not raise."""
        mgr = ScrcpyManager()
        mgr.stop_stream("NONEXISTENT")  # Should not raise


class TestWaitForFirstFrame:
    """ScrcpyStream.wait_for_first_frame — keyframe gate before screenshot."""

    def test_returns_true_when_frame_present(self) -> None:
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True
        stream._frame_count = 1
        assert stream.wait_for_first_frame(timeout=0.1) is True

    def test_returns_false_on_zero_frame_timeout(self) -> None:
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True
        stream._frame_count = 0
        assert stream.wait_for_first_frame(timeout=0.05) is False

    def test_returns_false_when_not_running(self) -> None:
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = False
        stream._frame_count = 0
        assert stream.wait_for_first_frame(timeout=0.05) is False


class _FakeStream:
    """Stand-in for ScrcpyStream in start_stream/screenshot tests."""

    def __init__(self, *, first_frame: bool, frames: int = 0) -> None:
        self._first_frame = first_frame
        self.frame_count = frames
        self.is_running = True
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def wait_for_first_frame(self, timeout: float) -> bool:
        return self._first_frame

    def stop(self) -> None:
        self.stopped = True
        self.is_running = False

    def screenshot(self) -> bytes:
        return b"PNGDATA"


class TestStartStreamKeyframeGate:
    """start_stream waits for the first keyframe and rejects 0-frame streams."""

    def test_zero_frame_stream_raises_and_is_not_registered(self) -> None:
        from linc_nerve.core.scrcpy import ScrcpyStreamStartError

        mgr = ScrcpyManager()
        fake = _FakeStream(first_frame=False, frames=0)
        with (
            patch("linc_nerve.core.scrcpy.ScrcpyStream", return_value=fake),
            pytest.raises(ScrcpyStreamStartError),
        ):
            mgr.start_stream("DEV1")
        assert fake.stopped is True
        assert "DEV1" not in mgr._streams
        # A 0-frame start counts toward the circuit breaker.
        assert mgr._failures.get("DEV1", 0) >= 1

    def test_keyframe_stream_is_returned_and_registered(self) -> None:
        mgr = ScrcpyManager()
        fake = _FakeStream(first_frame=True, frames=3)
        with patch("linc_nerve.core.scrcpy.ScrcpyStream", return_value=fake):
            stream = mgr.start_stream("DEV1")
        assert stream is fake
        assert mgr._streams["DEV1"] is fake
        assert fake.started is True


class TestScreenshotSkipsRetryOnZeroFrame:
    """A 0-frame stream skips the in-scrcpy retry so the caller falls back fast."""

    def test_zero_frame_propagates_without_second_start(self) -> None:
        from linc_nerve.core.scrcpy import ScrcpyStreamStartError

        mgr = ScrcpyManager()
        fake = _FakeStream(first_frame=False, frames=0)
        with (
            patch("linc_nerve.core.scrcpy.ScrcpyStream", return_value=fake) as mk,
            pytest.raises(ScrcpyStreamStartError),
        ):
            mgr.screenshot("DEV1")
        # Exactly one stream construction — the retry path was NOT taken.
        assert mk.call_count == 1

    def test_healthy_stream_screenshot_succeeds(self) -> None:
        mgr = ScrcpyManager()
        fake = _FakeStream(first_frame=True, frames=2)
        with patch("linc_nerve.core.scrcpy.ScrcpyStream", return_value=fake):
            data = mgr.screenshot("DEV1")
        assert data == b"PNGDATA"


class TestScrcpyStreamProtocol:
    """Tests that verify the H264 frame decoding pipeline using a mock server."""

    def _make_fake_server(
        self, port: int, frames: list[bytes] | None = None
    ) -> tuple[threading.Thread, threading.Event]:
        """Create a fake scrcpy server that sends codec meta + frame headers + data."""
        ready = threading.Event()

        def serve() -> None:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", port))
            srv.listen(1)
            srv.settimeout(5)
            ready.set()
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                srv.close()
                return

            try:
                # Dummy byte
                conn.sendall(b"\x00")

                # Codec meta: codec_id=0x68323634 (h264), width=480, height=800
                conn.sendall(struct.pack(">III", 0x68323634, 480, 800))

                if frames:
                    for frame_data in frames:
                        # Frame header: PTS with config flag cleared, packet size
                        pts_flags = 0  # Not config, not keyframe
                        header = struct.pack(">QI", pts_flags, len(frame_data))
                        conn.sendall(header + frame_data)

                # Keep connection open briefly
                time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                conn.close()
                srv.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        return t, ready

    def test_read_loop_parses_codec_meta(self) -> None:
        """Verify the read loop correctly parses codec metadata."""
        port = _allocate_port()
        thread, ready = self._make_fake_server(port)
        ready.wait(timeout=2)

        stream = ScrcpyStream("TEST", port=port)
        stream._running = True

        # Connect directly (skip server push/start)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", port))
        # Consume dummy byte (normally done by _connect)
        sock.recv(1)
        stream._sock = sock

        # Run read loop in background
        read_thread = threading.Thread(target=stream._read_loop, daemon=True)
        read_thread.start()

        # Wait for codec meta to be parsed (poll instead of fixed sleep)
        for _ in range(50):
            if stream.device_width != 0:
                break
            time.sleep(0.05)
        stream._running = False
        sock.close()
        read_thread.join(timeout=2)

        assert stream.device_width == 480
        assert stream.device_height == 800

        thread.join(timeout=2)


class TestScrcpyReadLoop:
    """Characterization tests for ScrcpyStream._read_loop (CC=13, CRAP=95)."""

    def test_read_loop_no_av_module(self) -> None:
        """When pyav (av) is not importable, _read_loop sets _running=False and returns."""
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True

        with patch.dict("sys.modules", {"av": None}):
            # Force ImportError on `import av`
            import builtins

            original_import = builtins.__import__

            def mock_import(name, *args, **kwargs):
                if name == "av":
                    raise ImportError("No module named 'av'")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                stream._read_loop()

        assert stream._running is False

    def test_read_loop_no_socket(self) -> None:
        """When _sock is None, _read_loop returns immediately (no crash)."""
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True
        stream._sock = None

        # Should return without error (sock is None guard)
        stream._read_loop()

    def test_read_loop_connection_error(self) -> None:
        """ConnectionError during codec meta read -> logs and sets _running=False."""
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True
        stream._server_proc = None  # No server process (for _read_server_stderr)

        mock_sock = MagicMock()
        # recv returns empty bytes -> _recv_exact raises ConnectionError
        mock_sock.recv.return_value = b""
        stream._sock = mock_sock

        stream._read_loop()

        assert stream._running is False

    def test_read_loop_general_exception(self) -> None:
        """Non-connection exception during read -> logs warning and sets _running=False."""
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True
        stream._server_proc = None

        mock_sock = MagicMock()
        # recv raises a non-connection exception
        mock_sock.recv.side_effect = RuntimeError("unexpected error")
        stream._sock = mock_sock

        stream._read_loop()

        assert stream._running is False

    def test_read_loop_connection_error_not_running(self) -> None:
        """ConnectionError when _running is already False -> no logging, just stops."""
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = False  # Already stopped
        stream._server_proc = None

        mock_sock = MagicMock()
        mock_sock.recv.return_value = b""
        stream._sock = mock_sock

        # Should not raise even though not running
        stream._read_loop()

        assert stream._running is False

    def _make_stream_with_socket(self, recv_data: bytes) -> tuple:
        """Helper: create a ScrcpyStream with a mock socket feeding recv_data."""
        stream = ScrcpyStream("DEV1", port=12345)
        stream._running = True
        stream._server_proc = None
        # Bypassing start() (which normally sets this) — a zero/unset last-access
        # would otherwise read as idle-since-epoch and trip the #138 idle reap
        # before the fake frame is ever decoded.
        stream._last_access = time.monotonic()

        pos = 0

        def fake_recv(n):
            nonlocal pos
            if pos >= len(recv_data):
                raise ConnectionError("done")
            chunk = recv_data[pos : pos + n]
            pos += n
            return chunk

        mock_sock = MagicMock()
        mock_sock.recv.side_effect = fake_recv
        stream._sock = mock_sock

        mock_frame = MagicMock()
        mock_frame.to_image.return_value = Image.new("RGB", (100, 100))

        mock_packet = MagicMock()
        mock_codec = MagicMock()
        mock_codec.parse.return_value = [mock_packet]
        mock_codec.decode.return_value = [mock_frame]

        mock_av = MagicMock()
        mock_av.CodecContext.create.return_value = mock_codec
        return stream, mock_av

    def test_read_loop_decodes_frame(self) -> None:
        """Happy path: reads codec metadata, decodes one frame, stores PIL image."""
        codec_meta = struct.pack(">III", 0, 1080, 1920)
        frame_header = struct.pack(">QI", 100, 4)
        frame_data = b"\x00\x00\x00\x01"
        recv_data = codec_meta + frame_header + frame_data

        stream, mock_av = self._make_stream_with_socket(recv_data)

        import sys

        real_av = sys.modules.get("av")
        sys.modules["av"] = mock_av
        try:
            stream._read_loop()
        finally:
            if real_av is not None:
                sys.modules["av"] = real_av
            else:
                sys.modules.pop("av", None)

        assert stream.device_width == 1080
        assert stream.device_height == 1920
        assert stream._frame_count >= 1
        assert stream._latest_frame is not None

    def test_read_loop_skips_config_frame(self) -> None:
        """Config frames (bit 63 set in PTS) are parsed but not stored."""
        codec_meta = struct.pack(">III", 0, 640, 480)
        config_pts = 1 << 63
        frame_header = struct.pack(">QI", config_pts, 4)
        frame_data = b"\x00\x00\x00\x01"
        recv_data = codec_meta + frame_header + frame_data

        stream, mock_av = self._make_stream_with_socket(recv_data)

        import sys

        real_av = sys.modules.get("av")
        sys.modules["av"] = mock_av
        try:
            stream._read_loop()
        finally:
            if real_av is not None:
                sys.modules["av"] = real_av
            else:
                sys.modules.pop("av", None)

        assert stream._frame_count == 0
        assert stream._latest_frame is None


class TestScrcpyCircuitBreaker:
    """Tests for the ScrcpyManager circuit breaker."""

    def test_circuit_opens_after_threshold_failures(self) -> None:
        mgr = ScrcpyManager()
        for _ in range(_SCRCPY_CB_THRESHOLD):
            mgr.record_failure("DEV1")
        assert mgr.is_open("DEV1") is True

    def test_circuit_closed_before_threshold(self) -> None:
        mgr = ScrcpyManager()
        for _ in range(_SCRCPY_CB_THRESHOLD - 1):
            mgr.record_failure("DEV1")
        assert mgr.is_open("DEV1") is False

    def test_circuit_resets_after_cooldown(self) -> None:
        mgr = ScrcpyManager()
        for _ in range(_SCRCPY_CB_THRESHOLD):
            mgr.record_failure("DEV1")
        assert mgr.is_open("DEV1") is True

        with patch("linc_nerve.core.scrcpy.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + _SCRCPY_CB_COOLDOWN + 1
            assert mgr.is_open("DEV1") is False

    def test_success_clears_failures(self) -> None:
        mgr = ScrcpyManager()
        mgr.record_failure("DEV1")
        mgr.record_failure("DEV1")
        mgr.record_success("DEV1")
        # Counter reset — two more failures should not open circuit
        mgr.record_failure("DEV1")
        mgr.record_failure("DEV1")
        assert mgr.is_open("DEV1") is False

    def test_per_device_isolation(self) -> None:
        mgr = ScrcpyManager()
        for _ in range(_SCRCPY_CB_THRESHOLD):
            mgr.record_failure("DEV_A")
        assert mgr.is_open("DEV_A") is True
        assert mgr.is_open("DEV_B") is False


class TestStartFailureCleanup:
    """A failed start() must not leak its adb forward (improvement 29d723d7).

    _start_server() creates the forward before _connect() can fail; without
    teardown on that path, every failed start leaks one forward into the host
    adb server (observed: ~230 stale forwards after one bench night).
    """

    def test_connect_failure_removes_forward_and_kills_server(self) -> None:
        stream = ScrcpyStream("DEV1", port=12345)
        proc = MagicMock()

        def fake_start_server() -> None:
            stream._server_proc = proc

        with (
            patch.object(stream, "_ensure_server_pushed"),
            patch.object(stream, "_start_server", side_effect=fake_start_server),
            patch.object(stream, "_connect", side_effect=ConnectionError("no dummy byte")),
            patch("linc_nerve.core.scrcpy.subprocess.run") as mock_run,
            pytest.raises(ConnectionError),
        ):
            stream.start()

        remove_calls = [
            c
            for c in mock_run.call_args_list
            if "--remove" in c.args[0] and "tcp:12345" in c.args[0]
        ]
        assert remove_calls, "failed start() must remove its adb forward"
        proc.kill.assert_called_once()
        assert stream._server_proc is None
        assert stream.is_running is False

    def test_start_server_failure_still_removes_forward(self) -> None:
        """Popen dying after the forward was created must also clean up."""
        stream = ScrcpyStream("DEV1", port=23456)

        with (
            patch.object(stream, "_ensure_server_pushed"),
            patch.object(stream, "_start_server", side_effect=OSError("adb gone")),
            patch("linc_nerve.core.scrcpy.subprocess.run") as mock_run,
            pytest.raises(OSError),
        ):
            stream.start()

        remove_calls = [
            c
            for c in mock_run.call_args_list
            if "--remove" in c.args[0] and "tcp:23456" in c.args[0]
        ]
        assert remove_calls, "failed _start_server() must remove its adb forward"


class TestOrphanForwardSweep:
    """Manager init sweeps scrcpy_* forwards left by dead sessions (29d723d7).

    Runs right after _cleanup_orphans() has pkilled orphaned scrcpy-server
    processes — any scrcpy_* forward still listed at that point is dead.
    """

    @staticmethod
    def _fake_run(removed: list[tuple[str, str]], list_stdout: str):
        def side_effect(cmd, **kwargs):
            if cmd[0] == "pkill":
                return MagicMock(returncode=1)
            if cmd[-1] == "--list":
                return MagicMock(returncode=0, stdout=list_stdout)
            if "--remove" in cmd:
                removed.append((cmd[cmd.index("-s") + 1], cmd[-1]))
                return MagicMock(returncode=0)
            return MagicMock(returncode=0, stdout="")

        return side_effect

    def test_removes_only_scrcpy_forwards(self) -> None:
        removed: list[tuple[str, str]] = []
        listing = (
            "PIX8 tcp:27183 localabstract:scrcpy_00027183\n"
            "PIX8 tcp:8081 tcp:8081\n"
            "WATCH tcp:31000 localabstract:scrcpy_00031000\n"
        )
        with patch(
            "linc_nerve.core.scrcpy.subprocess.run",
            side_effect=self._fake_run(removed, listing),
        ):
            ScrcpyManager()

        assert ("PIX8", "tcp:27183") in removed
        assert ("WATCH", "tcp:31000") in removed
        assert ("PIX8", "tcp:8081") not in removed
        assert len(removed) == 2

    def test_sweep_survives_missing_adb(self) -> None:
        """No adb on PATH must not break manager construction."""
        with patch(
            "linc_nerve.core.scrcpy.subprocess.run",
            side_effect=FileNotFoundError("adb"),
        ):
            ScrcpyManager()  # must not raise

    def test_sweep_survives_malformed_listing(self) -> None:
        removed: list[tuple[str, str]] = []
        listing = "garbage\n\nPIX8 tcp:27183\n"
        with patch(
            "linc_nerve.core.scrcpy.subprocess.run",
            side_effect=self._fake_run(removed, listing),
        ):
            ScrcpyManager()  # must not raise
        assert removed == []


# --- #138: idle reaping (mirrors PR #135's WDA MJPEG fix for the same defect
# in ScrcpyStream — an unread stream must stop decoding H264 forever) ---


def _wait_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


class _InfiniteFrameSocket:
    """Fake socket ``.recv()`` serving codec meta once, then endless frame packets.

    Drives the real ``ScrcpyStream._read_loop`` the way a live scrcpy-server
    connection would, without any real device/process — codec meta (12 bytes),
    then an unbounded stream of minimal frame-header + 4-byte-payload packets,
    paced with a short sleep so an idle-reap test isn't a busy spin.
    """

    def __init__(self) -> None:
        self._buf = bytearray(struct.pack(">III", 0x68323634, 480, 800))
        self._frame_payload = b"\x00\x00\x00\x01"
        self._frame_packet = (
            struct.pack(">QI", 0, len(self._frame_payload)) + self._frame_payload
        )

    def recv(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._buf.extend(self._frame_packet)
            time.sleep(0.001)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def settimeout(self, value: float | None) -> None:
        """No-op — production `_read_loop` now calls `sock.settimeout(...)`
        unconditionally (#138 finding 2); this fake must tolerate the call."""

    def close(self) -> None:
        pass


class _NeverDataSocket:
    """Fake socket: serves codec meta once, then never produces another byte.

    Simulates a quiet/static device screen where scrcpy-server emits no
    further frames — `recv()` raises `TimeoutError` after the handshake bytes
    are exhausted, mirroring what a real socket with `settimeout()` does when
    nothing arrives (round-1 review finding 2 on #138/PR #139).
    """

    def __init__(self) -> None:
        self._buf = bytearray(struct.pack(">III", 0x68323634, 480, 800))

    def settimeout(self, value: float | None) -> None:
        pass

    def recv(self, n: int) -> bytes:
        if self._buf:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
            return chunk
        raise TimeoutError("simulated quiet device — no data ever arrives")

    def close(self) -> None:
        pass


def _mock_av_module() -> MagicMock:
    mock_frame = MagicMock()
    mock_frame.to_image.return_value = Image.new("RGB", (8, 8))
    mock_packet = MagicMock()
    mock_codec = MagicMock()
    mock_codec.parse.return_value = [mock_packet]
    mock_codec.decode.return_value = [mock_frame]
    mock_av = MagicMock()
    mock_av.CodecContext.create.return_value = mock_codec
    return mock_av


@pytest.fixture
def live_scrcpy_stream(monkeypatch):
    """Build a ScrcpyStream whose real ``_read_loop`` can be run against a fake
    infinite frame source + mocked ``av``, without touching any real device."""
    import sys

    monkeypatch.setitem(sys.modules, "av", _mock_av_module())

    made: list[ScrcpyStream] = []

    def _make(idle_timeout_s: float) -> ScrcpyStream:
        stream = ScrcpyStream("DEV-IDLE", port=55201, idle_timeout_s=idle_timeout_s)
        stream._running = True
        stream._server_proc = None
        stream._sock = _InfiniteFrameSocket()
        stream._last_access = time.monotonic()
        made.append(stream)
        return stream

    yield _make
    for s in made:
        s._running = False


class TestScrcpyIdleReap:
    """[test] ACs 1-3: RED-first idle-reap coverage, mirroring wda_mjpeg's B1."""

    def test_unread_stream_self_reaps(self, live_scrcpy_stream) -> None:
        """An abandoned ScrcpyStream must stop decoding on its own.

        Must fail against pre-fix code: ``_read_loop`` runs `while self._running`
        with no idle condition, so the stream never stops here.
        """
        stream = live_scrcpy_stream(0.15)
        t = threading.Thread(target=stream._read_loop, daemon=True)
        t.start()
        try:
            assert _wait_until(lambda: stream.frame_count > 0), (
                "stream should decode at least one frame while warm"
            )
            assert _wait_until(lambda: not stream.is_running), (
                "idle stream never self-reaped"
            )
        finally:
            stream._running = False
            t.join(timeout=2)

    def test_stream_stays_alive_while_being_read(self, live_scrcpy_stream) -> None:
        """Active screenshot traffic must keep the warming cache warm (no regression)."""
        stream = live_scrcpy_stream(0.3)
        t = threading.Thread(target=stream._read_loop, daemon=True)
        t.start()
        try:
            deadline = time.monotonic() + 0.9
            while time.monotonic() < deadline:
                stream._last_access = time.monotonic()
                time.sleep(0.02)
            assert stream.is_running, "stream was reaped despite continuous reads"
        finally:
            stream._running = False
            t.join(timeout=2)

    def test_idle_reap_releases_transport(self, live_scrcpy_stream) -> None:
        """Self-reap must release the transport (sockets/process/adb forward),
        not just stop decoding — otherwise the reap leaks a port forward."""
        stream = live_scrcpy_stream(0.1)
        control_sock = MagicMock()
        proc = MagicMock()
        stream._control_sock = control_sock
        stream._server_proc = proc

        with patch("linc_nerve.core.scrcpy.subprocess.run") as mock_run:
            t = threading.Thread(target=stream._read_loop, daemon=True)
            t.start()
            try:
                assert _wait_until(lambda: not stream.is_running), (
                    "idle stream never self-reaped"
                )
                t.join(timeout=2)
                assert stream._sock is None, "reap must close the video socket"
                assert stream._control_sock is None, "reap must close the control socket"
                proc.kill.assert_called_once()
                remove_calls = [
                    c
                    for c in mock_run.call_args_list
                    if c.args and "--remove" in c.args[0] and "tcp:55201" in c.args[0]
                ]
                assert remove_calls, "reap must remove the adb forward"
            finally:
                stream._running = False


# --- round-1 MINOS review on #138/PR #139: 3 additional findings ---


class TestScrcpyLastAccessRealWrites:
    """[test] Finding 1 (unpinned-ac): the idle-reap suite above never drives
    the real production `_last_access` writes in `start()` / `screenshot_pil()`
    — the fixtures hand-set `_last_access` directly, so deleting either
    assignment in scrcpy.py left the whole suite green. These tests reach both
    writes through the real code path and must fail if the corresponding
    assignment is removed.
    """

    def test_start_sets_last_access_via_real_code_path(self, monkeypatch) -> None:
        """Deleting `self._last_access = self._started_at` in `start()` must
        fail this test: `_last_access` would stay at its 0.0 init value."""
        import sys

        monkeypatch.setitem(sys.modules, "av", _mock_av_module())

        stream = ScrcpyStream("DEV-START", port=55301, idle_timeout_s=5.0)

        def fake_connect() -> None:
            stream._sock = _InfiniteFrameSocket()

        with (
            patch.object(stream, "_ensure_server_pushed"),
            patch.object(stream, "_start_server"),
            patch.object(stream, "_connect", side_effect=fake_connect),
            patch.object(stream, "_connect_control"),
        ):
            before = time.monotonic()
            stream.start()
            try:
                assert stream._last_access >= before, (
                    "start() must set _last_access via the real assignment — "
                    "it is still at its unset 0.0 default otherwise"
                )
                # Prove the real read loop is actually reachable/live through
                # this same start() call, not just that a field got poked.
                assert _wait_until(lambda: stream.frame_count > 0), (
                    "real read loop never produced a frame after start()"
                )
            finally:
                with patch.object(stream, "_teardown_transport"):
                    stream.stop()

    def test_screenshot_pil_advances_last_access_via_real_code_path(
        self, monkeypatch
    ) -> None:
        """Deleting `self._last_access = time.monotonic()` in `screenshot_pil()`
        must fail this test: `_last_access` would never advance past the value
        set before the stream started."""
        import sys

        monkeypatch.setitem(sys.modules, "av", _mock_av_module())

        stream = ScrcpyStream("DEV-SCR", port=55302, idle_timeout_s=5.0)
        stream._running = True
        stream._server_proc = None
        stream._sock = _InfiniteFrameSocket()
        stream._last_access = time.monotonic()

        t = threading.Thread(target=stream._read_loop, daemon=True)
        t.start()
        try:
            assert _wait_until(lambda: stream.frame_count > 0), (
                "stream should decode at least one frame while warm"
            )
            before = time.monotonic()
            time.sleep(0.05)
            stream.screenshot_pil()
            assert stream._last_access >= before, (
                "screenshot_pil() must advance _last_access via the real "
                "assignment — a stream polled only through screenshot_pil() "
                "would otherwise idle-reap despite active reads"
            )
        finally:
            stream._running = False
            t.join(timeout=2)


class TestScrcpyQuietDeviceReap:
    """[test] Finding 2 (correctness): the idle check ran only after a frame
    header had already arrived, so a quiet/static device screen (scrcpy-server
    emits no frames at all when nothing changes) blocked `_recv_exact` forever
    and the reap never fired — thread, sockets, server subprocess, and the adb
    forward all leaked. The socket now carries a timeout so the idle check also
    runs on a `TimeoutError`, not only between successfully-read packets.
    """

    def test_quiet_device_still_self_reaps(self, monkeypatch) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "av", _mock_av_module())

        stream = ScrcpyStream("DEV-QUIET", port=55303, idle_timeout_s=0.2)
        stream._running = True
        stream._server_proc = None
        stream._sock = _NeverDataSocket()
        stream._last_access = time.monotonic()

        with patch("linc_nerve.core.scrcpy.subprocess.run"):
            t = threading.Thread(target=stream._read_loop, daemon=True)
            t.start()
            try:
                assert _wait_until(lambda: not stream.is_running, timeout=3.0), (
                    "a quiet device that sends zero frames must still self-reap "
                    "— if the idle check only runs after a header arrives, this "
                    "blocks forever in _recv_exact and the reap never fires"
                )
                t.join(timeout=2)
                assert stream._sock is None, "reap must close the video socket"
                assert stream.frame_count == 0, (
                    "a quiet device never decodes a single frame"
                )
            finally:
                stream._running = False
                t.join(timeout=2)


class TestScrcpyStaleFrameAfterReap:
    """[test] Finding 3 (correctness): the reap block tore down the transport
    but left `_latest_frame` populated, so `screenshot_pil()` restarting a
    reaped stream in place could hand back a frame at least `idle_timeout_s`
    stale, presented as current. The reap now clears the cached frame too.
    """

    def test_reap_clears_cached_frame(self, live_scrcpy_stream) -> None:
        stream = live_scrcpy_stream(0.1)
        t = threading.Thread(target=stream._read_loop, daemon=True)
        t.start()
        try:
            assert _wait_until(lambda: stream.frame_count > 0), (
                "stream should decode at least one frame while warm"
            )
            assert _wait_until(lambda: not stream.is_running), (
                "idle stream never self-reaped"
            )
            t.join(timeout=2)
            assert stream.latest_frame is None, (
                "reap must clear the cached frame — otherwise a stream "
                "restarted in place by screenshot_pil() can hand back the "
                "pre-reap frame as if it were current"
            )
        finally:
            stream._running = False


class TestScrcpyManagerSelfReap:
    """[build] AC 4: a self-reaped stream must not be served from cache."""

    def test_manager_replaces_self_reaped_stream(self) -> None:
        mgr = ScrcpyManager()
        first = _FakeStream(first_frame=True, frames=1)
        with patch("linc_nerve.core.scrcpy.ScrcpyStream", return_value=first):
            mgr.start_stream("DEV1")
        assert mgr._streams["DEV1"] is first

        first.is_running = False  # simulate the read loop self-reaping after idle

        second = _FakeStream(first_frame=True, frames=1)
        with patch("linc_nerve.core.scrcpy.ScrcpyStream", return_value=second):
            mgr.start_stream("DEV1")
        assert mgr._streams["DEV1"] is second
        assert mgr._streams["DEV1"] is not first


# --- round-2 MINOS review on #138/PR #139: findings 1 and 2 ---


class TestRecvExactInto:
    """[test] `_recv_exact_into` resumes across a timeout instead of losing
    already-read bytes (round-2 finding 1's second defect)."""

    def test_resumes_partial_read_after_timeout(self) -> None:
        calls = iter(
            [
                b"AB",  # partial read: 2 of 5 bytes
                TimeoutError("simulated stall"),
                b"CDE",  # remaining 3 bytes, completing the read
            ]
        )

        def fake_recv(_n: int) -> bytes:
            item = next(calls)
            if isinstance(item, Exception):
                raise item
            return item

        sock = MagicMock()
        sock.recv.side_effect = fake_recv

        buf = bytearray()
        with pytest.raises(TimeoutError):
            _recv_exact_into(sock, 5, buf)
        # The 2 bytes read before the timeout must survive in `buf`.
        assert bytes(buf) == b"AB", (
            "a timeout mid-read must not discard bytes already accumulated"
        )

        # Resuming with the SAME buf completes the read from where it left off.
        result = _recv_exact_into(sock, 5, buf)
        assert result == b"ABCDE"

    def test_raises_connection_error_on_closed_socket(self) -> None:
        sock = MagicMock()
        sock.recv.return_value = b""
        with pytest.raises(ConnectionError):
            _recv_exact_into(sock, 5, bytearray())


class _BodyStallSocket:
    """Fake socket: serves codec meta + one valid header, then times out on
    every subsequent recv — simulating a stall on the packet-BODY read.

    Before round-2 finding 1's fix, only the HEADER read was wrapped in
    ``try/except TimeoutError``; the body read was unguarded, so a stall
    here escaped straight to the broad ``except (ConnectionError, OSError)``
    handler with no teardown, leaking the sockets/process/adb forward — a
    fresh instance of the AC5 leak this PR exists to prevent.
    """

    def __init__(self) -> None:
        payload_len = 4
        self._scripted: list[bytes] = [
            struct.pack(">III", 0x68323634, 480, 800),  # codec meta
            struct.pack(">QI", 0, payload_len),  # one valid 12-byte header
        ]

    def settimeout(self, value: float | None) -> None:
        pass

    def recv(self, n: int) -> bytes:
        if self._scripted:
            return self._scripted.pop(0)
        # Body read (and everything after) stalls forever.
        raise TimeoutError("simulated stall mid packet-body read")

    def close(self) -> None:
        pass


class TestScrcpyBodyReadTimeout:
    """[test] Finding 1 (correctness): a stall on the packet-BODY read must
    reach the same idle-reap teardown path as a header-read stall, not leak
    the transport via the broad except handler."""

    def test_body_read_timeout_reaps_and_releases_transport(self, monkeypatch) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "av", _mock_av_module())

        stream = ScrcpyStream("DEV-BODYSTALL", port=55401, idle_timeout_s=0.2)
        stream._running = True
        stream._server_proc = None
        stream._sock = _BodyStallSocket()
        stream._last_access = time.monotonic()

        with patch("linc_nerve.core.scrcpy.subprocess.run"):
            t = threading.Thread(target=stream._read_loop, daemon=True)
            t.start()
            try:
                assert _wait_until(lambda: not stream.is_running, timeout=3.0), (
                    "a stall on the packet-body read must still self-reap — "
                    "if only the header read is guarded, this escapes to the "
                    "broad except handler and the transport leaks"
                )
                t.join(timeout=2)
                assert stream._sock is None, (
                    "a body-read stall must release the video socket, not "
                    "leak it via an unguarded timeout"
                )
                assert stream.frame_count == 0, (
                    "the stalled packet body is never decoded"
                )
            finally:
                stream._running = False
                t.join(timeout=2)


class _PartialHeaderThenResumeSocket:
    """Fake socket: times out partway through the 12-byte frame header, then
    resumes and completes correctly, followed by a normal packet body and a
    steady stream of further frames.

    Proves a timeout mid-accumulation does not discard already-read header
    bytes (round-2 finding 1's second defect) — losing them would desync the
    stream and unpack garbage as `packet_size` from what is actually
    mid-frame-payload bytes on the next successful read.
    """

    def __init__(self) -> None:
        payload = b"\x00\x00\x00\x01"
        header = struct.pack(">QI", 0, len(payload))
        assert len(header) == 12
        self._scripted: list[bytes | None] = [
            struct.pack(">III", 0x68323634, 480, 800),  # codec meta
            header[:5],  # partial header: first 5 of 12 bytes
            None,  # simulated stall mid-header
            header[5:],  # remaining 7 bytes — must resume, not restart
            payload,  # the body this (correctly reassembled) header describes
        ]
        self._steady_packet = struct.pack(">QI", 0, len(payload)) + payload
        self._steady = bytearray()

    def settimeout(self, value: float | None) -> None:
        pass

    def recv(self, n: int) -> bytes:
        if self._scripted:
            item = self._scripted.pop(0)
            if item is None:
                raise TimeoutError("simulated stall mid-header")
            return item
        while len(self._steady) < n:
            self._steady.extend(self._steady_packet)
        chunk = bytes(self._steady[:n])
        del self._steady[:n]
        return chunk

    def close(self) -> None:
        pass


class TestScrcpyPartialHeaderResume:
    """[test] Finding 1 (correctness, second defect): a partial header that
    times out mid-read must resume from where it left off, not desync."""

    def test_partial_header_resumes_without_desync(self, monkeypatch) -> None:
        import sys

        mock_av = _mock_av_module()
        monkeypatch.setitem(sys.modules, "av", mock_av)
        mock_codec = mock_av.CodecContext.create.return_value

        stream = ScrcpyStream("DEV-PARTIALHDR", port=55402, idle_timeout_s=5.0)
        stream._running = True
        stream._server_proc = None
        stream._sock = _PartialHeaderThenResumeSocket()
        stream._last_access = time.monotonic()

        t = threading.Thread(target=stream._read_loop, daemon=True)
        t.start()
        try:
            assert _wait_until(lambda: mock_codec.parse.call_count > 0), (
                "the reassembled header's packet body was never handed to "
                "the decoder"
            )
            # Pin the WIRING of the persistent header buffer, not just "some
            # frame decoded": the mocked decoder turns ANY bytes into a frame,
            # so `frame_count > 0` alone passes even when the header desyncs
            # (round-3 MINOS finding on PR #139). A non-resumable read after
            # the mid-header stall unpacks a garbage 256-byte packet_size from
            # mid-payload bytes, so the first body handed to `parse` would be
            # 256 bytes of steady-stream data instead of the 4-byte payload
            # the (correctly reassembled) header describes.
            first_body = mock_codec.parse.call_args_list[0].args[0]
            assert first_body == b"\x00\x00\x00\x01", (
                "a header split across a timeout must be reassembled from the "
                "bytes already read — the first packet body must be exactly "
                f"the 4-byte payload it describes, got {len(first_body)} bytes"
            )
            assert stream.device_width == 480
            assert stream.device_height == 800
        finally:
            stream._running = False
            t.join(timeout=2)


class TestScrcpyReapRestartRace:
    """[test] Finding 2 (correctness): `_reap()` must be atomic with respect
    to a concurrent `screenshot_pil()` restart-in-place — otherwise a
    screenshot landing between the flag flip and `_teardown_transport()`
    starts a fresh transport that the in-flight reap then tears down."""

    def test_screenshot_pil_during_reap_keeps_the_new_transport_alive(
        self, monkeypatch
    ) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "av", _mock_av_module())

        stream = ScrcpyStream("DEV-RACE", port=55403, idle_timeout_s=5.0)
        stream._server_proc = None

        reap_entered = threading.Event()
        release_reap = threading.Event()
        real_teardown = stream._teardown_transport

        def slow_teardown() -> None:
            reap_entered.set()
            # Give a concurrent screenshot_pil() a real chance to observe
            # `_running is False` and call start() before this reap finishes
            # tearing down — the exact window round-2 finding 2 describes.
            release_reap.wait(timeout=2.0)
            with patch("linc_nerve.core.scrcpy.subprocess.run"):
                real_teardown()

        new_sock_holder: dict[str, object] = {}

        def fake_start() -> None:
            # Simulate a real start(): install a fresh transport and flip
            # `_running` back to True, without touching the network/adb.
            sock = _InfiniteFrameSocket()
            new_sock_holder["sock"] = sock
            stream._sock = sock
            stream._running = True
            stream._thread = threading.Thread(
                target=stream._read_loop, daemon=True, name="scrcpy-race-restart"
            )
            stream._thread.start()

        try:
            with (
                patch.object(stream, "_teardown_transport", side_effect=slow_teardown),
                patch.object(stream, "start", side_effect=fake_start),
            ):
                stream._running = True
                stream._sock = _InfiniteFrameSocket()
                stream._last_access = time.monotonic()

                reap_thread = threading.Thread(
                    target=stream._reap, args=(999.0,), daemon=True
                )
                reap_thread.start()
                assert reap_entered.wait(timeout=2.0), "reap did not start teardown"

                # screenshot_pil() races the in-flight reap. It must not observe
                # `_running is False` until the reap has FULLY finished, so the
                # transport it starts survives.
                def call_screenshot() -> None:
                    stream.screenshot_pil()

                screenshot_thread = threading.Thread(target=call_screenshot, daemon=True)
                screenshot_thread.start()

                # Let the reap's teardown proceed now that both threads are racing.
                release_reap.set()
                reap_thread.join(timeout=3.0)
                screenshot_thread.join(timeout=3.0)

            assert "sock" in new_sock_holder, "screenshot_pil() never restarted the stream"
            assert stream._sock is new_sock_holder["sock"], (
                "the reap must not close a transport started AFTER it finished "
                "tearing down — the stale reap tore down the freshly-started "
                "transport instead of leaving it alone"
            )
            assert stream.is_running, (
                "the stream must end up running with a live transport, not "
                "flagged running with a transport the stale reap already killed"
            )
        finally:
            release_reap.set()
            with patch.object(stream, "_teardown_transport"):
                stream.stop()

    def test_reap_restart_race_stops_thread_without_calling_subprocess_run(
        self, monkeypatch
    ) -> None:
        """Regression (linc-nerve-twyb / reactor 29e39f9d): the restarted
        ``scrcpy-race-restart`` read thread must not outlive the race test or
        reach ``ScrcpyStream._teardown_transport``'s real ``subprocess.run``."""
        subprocess_calls: list[object] = []

        def record_run(*args, **kwargs):
            subprocess_calls.append((args, kwargs))
            return MagicMock(returncode=0)

        monkeypatch.setattr("linc_nerve.core.scrcpy.subprocess.run", record_run)
        self.test_screenshot_pil_during_reap_keeps_the_new_transport_alive(monkeypatch)

        assert _wait_until(
            lambda: not any(
                t.name.startswith("scrcpy") and t.is_alive() for t in threading.enumerate()
            ),
            timeout=0.5,
        ), "scrcpy thread still alive within 0.5s of race test finishing"
        assert subprocess_calls == [], (
            "restarted stream teardown must be stubbed so subprocess.run is "
            f"never called, got: {subprocess_calls!r}"
        )


class TestScrcpyReadLoopFinallyDoesNotClobberRestart:
    """[test] Round-3 MINOS finding on PR #139: `_reap()` is atomic, but the
    reaping `_read_loop`'s own `finally:` still ran `self._running = False`
    outside the lock AFTER `_reap()` released it. A `screenshot_pil()` restart
    landing in that window (sees `_running is False`, calls `start()`, installs
    a fresh transport, sets `_running = True`) was then clobbered by the OLD
    thread's `finally`, and the NEW read thread exited and tore down the fresh
    transport. Unlike `TestScrcpyReapRestartRace` (which calls `_reap()`
    directly), these tests drive the REAL `_read_loop` so its `finally` runs.
    """

    def _run_old_loop_with_restart_after(
        self, monkeypatch, stream: ScrcpyStream, hook_name: str
    ) -> tuple[object, MagicMock]:
        """Run the real `_read_loop` on `stream`; the moment `hook_name`
        finishes (i.e. just after the old generation released the lock),
        simulate a `screenshot_pil()` restart-in-place installing a new
        transport. Returns the new socket and the teardown spy."""
        new_sock = _InfiniteFrameSocket()
        real_hook = getattr(stream, hook_name)

        def hook_then_restart(*args, **kwargs):
            real_hook(*args, **kwargs)
            # What a concurrent start() leaves behind: a fresh transport and
            # the running flag back up. (No new read thread here — the point
            # is what the OLD thread's `finally` does to this state.)
            stream._sock = new_sock
            stream._running = True

        monkeypatch.setattr(stream, hook_name, hook_then_restart)
        teardown_spy = MagicMock(wraps=stream._teardown_transport)
        monkeypatch.setattr(stream, "_teardown_transport", teardown_spy)

        with patch("linc_nerve.core.scrcpy.subprocess.run"):
            t = threading.Thread(target=stream._read_loop, daemon=True)
            t.start()
            t.join(timeout=5.0)
            assert not t.is_alive(), "old read loop never exited"
        return new_sock, teardown_spy

    def test_idle_reap_then_restart_is_not_clobbered_by_old_finally(
        self, monkeypatch
    ) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "av", _mock_av_module())

        stream = ScrcpyStream("DEV-FINALLY", port=55404, idle_timeout_s=0.2)
        stream._running = True
        stream._server_proc = None
        stream._sock = _NeverDataSocket()
        stream._last_access = time.monotonic()

        new_sock, teardown_spy = self._run_old_loop_with_restart_after(
            monkeypatch, stream, "_reap"
        )

        assert stream.is_running, (
            "the reaping thread's `finally` must not flip `_running` back to "
            "False after a restart landed — that clobbers the new generation"
        )
        assert stream._sock is new_sock, (
            "the reaping thread must not tear down a transport started after "
            "its reap finished"
        )
        assert teardown_spy.call_count == 1, (
            "only the reap itself may tear down — the old thread's `finally` "
            "must not run a second teardown against the new transport"
        )

    def test_disconnect_exit_does_not_tear_down_a_newer_transport(
        self, monkeypatch
    ) -> None:
        """Same generation hazard on the non-reap exit: if the old loop's
        socket died (e.g. `stop()` closed it) and a restart then installed a
        NEW transport before the old thread reached `finally`, that `finally`
        must leave the new generation alone rather than tear it down."""
        import sys

        monkeypatch.setitem(sys.modules, "av", _mock_av_module())

        restart_state: dict[str, object] = {}

        class _DisconnectAfterMetaSocket(_NeverDataSocket):
            def recv(self, n: int) -> bytes:
                if self._buf:
                    return super().recv(n)
                # The old socket dies, but only after a restart has already
                # installed a newer transport (stop() + screenshot_pil()).
                new_sock = _InfiniteFrameSocket()
                restart_state["sock"] = new_sock
                stream._sock = new_sock
                stream._running = True
                raise ConnectionError("old socket closed")

        stream = ScrcpyStream("DEV-FINALLY2", port=55405, idle_timeout_s=5.0)
        stream._running = True
        stream._server_proc = None
        stream._sock = _DisconnectAfterMetaSocket()
        stream._last_access = time.monotonic()

        teardown_spy = MagicMock(wraps=stream._teardown_transport)
        monkeypatch.setattr(stream, "_teardown_transport", teardown_spy)

        with patch("linc_nerve.core.scrcpy.subprocess.run"):
            t = threading.Thread(target=stream._read_loop, daemon=True)
            t.start()
            t.join(timeout=5.0)
            assert not t.is_alive(), "old read loop never exited"

        assert stream._sock is restart_state["sock"], (
            "a dying old generation must not tear down the newer transport"
        )
        assert stream.is_running, (
            "a dying old generation must not flip the newer generation's "
            "`_running` flag to False"
        )
        teardown_spy.assert_not_called()

    def test_disconnect_exit_still_releases_its_own_transport(
        self, monkeypatch
    ) -> None:
        """The generation guard must not regress round-2's fix: a real
        disconnect with NO restart still tears down its own transport."""
        import sys

        monkeypatch.setitem(sys.modules, "av", _mock_av_module())

        class _DisconnectAfterMetaSocket(_NeverDataSocket):
            def recv(self, n: int) -> bytes:
                if self._buf:
                    return super().recv(n)
                raise ConnectionError("device unplugged")

        stream = ScrcpyStream("DEV-FINALLY3", port=55406, idle_timeout_s=5.0)
        stream._running = True
        proc = MagicMock()
        stream._server_proc = proc
        stream._sock = _DisconnectAfterMetaSocket()
        stream._last_access = time.monotonic()

        with patch("linc_nerve.core.scrcpy.subprocess.run"):
            t = threading.Thread(target=stream._read_loop, daemon=True)
            t.start()
            t.join(timeout=5.0)
            assert not t.is_alive(), "old read loop never exited"

        assert not stream.is_running
        assert stream._sock is None, "a real disconnect must release the socket"
        proc.kill.assert_called_once()
