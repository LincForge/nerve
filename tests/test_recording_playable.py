"""assert_recording_playable — a pulled MP4 must carry a decodable video frame.

Wear OS ``adb shell screenrecord`` exits 0 but writes an ~8 KB MP4 with no frames,
which the 0-byte guard lets through (reactor 25be2624, bead linc-nerve-ayv9).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.core.android import AndroidController
from linc_nerve.core.recording import assert_recording_playable
from tests._mp4_fixture import write_empty_mp4, write_mp4


def test_empty_mp4_raises_with_size(tmp_path: Path) -> None:
    path = write_empty_mp4(tmp_path / "wear.mp4")
    size = path.stat().st_size
    assert size > 0

    with pytest.raises(RuntimeError) as exc:
        assert_recording_playable(path, "")

    message = str(exc.value)
    assert "no decodable video frames" in message
    assert f"({size} bytes)" in message
    assert str(path) in message
    assert "scrcpy" in message
    assert "stderr" not in message


def test_playable_mp4_returns_none(tmp_path: Path) -> None:
    path = write_mp4(tmp_path / "ok.mp4", frames=3)
    assert assert_recording_playable(path, "") is None


def test_random_bytes_raise_runtime_error(tmp_path: Path) -> None:
    path = tmp_path / "garbage.mp4"
    path.write_bytes(os.urandom(4096))

    with pytest.raises(RuntimeError, match="no decodable video frames"):
        assert_recording_playable(path, "")


def test_stderr_is_appended_when_present(tmp_path: Path) -> None:
    path = write_empty_mp4(tmp_path / "wear.mp4")

    with pytest.raises(RuntimeError, match="stderr: Encoder failed"):
        assert_recording_playable(path, "  Encoder failed (err=-38)\n")


def test_audio_only_mp4_has_no_video_stream(tmp_path: Path) -> None:
    import av

    path = tmp_path / "audio.mp4"
    with av.open(str(path), mode="w", format="mp4") as container:
        container.add_stream("aac", rate=44100)
        container.start_encoding()

    with pytest.raises(RuntimeError, match="no decodable video frames"):
        assert_recording_playable(path, "")


@patch("subprocess.run")
def test_guest_stop_recording_rejects_frameless_mp4(
    mock_run: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the controller: the Wear OS empty MP4 is an error, not a path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ctrl = AndroidController()
    proc = MagicMock()
    proc.communicate.return_value = ("", "")
    ctrl._recording_processes = {"WATCH": {"target": "guest", "process": proc}}

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if "pull" in cmd:
            write_empty_mp4(Path(cmd[cmd.index("pull") + 2]))
        return MagicMock(returncode=0, stderr="")

    mock_run.side_effect = fake_run

    with (
        patch("linc_nerve.core.android.time.sleep"),
        pytest.raises(RuntimeError, match="no decodable video frames"),
    ):
        ctrl.stop_recording("WATCH")

    assert "WATCH" not in ctrl._recording_processes
