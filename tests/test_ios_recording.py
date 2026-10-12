# tests/test_ios_recording.py
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.core.device import DeviceError
from linc_nerve.core.ios import IOSController, _simulator_state
from linc_nerve.core.recording import assert_recording_nonempty


@pytest.fixture
def ios_controller():
    ctrl = IOSController()
    ctrl._recording_processes = {}  # serial -> Popen
    ctrl._recording_paths = {}  # serial -> str
    return ctrl


def test_assert_recording_nonempty_missing_file(tmp_path):
    missing = tmp_path / "nope.mp4"
    with pytest.raises(RuntimeError) as exc:
        assert_recording_nonempty(missing, "some stderr")
    assert "Recording produced a 0-byte artifact at" in str(exc.value)
    assert "stderr: some stderr" in str(exc.value)


def test_assert_recording_nonempty_empty_file(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(RuntimeError) as exc:
        assert_recording_nonempty(empty, "")
    assert "Recording produced a 0-byte artifact at" in str(exc.value)
    assert "stderr:" not in str(exc.value)


def test_assert_recording_nonempty_valid_file(tmp_path):
    valid = tmp_path / "valid.mp4"
    valid.write_bytes(b"hello")
    assert_recording_nonempty(valid, "ignored stderr")


def test_simulator_state_lookup():
    list_json = {
        "devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-17-0": [
                {"udid": "UDID-BOOTED", "state": "Booted", "name": "iPhone 15"},
                {"udid": "UDID-SHUTDOWN", "state": "Shutdown", "name": "iPhone 14"},
            ],
            "com.apple.CoreSimulator.SimRuntime.watchOS-10-0": [
                {"udid": "UDID-WATCH", "state": "Booted", "name": "Apple Watch"},
            ],
        }
    }
    assert _simulator_state(list_json, "UDID-BOOTED") == "Booted"
    assert _simulator_state(list_json, "UDID-WATCH") == "Booted"
    assert _simulator_state(list_json, "UDID-SHUTDOWN") == "Shutdown"
    assert _simulator_state(list_json, "UNKNOWN_UDID") is None


def test_stop_recording_nothing_recording_raises(ios_controller):
    with pytest.raises(ValueError, match="No recording in progress for MOCK_UDID"):
        ios_controller.stop_recording("MOCK_UDID")


def test_ios_stop_recording_missing_file_raises_with_stderr(ios_controller, tmp_path):
    missing_path = tmp_path / "missing.mp4"
    mock_process = MagicMock()
    mock_process.poll.return_value = 1
    mock_process.stderr.read.return_value = b"simctl encoder error"
    ios_controller._recording_processes["MOCK_UDID"] = mock_process
    ios_controller._recording_paths["MOCK_UDID"] = str(missing_path)

    with pytest.raises(RuntimeError) as exc_info:
        ios_controller.stop_recording("MOCK_UDID")

    assert "0-byte" in str(exc_info.value)
    assert "simctl encoder error" in str(exc_info.value)
    assert "MOCK_UDID" not in ios_controller._recording_processes
    assert "MOCK_UDID" not in ios_controller._recording_paths


def test_ios_stop_recording_zero_byte_file_raises_with_stderr(ios_controller, tmp_path):
    empty_file = tmp_path / "empty.mp4"
    empty_file.write_bytes(b"")
    mock_process = MagicMock()
    mock_process.poll.return_value = 1
    mock_process.stderr.read.return_value = "fake stderr text"
    ios_controller._recording_processes["MOCK_UDID"] = mock_process
    ios_controller._recording_paths["MOCK_UDID"] = str(empty_file)

    with pytest.raises(RuntimeError) as exc_info:
        ios_controller.stop_recording("MOCK_UDID")

    assert "0-byte" in str(exc_info.value)
    assert "fake stderr text" in str(exc_info.value)
    assert "MOCK_UDID" not in ios_controller._recording_processes
    assert "MOCK_UDID" not in ios_controller._recording_paths


def test_ios_stop_recording(ios_controller, tmp_path):
    nonempty_file = tmp_path / "recording.mp4"
    nonempty_file.write_bytes(b"non-empty mp4 content")
    mock_process = MagicMock()
    mock_process.poll.return_value = 0
    ios_controller._recording_processes["MOCK_UDID"] = mock_process
    ios_controller._recording_paths["MOCK_UDID"] = str(nonempty_file)

    path = ios_controller.stop_recording("MOCK_UDID")

    import signal

    mock_process.send_signal.assert_called_once_with(
        signal.SIGINT
    )  # simctl requires SIGINT to finalize
    mock_process.wait.assert_called_once()
    assert path == str(nonempty_file)
    assert "MOCK_UDID" not in ios_controller._recording_processes
    assert "MOCK_UDID" not in ios_controller._recording_paths


def test_stop_recording_post_kill_wait_is_bounded(ios_controller, tmp_path):
    """If SIGINT wait times out, kill() then the second wait must carry a timeout
    so a wedged simctl can't pin the daemon's single tool worker."""
    nonempty_file = tmp_path / "recording.mp4"
    nonempty_file.write_bytes(b"non-empty mp4 content")
    proc = MagicMock()
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="simctl", timeout=10), 0]
    proc.poll.return_value = 0
    ios_controller._recording_processes["MOCK_UDID"] = proc
    ios_controller._recording_paths["MOCK_UDID"] = str(nonempty_file)

    path = ios_controller.stop_recording("MOCK_UDID")

    proc.kill.assert_called_once()
    assert proc.wait.call_args_list[1].kwargs.get("timeout") == 5
    assert path == str(nonempty_file)


@patch("subprocess.Popen")
def test_start_recording_physical_serial_raises_device_error(mock_popen, ios_controller):
    ios_controller._physical_serials.add("00008110-0000000000000001")
    with pytest.raises(
        DeviceError,
        match="iOS recording is simulator-only; 00008110-0000000000000001 is a physical device",
    ):
        ios_controller.start_recording("00008110-0000000000000001")
    mock_popen.assert_not_called()


@patch("subprocess.Popen")
def test_start_recording_shutdown_simulator_raises_device_error(mock_popen, ios_controller):
    devices_json = json.dumps(
        {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-17-0": [
                    {"udid": "SHUTDOWN-UDID", "state": "Shutdown", "name": "iPhone 15"},
                ]
            }
        }
    )
    ios_controller._simulator_serials.add("SHUTDOWN-UDID")
    with (
        patch.object(ios_controller, "_run", return_value=MagicMock(stdout=devices_json)),
        pytest.raises(
            DeviceError, match=r"simulator SHUTDOWN-UDID is not booted \(state=Shutdown\)"
        ),
    ):
        ios_controller.start_recording("SHUTDOWN-UDID")
    mock_popen.assert_not_called()


@patch("time.sleep")
@patch("subprocess.Popen")
def test_start_recording_process_exits_immediately_raises(mock_popen, mock_sleep, ios_controller):
    devices_json = json.dumps(
        {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-17-0": [
                    {"udid": "BOOTED-UDID", "state": "Booted", "name": "iPhone 15"},
                ]
            }
        }
    )
    ios_controller._simulator_serials.add("BOOTED-UDID")
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1
    mock_proc.stderr.read.return_value = b"Unable to record"
    mock_popen.return_value = mock_proc

    with (
        patch.object(ios_controller, "_run", return_value=MagicMock(stdout=devices_json)),
        pytest.raises(RuntimeError) as exc_info,
    ):
        ios_controller.start_recording("BOOTED-UDID")

    assert "Unable to record" in str(exc_info.value)
    assert "BOOTED-UDID" not in ios_controller._recording_processes
    assert "BOOTED-UDID" not in ios_controller._recording_paths


@patch("time.sleep")
@patch("subprocess.Popen")
def test_ios_start_recording(mock_popen, mock_sleep, ios_controller):
    devices_json = json.dumps(
        {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-17-0": [
                    {"udid": "MOCK_UDID", "state": "Booted", "name": "iPhone 15"},
                ]
            }
        }
    )
    ios_controller._simulator_serials.add("MOCK_UDID")
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_popen.return_value = mock_proc

    with patch.object(ios_controller, "_run", return_value=MagicMock(stdout=devices_json)):
        ios_controller.start_recording("MOCK_UDID")

    mock_popen.assert_called_once()
    assert "simctl" in mock_popen.call_args[0][0]
    assert "io" in mock_popen.call_args[0][0]
    assert "recordVideo" in mock_popen.call_args[0][0]
    assert "MOCK_UDID" in ios_controller._recording_processes
    assert "MOCK_UDID" in ios_controller._recording_paths
