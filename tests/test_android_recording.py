# tests/test_android_recording.py
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.core.android import AndroidController
from tests._mp4_fixture import mp4_bytes


@pytest.fixture
def android_controller():
    # Use actual instantiation pattern
    ctrl = AndroidController()
    ctrl._recording_processes = {}  # dict mapping serial -> recording-session dict
    return ctrl


@patch("subprocess.Popen")
def test_android_start_recording(mock_popen, android_controller):
    # start_recording now pre-checks readiness (folds f33c9fc3 0-byte MP4); stub it ready.
    android_controller.ensure_ready = MagicMock(return_value={"ready": True, "reason": "ready"})
    android_controller.start_recording("MOCK_SERIAL")
    mock_popen.assert_called_once()
    assert "screenrecord" in mock_popen.call_args[0][0]
    assert "MOCK_SERIAL" in android_controller._recording_processes
    assert android_controller._recording_processes["MOCK_SERIAL"]["target"] == "guest"


@patch("subprocess.run")
@patch("subprocess.Popen")
def test_android_stop_recording(mock_popen, mock_run, android_controller, tmp_path, monkeypatch):
    # Sandbox the recording output dir (~/.linc/nerve/recordings) under tmp_path so
    # this test's fake `adb pull` materialises its artifact in a throwaway location
    # instead of the real host path (test-hygiene, PR#124 MINOS review round 1).
    monkeypatch.setenv("HOME", str(tmp_path))
    android_controller.ensure_ready = MagicMock(return_value={"ready": True, "reason": "ready"})
    mock_process = MagicMock()
    mock_process.communicate.return_value = ("", "")
    mock_popen.return_value = mock_process

    android_controller.start_recording("MOCK_SERIAL")

    # Simulate a real, non-empty artifact at the path stop_recording will pull to.
    def fake_run(cmd, **kwargs):
        if "pull" in cmd:
            local_path = cmd[cmd.index("pull") + 2]
            with open(local_path, "wb") as f:
                f.write(mp4_bytes())
        return MagicMock(returncode=0, stderr="")

    mock_run.side_effect = fake_run

    path = android_controller.stop_recording("MOCK_SERIAL")

    mock_process.terminate.assert_called_once()
    mock_process.communicate.assert_called_once_with(timeout=10)
    mock_run.assert_called()  # For adb pull and adb shell rm
    assert path.endswith(".mp4")
    assert "MOCK_SERIAL" not in android_controller._recording_processes


@patch("subprocess.run")
def test_stop_recording_kills_process_if_terminate_hangs(
    mock_run, android_controller, tmp_path, monkeypatch
):
    """If terminate() doesn't stop screenrecord, communicate(timeout) raises and we kill()."""
    # Sandbox the recording output dir under tmp_path (test-hygiene, see
    # test_android_stop_recording above).
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_run(cmd, **kwargs):
        if "pull" in cmd:
            local_path = cmd[cmd.index("pull") + 2]
            with open(local_path, "wb") as f:
                f.write(mp4_bytes())
        return MagicMock(returncode=0, stderr="")

    mock_run.side_effect = fake_run
    proc = MagicMock()
    # First communicate() (after terminate) times out; after kill(), communicate() returns.
    proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="screenrecord", timeout=10),
        ("", ""),
    ]
    android_controller._recording_processes["MOCK_SERIAL"] = {"target": "guest", "process": proc}

    android_controller.stop_recording("MOCK_SERIAL")

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.communicate.call_args_list[0].kwargs.get("timeout") == 10


@patch("subprocess.run")
def test_stop_recording_pull_and_rm_pass_timeouts(
    mock_run, android_controller, tmp_path, monkeypatch
):
    """adb pull (60s) and adb shell rm (15s) must be bounded so a stalled USB
    transfer can't pin the daemon's single tool worker."""
    # Sandbox the recording output dir under tmp_path (test-hygiene, see
    # test_android_stop_recording above).
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_run(cmd, **kwargs):
        if "pull" in cmd:
            local_path = cmd[cmd.index("pull") + 2]
            with open(local_path, "wb") as f:
                f.write(mp4_bytes())
        return MagicMock(returncode=0, stderr="")

    mock_run.side_effect = fake_run
    proc = MagicMock()
    proc.communicate.return_value = ("", "")
    android_controller._recording_processes["MOCK_SERIAL"] = {"target": "guest", "process": proc}

    android_controller.stop_recording("MOCK_SERIAL")

    timeouts = [c.kwargs.get("timeout") for c in mock_run.call_args_list]
    assert 60 in timeouts  # pull
    assert 15 in timeouts  # rm


def test_stop_recording_drains_real_pipe_without_losing_stderr(
    android_controller, tmp_path, monkeypatch
):
    """Regression guard (PR#124 MINOS review round 1, correctness-risk): a real
    child process that's genuinely blocked writing to a full stderr pipe while
    handling SIGTERM (as screenrecord can do on shutdown) must still be
    drained in full, not truncated.

    The prior implementation called `process.wait(timeout=10)` BEFORE reading
    stderr at all. A child stuck writing past the pipe's buffer capacity would
    block wait() for the full 10s ceiling (nobody is draining the pipe to let
    it proceed), forcing a kill() — truncating whatever the child hadn't yet
    flushed. `communicate()` drains both pipes *while* waiting, so a child
    that's merely pipe-blocked (not truly hung) unblocks and finishes well
    before that ceiling, with its full output intact.

    Round-1 finding (MINOS): the original version of this test raced
    interpreter startup — it called `terminate()` immediately after `Popen()`
    with no synchronization, so SIGTERM routinely arrived before the child had
    even installed its handler (empirically: 5/5 runs returned rc=-15 with
    ZERO stderr bytes — the handler never ran, `communicate()` vs. `wait()` was
    never actually exercised, and the test passed against both the pre-fix and
    post-fix implementation). Fixed here with an explicit readiness handshake:
    the child prints `READY` to stdout only after `signal.signal()` has
    returned, and the parent blocks on `readline()` for it before calling
    `terminate()` — so the handler is provably installed, and the subsequent
    `time.sleep(30)` is a deterministic, interruptible point for SIGTERM to
    land on (not a race against import/exec startup).

    Discriminating on content, not just timing: the child's SIGTERM handler
    writes a large payload with a trailing marker to stderr, and the pulled
    artifact is left 0 bytes so `stop_recording` raises with the drained
    stderr embedded in the message. The pipe buffer is far smaller than the
    payload, so a kill-before-fully-drained child can only ever surface a
    truncated prefix — the marker at the very end can only appear in the
    message if the full write was drained, which requires `communicate()`.
    Verified empirically against this repo's pre-fix implementation
    (`process.wait(timeout=10)` + post-wait `stderr.read()`, e837d5f0^): that
    version needs the full ~10s wait()+kill() escalation (elapsed >= 8) and its
    truncated capture never contains the marker, so both assertions below
    fail against it, while both pass against the current communicate()-based
    fix.
    """
    marker = "STDERR_FULLY_DRAINED_MARKER"
    script = (
        "import signal, sys, time\n"
        "def _h(signum, frame):\n"
        f"    sys.stderr.write('E' * 200000 + {marker!r})\n"
        "    sys.stderr.flush()\n"
        "    sys.exit(1)\n"
        "signal.signal(signal.SIGTERM, _h)\n"
        "print('READY', flush=True)\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Handshake: block until the child has installed its SIGTERM handler and
    # printed READY, then a short settle so it has reached time.sleep(30).
    assert process.stdout.readline().strip() == "READY"
    time.sleep(0.2)
    android_controller._recording_processes["REAL_PROC"] = {"target": "guest", "process": process}

    # Sandbox the recording output dir under tmp_path (test-hygiene, see
    # test_android_stop_recording above) and leave the pulled artifact 0 bytes
    # so the 0-byte guard raises with the drained stderr embedded — that's how
    # this test observes what was actually captured.
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_run(cmd, **kwargs):
        if "pull" in cmd:
            local_path = cmd[cmd.index("pull") + 2]
            open(local_path, "wb").close()
        return MagicMock(returncode=0, stderr="")

    start = time.monotonic()
    with patch("subprocess.run", side_effect=fake_run), pytest.raises(RuntimeError) as excinfo:
        android_controller.stop_recording("REAL_PROC")
    elapsed = time.monotonic() - start

    # wait()-then-read() would block for the full 10s wait() timeout before
    # even attempting a kill(), then truncate the child's output at the pipe's
    # buffer capacity; communicate() drains concurrently so the full payload
    # (including the trailing marker) is captured in well under 8s.
    assert elapsed < 8
    assert marker in str(excinfo.value)


class TestEmulatorRecordingRouting:
    """linc-nerve#118: emulator targets must use the host-side console recorder
    (`adb emu screenrecord start/stop`) instead of guest-side `shell screenrecord`,
    which cannot init an AVC encoder on our AVDs (`Encoder failed (err=-38)`)."""

    @patch("subprocess.run")
    def test_start_recording_routes_emulator_target_to_host_side_recorder(
        self, mock_run, android_controller
    ):
        """start_recording('emulator-5554') must execute argv containing the
        subsequence ['-s', 'emulator-5554', 'emu', 'screenrecord', 'start'] with a
        HOST output path ending in .webm, and must NOT contain 'shell'.
        Fails pre-fix: android_sensor.py always builds
        [ADB_PATH, '-s', serial, 'shell', 'screenrecord', '/sdcard/...mp4']."""
        android_controller.ensure_ready = MagicMock(
            return_value={"ready": True, "reason": "ready"}
        )
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        android_controller.start_recording("emulator-5554")

        mock_run.assert_called_once()
        argv = mock_run.call_args[0][0]
        assert "shell" not in argv

        # Subsequence check: ['-s', 'emulator-5554', 'emu', 'screenrecord', 'start']
        # appears in order (not necessarily contiguous with the trailing path arg).
        needle = ["-s", "emulator-5554", "emu", "screenrecord", "start"]
        idxs = [argv.index(tok) for tok in needle]
        assert idxs == sorted(idxs), f"argv out of order: {argv}"

        host_path = argv[-1]
        assert host_path.endswith(".webm")
        assert not host_path.startswith("/sdcard")

        assert "emulator-5554" in android_controller._recording_processes
        entry = android_controller._recording_processes["emulator-5554"]
        assert entry["target"] == "emulator"

    @patch("subprocess.Popen")
    def test_start_recording_physical_serial_still_uses_guest_shell(
        self, mock_popen, android_controller
    ):
        """Regression guard: a real (non-emulator) serial must NOT be routed
        through the emulator console path."""
        android_controller.ensure_ready = MagicMock(
            return_value={"ready": True, "reason": "ready"}
        )
        android_controller.start_recording("FAKESERIAL0001")

        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert "shell" in argv
        assert "screenrecord" in argv
        entry = android_controller._recording_processes["FAKESERIAL0001"]
        assert entry["target"] == "guest"

    @patch("subprocess.run")
    def test_stop_recording_emulator_target_sends_stop_and_returns_host_path(
        self, mock_run, android_controller, tmp_path
    ):
        """stop_recording on an emulator target sends `emu screenrecord stop` and
        returns the HOST .webm path directly — no adb pull/rm (the file never
        left the host)."""
        host_path = tmp_path / "android_emulator-5554_123.webm"
        host_path.write_bytes(b"fake-webm-bytes-not-empty")

        android_controller._recording_processes["emulator-5554"] = {
            "target": "emulator",
            "host_path": str(host_path),
            "start_stderr": "",
        }
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        result = android_controller.stop_recording("emulator-5554")

        assert result == str(host_path)
        mock_run.assert_called_once()
        argv = mock_run.call_args[0][0]
        assert argv == ["adb", "-s", "emulator-5554", "emu", "screenrecord", "stop"] or (
            "emu" in argv and "screenrecord" in argv and "stop" in argv
        )
        assert "emulator-5554" not in android_controller._recording_processes


class TestZeroByteRecordingGuard:
    """linc-nerve#118 AC4: stop_recording must never return success for an
    empty artifact — encoder failures on our AVDs (`Encoder failed (err=-38)`)
    otherwise return a 0-byte file as if it were a real recording."""

    @patch("subprocess.run")
    def test_emulator_zero_byte_artifact_raises_with_stderr(
        self, mock_run, android_controller, tmp_path
    ):
        host_path = tmp_path / "android_emulator-5554_999.webm"
        host_path.write_bytes(b"")  # 0 bytes — simulated encoder failure

        android_controller._recording_processes["emulator-5554"] = {
            "target": "emulator",
            "host_path": str(host_path),
            "start_stderr": "Encoder failed (err=-38)",
        }
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        with pytest.raises(RuntimeError) as excinfo:
            android_controller.stop_recording("emulator-5554")

        assert "0-byte" in str(excinfo.value)
        assert "Encoder failed (err=-38)" in str(excinfo.value)
        # Must not return the path of the empty file as success.
        assert "emulator-5554" not in android_controller._recording_processes

    @patch("subprocess.run")
    def test_guest_zero_byte_artifact_raises_with_stderr(
        self, mock_run, android_controller, tmp_path, monkeypatch
    ):
        """Fails pre-fix: android_sensor.py never checks output size (Popen
        without stderr capture) and returns the path of the empty file as
        success."""
        # Sandbox the recording output dir under tmp_path (test-hygiene, see
        # test_android_stop_recording above).
        monkeypatch.setenv("HOME", str(tmp_path))

        def fake_run(cmd, **kwargs):
            if "pull" in cmd:
                local_path = cmd[cmd.index("pull") + 2]
                # 0 bytes — simulated encoder failure: just create the empty file.
                open(local_path, "wb").close()
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = fake_run
        proc = MagicMock()
        proc.communicate.return_value = ("", "Encoder failed (err=-38)")
        android_controller._recording_processes["MOCK_SERIAL"] = {
            "target": "guest",
            "process": proc,
        }

        with pytest.raises(RuntimeError) as excinfo:
            android_controller.stop_recording("MOCK_SERIAL")

        assert "0-byte" in str(excinfo.value)
        assert "Encoder failed (err=-38)" in str(excinfo.value)
        assert "MOCK_SERIAL" not in android_controller._recording_processes

    @patch("subprocess.run")
    def test_nonempty_artifact_does_not_raise(self, mock_run, android_controller, tmp_path):
        """Sanity check: the guard doesn't false-positive on a real recording."""
        host_path = tmp_path / "android_emulator-5554_1.webm"
        host_path.write_bytes(b"a real webm payload, definitely not empty")

        android_controller._recording_processes["emulator-5554"] = {
            "target": "emulator",
            "host_path": str(host_path),
            "start_stderr": "",
        }
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        result = android_controller.stop_recording("emulator-5554")
        assert result == str(host_path)


class TestTruncationVisibility:
    """linc-nerve-zqy.3: Android's guest-side `screenrecord` self-terminates at
    180s with no signal — stop_recording must expose elapsed/truncated info
    for GUEST recordings via `recording_info(serial)`."""

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_truncated_guest_recording_reports_elapsed_and_capped_at(
        self, mock_popen, mock_run, android_controller, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        android_controller.ensure_ready = MagicMock(
            return_value={"ready": True, "reason": "ready"}
        )
        mock_process = MagicMock()
        mock_process.communicate.return_value = ("", "")
        mock_popen.return_value = mock_process

        def fake_run(cmd, **kwargs):
            if "pull" in cmd:
                local_path = cmd[cmd.index("pull") + 2]
                with open(local_path, "wb") as f:
                    f.write(mp4_bytes())
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = fake_run

        with patch("linc_nerve.core.android_sensor._clock", side_effect=[1000.0, 1185.0]):
            android_controller.start_recording("MOCK_SERIAL")
            path = android_controller.stop_recording("MOCK_SERIAL")

        assert path.endswith(".mp4")
        assert android_controller.recording_info("MOCK_SERIAL") == {
            "elapsed_s": 185.0,
            "truncated": True,
            "capped_at_s": 180,
        }

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_untruncated_guest_recording_has_no_capped_at(
        self, mock_popen, mock_run, android_controller, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        android_controller.ensure_ready = MagicMock(
            return_value={"ready": True, "reason": "ready"}
        )
        mock_process = MagicMock()
        mock_process.communicate.return_value = ("", "")
        mock_popen.return_value = mock_process

        def fake_run(cmd, **kwargs):
            if "pull" in cmd:
                local_path = cmd[cmd.index("pull") + 2]
                with open(local_path, "wb") as f:
                    f.write(mp4_bytes())
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = fake_run

        with patch("linc_nerve.core.android_sensor._clock", side_effect=[1000.0, 1060.0]):
            android_controller.start_recording("MOCK_SERIAL")
            path = android_controller.stop_recording("MOCK_SERIAL")

        assert path.endswith(".mp4")
        info = android_controller.recording_info("MOCK_SERIAL")
        assert info == {"elapsed_s": 60.0, "truncated": False}
        assert "capped_at_s" not in info

    def test_truncation_info_boundary(self):
        from linc_nerve.core.android_sensor import _truncation_info

        assert _truncation_info(0.0, 180.0)["truncated"] is False
        assert _truncation_info(0.0, 180.1)["truncated"] is True

    @patch("subprocess.run")
    def test_emulator_recording_leaves_recording_info_none(
        self, mock_run, android_controller, tmp_path
    ):
        """PIN: emulator host-side recordings have separate, unverified timing
        semantics (out of scope) and must not populate recording_info."""
        host_path = tmp_path / "android_emulator-5554_1.webm"
        host_path.write_bytes(b"a real webm payload, definitely not empty")

        android_controller._recording_processes["emulator-5554"] = {
            "target": "emulator",
            "host_path": str(host_path),
            "start_stderr": "",
        }
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        android_controller.stop_recording("emulator-5554")

        assert android_controller.recording_info("emulator-5554") is None
