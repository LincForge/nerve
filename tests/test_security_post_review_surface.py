"""Security review of the surface added since the 2026-10-02 review (linc-nerve-t87e).

Scope: every ``src/`` change in ``git log 7a62c89..origin/main -- src/``,
grouped (a) action log, (b) guarded session, (c) replay, (d) extract_frames,
(e) nerve_network + the ensure_ready connectivity read, (f) adb server
selection, (g) usage-log secret handling. Each test maps to one row of
``docs/reviews/2026-10-10-linc-nerve-post-review-surface.md``; the
``FAIL`` rows are the regression tests that failed on origin/main.

Method (as in the 2026-10-02 review): everything runs in-process against
fakes. A real ``AndroidController`` whose ``_shell`` is a recording mock stands
in for the device shell, so the exact shell strings are asserted. The
autouse ``_tripwire`` fixture replaces ``subprocess.run`` / ``subprocess.Popen``
/ ``subprocess.check_output`` and ``adbutils.AdbClient`` with recorders that
raise, and fails the test if any real-process path was reached — no adb,
xcrun, emulator or device is ever contacted. The one HTTP test starts
``sse_server.start_http_server`` on a free loopback port; the nerve daemon is
never run. MP4 fixtures are built in-test with PyAV (codec ``mpeg4``).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import socket
import subprocess
import threading
from collections.abc import AsyncGenerator, Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import adbutils
import av
import pytest
import pytest_asyncio
from PIL import Image

from linc_nerve.core import action_log, adb_health, frames, replay
from linc_nerve.core.adb_server import adb_server_address
from linc_nerve.core.android import AndroidController
from linc_nerve.core.ios import IOSController
from linc_nerve.core.observability import log_mcp_call
from linc_nerve.mcp import _core, actionlog_tools, app_tools, input_tools, state_tools
from linc_nerve.mcp.actionlog_tools import (
    _nerve_actionlog_start_sync,
    _nerve_actionlog_stop_sync,
    _nerve_replay_sync,
)
from linc_nerve.mcp.navigation_tools import _nerve_navigate_sync

# http_router resolves the served git sha at import (``git rev-parse``): import it
# here with that one subprocess stubbed, so no test reaches a real process.
with patch.object(subprocess, "check_output", side_effect=OSError("stubbed at import")):
    importlib.import_module("linc_nerve.daemon.http_router")

TARGET = "com.example.app"
SERIAL = "emulator-5554"
IOS_SIM = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"

# ---------------------------------------------------------------------------
# Tripwire: no real process, no real adb socket
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tripwire(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """Fail the test if any real subprocess or adbutils client is created."""
    tripped: list[Any] = []

    def trip(*args: Any, **kwargs: Any) -> Any:
        tripped.append(args[0] if args else kwargs)
        raise AssertionError(f"real process/socket path reached: {args!r} {kwargs!r}")

    monkeypatch.setattr(subprocess, "run", trip)
    monkeypatch.setattr(subprocess, "Popen", trip)
    monkeypatch.setattr(subprocess, "check_output", trip)
    monkeypatch.setattr(adbutils, "AdbClient", trip)
    yield tripped
    assert tripped == [], f"real process/socket path reached: {tripped!r}"


@pytest.fixture(autouse=True)
def _no_wda_process_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """IOSController() builds a WDAManager whose orphan sweep runs ``pgrep``/``ps``."""
    from linc_nerve.core import wda

    monkeypatch.setattr(wda.WDAManager, "_cleanup_orphans", staticmethod(lambda: None))
    monkeypatch.setattr(wda.WDAManager, "_sweep_dead_ports", lambda self: None)


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "actionlogs"
    root.mkdir()
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(root))
    monkeypatch.setattr(actionlog_tools, "_environ", os.environ)
    action_log.REGISTRY.reset()
    yield root
    action_log.REGISTRY.reset()


@pytest.fixture
def root(_clean_registry: Path) -> Path:
    return _clean_registry


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _recording_android(shell_output: str | dict[str, str] = "") -> AndroidController:
    """A real AndroidController whose device shell is a recording mock."""
    ctrl = AndroidController()

    def shell(serial: str, cmd: str, timeout: int = 15) -> str:
        if isinstance(shell_output, dict):
            return next((out for key, out in shell_output.items() if cmd.startswith(key)), "")
        return shell_output

    ctrl._shell = MagicMock(side_effect=shell)  # type: ignore[method-assign]
    ctrl.get_foreground = MagicMock(  # type: ignore[method-assign]
        return_value={"package": TARGET, "activity": ".Main"}
    )
    ctrl.elements = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {"class": "android.widget.FrameLayout", "bounds": "[0,0][1080,2400]"},
            {"class": "android.widget.Button", "bounds": "[100,100][300,200]", "text": "Next"},
        ]
    )
    return ctrl


def _shell_cmds(ctrl: AndroidController) -> list[str]:
    return [c.args[1] for c in ctrl._shell.call_args_list]  # type: ignore[attr-defined]


class FakeIOS:
    """iOS stand-in: no ``get_foreground`` (as today), records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def elements(self, serial: str, force_refresh: bool = False) -> list[dict[str, Any]]:
        return [{"class": "XCUIElementTypeButton", "bounds": "[0,0][10,10]", "text": "Ok"}]

    def launch(self, serial: str, package: str, **kwargs: Any) -> None:
        self.calls.append(("launch", (serial, package, kwargs.get("uri", ""))))


@contextlib.contextmanager
def _device(ctrl: Any, platform: str = "android") -> Iterator[Any]:
    with (
        patch.object(_core, "_resolve_device", return_value=(object(), platform)),
        patch.object(_core, "_get_controller", return_value=ctrl),
        patch.object(_core, "_check_guard", return_value=None),
    ):
        yield ctrl


def _write_log(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def _action(seq: int, tool: str, args: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "type": "action",
        "seq": seq,
        "tool": tool,
        "args": args,
        "duration_ms": 10.0,
        "ok": True,
        "error": None,
        "t_session_s": float(seq),
        "t_rec_s": None,
        **extra,
    }


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ===========================================================================
# (a) action log — path confinement, O_NOFOLLOW, TOCTOU, sizes, redaction
# ===========================================================================


def test_a1_symlink_final_component_is_not_followed(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.jsonl"
    victim.write_text("untouched\n")
    (root / "evil.jsonl").symlink_to(victim)

    result = _nerve_actionlog_start_sync(SERIAL, str(root / "evil.jsonl"))

    assert "Cannot open action log" in result["error"]
    assert victim.read_text() == "untouched\n"


@pytest.mark.parametrize(
    "path",
    ["../escape.jsonl", "sub/../../escape.jsonl", "{root}/../escape.jsonl", "/etc/x.jsonl"],
)
def test_a2_dotdot_and_absolute_escapes_refused(root: Path, path: str) -> None:
    result = _nerve_actionlog_start_sync(SERIAL, path.format(root=root))

    assert result["validation_error"] is True
    assert "must be under an allowed action-log root" in result["error"]
    assert not (root.parent / "escape.jsonl").exists()


def test_a3_symlinked_directory_inside_root_refused(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    result = _nerve_actionlog_start_sync(SERIAL, str(root / "link" / "x.jsonl"))

    assert result["validation_error"] is True
    assert list(outside.iterdir()) == []


def test_a4_fifo_named_jsonl_does_not_hang_start(root: Path) -> None:
    """FAIL on origin/main: opening a FIFO for append blocked the tool worker forever."""
    fifo = root / "pipe.jsonl"
    os.mkfifo(fifo)
    result: dict[str, Any] = {}

    def start() -> None:
        result.update(_nerve_actionlog_start_sync(SERIAL, str(fifo)))

    worker = threading.Thread(target=start, daemon=True)
    worker.start()
    worker.join(timeout=3.0)
    if worker.is_alive():  # unblock the stuck open() so the thread can finish
        reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        worker.join(timeout=3.0)
        os.close(reader)
        pytest.fail("nerve_actionlog_start blocked on a FIFO named *.jsonl")
    assert result["error"].startswith("Cannot open action log")  # ENXIO: no reader
    assert action_log.REGISTRY.active(SERIAL) is None
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # with a reader it opens...
    try:
        with_reader = _nerve_actionlog_start_sync(SERIAL, str(fifo))
    finally:
        os.close(reader)
    assert "not a regular file" in with_reader["error"]  # ...and is refused by fstat
    assert action_log.REGISTRY.active(SERIAL) is None


def test_a5_replay_refuses_oversized_non_utf8_and_fifo_logs(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = root / "big.jsonl"
    big.write_text("x" * 64)
    binary = root / "binary.jsonl"
    binary.write_bytes(b"\xff\xfe\x00garbage\n")
    fifo = root / "pipe.jsonl"
    os.mkfifo(fifo)
    ctrl = _recording_android()

    with _device(ctrl):
        not_utf8 = _nerve_replay_sync(SERIAL, str(binary))
        not_regular = _nerve_replay_sync(SERIAL, str(fifo))
        monkeypatch.setattr(replay, "MAX_LOG_BYTES", 32)
        too_big = _nerve_replay_sync(SERIAL, str(big))

    assert "larger than 32 bytes" in too_big["error"]
    assert not_utf8["error"] == "action log is not UTF-8 text"
    assert "not a regular file" in not_regular["error"]
    assert _shell_cmds(ctrl) == []


def test_a6_secret_typed_text_never_reaches_the_log(root: Path) -> None:
    secret = "hunter2 pass"
    ctrl = _recording_android()
    ctrl.type_text = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError(f"input failed for {secret}")
    )
    log_path = root / "run.jsonl"

    with _device(ctrl):
        _nerve_actionlog_start_sync(SERIAL, str(log_path))
        result = input_tools._nerve_type_sync(SERIAL, secret, secret=True)
        _nerve_actionlog_stop_sync(SERIAL)

    raw = log_path.read_text(encoding="utf-8")
    action = _lines(log_path)[1]
    assert secret not in raw and secret not in json.dumps(result)
    assert action["args"] == {"text": "<redacted>", "secret": True, "length": len(secret)}
    assert action["error"] == "Type failed: input failed for <redacted>"
    assert oct(log_path.stat().st_mode & 0o777) == "0o600"


def test_a7_toctou_swap_after_check_is_not_followed(root: Path, tmp_path: Path) -> None:
    """The checked path is opened O_NOFOLLOW: a symlink swapped in after the check fails."""
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.jsonl"
    victim.write_text("untouched\n")
    resolved = action_log.resolve_log_path(str(root / "race.jsonl"), [root])
    assert isinstance(resolved, Path)
    resolved.symlink_to(victim)  # attacker wins the race between check and open

    with pytest.raises(OSError):
        action_log.REGISTRY.start(SERIAL, resolved)

    assert victim.read_text() == "untouched\n"


# ===========================================================================
# (b) guarded session — escapes via launch URI, intents, keys, replay
# ===========================================================================


def test_b1_android_uri_launch_is_confined_to_the_target_package(root: Path) -> None:
    ctrl = _recording_android()

    with _device(ctrl):
        _nerve_actionlog_start_sync(SERIAL, str(root / "g.jsonl"), target_package=TARGET)
        result = app_tools._nerve_launch_sync(SERIAL, TARGET, uri="https://evil.example/pay")

    assert result["success"] is True
    assert _shell_cmds(ctrl) == [
        f"am start -a android.intent.action.VIEW -d https://evil.example/pay {TARGET}"
    ]


def test_b2_ios_uri_launch_cannot_escape_the_target_app(root: Path) -> None:
    """FAIL on origin/main: iOS opens the URI in whatever app claims it (bundle id ignored)."""
    ios = FakeIOS()

    with _device(ios, platform="ios"):
        _nerve_actionlog_start_sync(IOS_SIM, str(root / "g.jsonl"), target_package=TARGET)
        launched = app_tools._nerve_launch_sync(IOS_SIM, TARGET, uri="https://evil.example")
        _nerve_actionlog_stop_sync(IOS_SIM)

    assert launched.get("guard") == "other_package", launched
    assert ios.calls == []
    guard_lines = [r for r in _lines(root / "g.jsonl") if r["type"] == "guard"]
    assert [r["tool"] for r in guard_lines] == ["nerve_launch"]


def test_b2_evidence_real_ios_launch_ignores_bundle_when_uri_set() -> None:
    """Why b2 matters: the real controller runs ``simctl openurl`` without the bundle id."""
    ios = IOSController()
    ios._run = MagicMock()  # type: ignore[method-assign]

    ios.launch(IOS_SIM, TARGET, uri="https://evil.example")

    ios._run.assert_called_once_with("openurl", IOS_SIM, "https://evil.example")


def test_b2_ios_plain_launch_of_target_still_runs(root: Path) -> None:
    ios = FakeIOS()

    with _device(ios, platform="ios"):
        _nerve_actionlog_start_sync(IOS_SIM, str(root / "g.jsonl"), target_package=TARGET)
        launched = app_tools._nerve_launch_sync(IOS_SIM, TARGET)

    assert launched["success"] is True
    assert ios.calls == [("launch", (IOS_SIM, TARGET, ""))]


def test_b3_other_package_launch_and_navigate_refused(root: Path) -> None:
    ctrl = _recording_android()

    with _device(ctrl):
        _nerve_actionlog_start_sync(SERIAL, str(root / "g.jsonl"), target_package=TARGET)
        launched = app_tools._nerve_launch_sync(SERIAL, "com.android.vending")
        navigated = _nerve_navigate_sync(SERIAL, "precisionlevel", "settings")

    assert launched["guard"] == "other_package"
    assert navigated["guard"] == "other_package"
    assert _shell_cmds(ctrl) == []


def test_b4_replayed_launch_of_other_package_refused_by_guard(root: Path) -> None:
    log = _write_log(
        root / "hostile.jsonl",
        [_action(1, "nerve_launch", {"package": "com.android.vending", "uri": ""})],
    )
    ctrl = _recording_android()

    with _device(ctrl):
        _nerve_actionlog_start_sync(SERIAL, str(root / "g.jsonl"), target_package=TARGET)
        result = _nerve_replay_sync(SERIAL, str(log))
        _nerve_actionlog_stop_sync(SERIAL)

    assert result["outcome"] == "diverged"
    assert "outside" in result["reason"] or "refused" in result["reason"]
    assert _shell_cmds(ctrl) == []
    assert [r["tool"] for r in _lines(root / "g.jsonl") if r["type"] == "guard"] == [
        "nerve_launch"
    ]


def test_b5_app_switch_key_lands_where_input_is_refused(root: Path) -> None:
    ctrl = _recording_android()

    with _device(ctrl):
        _nerve_actionlog_start_sync(SERIAL, str(root / "g.jsonl"), target_package=TARGET)
        pressed = input_tools._nerve_key_sync(SERIAL, "EXPLORER")
        ctrl.get_foreground.return_value = {  # type: ignore[attr-defined]
            "package": "com.android.chrome",
            "activity": ".Main",
        }
        tapped = input_tools._nerve_tap_sync(SERIAL, 150, 150)
        typed = input_tools._nerve_type_sync(SERIAL, "evil.example")

    assert pressed["success"] is True
    assert tapped["guard"] == typed["guard"] == "foreground_not_allowed"
    assert _shell_cmds(ctrl) == ["input keyevent EXPLORER"]


# ===========================================================================
# (c) replay — hostile lines vs the shell sinks, unbounded timeouts
# ===========================================================================


def _replay_lines(root: Path, *entries: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    log = _write_log(root / "hostile.jsonl", list(entries))
    ctrl = _recording_android()
    with _device(ctrl):
        result = _nerve_replay_sync(SERIAL, str(log))
    return result, _shell_cmds(ctrl)


@pytest.mark.parametrize("key", ["HOME;reboot", "HOME\n", "HOME $(reboot)", "BACK&&id"])
def test_c1_hostile_key_never_reaches_the_shell(root: Path, key: str) -> None:
    result, cmds = _replay_lines(root, _action(1, "nerve_key", {"key": key}))

    assert result["outcome"] == "diverged"
    assert "Invalid key" in result["reason"]
    assert cmds == []


def test_c2_hostile_coordinates_and_text_are_neutralised(root: Path) -> None:
    result, cmds = _replay_lines(
        root,
        _action(1, "nerve_type", {"text": "$(reboot)`id`;x"}),
        _action(2, "nerve_tap", {"x": "1;reboot", "y": 2}),
    )

    assert cmds == ["input text '$(reboot)`id`;x'"]
    assert result["outcome"] == "diverged" and result["diverged_at"] == 1


def test_c3_hostile_launch_package_and_uri(root: Path) -> None:
    bad_pkg, cmds_pkg = _replay_lines(
        root, _action(1, "nerve_launch", {"package": "com.a;reboot", "uri": ""})
    )
    bad_uri, cmds_uri = _replay_lines(
        root, _action(1, "nerve_launch", {"package": TARGET, "uri": "x://$(reboot) -n a/.B"})
    )

    assert bad_pkg["outcome"] == "diverged" and cmds_pkg == []
    # linc-nerve-o2ro: a uri outside the RFC 3986 grammar (space here) is now
    # refused before the shell string is built, instead of only being quoted.
    assert bad_uri["outcome"] == "diverged" and "Invalid uri" in bad_uri["reason"]
    assert cmds_uri == []


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0.0, -1.0, 120.5])
def test_c4_step_timeout_bounded(root: Path, timeout: float) -> None:
    log = _write_log(root / "ok.jsonl", [_action(1, "nerve_key", {"key": "BACK"})])
    ctrl = _recording_android()

    with _device(ctrl):
        result = _nerve_replay_sync(SERIAL, str(log), step_timeout_s=timeout)

    assert result["validation_error"] is True
    assert _shell_cmds(ctrl) == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("nerve_long_press", {"x": 1, "y": 2, "duration_ms": 600_000}),
        ("nerve_swipe", {"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4, "duration_ms": 9e8}),
    ],
)
def test_c5_gesture_longer_than_the_run_budget_is_not_dispatched(
    root: Path, tool: str, args: dict[str, Any]
) -> None:
    """FAIL on origin/main: a hostile duration_ms held the device far past the 75 s budget."""
    result, cmds = _replay_lines(root, _action(1, tool, args))

    assert result["outcome"] == "diverged"
    assert "budget" in result["reason"]
    assert cmds == []


def test_c6_hostile_device_arg_cannot_redirect_a_step(root: Path) -> None:
    result, cmds = _replay_lines(
        root, _action(1, "nerve_key", {"key": "BACK", "device": "OTHER-SERIAL"})
    )

    assert result["outcome"] == "diverged"
    assert "multiple values" in result["reason"]
    assert cmds == []


# ===========================================================================
# (d) nerve_extract_frames — demuxer pinning, out_dir, timestamps, bombs
# ===========================================================================


def _mp4(path: Path, size: tuple[int, int] = (64, 48), seconds: int = 2, fps: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("mpeg4", rate=fps)
        stream.width, stream.height = size
        stream.pix_fmt = "yuv420p"
        for i in range(fps * seconds):
            frame = av.VideoFrame.from_image(Image.new("RGB", size, (i * 8 % 255, 0, 0)))
            frame.pts = i
            frame.time_base = Fraction(1, fps)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


@pytest.fixture
def video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    vid_dir = tmp_path / "vid"
    monkeypatch.setattr(frames, "_system_tmp", lambda: vid_dir)
    return _mp4(vid_dir / "rec.mp4")


@pytest.mark.parametrize(
    "body",
    [
        "ffconcat version 1.0\nfile '{video}'\n",
        "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nfile://{video}\n#EXT-X-ENDLIST\n",
    ],
)
def test_d1_playlists_disguised_as_mp4_are_not_followed(
    video: Path, body: str, tmp_path: Path
) -> None:
    disguised = video.parent / "playlist.mp4"
    disguised.write_text(body.format(video=video))

    result = frames.extract_request(str(disguised), "0.5", str(tmp_path / "vid" / "out"))

    assert result["validation_error"] is True
    assert "cannot read video" in result["error"]
    assert not (tmp_path / "vid" / "out").exists()


@pytest.mark.parametrize("out", ["{tmp}/elsewhere", "{vid}/../../escape", "{vid}/link"])
def test_d2_out_dir_outside_the_roots_refused(video: Path, tmp_path: Path, out: str) -> None:
    (tmp_path / "target").mkdir()
    (video.parent / "link").symlink_to(tmp_path / "target", target_is_directory=True)

    result = frames.extract_request(str(video), "0.5", out.format(tmp=tmp_path, vid=video.parent))

    assert result["validation_error"] is True
    assert "outside the video's directory" in result["error"]
    assert list((tmp_path / "target").iterdir()) == []


def test_d3_planted_frame_symlink_is_not_followed(video: Path, tmp_path: Path) -> None:
    """FAIL on origin/main: PIL followed a planted frame_<ms>.png symlink out of the roots."""
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    victim = victim_dir / "authorized_keys"
    victim.write_text("ssh-ed25519 AAAA keep-me\n")
    out = video.parent / "out"
    out.mkdir()
    (out / "frame_500.png").symlink_to(victim)

    with contextlib.suppress(OSError, ValueError):
        frames.extract_request(str(video), "0.5", str(out))

    assert victim.read_bytes() == b"ssh-ed25519 AAAA keep-me\n"


@pytest.mark.parametrize("stamps", ["nan", "inf", "-inf", "1e999", "-0.001", "2", "0x10", ""])
def test_d4_hostile_timestamps_rejected_before_writing(video: Path, stamps: str) -> None:
    out = video.parent / "out"

    result = frames.extract_request(str(video), stamps, str(out))

    assert result["validation_error"] is True
    assert not out.exists()


def test_d5_timestamp_count_is_capped(video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FAIL on origin/main: any number of timestamps was decoded and written."""
    monkeypatch.setattr(frames, "MAX_TIMESTAMPS", 3, raising=False)
    out = video.parent / "out"

    result = frames.extract_request(str(video), "0.1,0.2,0.3,0.4", str(out))

    assert result.get("validation_error") is True, result
    assert "at most 3 timestamps" in result["error"]
    assert not out.exists()


def test_d6_oversized_frames_refused_before_decoding(
    video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAIL on origin/main: no cap on frame dimensions (decompression bomb)."""
    monkeypatch.setattr(frames, "MAX_FRAME_PIXELS", 64 * 48 - 1, raising=False)
    out = video.parent / "out"

    result = frames.extract_request(str(video), "0.5", str(out))

    assert result.get("validation_error") is True, result
    assert "pixels" in result["error"]
    assert not out.exists()


def test_d_normal_extraction_still_works(video: Path) -> None:
    out = video.parent / "out"

    result = frames.extract_request(str(video), "0.5,1.5", str(out))

    assert [Path(f["path"]).name for f in result["frames"]] == ["frame_500.png", "frame_1500.png"]
    with Image.open(out / "frame_500.png") as png:
        assert png.size == (64, 48)


# ===========================================================================
# (e) nerve_network + ensure_ready connectivity read
# ===========================================================================

_DUMPSYS = (
    "Active default network: 100\n"
    'NetworkAgentInfo{network{100} handle{1} ni{WIFI CONNECTED extra: "HomeSSID"} '
    "lp{{InterfaceName: wlan0 LinkAddresses: [ 192.0.2.23/24 ]}} "
    "nc{[ Transports: WIFI Capabilities: INTERNET&NOT_METERED&VALIDATED "
    'SSID: "HomeSSID" BSSID: aa:bb:cc:dd:ee:ff]}}\n'
)
_PING = "1 packets transmitted, 1 received, 0% packet loss"


def test_e1_network_probe_runs_only_the_fixed_commands() -> None:
    ctrl = _recording_android({"dumpsys connectivity": _DUMPSYS, "ping": _PING})

    with _device(ctrl):
        result = state_tools._nerve_network_sync(SERIAL, probe=True)

    assert _shell_cmds(ctrl) == ["dumpsys connectivity", "ping -c 1 -w 3 8.8.8.8"]
    assert result["online"] is True and result["reachable"] is True


def test_e2_network_result_carries_no_ssid_ip_or_bssid() -> None:
    ctrl = _recording_android({"dumpsys connectivity": _DUMPSYS, "ping": _PING})

    with _device(ctrl):
        result = state_tools._nerve_network_sync(SERIAL, probe=True)

    assert set(result) == {
        "device",
        "platform",
        "transport",
        "default_network",
        "validated",
        "reachable",
        "online",
    }
    flat = json.dumps(result)
    for leak in ("HomeSSID", "192.0.2", "aa:bb", "wlan0"):
        assert leak not in flat
    assert result["transport"] == "wifi"


def test_e3_ensure_ready_require_network_never_pings() -> None:
    ctrl = _recording_android({"dumpsys connectivity": _DUMPSYS})
    ctrl.ensure_ready = MagicMock(  # type: ignore[method-assign]
        return_value={"ready": True, "reason": "ready", "platform": "android"}
    )

    with _device(ctrl):
        result = state_tools._nerve_ensure_ready_sync(SERIAL, require_network=True)

    assert _shell_cmds(ctrl) == ["dumpsys connectivity"]
    assert result["ready"] is True and result["network"]["reachable"] is None


@pytest.mark.parametrize(
    "dump",
    [
        "Active default network: 1|.*\nNetworkAgentInfo{network{1} Transports: WIFI}\n",
        "Active default network: 1\nNetworkAgentInfo{network{100} nc{[ Transports: WIFI "
        "Capabilities: VALIDATED]}}\n",
    ],
)
def test_e4_hostile_dumpsys_cannot_forge_online(dump: str) -> None:
    ctrl = _recording_android({"dumpsys connectivity": dump})

    with _device(ctrl):
        result = state_tools._nerve_network_sync(SERIAL)

    assert result["online"] is None
    assert result["validated"] is None


# ===========================================================================
# (f) adb server selection — ADB_SERVER_SOCKET / ANDROID_ADB_SERVER_PORT
# ===========================================================================


def test_f1_non_loopback_adb_server_socket_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:10.0.0.5:5037")

    with pytest.raises(ValueError, match="only loopback hosts are supported"):
        adb_server_address()


@pytest.mark.parametrize(
    "spec",
    [
        "tcp:0.0.0.0:5037",
        "tcp:127.0.0.1.nip.io:5037",
        "tcp:[::ffff:10.0.0.5]:5037",
        "tcp:localhost.:5037",
        "tcp:10.0.0.5:5037 ",
        "tcp:127.0.0.2@10.0.0.5:5037",
        "unix:/tmp/adb.sock",
        "localfilesystem:/tmp/adb",
        "tcp:127.0.0.1:٥٠٣٧",
        "tcp:127.0.0.1:65536",
    ],
)
def test_f2_lookalike_and_malformed_specs_refused(
    monkeypatch: pytest.MonkeyPatch, spec: str
) -> None:
    monkeypatch.setenv("ADB_SERVER_SOCKET", spec)
    if spec == "tcp:10.0.0.5:5037 ":  # surrounding whitespace is stripped, host still refused
        pass

    with pytest.raises(ValueError):
        adb_server_address()


def test_f3_client_never_built_for_a_non_loopback_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:10.0.0.5:5037")
    ctrl = AndroidController()

    with pytest.raises(ValueError, match="ADB_SERVER_SOCKET"):
        ctrl._get_client()
    assert ctrl._client is None


def test_f4_watchdog_probe_never_runs_adb_against_a_non_loopback_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAIL on origin/main: ``adb devices`` inherited ADB_SERVER_SOCKET=tcp:10.0.0.5:5037."""
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:10.0.0.5:5037")
    ran: list[list[str]] = []
    monkeypatch.setattr(
        adb_health,
        "_run",
        lambda argv, timeout: ran.append(argv) or subprocess.CompletedProcess(argv, 1, "", ""),
    )

    status = adb_health.probe_transport(adb_path="adb")

    assert [argv for argv in ran if argv[:1] == ["adb"]] == []
    assert status.responsive is False
    assert "ADB_SERVER_SOCKET" in status.detail


def test_f5_self_heal_never_starts_or_connects_a_non_loopback_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAIL on origin/main: ``adb start-server`` / ``adb connect`` ran against 10.0.0.5."""
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:10.0.0.5:5037")
    ran: list[list[str]] = []
    monkeypatch.setattr(
        adb_health,
        "_run",
        lambda argv, timeout: ran.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )

    healed = adb_health.recover_adb_server(None, reconnect=["192.0.2.9:5555"])

    assert healed is False
    assert ran == []


# ===========================================================================
# (g) usage log — NERVE_USAGE_LOG=full secret handling (#208)
# ===========================================================================


def _usage_entries(home: Path) -> list[dict[str, Any]]:
    path = home / "mcp-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def full_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("LINC_HOME", str(home))
    monkeypatch.setenv("NERVE_USAGE_LOG", "full")
    return home


def test_g1_full_mode_redacts_flagged_secret_to_length(full_mode: Path) -> None:
    log_mcp_call(
        "linc-nerve", "nerve_type", {"device": "S", "text": "1234", "secret": True}, 1, True
    )

    entry = _usage_entries(full_mode)[0]
    assert entry["params"]["text"] == "<redacted len=4>"
    assert "1234" not in json.dumps(entry)


def test_g2_full_mode_scrubs_the_secret_out_of_errors(full_mode: Path) -> None:
    log_mcp_call(
        "linc-nerve",
        "nerve_type",
        {"device": "S", "text": "pw-xyz", "secret": True},
        1,
        False,
        "Type failed: pw-xyz rejected",
    )

    entry = _usage_entries(full_mode)[0]
    assert entry["error"] == "Type failed: <redacted> rejected"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_accepting(port: int) -> None:
    for _ in range(250):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.02)
            continue
        writer.close()
        return
    raise TimeoutError(f"test server on {port} never accepted")


async def _post(port: int, tool: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    payload = json.dumps(body).encode()
    head = (
        f"POST /tools/{tool} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n"
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode() + payload)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout=10.0)
    writer.close()
    status_line, _, rest = raw.partition(b"\r\n")
    return int(status_line.split()[1]), json.loads(rest.partition(b"\r\n\r\n")[2] or b"{}")


@pytest_asyncio.fixture
async def http_port(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[int]:
    from linc_nerve.mcp import sse_server

    monkeypatch.delenv("NERVE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("NERVE_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setattr(
        sse_server,
        "_TOOL_REGISTRY",
        {"nerve_type": input_tools._nerve_type_sync, "nerve_replay": _nerve_replay_sync},
    )
    port = _free_port()
    task = asyncio.create_task(sse_server.start_http_server(port=port))
    await _wait_accepting(port)
    yield port
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["yes", "True", 1])
async def test_g3_daemon_rejected_secret_flag_still_redacts_text(
    full_mode: Path, http_port: int, flag: Any
) -> None:
    """FAIL on origin/main: a 400 on a non-canonical secret flag logged the text in clear."""
    ctrl = _recording_android()
    with _device(ctrl):
        status, _ = await _post(
            http_port, "nerve_type", {"device": SERIAL, "text": "hunter2", "secret": flag}
        )

    assert status == 400
    assert _shell_cmds(ctrl) == []
    entries = _usage_entries(full_mode)
    assert [e["tool"] for e in entries] == ["nerve_type"]
    assert "hunter2" not in json.dumps(entries)


@pytest.mark.asyncio
async def test_g4_replayed_secret_never_reaches_usage_or_action_log(
    full_mode: Path, http_port: int, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "s3cr3t value"
    monkeypatch.setenv("NERVE_SECRET_PW", secret)
    log = _write_log(
        root / "login.jsonl",
        [_action(1, "nerve_type", {"text": "<redacted>", "secret": True, "length": 12})],
    )
    ctrl = _recording_android()
    with _device(ctrl):
        _nerve_actionlog_start_sync(SERIAL, str(root / "audit.jsonl"))
        status, body = await _post(
            http_port,
            "nerve_replay",
            {"device": SERIAL, "log_path": str(log), "secrets_env": "1=NERVE_SECRET_PW"},
        )
        _nerve_actionlog_stop_sync(SERIAL)

    assert status == 200 and body["result"]["outcome"] == "completed"
    assert _shell_cmds(ctrl) == ["input text s3cr3t%svalue"]
    assert secret not in json.dumps(_usage_entries(full_mode))
    assert secret not in (root / "audit.jsonl").read_text()
