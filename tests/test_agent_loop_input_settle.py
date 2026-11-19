"""Contract and unit tests for opt-in wait_idle_ms on input tools (linc-nerve-byh0)."""

from __future__ import annotations

import contextlib
import io
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from PIL import Image

from linc_nerve.cli.main import main
from linc_nerve.core import state_engine
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.emulator import EmulatorManager
from linc_nerve.core.ios import IOSController
from linc_nerve.core.metrics import MetricsCollector
from linc_nerve.core.state_engine import StateSessionManager
from linc_nerve.mcp import _core
from linc_nerve.mcp.input_tools import (
    _nerve_key_sync,
    _nerve_long_press_sync,
    _nerve_swipe_sync,
    _nerve_tap_sync,
    _nerve_type_sync,
)

pytestmark = pytest.mark.usefixtures("restore_signal_handlers")


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


class FakeInputController:
    """Fake platform controller recording ordered action and screenshot calls."""

    def __init__(
        self,
        frames: list[bytes] | None = None,
        clock: FakeClock | None = None,
        settle_on_third_poll: bool = False,
        fail_action: Exception | None = None,
        pil_frames: list[Image.Image] | None = None,
    ) -> None:
        self.events: list[str] = []
        self.actions: list[tuple[str, tuple[Any, ...]]] = []
        self.screenshot_calls: int = 0
        self.screenshot_pil_calls: int = 0
        self._frames = frames or [_make_gradient_png(0)]
        self._clock = clock
        self._settle_on_third_poll = settle_on_third_poll
        self._fail_action = fail_action
        self._pil_frames = pil_frames

    def discover(self) -> list[NerveDevice]:
        return [
            NerveDevice(
                serial="PIXEL8PRO",
                platform=Platform.ANDROID,
                device_type=DeviceType.PHONE,
                model="Pixel 8 Pro",
                os_version="14",
            )
        ]

    def _record_action(self, name: str, *args: Any) -> None:
        if self._fail_action is not None:
            raise self._fail_action
        self.events.append(name)
        self.actions.append((name, args))

    def tap(self, serial: str, x: int, y: int) -> None:
        self._record_action("tap", serial, x, y)

    def long_press(self, serial: str, x: int, y: int, duration_ms: int = 500) -> None:
        self._record_action("long_press", serial, x, y, duration_ms)

    def swipe(
        self,
        serial: str,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 300,
    ) -> None:
        self._record_action("swipe", serial, start_x, start_y, end_x, end_y, duration_ms)

    def type_text(self, serial: str, text: str) -> None:
        self._record_action("type_text", serial, text)

    def press_key(self, serial: str, key: str) -> None:
        self._record_action("press_key", serial, key)

    def screenshot_pil(self, serial: str) -> Image.Image | None:
        if self._pil_frames is None:
            return None
        self.events.append("screenshot_pil")
        idx = min(self.screenshot_pil_calls, len(self._pil_frames) - 1)
        self.screenshot_pil_calls += 1
        if (
            self._settle_on_third_poll
            and self._clock is not None
            and self.screenshot_pil_calls == 4
        ):
            self._clock.now = round(self._clock.now + 0.30, 9)
        return self._pil_frames[idx]

    def screenshot(self, serial: str) -> bytes:
        self.events.append("screenshot")
        idx = min(self.screenshot_calls, len(self._frames) - 1)
        self.screenshot_calls += 1
        if self._settle_on_third_poll and self._clock is not None and self.screenshot_calls == 3:
            self._clock.now = round(self._clock.now + 0.30, 9)
        return self._frames[idx]


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


def _install_fake_controller(ctrl: FakeInputController) -> None:
    _reset_singletons()
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    _core._android = ctrl  # type: ignore[assignment]
    _core._ios = mock_ios
    _core._state_mgr = MagicMock(spec=StateSessionManager)
    _core._metrics = MagicMock(spec=MetricsCollector)
    _core._device_mgr = MagicMock(spec=DeviceManager)
    _core._device_mgr.guard.return_value = None
    _core._emulator = MagicMock(spec=EmulatorManager)


TOOL_INVOCATIONS: tuple[tuple[str, Callable[..., dict[str, Any]], dict[str, Any], str], ...] = (
    ("nerve_tap", _nerve_tap_sync, {"device": "PIXEL8PRO", "x": 100, "y": 200}, "tap"),
    (
        "nerve_long_press",
        _nerve_long_press_sync,
        {"device": "PIXEL8PRO", "x": 100, "y": 200, "duration_ms": 600},
        "long_press",
    ),
    (
        "nerve_swipe",
        _nerve_swipe_sync,
        {
            "device": "PIXEL8PRO",
            "start_x": 10,
            "start_y": 20,
            "end_x": 30,
            "end_y": 40,
            "duration_ms": 300,
        },
        "swipe",
    ),
    ("nerve_type", _nerve_type_sync, {"device": "PIXEL8PRO", "text": "hello"}, "type_text"),
    ("nerve_key", _nerve_key_sync, {"device": "PIXEL8PRO", "key": "HOME"}, "press_key"),
)


@pytest.mark.parametrize(
    ("tool_name", "tool_fn", "call_kwargs", "_action_method"),
    TOOL_INVOCATIONS,
    ids=[t[0] for t in TOOL_INVOCATIONS],
)
@pytest.mark.parametrize(
    "bad_wait_idle_ms",
    (-5, 10001, True, False, -1, 1.5, "500", None),
    ids=["neg5", "gt10000", "bool_true", "bool_false", "neg1", "float", "str", "none"],
)
def test_contract_invalid_wait_idle_ms_rejected_before_action(
    tool_name: str,
    tool_fn: Callable[..., dict[str, Any]],
    call_kwargs: dict[str, Any],
    _action_method: str,
    bad_wait_idle_ms: Any,
) -> None:
    """[contract] Out-of-range or non-int wait_idle_ms returns tool error and records NO action."""
    ctrl = FakeInputController()
    _install_fake_controller(ctrl)
    try:
        result = tool_fn(**call_kwargs, wait_idle_ms=bad_wait_idle_ms)
        assert "error" in result, f"{tool_name} should reject wait_idle_ms={bad_wait_idle_ms!r}"
        assert "wait_idle_ms" in result["error"]
        assert result.get("validation_error") is True
        assert ctrl.actions == []
        assert ctrl.events == []
        assert ctrl.screenshot_calls == 0
    finally:
        _reset_singletons()


@pytest.mark.parametrize(
    ("tool_name", "tool_fn", "call_kwargs", "expected_event"),
    TOOL_INVOCATIONS,
    ids=[t[0] for t in TOOL_INVOCATIONS],
)
def test_wait_idle_ms_settles_on_third_poll_after_action(
    tool_name: str,
    tool_fn: Callable[..., dict[str, Any]],
    call_kwargs: dict[str, Any],
    expected_event: str,
) -> None:
    """[test] wait_idle_ms=500 with frames settling on 3rd poll returns idle.stable True."""
    clock = FakeClock()
    frame_changing = _make_gradient_png(0)
    frame_settled = _make_gradient_png(97)
    ctrl = FakeInputController(
        frames=[frame_changing, frame_settled, frame_settled],
        clock=clock,
        settle_on_third_poll=True,
    )
    _install_fake_controller(ctrl)
    orig_wait = state_engine.wait_for_idle
    try:
        with patch(
            "linc_nerve.core.state_engine.wait_for_idle",
            side_effect=lambda fn, timeout_ms, **kw: orig_wait(
                fn, timeout_ms, sleep=clock.sleep, clock=clock, **kw
            ),
        ):
            result = tool_fn(**call_kwargs, wait_idle_ms=500)
        assert result.get("success") is True, f"{tool_name} failed: {result}"
        assert "idle" in result
        idle = result["idle"]
        assert set(idle.keys()) == {"stable", "elapsed_ms", "polls", "reason"}
        assert idle["stable"] is True
        assert idle["polls"] == 3
        assert idle["reason"] == ""
        assert len(ctrl.actions) == 1
        assert ctrl.events == [expected_event, "screenshot", "screenshot", "screenshot"]
    finally:
        _reset_singletons()


def test_wait_idle_ms_omitted_returns_todays_dict_and_zero_screenshots() -> None:
    """[test] wait_idle_ms omitted returns exact today's dict (pin) and zero screenshot calls."""
    ctrl = FakeInputController()
    _install_fake_controller(ctrl)
    try:
        assert _nerve_tap_sync("PIXEL8PRO", 100, 200) == {
            "success": True,
            "action": "tap",
            "x": 100,
            "y": 200,
        }
        assert _nerve_long_press_sync("PIXEL8PRO", 100, 200, duration_ms=600) == {
            "success": True,
            "action": "long_press",
            "x": 100,
            "y": 200,
            "duration_ms": 600,
        }
        assert _nerve_swipe_sync("PIXEL8PRO", 10, 20, 30, 40, duration_ms=300) == {
            "success": True,
            "action": "swipe",
            "from": [10, 20],
            "to": [30, 40],
        }
        assert _nerve_type_sync("PIXEL8PRO", "hello") == {
            "success": True,
            "action": "type",
            "text": "hello",
        }
        assert _nerve_key_sync("PIXEL8PRO", "HOME") == {
            "success": True,
            "action": "key",
            "key": "HOME",
        }
        assert ctrl.screenshot_calls == 0
        assert ctrl.screenshot_pil_calls == 0
    finally:
        _reset_singletons()


def test_wait_idle_ms_explicit_zero_returns_todays_dict_and_zero_screenshots() -> None:
    """Explicit wait_idle_ms=0 returns exact today's dict and zero screenshot calls."""
    ctrl = FakeInputController()
    _install_fake_controller(ctrl)
    try:
        assert _nerve_tap_sync("PIXEL8PRO", 100, 200, wait_idle_ms=0) == {
            "success": True,
            "action": "tap",
            "x": 100,
            "y": 200,
        }
        assert ctrl.screenshot_calls == 0
        assert ctrl.screenshot_pil_calls == 0
    finally:
        _reset_singletons()


def test_wait_idle_ms_third_poll_frame_settles_over_standard_50ms_ticks() -> None:
    """Frames changing on polls 1-2 and settling from poll 3 onwards return stable True."""
    clock = FakeClock()
    f1 = _make_gradient_png(0)
    f2 = _make_gradient_png(53)
    f3 = _make_gradient_png(119)
    ctrl = FakeInputController(frames=[f1, f2, f3], clock=clock)
    _install_fake_controller(ctrl)
    orig_wait = state_engine.wait_for_idle
    try:
        with patch(
            "linc_nerve.core.state_engine.wait_for_idle",
            side_effect=lambda fn, timeout_ms, **kw: orig_wait(
                fn, timeout_ms, sleep=clock.sleep, clock=clock, **kw
            ),
        ):
            result = _nerve_tap_sync("PIXEL8PRO", 100, 200, wait_idle_ms=500)
        assert result["success"] is True
        assert result["idle"]["stable"] is True
        assert result["idle"]["reason"] == ""
        assert result["idle"]["elapsed_ms"] == 400.0
        assert result["idle"]["polls"] == 9
        assert ctrl.events[0] == "tap"
    finally:
        _reset_singletons()


def test_wait_idle_ms_timeout_and_ios_platform() -> None:
    """Unsettled frames report reason 'timeout'; iOS platform uses screenshot bytes path."""
    clock = FakeClock()
    frames = [_make_gradient_png(i * 41) for i in range(20)]
    ctrl = FakeInputController(frames=frames, clock=clock)
    _install_fake_controller(ctrl)
    _core._android.discover = lambda: []  # type: ignore[method-assign]
    _core._ios.discover.return_value = [
        NerveDevice(
            serial="IOS-SIM-1",
            platform=Platform.IOS,
            device_type=DeviceType.PHONE,
            model="iPhone 16",
            os_version="18.0",
        )
    ]
    _core._ios.tap.side_effect = lambda s, x, y: ctrl.tap(s, x, y)
    _core._ios.screenshot.side_effect = lambda s: ctrl.screenshot(s)
    orig_wait = state_engine.wait_for_idle
    try:
        with patch(
            "linc_nerve.core.state_engine.wait_for_idle",
            side_effect=lambda fn, timeout_ms, **kw: orig_wait(
                fn, timeout_ms, sleep=clock.sleep, clock=clock, **kw
            ),
        ):
            result = _nerve_tap_sync("IOS-SIM-1", 100, 200, wait_idle_ms=500)
        assert result["success"] is True
        assert result["idle"]["stable"] is False
        assert result["idle"]["reason"] == "timeout"
        assert ctrl.screenshot_pil_calls == 0
        assert ctrl.screenshot_calls >= 1
        assert ctrl.events[0] == "tap"
    finally:
        _reset_singletons()


@pytest.mark.parametrize(
    ("_tool_name", "tool_fn", "call_kwargs", "_expected_event"),
    TOOL_INVOCATIONS,
    ids=[t[0] for t in TOOL_INVOCATIONS],
)
def test_failed_action_with_wait_idle_ms_makes_zero_screenshot_calls(
    _tool_name: str,
    tool_fn: Callable[..., dict[str, Any]],
    call_kwargs: dict[str, Any],
    _expected_event: str,
) -> None:
    """[test] A failed tap (or other input tool) with wait_idle_ms=500 makes zero screenshots."""
    ctrl = FakeInputController(fail_action=RuntimeError("controller rejected input"))
    _install_fake_controller(ctrl)
    try:
        result = tool_fn(**call_kwargs, wait_idle_ms=500)
        assert "error" in result
        assert "idle" not in result
        assert ctrl.screenshot_calls == 0
        assert ctrl.screenshot_pil_calls == 0
    finally:
        _reset_singletons()


def test_failed_tap_variants_with_wait_idle_ms_make_zero_screenshot_calls() -> None:
    """NotImplementedError, grounded mismatch, missing device, and guard refusal do not poll."""
    ctrl_ni = FakeInputController(fail_action=NotImplementedError("not supported"))
    _install_fake_controller(ctrl_ni)
    try:
        assert "error" in _nerve_tap_sync("PIXEL8PRO", 10, 20, wait_idle_ms=500)
        assert "error" in _nerve_long_press_sync("PIXEL8PRO", 10, 20, wait_idle_ms=500)
        assert "error" in _nerve_swipe_sync("PIXEL8PRO", 1, 2, 3, 4, wait_idle_ms=500)
        assert ctrl_ni.screenshot_calls == 0
    finally:
        _reset_singletons()

    ctrl = FakeInputController()
    _install_fake_controller(ctrl)
    try:
        assert "error" in _nerve_tap_sync("UNKNOWN_DEV", 10, 20, wait_idle_ms=500)
        assert ctrl.screenshot_calls == 0

        _core._device_mgr.guard.return_value = "reserved by other"  # type: ignore[union-attr]
        assert "error" in _nerve_tap_sync("PIXEL8PRO", 10, 20, wait_idle_ms=500)
        assert ctrl.screenshot_calls == 0
        _core._device_mgr.guard.return_value = None  # type: ignore[union-attr]

        with patch(
            "linc_nerve.core.grounding.verified_tap",
            return_value={"error": "screen changed before tap"},
        ):
            res = _nerve_tap_sync("PIXEL8PRO", 10, 20, wait_idle_ms=500)
            assert res == {"error": "screen changed before tap"}
            assert ctrl.screenshot_calls == 0
    finally:
        _reset_singletons()


def test_tap_pil_fast_path_on_android_when_available() -> None:
    """When Android screenshot_pil is available, wait_idle_ms uses the PIL fast path."""
    clock = FakeClock()
    img1 = Image.open(io.BytesIO(_make_gradient_png(0))).copy()
    img2 = Image.open(io.BytesIO(_make_gradient_png(97))).copy()
    ctrl = FakeInputController(
        pil_frames=[img1, img1, img2, img2],
        clock=clock,
        settle_on_third_poll=True,
    )
    _install_fake_controller(ctrl)
    orig_wait = state_engine.wait_for_idle
    try:
        with patch(
            "linc_nerve.core.state_engine.wait_for_idle",
            side_effect=lambda fn, timeout_ms, **kw: orig_wait(
                fn, timeout_ms, sleep=clock.sleep, clock=clock, **kw
            ),
        ):
            result = _nerve_tap_sync("PIXEL8PRO", 50, 60, wait_idle_ms=500)
        assert result["success"] is True
        assert result["idle"]["stable"] is True
        assert ctrl.screenshot_calls == 0
        assert ctrl.screenshot_pil_calls >= 3
        assert ctrl.events[0] == "tap"
    finally:
        _reset_singletons()


def test_cli_nerve_tap_help_lists_wait_idle_ms() -> None:
    """[test] `nerve tap --help` (CLI schema) lists --wait-idle-ms."""
    runner = CliRunner()
    help_res = runner.invoke(main, ["tap", "--help"])
    assert help_res.exit_code == 0, help_res.output
    assert "--wait-idle-ms" in help_res.output

    for subcmd in ("long-press", "swipe", "type", "key"):
        sub_res = runner.invoke(main, [subcmd, "--help"])
        assert sub_res.exit_code == 0, sub_res.output
        assert "--wait-idle-ms" in sub_res.output

    with patch("linc_nerve.cli.main._call") as mock_call:
        mock_call.return_value = {"success": True}
        call_res = runner.invoke(
            main, ["--human", "tap", "DEV123", "200", "400", "--wait-idle-ms", "500"]
        )
        assert call_res.exit_code == 0, call_res.output
        mock_call.assert_called_once_with(
            "nerve_tap", {"device": "DEV123", "x": 200, "y": 400, "wait_idle_ms": 500}
        )


# ---------------------------------------------------------------------------
# Helper-level tests pinning the exact arguments threaded through the shared
# input-tool helpers (kills pr-diff-mutation survivors on PR #267).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", (-1, 10001, True, "500"), ids=["neg", "gt", "bool", "str"])
def test_contract_validate_wait_idle_ms_error_names_param_exactly(bad_value: Any) -> None:
    """[contract] The validation error names the ``wait_idle_ms`` parameter verbatim."""
    from linc_nerve.mcp.input_tools import _validate_wait_idle_ms

    result = _validate_wait_idle_ms(bad_value)
    assert result == {
        "error": f"wait_idle_ms must be an int in 0..10000, got {bad_value!r}",
        "validation_error": True,
    }


@pytest.mark.parametrize("good_value", (0, 1, 500, 10000), ids=["zero", "one", "mid", "max"])
def test_validate_wait_idle_ms_accepts_bounds(good_value: int) -> None:
    """In-range ints (inclusive bounds) validate cleanly."""
    from linc_nerve.mcp.input_tools import _validate_wait_idle_ms

    assert _validate_wait_idle_ms(good_value) is None


def test_tool_error_for_bad_wait_idle_ms_names_param_exactly() -> None:
    """[contract] The tool-level error string starts with the exact parameter name."""
    ctrl = FakeInputController()
    _install_fake_controller(ctrl)
    try:
        result = _nerve_key_sync("PIXEL8PRO", "HOME", wait_idle_ms=10001)
        assert result["error"] == "wait_idle_ms must be an int in 0..10000, got 10001"
        assert ctrl.actions == []
    finally:
        _reset_singletons()


@pytest.mark.parametrize("platform", ("android", "ios"))
def test_resolve_and_guard_threads_device_and_platform(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """The guard sees the device serial and the controller lookup sees the platform."""
    from linc_nerve.mcp import input_tools

    guard_calls: list[Any] = []
    controller_calls: list[Any] = []
    controllers = {"android": object(), "ios": object()}

    def fake_check_guard(serial: Any) -> None:
        guard_calls.append(serial)
        return None

    def fake_get_controller(plat: Any) -> Any:
        controller_calls.append(plat)
        return controllers[plat]

    monkeypatch.setattr(_core, "_resolve_device", lambda serial: (object(), platform))
    monkeypatch.setattr(_core, "_check_guard", fake_check_guard)
    monkeypatch.setattr(_core, "_get_controller", fake_get_controller)

    ctrl, resolved_platform, err = input_tools._resolve_and_guard("SERIAL-42")

    assert err is None
    assert resolved_platform == platform
    assert ctrl is controllers[platform]
    assert guard_calls == ["SERIAL-42"]
    assert controller_calls == [platform]


def test_resolve_and_guard_returns_guard_refusal_for_this_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guard that refuses only the requested serial blocks the call with its error."""
    from linc_nerve.mcp import input_tools

    def fake_check_guard(serial: Any) -> dict[str, str] | None:
        return {"error": "reserved by other"} if serial == "SERIAL-42" else None

    monkeypatch.setattr(_core, "_resolve_device", lambda serial: (object(), "android"))
    monkeypatch.setattr(_core, "_check_guard", fake_check_guard)
    monkeypatch.setattr(_core, "_get_controller", lambda plat: pytest.fail("must not resolve"))

    ctrl, platform, err = input_tools._resolve_and_guard("SERIAL-42")

    assert ctrl is None
    assert platform == "android"
    assert err == {"error": "reserved by other"}


def test_resolve_and_guard_unknown_device_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable device returns the resolver message without guard/controller calls."""
    from linc_nerve.mcp import input_tools

    monkeypatch.setattr(_core, "_resolve_device", lambda serial: (None, "Device X not found"))
    monkeypatch.setattr(_core, "_check_guard", lambda serial: pytest.fail("must not guard"))
    monkeypatch.setattr(_core, "_get_controller", lambda plat: pytest.fail("must not resolve"))

    assert input_tools._resolve_and_guard("X") == (
        None,
        "Device X not found",
        {"error": "Device X not found"},
    )


class _StabilitySpy:
    """Records the arguments _with_idle threads into the stability helpers."""

    def __init__(self) -> None:
        self.pil_calls: list[tuple[Any, ...]] = []
        self.poll_calls: list[tuple[Any, ...]] = []

    def check_pil_support(self, ctrl: Any, device: Any, platform: Any) -> bool:
        self.pil_calls.append((ctrl, device, platform))
        return True

    def poll_stability(self, ctrl: Any, device: Any, settle_ms: Any, use_pil: Any) -> dict:
        self.poll_calls.append((ctrl, device, settle_ms, use_pil))
        return {"stable": True, "elapsed_ms": 1.0, "polls": 2, "reason": "", "extra": "dropped"}


@pytest.mark.parametrize("wait_idle_ms", (1, 2, 500, 10000))
def test_with_idle_polls_for_any_positive_wait_and_threads_args(
    monkeypatch: pytest.MonkeyPatch, wait_idle_ms: int
) -> None:
    """Every positive wait (including 1 ms) polls, passing ctrl/device/platform/budget through."""
    from linc_nerve.mcp import input_tools

    spy = _StabilitySpy()
    monkeypatch.setattr(input_tools, "_check_pil_support", spy.check_pil_support)
    monkeypatch.setattr(input_tools, "_poll_stability", spy.poll_stability)
    ctrl = object()

    result = input_tools._with_idle(
        {"success": True, "action": "tap"}, ctrl, "SERIAL-7", "android", wait_idle_ms
    )

    assert spy.pil_calls == [(ctrl, "SERIAL-7", "android")]
    assert spy.poll_calls == [(ctrl, "SERIAL-7", wait_idle_ms, True)]
    assert result == {
        "success": True,
        "action": "tap",
        "idle": {"stable": True, "elapsed_ms": 1.0, "polls": 2, "reason": ""},
    }


@pytest.mark.parametrize(
    ("result", "wait_idle_ms"),
    (({"success": True}, 0), ({"error": "boom"}, 500), ({"error": "boom"}, 1)),
    ids=["zero_wait", "error_500", "error_1"],
)
def test_with_idle_skips_polling_for_zero_wait_or_error(
    monkeypatch: pytest.MonkeyPatch, result: dict[str, Any], wait_idle_ms: int
) -> None:
    """A zero wait or an error result returns the input dict untouched with no polling."""
    from linc_nerve.mcp import input_tools

    spy = _StabilitySpy()
    monkeypatch.setattr(input_tools, "_check_pil_support", spy.check_pil_support)
    monkeypatch.setattr(input_tools, "_poll_stability", spy.poll_stability)

    assert input_tools._with_idle(result, object(), "SERIAL-7", "android", wait_idle_ms) is result
    assert spy.pil_calls == []
    assert spy.poll_calls == []


def test_wait_idle_ms_one_polls_through_the_tool() -> None:
    """End-to-end: wait_idle_ms=1 runs the action and then polls at least one screenshot."""
    clock = FakeClock()
    ctrl = FakeInputController(frames=[_make_gradient_png(0)], clock=clock)
    _install_fake_controller(ctrl)
    orig_wait = state_engine.wait_for_idle
    try:
        with patch(
            "linc_nerve.core.state_engine.wait_for_idle",
            side_effect=lambda fn, timeout_ms, **kw: orig_wait(
                fn, timeout_ms, sleep=clock.sleep, clock=clock, **kw
            ),
        ):
            result = _nerve_tap_sync("PIXEL8PRO", 100, 200, wait_idle_ms=1)
        assert result["success"] is True
        assert "idle" in result
        assert ctrl.events[0] == "tap"
        assert ctrl.screenshot_calls >= 1
    finally:
        _reset_singletons()


def test_pil_probe_uses_the_requested_serial() -> None:
    """The Android PIL fast-path probe and polls screenshot the requested serial only."""
    clock = FakeClock()
    img = Image.open(io.BytesIO(_make_gradient_png(0))).copy()
    ctrl = FakeInputController(pil_frames=[img], clock=clock)
    pil_serials: list[Any] = []
    orig_pil = ctrl.screenshot_pil

    def recording_pil(serial: Any) -> Image.Image | None:
        pil_serials.append(serial)
        return orig_pil(serial)

    ctrl.screenshot_pil = recording_pil  # type: ignore[method-assign]
    _install_fake_controller(ctrl)
    orig_wait = state_engine.wait_for_idle
    try:
        with patch(
            "linc_nerve.core.state_engine.wait_for_idle",
            side_effect=lambda fn, timeout_ms, **kw: orig_wait(
                fn, timeout_ms, sleep=clock.sleep, clock=clock, **kw
            ),
        ):
            result = _nerve_tap_sync("PIXEL8PRO", 50, 60, wait_idle_ms=500)
        assert result["success"] is True
        assert pil_serials
        assert set(pil_serials) == {"PIXEL8PRO"}
    finally:
        _reset_singletons()
