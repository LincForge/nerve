"""[contract] Wave 4 honest guard refusal and empty-recording error messages (linc-nerve-gm5g).

Covers:
1. A guarded session + nerve_launch(uri=...) on unknown serial "NOPE" returns a
   refusal whose error contains "not found" and does NOT contain platform text
   built from that sentence (uri_launch_refusal wording absent).
2. assert_recording_playable on an 8 KB frame-less MP4 fixture with is_wear=False
   raises RuntimeError whose message has no "Wear OS"/"scrcpy", names a
   phone/tablet recording with no decodable frames, suggests re-recording, and
   keeps stderr detail; with is_wear=True the message keeps the Wear OS / scrcpy
   advice (pin).
3. AndroidController.stop_recording passes is_wear from _is_wear_os(serial) to
   assert_recording_playable.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.core import action_log
from linc_nerve.core.android import AndroidController
from linc_nerve.core.ios import IOSController  # noqa: F401 - preload before conftest ios_discovery
from linc_nerve.core.recording import assert_recording_playable
from linc_nerve.mcp import _core, app_tools
from linc_nerve.mcp.actionlog_tools import (
    _nerve_actionlog_start_sync,
    _nerve_actionlog_stop_sync,
)
from tests._mp4_fixture import write_empty_mp4

TARGET_PKG = "com.example.app"


def _write_8kb_frameless_mp4(path: Path) -> Path:
    """Write an 8 KB (8192-byte) MP4 with one video stream and 0 frames."""
    write_empty_mp4(path)
    raw = path.read_bytes()
    pad_len = 8192 - len(raw)
    free_box = struct.pack(">I4s", pad_len, b"free") + (b"\x00" * (pad_len - 8))
    path.write_bytes(raw + free_box)
    return path


def test_guarded_uri_launch_on_unknown_serial_reports_not_found_not_platform_sentence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[contract] Guarded nerve_launch(uri=...) on serial 'NOPE' refuses with 'not found'
    without interpolating that sentence as the platform in uri_launch_refusal."""
    action_log.REGISTRY.reset()
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(tmp_path))
    ctrl = MagicMock()
    not_found_msg = "Device 'NOPE' not found. Run nerve_discover to list devices."
    monkeypatch.setattr(_core, "_ensure_initialized", lambda: (ctrl, ctrl))
    monkeypatch.setattr(
        _core,
        "_resolve_device",
        lambda serial: (None, not_found_msg) if serial == "NOPE" else (ctrl, "ios"),
    )
    monkeypatch.setattr(_core, "_check_guard", lambda serial: None)
    monkeypatch.setattr(_core, "_get_controller", lambda platform: ctrl)

    log_path = tmp_path / "guarded.jsonl"
    try:
        started = _nerve_actionlog_start_sync("NOPE", str(log_path), target_package=TARGET_PKG)
        assert started["success"] is True

        result = app_tools._nerve_launch_sync(
            "NOPE", TARGET_PKG, uri="https://example.com/deeplink"
        )
        _nerve_actionlog_stop_sync("NOPE")
    finally:
        action_log.REGISTRY.reset()

    assert result == {
        "error": not_found_msg,
        "detail": not_found_msg,
        "guard": "other_package",
        "validation_error": True,
    }
    error = str(result["error"])
    detail = str(result["detail"])
    assert "not found" in error.lower()
    assert "NOPE" in error
    # Exact uri_launch_refusal wording must be absent:
    assert "uri launch on" not in error
    assert "is not confined to" not in error
    assert f"is not confined to {TARGET_PKG}" not in error
    assert "uri launch on" not in detail
    assert "is not confined to" not in detail
    ctrl.launch.assert_not_called()

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    guard_lines = [r for r in records if r.get("type") == "guard"]
    assert len(guard_lines) == 1
    assert guard_lines[0]["tool"] == "nerve_launch"
    assert guard_lines[0]["reason"] == "other_package"
    assert guard_lines[0]["detail"] == not_found_msg

    # When _resolve_device finds a known non-confined device, uri_launch_refusal still fires:
    known_refusal = _core._app_refusal(
        action_log.build_guard(target_package=TARGET_PKG),
        "nerve_launch",
        "IOS_SIM",
        {"package": TARGET_PKG, "uri": "https://example.com/deeplink"},
    )
    assert known_refusal is not None
    assert f"uri launch on ios is not confined to {TARGET_PKG}" in known_refusal["error"]


def test_assert_recording_playable_non_wear_omits_wear_and_scrcpy(tmp_path: Path) -> None:
    """[contract] assert_recording_playable(..., is_wear=False) has no 'Wear OS'/'scrcpy'
    and suggests re-recording while preserving stderr detail."""
    path = _write_8kb_frameless_mp4(tmp_path / "phone.mp4")
    assert path.stat().st_size == 8192

    expected = (
        f"Recording at {path} has no decodable video frames (8192 bytes). "
        "The phone/tablet recording produced no decodable frames; "
        "try re-recording. stderr: Encoder failed (err=-38)"
    )

    with pytest.raises(RuntimeError) as exc_info:
        assert_recording_playable(path, "Encoder failed (err=-38)\n", is_wear=False)

    msg = str(exc_info.value)
    assert "Wear OS" not in msg
    assert "scrcpy" not in msg
    assert msg == expected

    # Default keyword value is_wear=False produces the identical non-Wear message:
    with pytest.raises(RuntimeError) as default_exc:
        assert_recording_playable(path, "Encoder failed (err=-38)\n")
    assert str(default_exc.value) == expected


def test_assert_recording_playable_wear_keeps_scrcpy_advice(tmp_path: Path) -> None:
    """[pin] assert_recording_playable(..., is_wear=True) keeps the Wear OS scrcpy advice."""
    path = _write_8kb_frameless_mp4(tmp_path / "wear.mp4")
    assert path.stat().st_size == 8192

    with pytest.raises(RuntimeError) as exc_info:
        assert_recording_playable(path, "watch stderr\n", is_wear=True)

    msg = str(exc_info.value)
    assert "Wear OS" in msg
    assert "scrcpy" in msg
    assert msg == (
        f"Recording at {path} has no decodable video frames (8192 bytes). "
        "On Wear OS, adb screenrecord exits 0 but writes an empty MP4; "
        "record the watch with scrcpy instead. stderr: watch stderr"
    )


@pytest.mark.parametrize(
    ("chars", "has_client", "expect_wear"),
    [
        ("nosdcard,default\n", True, False),
        ("nosdcard,watch\n", True, True),
        ("", False, False),
    ],
    ids=["phone", "wear", "no_client"],
)
@patch("subprocess.run")
def test_android_stop_recording_passes_is_wear_to_assert_recording_playable(
    mock_run: MagicMock,
    chars: str,
    has_client: bool,
    expect_wear: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] android_sensor._SensorRecordMixin._stop_guest_recording passes is_wear."""
    from linc_nerve.core import android_sensor

    assert callable(android_sensor._SensorRecordMixin._stop_guest_recording)
    monkeypatch.setenv("HOME", str(tmp_path))
    ctrl = AndroidController()
    if has_client:
        mock_device = MagicMock()
        mock_device.shell.return_value = chars
        mock_client = MagicMock()
        mock_client.device.side_effect = lambda serial: (
            mock_device if serial == "DEV1" else MagicMock()
        )
        ctrl._client = mock_client
    proc = MagicMock()
    proc.communicate.return_value = ("", "enc detail")
    ctrl._recording_processes = {"DEV1": {"target": "guest", "process": proc}}

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if "pull" in cmd:
            _write_8kb_frameless_mp4(Path(cmd[cmd.index("pull") + 2]))
        return MagicMock(returncode=0, stderr="")

    mock_run.side_effect = fake_run

    with (
        patch("linc_nerve.core.android.time.sleep"),
        pytest.raises(RuntimeError) as exc_info,
    ):
        ctrl.stop_recording("DEV1")

    msg = str(exc_info.value)
    assert "stderr: enc detail" in msg
    if expect_wear:
        assert "Wear OS" in msg
        assert "scrcpy" in msg
    else:
        assert "Wear OS" not in msg
        assert "scrcpy" not in msg
        assert "The phone/tablet recording produced no decodable frames; try re-recording." in msg
