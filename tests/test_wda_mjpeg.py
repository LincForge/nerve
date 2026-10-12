"""WDA MJPEG settings push + WdaMjpegManager."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from linc_nerve.core import wda_mjpeg
from linc_nerve.core.wda import (
    _MJPEG_FRAMERATE,
    _MJPEG_QUALITY,
    _MJPEG_SCALING,
    WDASession,
)
from linc_nerve.core.wda_mjpeg import WdaMjpegManager


def _session_with_mock_client() -> tuple[WDASession, MagicMock]:
    s = WDASession("serialX", 8100)
    s.mjpeg_port = 9100
    s._session_id = "sess-1"
    client = MagicMock()
    s._client = client
    return s, client


def test_configure_mjpeg_settings_posts_expected_payload():
    s, client = _session_with_mock_client()
    client.post.return_value = MagicMock(status_code=200)
    s._configure_mjpeg_settings()
    args, kwargs = client.post.call_args
    assert args[0] == "/session/sess-1/appium/settings"
    assert kwargs["json"]["settings"] == {
        "mjpegServerFramerate": _MJPEG_FRAMERATE,
        "mjpegServerScreenshotQuality": _MJPEG_QUALITY,
        "mjpegScalingFactor": _MJPEG_SCALING,
    }


def test_configure_mjpeg_settings_swallows_failure():
    s, client = _session_with_mock_client()
    client.post.side_effect = RuntimeError("boom")
    # Must not raise — best-effort, warn + continue.
    s._configure_mjpeg_settings()


def test_configure_mjpeg_settings_skipped_when_no_port():
    s, client = _session_with_mock_client()
    s.mjpeg_port = None
    s._configure_mjpeg_settings()
    client.post.assert_not_called()


# --- Task 5: WdaMjpegManager ---


class _FakeStream:
    """Stand-in for WdaMjpegStream with controllable frame_count + payload."""

    def __init__(self, url, *, connect_timeout_s=1.5, frames=1, payload=b"PNG"):
        self.url = url
        self._frames = frames
        self._payload = payload
        self.stopped = False
        self.running = True

    @property
    def is_running(self):
        return self.running

    def start(self):
        pass

    @property
    def frame_count(self):
        return self._frames

    def screenshot(self):
        if self._frames == 0:
            raise RuntimeError("no frame")
        return self._payload

    def stop(self):
        self.stopped = True


@pytest.fixture
def mgr(monkeypatch):
    monkeypatch.setattr(wda_mjpeg, "WdaMjpegStream", _FakeStream)
    return WdaMjpegManager()


def test_screenshot_returns_frame_bytes(mgr):
    assert mgr.screenshot("A", 9100) == b"PNG"


def test_reuses_stream_for_same_serial_and_port(mgr):
    mgr.screenshot("A", 9100)
    first = mgr._streams[("A", 9100)]
    mgr.screenshot("A", 9100)
    assert mgr._streams[("A", 9100)] is first


def test_port_change_invalidates_stale_stream(mgr):
    mgr.screenshot("A", 9100)
    stale = mgr._streams[("A", 9100)]
    mgr.screenshot("A", 9105)  # session restarted on a new port
    assert stale.stopped is True
    assert ("A", 9100) not in mgr._streams
    assert ("A", 9105) in mgr._streams


def test_zero_frame_stream_raises_and_records_failure(monkeypatch):
    monkeypatch.setattr(
        wda_mjpeg,
        "WdaMjpegStream",
        lambda *a, **k: _FakeStream(*a, frames=0, **k),
    )
    m = WdaMjpegManager()
    with pytest.raises(RuntimeError):
        m.screenshot("A", 9100)
    assert m._failures.get("A", 0) == 1


def test_circuit_opens_after_threshold_then_auto_clears(monkeypatch):
    monkeypatch.setattr(
        wda_mjpeg,
        "WdaMjpegStream",
        lambda *a, **k: _FakeStream(*a, frames=0, **k),
    )
    m = WdaMjpegManager()
    for _ in range(3):
        with pytest.raises(RuntimeError):
            m.screenshot("A", 9100)
    assert m.is_open("A") is True
    # Force cooldown to elapse → breaker auto-clears (half-open re-probe).
    m._open_until["A"] = 0.0
    assert m.is_open("A") is False


def test_stop_all_stops_every_stream(mgr):
    mgr.screenshot("A", 9100)
    mgr.screenshot("B", 9101)
    streams = list(mgr._streams.values())
    mgr.stop_all()
    assert all(s.stopped for s in streams)
    assert mgr._streams == {}


# --- B1: idle reaping (a stream nobody reads must not decode forever) ---


def _jpeg_bytes() -> bytes:
    """A real (tiny) JPEG so Image.open/load in the read loop does real work."""
    import io as _io

    from PIL import Image as _Image

    buf = _io.BytesIO()
    _Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


class _FakeResponse:
    """Streams an endless MJPEG multipart body, like a live WDA server."""

    def __init__(self, frame: bytes, boundary: bytes = b"--BoundaryString"):
        self.headers = {"content-type": "multipart/x-mixed-replace; boundary=--BoundaryString"}
        self._frame = frame
        self._boundary = boundary

    def iter_bytes(self):
        part = (
            self._boundary
            + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(self._frame)).encode()
            + b"\r\n\r\n"
            + self._frame
        )
        while True:
            yield part
            time.sleep(0.002)  # keep the fake server from pegging the test runner

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeHttpxClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, _method, _url):
        return _FakeResponse(_jpeg_bytes())


@pytest.fixture
def live_stream(monkeypatch):
    """Patch httpx so WdaMjpegStream reads an endless local MJPEG body."""
    monkeypatch.setattr(wda_mjpeg.httpx, "Client", _FakeHttpxClient)
    made: list[wda_mjpeg.WdaMjpegStream] = []

    def _make(**kwargs):
        s = wda_mjpeg.WdaMjpegStream("http://localhost:9100", **kwargs)
        made.append(s)
        return s

    yield _make
    for s in made:
        s.stop()


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_idle_stream_self_reaps(live_stream):
    """An unread stream must stop decoding instead of burning a core forever."""
    s = live_stream(idle_timeout_s=0.15)
    s.start()
    assert s.frame_count > 0, "stream should decode at least one frame while warm"
    assert _wait_until(lambda: not s.is_running), "idle stream never self-reaped"


def test_stream_stays_alive_while_being_read(live_stream):
    """Active screenshot traffic must keep the warming cache warm."""
    s = live_stream(idle_timeout_s=0.3)
    s.start()
    deadline = time.monotonic() + 0.9
    while time.monotonic() < deadline:
        s.screenshot_pil()
        time.sleep(0.02)
    assert s.is_running, "stream was reaped despite continuous reads"


def test_idle_reaped_stream_is_replaced_by_manager(mgr):
    """A self-reaped stream must not be served from cache — start a fresh one."""
    mgr.screenshot("A", 9100)
    dead = mgr._streams[("A", 9100)]
    dead.running = False  # simulate the read loop self-reaping after idle
    mgr.screenshot("A", 9100)
    assert mgr._streams[("A", 9100)] is not dead, "manager reused a dead stream"
    assert dead.stopped is True, "manager must stop the reaped stream it drops"
