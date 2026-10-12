from unittest.mock import MagicMock, patch

from linc_nerve.mcp.state_tools import _nerve_ensure_ready_sync


@patch("linc_nerve.mcp.state_tools._core")
def test_android_delegates_to_controller(mock_core):
    mock_core._resolve_device.return_value = (object(), "android")
    mock_core._check_guard.return_value = None
    ctrl = MagicMock()
    ctrl.ensure_ready.return_value = {"ready": True, "reason": "ready", "platform": "android"}
    mock_core._get_controller.return_value = ctrl
    out = _nerve_ensure_ready_sync("S")
    assert out["ready"] is True
    ctrl.ensure_ready.assert_called_once_with("S")


@patch("linc_nerve.mcp.state_tools._core")
def test_ios_simulator_is_ready_noop(mock_core):
    mock_core._resolve_device.return_value = (object(), "ios")
    ios = MagicMock()
    ios._is_physical.return_value = False
    mock_core._ensure_initialized.return_value = (MagicMock(), ios)
    out = _nerve_ensure_ready_sync("SIM")
    assert out["ready"] is True and out["platform"] == "ios" and out["actions_taken"] == []
    assert "simulator" in out["reason"]


@patch("linc_nerve.mcp.state_tools._core")
def test_ios_physical_is_report_only(mock_core):
    mock_core._resolve_device.return_value = (object(), "ios")
    ios = MagicMock()
    ios._is_physical.return_value = True
    mock_core._ensure_initialized.return_value = (MagicMock(), ios)
    out = _nerve_ensure_ready_sync("PHYS")
    assert out["report_only"] is True and out["ready"] is True
    assert out["awake"] is None and out["reason"] == "ios_physical_state_not_detectable"


@patch("linc_nerve.mcp.state_tools._core")
def test_unknown_device_errors(mock_core):
    mock_core._resolve_device.return_value = (None, "Device 'X' not found.")
    assert "error" in _nerve_ensure_ready_sync("X")
