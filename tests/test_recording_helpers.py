# tests/test_recording_helpers.py
"""Direct unit tests for the fail-closed recording helpers (audit F7).

These predicates carry the decisions that ``IOSController.start_recording`` /
``stop_recording`` delegate to, so the controller methods stay branch-free.
"""

import json
import signal
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.core.device import DeviceError
from linc_nerve.core.ios import IOSController
from linc_nerve.core.recording import (
    exited_stderr,
    interrupt_and_reap,
    pop_recording,
    raise_if_exited,
)

# --- exited_stderr ---


def test_exited_stderr_running_process_returns_empty():
    proc = MagicMock()
    proc.poll.return_value = None
    assert exited_stderr(proc) == ""
    proc.stderr.read.assert_not_called()


def test_exited_stderr_no_pipe_returns_empty():
    proc = MagicMock()
    proc.poll.return_value = 0
    proc.stderr = None
    assert exited_stderr(proc) == ""


def test_exited_stderr_decodes_bytes():
    proc = MagicMock()
    proc.poll.return_value = 1
    proc.stderr.read.return_value = b"boom \xff"
    assert exited_stderr(proc) == "boom �"


def test_exited_stderr_passes_text_through():
    proc = MagicMock()
    proc.poll.return_value = 1
    proc.stderr.read.return_value = "text err"
    assert exited_stderr(proc) == "text err"


# --- raise_if_exited ---


def test_raise_if_exited_running_is_noop():
    proc = MagicMock()
    proc.poll.return_value = None
    raise_if_exited(proc, "simctl recordVideo")


def test_raise_if_exited_dead_child_raises_with_rc_and_stderr():
    proc = MagicMock()
    proc.poll.return_value = 2
    proc.stderr.read.return_value = b"Unable to record"
    with pytest.raises(RuntimeError, match=r"simctl recordVideo exited 2: Unable to record"):
        raise_if_exited(proc, "simctl recordVideo")


# --- pop_recording ---


def test_pop_recording_returns_and_removes_entry():
    proc = MagicMock()
    processes = {"S": proc}
    paths = {"S": "/tmp/x.mp4"}
    assert pop_recording(processes, paths, "S") == (proc, "/tmp/x.mp4")
    assert processes == {}
    assert paths == {}


def test_pop_recording_nothing_recording_raises():
    with pytest.raises(ValueError, match="No recording in progress for S"):
        pop_recording({}, {"S": "/tmp/stale.mp4"}, "S")


# --- interrupt_and_reap ---


def test_interrupt_and_reap_clean_exit():
    proc = MagicMock()
    interrupt_and_reap(proc)
    proc.send_signal.assert_called_once_with(signal.SIGINT)
    proc.wait.assert_called_once_with(timeout=10)
    proc.kill.assert_not_called()


def test_interrupt_and_reap_kills_with_bounded_wait():
    proc = MagicMock()
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="simctl", timeout=10), 0]
    interrupt_and_reap(proc)
    proc.kill.assert_called_once()
    assert proc.wait.call_args_list[1].kwargs == {"timeout": 5}


def test_interrupt_and_reap_swallows_post_kill_timeout():
    proc = MagicMock()
    proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="simctl", timeout=10),
        subprocess.TimeoutExpired(cmd="simctl", timeout=5),
    ]
    interrupt_and_reap(proc)
    proc.kill.assert_called_once()


# --- IOSController._require_recordable ---


def _list_json(udid: str, state: str) -> str:
    return json.dumps({"devices": {"rt": [{"udid": udid, "state": state}]}})


def test_require_recordable_physical_raises_without_simctl():
    ctrl = IOSController()
    ctrl._physical_serials.add("00008110-0000000000000001")
    with (
        patch.object(ctrl, "_run") as run,
        pytest.raises(DeviceError, match="simulator-only"),
    ):
        ctrl._require_recordable("00008110-0000000000000001")
    run.assert_not_called()


def test_require_recordable_unknown_udid_raises():
    ctrl = IOSController()
    ctrl._simulator_serials.add("GONE")
    with (
        patch.object(ctrl, "_run", return_value=MagicMock(stdout=_list_json("OTHER", "Booted"))),
        pytest.raises(DeviceError, match=r"not booted \(state=None\)"),
    ):
        ctrl._require_recordable("GONE")


def test_require_recordable_booted_simulator_passes():
    ctrl = IOSController()
    ctrl._simulator_serials.add("SIM")
    with patch.object(ctrl, "_run", return_value=MagicMock(stdout=_list_json("SIM", "Booted"))):
        ctrl._require_recordable("SIM")
