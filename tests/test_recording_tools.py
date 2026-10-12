from unittest.mock import MagicMock, patch

from linc_nerve.mcp.recording_tools import (
    _nerve_record_fixed_sync,
    _nerve_record_start_sync,
    _nerve_record_stop_sync,
)


@patch("linc_nerve.mcp.recording_tools.get_device_sync")
def test_record_start(mock_get_device):
    mock_dev = MagicMock()
    mock_get_device.return_value = mock_dev

    result = _nerve_record_start_sync("DEVICE_ID")

    mock_dev.start_recording.assert_called_once_with("DEVICE_ID")
    assert result["status"] == "success"


@patch("linc_nerve.mcp.recording_tools.get_device_sync")
def test_record_stop(mock_get_device):
    mock_dev = MagicMock()
    mock_dev.stop_recording.return_value = "/path/to/video.mp4"
    mock_get_device.return_value = mock_dev

    result = _nerve_record_stop_sync("DEVICE_ID")

    mock_dev.stop_recording.assert_called_once_with("DEVICE_ID")
    assert result["file_path"] == "/path/to/video.mp4"


@patch("linc_nerve.mcp.recording_tools.get_device_sync")
@patch("time.sleep")
def test_record_fixed(mock_sleep, mock_get_device):
    mock_dev = MagicMock()
    mock_dev.stop_recording.return_value = "/path/to/fixed.mp4"
    mock_get_device.return_value = mock_dev

    result = _nerve_record_fixed_sync("DEVICE_ID", 2)

    mock_dev.start_recording.assert_called_once_with("DEVICE_ID")
    mock_sleep.assert_called_once_with(2)
    mock_dev.stop_recording.assert_called_once_with("DEVICE_ID")
    assert result["file_path"] == "/path/to/fixed.mp4"


@patch("linc_nerve.mcp.recording_tools.get_device_sync")
def test_record_stop_surfaces_truncation_info(mock_get_device):
    """linc-nerve-zqy.3: a truncated Android recording's info dict must be
    merged into the tool result, plus a human-readable warning."""
    mock_dev = MagicMock()
    mock_dev.stop_recording.return_value = "/path/to/video.mp4"
    mock_dev.recording_info.return_value = {
        "elapsed_s": 185.0,
        "truncated": True,
        "capped_at_s": 180,
    }
    mock_get_device.return_value = mock_dev

    result = _nerve_record_stop_sync("D")

    assert result["status"] == "success"
    assert result["file_path"] == "/path/to/video.mp4"
    assert result["truncated"] is True
    assert result["capped_at_s"] == 180
    assert "180" in result["warning"]


@patch("linc_nerve.mcp.recording_tools.get_device_sync")
@patch("time.sleep")
def test_record_fixed_surfaces_non_truncated_info_without_warning(mock_sleep, mock_get_device):
    mock_dev = MagicMock()
    mock_dev.stop_recording.return_value = "/path/to/fixed.mp4"
    mock_dev.recording_info.return_value = {"elapsed_s": 60.0, "truncated": False}
    mock_get_device.return_value = mock_dev

    result = _nerve_record_fixed_sync("D", 2)

    assert result["status"] == "success"
    assert result["file_path"] == "/path/to/fixed.mp4"
    assert result["truncated"] is False
    assert "warning" not in result
    assert "capped_at_s" not in result


@patch("linc_nerve.mcp.recording_tools.get_device_sync")
def test_record_stop_magicmock_device_yields_no_extra_keys(mock_get_device):
    """PIN: a bare MagicMock device (no configured recording_info) must not
    leak MagicMock internals into the result dict."""
    mock_dev = MagicMock()
    mock_dev.stop_recording.return_value = "/path/to/video.mp4"
    mock_get_device.return_value = mock_dev

    result = _nerve_record_stop_sync("DEVICE_ID")

    assert result == {"status": "success", "file_path": "/path/to/video.mp4"}


@patch("linc_nerve.mcp.recording_tools.get_device_sync")
@patch("time.sleep")
def test_record_fixed_magicmock_device_yields_no_extra_keys(mock_sleep, mock_get_device):
    mock_dev = MagicMock()
    mock_dev.stop_recording.return_value = "/path/to/fixed.mp4"
    mock_get_device.return_value = mock_dev

    result = _nerve_record_fixed_sync("DEVICE_ID", 2)

    assert result == {"status": "success", "file_path": "/path/to/fixed.mp4"}
