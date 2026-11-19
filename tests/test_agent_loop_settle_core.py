"""Contract and unit tests for wait_for_idle and nerve_navigate_screenshot settle bounds."""

from __future__ import annotations

import contextlib
import io
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from linc_nerve.core import state_engine
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.emulator import EmulatorManager
from linc_nerve.core.ios import IOSController
from linc_nerve.core.metrics import MetricsCollector
from linc_nerve.core.state_engine import StateSessionManager
from linc_nerve.mcp import _core, _stability
from linc_nerve.mcp.navigation_tools import _nerve_navigate_screenshot_sync


class FakeClock:
    """Deterministic monotonic clock + sleep pair with zero real sleeping."""

    def __init__(self, start: float = 10.0) -> None:
        self.now: float = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now = round(self.now + seconds, 9)


def _make_png(color: tuple[int, int, int] = (128, 128, 128), size: int = 32) -> bytes:
    img = Image.new("RGB", (size, size), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_gradient_png(offset: int = 0, size: int = 32) -> bytes:
    img = Image.new("L", (size, size))
    pixels = img.load()
    assert pixels is not None
    for y in range(size):
        for x in range(size):
            pixels[x, y] = (x * 7 + y * 13 + offset) % 256
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _reset_singletons() -> None:
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


def _setup_mocks(screenshot_bytes: bytes | None = None) -> MagicMock:
    _reset_singletons()
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
    mock_android.screenshot.return_value = screenshot_bytes or _make_png()
    mock_android.screenshot_pil.return_value = None
    mock_android.elements.return_value = []
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    _core._android = mock_android
    _core._ios = mock_ios
    _core._state_mgr = MagicMock(spec=StateSessionManager)
    _core._metrics = MagicMock(spec=MetricsCollector)
    _core._device_mgr = MagicMock(spec=DeviceManager)
    _core._device_mgr.guard.return_value = None
    _core._emulator = MagicMock(spec=EmulatorManager)
    return mock_android


INVALID_TIMEOUTS: tuple[Any, ...] = (-1, 10001, True, False, 1.5, "500", None)


@pytest.mark.parametrize("bad_timeout", INVALID_TIMEOUTS)
def test_wait_for_idle_rejects_invalid_timeout_ms(bad_timeout: Any) -> None:
    """Precondition 0 <= timeout_ms <= 10000 (int, not bool); else ValueError."""
    clock = FakeClock()
    calls = 0

    def screenshot_fn() -> bytes:
        nonlocal calls
        calls += 1
        return _make_png()

    with pytest.raises(ValueError, match=r"^timeout_ms must be an int in 0\.\.10000"):
        state_engine.wait_for_idle(screenshot_fn, bad_timeout, sleep=clock.sleep, clock=clock)
    assert calls == 0
    assert clock.sleeps == []


@pytest.mark.parametrize("bad_settle", INVALID_TIMEOUTS)
def test_navigate_screenshot_rejects_invalid_settle_ms_without_navigating(
    bad_settle: Any,
) -> None:
    """Out-of-range or non-int settle_ms returns tool validation_error without navigating."""
    mock_android = _setup_mocks()
    try:
        with patch("linc_nerve.mcp.app_tools._nerve_launch_sync") as mock_launch:
            result = _nerve_navigate_screenshot_sync(
                "PIXEL8PRO", "precisionlevel", "home", settle_ms=bad_settle
            )
            assert "error" in result
            assert "settle_ms" in result["error"]
            assert result.get("validation_error") is True
            mock_launch.assert_not_called()
            mock_android.launch.assert_not_called()
            mock_android.screenshot.assert_not_called()
    finally:
        _reset_singletons()


def test_wait_for_idle_capture_failed_reports_reason_and_waits_remaining_budget() -> None:
    """Screenshot callable raising returns stable False, capture_failed reason, polls >= 1."""
    clock = FakeClock()
    long_msg = "adb transport dropped: " + "".join(str(i % 10) for i in range(250))

    def broken_screenshot() -> bytes:
        clock.now = round(clock.now + 0.12044, 9)  # 120.4ms spent before exception
        raise RuntimeError(long_msg)

    result = state_engine.wait_for_idle(broken_screenshot, 500, sleep=clock.sleep, clock=clock)

    assert result["stable"] is False
    assert result["polls"] == 1
    assert result["elapsed_ms"] == 500.0
    assert result["last_frame"] is None
    assert result["last_frame_pil"] is None
    assert clock.sleeps == [pytest.approx(0.3796)]
    expected_reason = f"capture_failed: RuntimeError: {long_msg[:200]}"
    assert result["reason"] == expected_reason
    assert len(result["reason"]) == len("capture_failed: RuntimeError: ") + 200


def test_wait_for_idle_capture_failed_on_second_poll_and_remaining_boundaries() -> None:
    """Failing on poll 2 records polls=2; 0ms vs 1ms remaining budget handled accurately."""
    clock = FakeClock()
    png = _make_png()
    calls = 0

    def fails_on_second() -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return png
        raise ValueError("corrupt frame")

    result = state_engine.wait_for_idle(fails_on_second, 50, sleep=clock.sleep, clock=clock)
    assert result["stable"] is False
    assert result["polls"] == 2
    assert result["elapsed_ms"] == 50.0
    assert clock.sleeps == [pytest.approx(0.05)]
    assert result["reason"] == "capture_failed: ValueError: corrupt frame"

    clock_1ms = FakeClock()

    def fails_at_499ms() -> bytes:
        clock_1ms.now = round(clock_1ms.now + 0.499, 9)
        raise RuntimeError("almost done")

    res_1ms = state_engine.wait_for_idle(
        fails_at_499ms, 500, sleep=clock_1ms.sleep, clock=clock_1ms
    )
    assert res_1ms["elapsed_ms"] == 500.0
    assert clock_1ms.sleeps == [pytest.approx(0.001)]


def test_wait_for_idle_capture_failed_past_budget_and_noop_sleep() -> None:
    """Capture failing past budget does not sleep; no-op sleep still reports >= timeout_ms."""
    clock = FakeClock()

    def slow_broken_screenshot() -> bytes:
        clock.now = round(clock.now + 0.60044, 9)
        raise OSError("timed out reading socket")

    result = state_engine.wait_for_idle(
        slow_broken_screenshot, 500, sleep=clock.sleep, clock=clock
    )
    assert result["stable"] is False
    assert result["polls"] == 1
    assert result["elapsed_ms"] == 600.4
    assert clock.sleeps == []
    assert result["reason"] == "capture_failed: OSError: timed out reading socket"

    noop_sleeps: list[float] = []
    res_noop = state_engine.wait_for_idle(
        lambda: (_ for _ in ()).throw(RuntimeError("fail")),
        400,
        sleep=noop_sleeps.append,
        clock=lambda: 50.0,
    )
    assert res_noop["stable"] is False
    assert res_noop["elapsed_ms"] == 400.0
    assert noop_sleeps == [pytest.approx(0.4)]


def test_wait_for_idle_changing_frames_return_timeout_reason() -> None:
    """Frames that never repeat return stable False and reason 'timeout' without real sleeping."""
    clock = FakeClock()
    frames = [_make_gradient_png(offset=i * 37) for i in range(20)]
    idx = 0

    def changing_screenshot() -> bytes:
        nonlocal idx
        if idx == 0:
            clock.now = round(clock.now + 0.00044, 9)
        frame = frames[idx % len(frames)]
        idx += 1
        return frame

    result = state_engine.wait_for_idle(changing_screenshot, 500, sleep=clock.sleep, clock=clock)

    assert result["stable"] is False
    assert result["reason"] == "timeout"
    assert result["elapsed_ms"] == 500.4
    assert result["polls"] == 11
    assert result["last_frame"] == frames[10]
    assert result["last_frame_pil"] is None


def test_wait_for_idle_identical_frames_return_stable_empty_reason() -> None:
    """Identical frames return stable True and reason '' without real sleeping."""
    clock = FakeClock()
    png = _make_png((64, 128, 192))
    seen = 0

    def steady_screenshot() -> bytes:
        nonlocal seen
        seen += 1
        if seen == 7:
            clock.now = round(clock.now + 0.00044, 9)
        return png

    result = state_engine.wait_for_idle(steady_screenshot, 500, sleep=clock.sleep, clock=clock)

    assert result["stable"] is True
    assert result["reason"] == ""
    assert result["elapsed_ms"] == 300.4
    assert result["polls"] == 7
    assert result["last_frame"] == png
    assert result["last_frame_pil"] is None


def test_wait_for_stable_ui_defaults_and_threshold_boundary() -> None:
    """wait_for_stable_ui honours defaults and resets stability only when distance > threshold."""
    clock = FakeClock()
    png = _make_png((10, 20, 30))
    res_stable = state_engine.wait_for_stable_ui(lambda: png, sleep=clock.sleep, clock=clock)
    assert res_stable["stable"] is True
    assert res_stable["elapsed_ms"] == 300.0
    assert res_stable["polls"] == 7
    assert clock.sleeps == [0.05] * 6

    clock_to = FakeClock()
    frames = [_make_gradient_png(offset=i * 37) for i in range(80)]
    idx = 0

    def never_stable() -> bytes:
        nonlocal idx
        f = frames[idx % len(frames)]
        idx += 1
        return f

    res_to = state_engine.wait_for_stable_ui(never_stable, sleep=clock_to.sleep, clock=clock_to)
    assert res_to["stable"] is False
    assert res_to["elapsed_ms"] == 3000.0
    assert res_to["polls"] == 61

    # Distance == threshold (5) does not reset; distance == threshold + 1 (6) resets last_hash.
    with patch.object(state_engine, "_average_hash", side_effect=[0, 0b011111, 0b011111]):
        c_eq = FakeClock()
        r_eq = state_engine.wait_for_stable_ui(
            lambda: b"x",
            stable_ms=250,
            poll_ms=125,
            timeout_ms=1000,
            sleep=c_eq.sleep,
            clock=c_eq,
        )
        assert r_eq["stable"] is True
        assert r_eq["polls"] == 3
        assert r_eq["elapsed_ms"] == 250.0

    with patch.object(
        state_engine, "_average_hash", side_effect=[0, 0b111111, 0b111111, 0b111111]
    ):
        c_gt = FakeClock()
        r_gt = state_engine.wait_for_stable_ui(
            lambda: b"x",
            stable_ms=250,
            poll_ms=125,
            timeout_ms=1000,
            sleep=c_gt.sleep,
            clock=c_gt,
        )
        assert r_gt["stable"] is True
        assert r_gt["polls"] == 4
        assert r_gt["elapsed_ms"] == 375.0

    # Verify /1000.0 conversion boundaries for stable_ms and timeout_ms.
    with patch.object(state_engine, "_average_hash", side_effect=[0, 0, 0]):
        c_div = FakeClock()
        r_div = state_engine.wait_for_stable_ui(
            lambda: b"x",
            stable_ms=1000,
            poll_ms=999.5,  # type: ignore[arg-type]
            timeout_ms=5000,
            sleep=c_div.sleep,
            clock=c_div,
        )
        assert r_div["stable"] is True
        assert r_div["polls"] == 3

    with patch.object(state_engine, "_average_hash", side_effect=[0, 0b111111, 0]):
        c_tdiv = FakeClock()
        r_tdiv = state_engine.wait_for_stable_ui(
            lambda: b"x",
            stable_ms=5000,
            poll_ms=999.5,  # type: ignore[arg-type]
            timeout_ms=1000,
            sleep=c_tdiv.sleep,
            clock=c_tdiv,
        )
        assert r_tdiv["stable"] is False
        assert r_tdiv["polls"] == 3


def test_wait_for_idle_pil_mode_and_bounds() -> None:
    """wait_for_idle supports pil_mode=True and boundary timeouts 0 and 10000."""
    assert state_engine.validate_idle_timeout_ms(0, "timeout_ms") == 0
    assert state_engine.validate_idle_timeout_ms(10000, "timeout_ms") == 10000

    clock = FakeClock()
    pil_img = Image.new("RGB", (32, 32), (10, 20, 30))

    res_zero = state_engine.wait_for_idle(
        lambda: pil_img, 0, pil_mode=True, sleep=clock.sleep, clock=clock
    )
    assert res_zero["stable"] is False
    assert res_zero["reason"] == "timeout"
    assert res_zero["polls"] == 1
    assert res_zero["elapsed_ms"] == 0.0
    assert res_zero["last_frame"] is None
    assert res_zero["last_frame_pil"] is pil_img

    res_max = state_engine.wait_for_idle(
        lambda: pil_img, 10000, pil_mode=True, sleep=clock.sleep, clock=clock
    )
    assert res_max["stable"] is True
    assert res_max["reason"] == ""
    assert res_max["last_frame"] is None
    assert res_max["last_frame_pil"] is pil_img


def test_poll_stability_delegates_to_wait_for_idle_pil_and_bytes() -> None:
    """_poll_stability is a thin call into wait_for_idle for both PIL and bytes paths."""
    ctrl = MagicMock()
    pil_img = Image.new("RGB", (16, 16), (1, 2, 3))
    ctrl.screenshot_pil.return_value = pil_img
    ctrl.screenshot.return_value = _make_png()

    with patch("linc_nerve.core.state_engine.wait_for_idle") as mock_idle:
        mock_idle.return_value = {
            "stable": True,
            "elapsed_ms": 300.0,
            "polls": 7,
            "reason": "",
            "last_frame_pil": pil_img,
        }
        res_pil = _stability._poll_stability(ctrl, "DEV1", 400, use_pil=True)
        assert res_pil["reason"] == ""
        fn_arg, timeout_arg = mock_idle.call_args.args
        assert timeout_arg == 400
        assert mock_idle.call_args.kwargs.get("pil_mode") is True
        assert fn_arg() is pil_img
        ctrl.screenshot_pil.assert_called_once_with("DEV1")

        res_bytes = _stability._poll_stability(ctrl, "DEV1", 600, use_pil=False)
        assert res_bytes["reason"] == ""
        fn_arg2, timeout_arg2 = mock_idle.call_args.args
        assert timeout_arg2 == 600
        assert mock_idle.call_args.kwargs.get("pil_mode") is False
        assert fn_arg2() == ctrl.screenshot.return_value
        ctrl.screenshot.assert_called_once_with("DEV1")


def test_build_nav_screenshot_result_defaults() -> None:
    """_build_nav_screenshot_result defaults and optional elements handling."""
    res = _stability._build_nav_screenshot_result(
        "precisionlevel", "home", {}, "/tmp/out.png", 123, 500, {}, False, [{"id": "ignored"}]
    )
    assert res["uri"] == ""
    assert res["stability_reason"] == ""
    assert res["stable"] is False
    assert res["stability_ms"] == 0
    assert res["stability_polls"] == 0
    assert "elements" not in res
    assert "element_count" not in res

    res_ann = _stability._build_nav_screenshot_result(
        "precisionlevel",
        "home",
        {"uri": "precisionlevel://screen/home"},
        "/tmp/out.png",
        123,
        500,
        {"stable": False, "elapsed_ms": 500.0, "polls": 11, "reason": "timeout"},
        True,
        [{"id": "btn"}],
    )
    assert res_ann["uri"] == "precisionlevel://screen/home"
    assert res_ann["stability_reason"] == "timeout"
    assert res_ann["elements"] == [{"id": "btn"}]
    assert res_ann["element_count"] == 1


def test_navigate_screenshot_default_args_keys_pin() -> None:
    """nerve_navigate_screenshot with default args returns existing keys + stability_reason."""
    png = _make_png((80, 90, 100))
    _setup_mocks(screenshot_bytes=png)
    try:
        with (
            patch(
                "linc_nerve.mcp.app_tools._nerve_launch_sync",
                return_value={"success": True, "uri": "precisionlevel://screen/home"},
            ),
            patch(
                "linc_nerve.core.state_engine.wait_for_stable_ui",
                return_value={
                    "stable": True,
                    "elapsed_ms": 150.0,
                    "polls": 4,
                    "last_frame": png,
                },
            ),
        ):
            result = _nerve_navigate_screenshot_sync("PIXEL8PRO", "precisionlevel", "home")
            expected_keys = {
                "success",
                "action",
                "app_id",
                "route_id",
                "uri",
                "screenshot",
                "size_bytes",
                "settle_ms",
                "stable",
                "stability_ms",
                "stability_polls",
                "stability_reason",
            }
            assert set(result.keys()) == expected_keys
            assert result["success"] is True
            assert result["action"] == "navigate_screenshot"
            assert result["app_id"] == "precisionlevel"
            assert result["route_id"] == "home"
            assert result["uri"] == "precisionlevel://screen/home"
            assert result["settle_ms"] == 500
            assert result["stable"] is True
            assert result["stability_ms"] == 150.0
            assert result["stability_polls"] == 4
            assert result["stability_reason"] == ""
    finally:
        _reset_singletons()
