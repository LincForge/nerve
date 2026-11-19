"""Tests for iOS simulator screenshot capture without starting WebDriverAgent (linc-nerve-ma0k)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.ios import IOSController
from linc_nerve.core.wda import WDAManager, WDASession

SIM_UDID = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"
FAKE_PNG = b"\x89PNG\r\n\x1a\nsimctl-png-bytes"
MJPEG_PNG = b"\x89PNG\r\n\x1a\nmjpeg-png-bytes"


def _iphone_sim_device(serial: str = SIM_UDID) -> NerveDevice:
    return NerveDevice(
        serial=serial,
        platform=Platform.IOS,
        device_type=DeviceType.PHONE,
        model="iPhone 16 Pro",
        state="online",
    )


def _fake_simctl_run_binary(png_bytes: bytes = FAKE_PNG):
    def _runner(*args: Any, timeout: int = 15) -> MagicMock:
        assert args[:4] == ("io", SIM_UDID, "screenshot", "--type=png")
        with open(args[-1], "wb") as f:
            f.write(png_bytes)
        return MagicMock(returncode=0, stdout=b"")

    return _runner


def test_simulator_screenshot_without_wda_project_dir_uses_simctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] With WDA_PROJECT_DIR unset and a fake _run_binary writing PNG bytes,
    IOSController.screenshot(sim_udid) returns those bytes and never calls
    WDAManager.get_or_start or WDASession.start.
    """
    monkeypatch.delenv("WDA_PROJECT_DIR", raising=False)
    monkeypatch.setattr("linc_nerve.core.wda._find_xctestrun", lambda *a, **kw: None)

    ctrl = IOSController()
    with (
        patch.object(ctrl, "discover", return_value=[_iphone_sim_device()]),
        patch.object(
            ctrl, "_run_binary", side_effect=_fake_simctl_run_binary(FAKE_PNG)
        ) as mock_run_binary,
        patch.object(ctrl._wda, "get_or_start", wraps=ctrl._wda.get_or_start) as spy_get_or_start,
        patch.object(WDASession, "start", autospec=True, wraps=WDASession.start) as spy_start,
    ):
        data = ctrl.screenshot(SIM_UDID)

    assert data == FAKE_PNG
    mock_run_binary.assert_called_once()
    spy_get_or_start.assert_not_called()
    spy_start.assert_not_called()


def test_simulator_screenshot_uses_running_healthy_mjpeg_session() -> None:
    """[test] With a running healthy fake session that has an mjpeg_port,
    the MJPEG frame is returned and simctl is not called (pin).
    """
    ctrl = IOSController()
    fake_session = MagicMock(spec=WDASession)
    fake_session.is_running = True
    fake_session._is_healthy.return_value = True
    fake_session.mjpeg_port = 9100
    ctrl._wda._sessions[SIM_UDID] = fake_session

    with (
        patch.object(ctrl, "discover", return_value=[_iphone_sim_device()]),
        patch.object(ctrl._mjpeg, "is_open", return_value=False),
        patch.object(ctrl._mjpeg, "screenshot", return_value=MJPEG_PNG) as mock_mjpeg,
        patch.object(ctrl, "_run_binary") as mock_run_binary,
        patch.object(ctrl._wda, "get_or_start") as spy_get_or_start,
    ):
        data = ctrl.screenshot(SIM_UDID, full_resolution=True)

    assert data == MJPEG_PNG
    mock_mjpeg.assert_called_once_with(SIM_UDID, 9100)
    mock_run_binary.assert_not_called()
    spy_get_or_start.assert_not_called()


def test_wda_manager_running_returns_only_running_healthy_session() -> None:
    """WDAManager.running(serial) returns the session only when running and healthy,
    never starting or building WDA.
    """
    mgr = WDAManager()
    assert mgr.running(SIM_UDID) is None

    stopped_session = MagicMock(spec=WDASession)
    stopped_session.is_running = False
    stopped_session._is_healthy.return_value = True
    mgr._sessions[SIM_UDID] = stopped_session
    assert mgr.running(SIM_UDID) is None

    unhealthy_session = MagicMock(spec=WDASession)
    unhealthy_session.is_running = True
    unhealthy_session._is_healthy.return_value = False
    mgr._sessions[SIM_UDID] = unhealthy_session
    assert mgr.running(SIM_UDID) is None

    healthy_session = MagicMock(spec=WDASession)
    healthy_session.is_running = True
    healthy_session._is_healthy.return_value = True
    mgr._sessions[SIM_UDID] = healthy_session
    assert mgr.running(SIM_UDID) is healthy_session


def test_contract_fast_path_errors_fall_back_and_simctl_error_names_simctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] A lookup that raises, an MJPEG read that raises, and a simctl exit 1
    each yield: fallback to simctl for the first two; for the last an error whose text
    contains 'simctl' and not 'WDA_PROJECT_DIR'.
    """
    monkeypatch.delenv("WDA_PROJECT_DIR", raising=False)
    monkeypatch.setattr("linc_nerve.core.wda._find_xctestrun", lambda *a, **kw: None)

    ctrl = IOSController()

    # 1. Lookup that raises -> falls back to simctl
    with (
        patch.object(ctrl, "discover", return_value=[_iphone_sim_device()]),
        patch.object(ctrl._wda, "running", side_effect=RuntimeError("wda lookup exploded")),
        patch.object(
            ctrl, "_run_binary", side_effect=_fake_simctl_run_binary(FAKE_PNG)
        ) as mock_run_binary,
    ):
        assert ctrl.screenshot(SIM_UDID) == FAKE_PNG
        mock_run_binary.assert_called_once()

    # 2. MJPEG read that raises -> falls back to simctl
    fake_session = MagicMock(spec=WDASession)
    fake_session.is_running = True
    fake_session._is_healthy.return_value = True
    fake_session.mjpeg_port = 9100
    ctrl._wda._sessions[SIM_UDID] = fake_session
    with (
        patch.object(ctrl, "discover", return_value=[_iphone_sim_device()]),
        patch.object(ctrl._mjpeg, "is_open", return_value=False),
        patch.object(ctrl._mjpeg, "screenshot", side_effect=OSError("mjpeg socket closed")),
        patch.object(
            ctrl, "_run_binary", side_effect=_fake_simctl_run_binary(FAKE_PNG)
        ) as mock_run_binary,
    ):
        assert ctrl.screenshot(SIM_UDID) == FAKE_PNG
        mock_run_binary.assert_called_once()

    # 3. simctl exit 1 -> error text contains "simctl" and not "WDA_PROJECT_DIR"
    ctrl._wda._sessions.clear()
    with (
        patch.object(ctrl, "discover", return_value=[_iphone_sim_device()]),
        patch.object(ctrl, "_run_binary", return_value=MagicMock(returncode=1)),
        pytest.raises(RuntimeError) as exc_info,
    ):
        ctrl.screenshot(SIM_UDID)

    err_text = str(exc_info.value)
    assert "simctl" in err_text
    assert "WDA_PROJECT_DIR" not in err_text
