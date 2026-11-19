"""Tests for MCP wrapper per-tool ceilings and nerve_record_fixed bounds (linc-nerve-us7g)."""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import anyio
import pytest

from linc_nerve.cli.main import _registry_server
from linc_nerve.core import action_log
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.mcp import _core, recording_tools
from linc_nerve.mcp.recording_tools import (
    _nerve_record_fixed_sync,
    _record_fixed,
    _validate_record_fixed_duration,
)


def _android_device(serial: str = "PIXEL8PRO") -> NerveDevice:
    return NerveDevice(
        serial=serial,
        platform=Platform.ANDROID,
        device_type=DeviceType.PHONE,
        model="Pixel 8 Pro",
        os_version="15",
    )


def _call_device_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    srv = _registry_server()
    result = anyio.run(srv.call_tool, tool_name, arguments)
    return json.loads(result.content[0].text)


def _scaled_queue_factory(
    recorded_timeouts: list[float | int | None],
    scale: float = 0.001,
) -> Any:
    """Return a queue.Queue factory whose get(timeout=...) scales 1 s = 1 ms."""
    real_queue_cls = queue.Queue

    class _ScaledQueue(real_queue_cls):  # type: ignore[type-arg]
        def get(self, block: bool = True, timeout: float | None = None) -> Any:
            recorded_timeouts.append(timeout)
            scaled = timeout * scale if timeout is not None else None
            return super().get(block=block, timeout=scaled)

    return _ScaledQueue


def test_nerve_install_mcp_wrapper_uses_150s_ceiling_and_honest_message() -> None:
    """[test] nerve_install times out at 150 units (not 90) with 'may still be running'."""
    action_log.REGISTRY.reset()
    _core._device_semaphores.clear()

    block_event = threading.Event()
    started_event = threading.Event()
    still_blocked_at_90ms: list[bool] = []
    recorded_timeouts: list[float | int | None] = []

    fake_ctrl = MagicMock()

    def _blocking_install(serial: str, path: str) -> None:
        started_event.set()
        block_event.wait(timeout=2.0)

    fake_ctrl.install.side_effect = _blocking_install

    def _probe_90ms() -> None:
        started_event.wait(timeout=1.0)
        time.sleep(0.095)
        still_blocked_at_90ms.append(not block_event.is_set())

    probe = threading.Thread(target=_probe_90ms, daemon=True)
    probe.start()

    t0 = time.monotonic()
    try:
        with (
            patch.object(_core, "_resolve_device", return_value=(_android_device(), "android")),
            patch.object(_core, "_check_guard", return_value=None),
            patch.object(_core, "_get_controller", return_value=fake_ctrl),
            patch.object(_core.queue, "Queue", _scaled_queue_factory(recorded_timeouts)),
            patch.object(_core.log, "error") as mock_log_err,
        ):
            result = _call_device_tool(
                "nerve_install",
                {"device": "PIXEL8PRO", "path": "/tmp/app.apk"},
            )
        elapsed_s = time.monotonic() - t0
    finally:
        block_event.set()
        probe.join(timeout=1.0)

    assert started_event.is_set()
    assert still_blocked_at_90ms == [True]
    assert recorded_timeouts == [150]
    assert elapsed_s >= 0.12
    mock_log_err.assert_called_once_with(
        "device_call_timeout",
        serial="PIXEL8PRO",
        timeout_s=150,
    )
    assert result == {
        "error": (
            "nerve_install did not finish within 150s and may still be running "
            "on the device; check its result before retrying."
        ),
        "device": "PIXEL8PRO",
    }
    assert "may still be running" in result["error"]
    assert "Device may be unresponsive" not in result["error"]


def test_nerve_install_mcp_wrapper_completes_when_worker_finishes_between_90_and_150_units() -> (
    None
):
    """[test] Worker finishing at 105 units (>90, <150) succeeds under the 150-unit ceiling."""
    action_log.REGISTRY.reset()
    _core._device_semaphores.clear()

    block_event = threading.Event()
    recorded_timeouts: list[float | int | None] = []
    fake_ctrl = MagicMock()

    def _install_105ms(serial: str, path: str) -> None:
        block_event.wait(timeout=0.105)

    fake_ctrl.install.side_effect = _install_105ms

    try:
        with (
            patch.object(_core, "_resolve_device", return_value=(_android_device(), "android")),
            patch.object(_core, "_check_guard", return_value=None),
            patch.object(_core, "_get_controller", return_value=fake_ctrl),
            patch.object(_core.queue, "Queue", _scaled_queue_factory(recorded_timeouts)),
        ):
            result = _call_device_tool(
                "nerve_install",
                {"device": "PIXEL8PRO", "path": "/tmp/app.apk"},
            )
    finally:
        block_event.set()

    assert recorded_timeouts == [150]
    assert result == {"success": True, "action": "install", "path": "/tmp/app.apk"}


def test_nerve_tap_mcp_wrapper_pins_90s_ceiling_and_unlisted_message() -> None:
    """[test] nerve_tap still times out at 90 units with today's unlisted timeout text (pin)."""
    action_log.REGISTRY.reset()
    _core._device_semaphores.clear()

    block_event = threading.Event()
    recorded_timeouts: list[float | int | None] = []
    fake_ctrl = MagicMock()

    def _blocking_tap(serial: str, x: int, y: int) -> None:
        block_event.wait(timeout=2.0)

    fake_ctrl.tap.side_effect = _blocking_tap

    try:
        with (
            patch.object(_core, "_resolve_device", return_value=(_android_device(), "android")),
            patch.object(_core, "_check_guard", return_value=None),
            patch.object(_core, "_get_controller", return_value=fake_ctrl),
            patch.object(_core.queue, "Queue", _scaled_queue_factory(recorded_timeouts)),
        ):
            result = _call_device_tool(
                "nerve_tap",
                {"device": "PIXEL8PRO", "x": 100, "y": 200},
            )
    finally:
        block_event.set()

    assert recorded_timeouts == [90]
    assert result == {
        "error": (
            "Operation timed out after 90s. "
            "Device may be unresponsive — try nerve_discover to check connectivity."
        ),
        "device": "PIXEL8PRO",
    }


def test_listed_recording_tools_use_mcp_ceilings_and_honest_messages() -> None:
    """[test] nerve_record_fixed (160s) and nerve_record_stop (150s) use per-tool ceilings."""
    action_log.REGISTRY.reset()
    _core._device_semaphores.clear()

    block_event = threading.Event()
    recorded_timeouts: list[float | int | None] = []
    fake_ctrl = MagicMock()

    def _blocking_stop(serial: str) -> str:
        block_event.wait(timeout=2.0)
        return "/tmp/out.mp4"

    fake_ctrl.stop_recording.side_effect = _blocking_stop

    try:
        with (
            patch.object(_core, "_resolve_device", return_value=(_android_device(), "android")),
            patch.object(_core, "_check_guard", return_value=None),
            patch.object(_core, "_get_controller", return_value=fake_ctrl),
            patch("linc_nerve.mcp.recording_tools.time.sleep"),
            patch.object(_core.queue, "Queue", _scaled_queue_factory(recorded_timeouts)),
        ):
            stop_result = _call_device_tool("nerve_record_stop", {"device": "PIXEL8PRO"})
            fixed_result = _call_device_tool(
                "nerve_record_fixed",
                {"device": "PIXEL8PRO", "duration_seconds": 15},
            )
    finally:
        block_event.set()

    assert recorded_timeouts == [150, 160]
    assert stop_result == {
        "error": (
            "nerve_record_stop did not finish within 150s and may still be running "
            "on the device; check its result before retrying."
        ),
        "device": "PIXEL8PRO",
    }
    assert fixed_result == {
        "error": (
            "nerve_record_fixed did not finish within 160s and may still be running "
            "on the device; check its result before retrying."
        ),
        "device": "PIXEL8PRO",
    }


@pytest.mark.parametrize(
    "bad_duration",
    [0, -1, 121, 10**9, True, False, 1.5, "15", None],
    ids=[
        "zero",
        "neg_one",
        "over_max_121",
        "one_billion",
        "bool_true",
        "bool_false",
        "float_1_5",
        "str_15",
        "none",
    ],
)
def test_nerve_record_fixed_rejects_invalid_duration_before_starting_recording(
    bad_duration: Any,
) -> None:
    """[contract] Invalid duration_seconds returns validation_error before start_recording."""
    action_log.REGISTRY.reset()
    fake_device = MagicMock()

    with (
        patch(
            "linc_nerve.mcp.recording_tools.get_device_sync",
            return_value=fake_device,
        ) as mock_get_dev,
        patch("linc_nerve.mcp.recording_tools.time.sleep") as mock_sleep,
    ):
        result = _nerve_record_fixed_sync("PIXEL8PRO", duration_seconds=bad_duration)

    assert result == {
        "error": "duration_seconds must be an integer from 1 to 120",
        "validation_error": True,
    }
    fake_device.start_recording.assert_not_called()
    fake_device.stop_recording.assert_not_called()
    mock_get_dev.assert_not_called()
    mock_sleep.assert_not_called()


@pytest.mark.parametrize(
    "valid_duration",
    [1, 15, 120],
    ids=["min_1", "default_15", "max_120"],
)
def test_nerve_record_fixed_records_for_valid_durations(valid_duration: int) -> None:
    """[contract] Valid duration_seconds in (1, 15, 120) records with time.sleep faked (pin)."""
    action_log.REGISTRY.reset()
    fake_device = MagicMock()
    fake_device.stop_recording.return_value = "/tmp/recorded.mp4"

    with (
        patch(
            "linc_nerve.mcp.recording_tools.get_device_sync",
            return_value=fake_device,
        ) as mock_get_dev,
        patch("linc_nerve.mcp.recording_tools.time.sleep") as mock_sleep,
    ):
        result = _nerve_record_fixed_sync("PIXEL8PRO", duration_seconds=valid_duration)

    mock_get_dev.assert_called_once_with("PIXEL8PRO")
    fake_device.start_recording.assert_called_once_with("PIXEL8PRO")
    mock_sleep.assert_called_once_with(valid_duration)
    fake_device.stop_recording.assert_called_once_with("PIXEL8PRO")
    assert result == {"status": "success", "file_path": "/tmp/recorded.mp4"}


def test_run_with_timeout_and_run_on_device_defaults_and_default_duration() -> None:
    """[test] Direct _run_with_timeout / _run_on_device defaults and default duration (pin)."""
    assert _core._DEVICE_CALL_TIMEOUT == 90
    assert callable(_core.device_tool)
    assert _core._timeout_error_message(150, "nerve_install") == (
        "nerve_install did not finish within 150s and may still be running "
        "on the device; check its result before retrying."
    )
    assert _core._timeout_error_message(90, "nerve_tap") == (
        "Operation timed out after 90s. "
        "Device may be unresponsive — try nerve_discover to check connectivity."
    )
    assert _validate_record_fixed_duration(1) is None
    assert _validate_record_fixed_duration(120) is None
    assert _validate_record_fixed_duration(0) == {
        "error": "duration_seconds must be an integer from 1 to 120",
        "validation_error": True,
    }
    assert _record_fixed("PIXEL8PRO", 0) == {
        "error": "duration_seconds must be an integer from 1 to 120",
        "validation_error": True,
    }
    assert recording_tools._validate_record_fixed_duration(121) == {
        "error": "duration_seconds must be an integer from 1 to 120",
        "validation_error": True,
    }

    captured_wrappers: dict[str, Any] = {}

    def _capture_register(wrapper: Any, fn: Any, tool_name: str) -> None:
        captured_wrappers[tool_name] = wrapper

    install_block = threading.Event()
    install_timeouts: list[float | int | None] = []

    def _nerve_install_sync(device: str, path: str) -> dict[str, Any]:
        """Install an app on the device.

        Args:
            device: Device serial.
            path: Path to the APK or .app to install.
        """
        install_block.wait(timeout=2.0)
        return {"success": True}

    try:
        with (
            patch.dict(_core.TOOL_REGISTRY, {}, clear=False),
            patch.object(_core, "_register_tool", side_effect=_capture_register),
            patch.object(_core.queue, "Queue", _scaled_queue_factory(install_timeouts)),
        ):
            _core.device_tool(_nerve_install_sync)
            wrapped_res = anyio.run(
                captured_wrappers["nerve_install"],
                "PIXEL8PRO",
                "/tmp/app.apk",
            )
    finally:
        install_block.set()

    assert install_timeouts == [150]
    assert "nerve_install did not finish within 150s" in wrapped_res["error"]

    block_event = threading.Event()
    recorded_timeouts: list[float | int | None] = []

    def _block() -> str:
        block_event.wait(timeout=2.0)
        return "done"

    try:
        with (
            patch.object(_core.queue, "Queue", _scaled_queue_factory(recorded_timeouts)),
            patch.object(_core.log, "error") as mock_log_err,
        ):
            direct_default = _core._run_with_timeout(_block, "SER-1")
            on_dev_default = anyio.run(_core._run_on_device, "SER-2", _block)
    finally:
        block_event.set()

    assert recorded_timeouts == [90, 90]
    assert mock_log_err.call_count == 2
    assert direct_default == {
        "error": (
            "Operation timed out after 90s. "
            "Device may be unresponsive — try nerve_discover to check connectivity."
        ),
        "device": "SER-1",
    }
    assert on_dev_default == {
        "error": (
            "Operation timed out after 90s. "
            "Device may be unresponsive — try nerve_discover to check connectivity."
        ),
        "device": "SER-2",
    }

    action_log.REGISTRY.reset()
    fake_device = MagicMock()
    fake_device.stop_recording.return_value = "/tmp/default15.mp4"
    fake_device.recording_info.return_value = {"elapsed_s": 15.0, "truncated": False}
    seen_started: list[float | None] = []

    def _on_sleep(secs: int) -> None:
        seen_started.append(action_log.REGISTRY.recording_started("PIXEL8PRO"))

    with (
        patch("linc_nerve.mcp.recording_tools.get_device_sync", return_value=fake_device),
        patch("linc_nerve.mcp.recording_tools.time.sleep", side_effect=_on_sleep) as mock_sleep,
        patch("linc_nerve.core.action_log._clock", return_value=42.5),
    ):
        res = _nerve_record_fixed_sync("PIXEL8PRO")
    mock_sleep.assert_called_once_with(15)
    assert seen_started == [42.5]
    assert action_log.REGISTRY.recording_started("PIXEL8PRO") is None
    assert res == {
        "status": "success",
        "file_path": "/tmp/default15.mp4",
        "elapsed_s": 15.0,
        "truncated": False,
    }
