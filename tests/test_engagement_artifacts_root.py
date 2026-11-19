"""Contract tests for engagement mode and NERVE_ARTIFACTS_ROOT confinement.

Covers acceptance criteria #1-#7 for bead linc-nerve-qo86 (epic linc-nerve-3m2o).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from fractions import Fraction
from pathlib import Path
from unittest.mock import MagicMock, patch

import av
import pytest
from click.testing import CliRunner
from PIL import Image

from linc_nerve.cli.doctor import required_linc_dirs
from linc_nerve.cli.main import main
from linc_nerve.core import action_log, frames, observability, output_retention, usage_stats
from linc_nerve.core.android import AndroidController
from linc_nerve.core.ios import IOSController
from linc_nerve.mcp import _core
from linc_nerve.mcp import server as mcp_server
from linc_nerve.mcp.actionlog_tools import (
    _nerve_actionlog_start_sync,
    _nerve_actionlog_stop_sync,
    _nerve_replay_sync,
)
from linc_nerve.mcp.diagnostic_tools import (
    _devicectl_json,
    _nerve_inspect_sync,
    _nerve_logcat_sync,
    _nerve_usage_stats_sync,
)
from linc_nerve.mcp.navigation_tools import _nerve_navigate_screenshot_sync
from linc_nerve.mcp.recording_tools import (
    _nerve_extract_frames_sync,
    _nerve_record_fixed_sync,
    _nerve_record_start_sync,
    _nerve_record_stop_sync,
)
from linc_nerve.mcp.screenshot_tools import _nerve_screenshot_sync
from linc_nerve.mcp.ui_tools import _nerve_diff_sync, _nerve_state_sync
from tests._mp4_fixture import mp4_bytes

REPO_ROOT = Path(__file__).resolve().parents[1]
SIM_UUID = "11111111-2222-3333-4444-555555555555"
PHYS_SERIAL = "00008130-001122334455001E"


def _tiny_png_bytes() -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (12, 34, 56)).save(buf, format="PNG")
    return buf.getvalue()


def _write_mp4(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width, stream.height = (32, 24)
        stream.pix_fmt = "yuv420p"
        for i in range(10):
            frame = av.VideoFrame.from_image(Image.new("RGB", (32, 24), (20, 40, 60)))
            frame.pts = i
            frame.time_base = Fraction(1, 10)
            for pkt in stream.encode(frame):
                container.mux(pkt)
        for pkt in stream.encode():
            container.mux(pkt)
    return path


def _reset_core_output_dir() -> None:
    if _core._OUTPUT_LOCK is not None:
        _core._OUTPUT_LOCK.close()
        _core._OUTPUT_LOCK = None
    _core._OUTPUT_DIR = None
    _core._file_counter = 0


def _assert_tree_empty(directory: Path) -> None:
    assert list(directory.iterdir()) == [], (
        f"Expected {directory} to stay empty, found {list(directory.iterdir())}"
    )


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Provide clean HOME, LINC_HOME, TMPDIR, and artifacts root dirs."""
    home = tmp_path / "home"
    linc_h = tmp_path / "linc_home"
    sys_tmp = tmp_path / "sys_tmp"
    root = tmp_path / "vault"
    for d in (home, linc_h, sys_tmp, root):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LINC_HOME", str(linc_h))
    monkeypatch.setenv("TMPDIR", str(sys_tmp))
    monkeypatch.delenv("NERVE_ARTIFACTS_ROOT", raising=False)
    monkeypatch.delenv("NERVE_ENGAGEMENT", raising=False)
    monkeypatch.delenv("NERVE_ACTIONLOG_ROOTS", raising=False)
    monkeypatch.setattr(output_retention.tempfile, "tempdir", None)
    _reset_core_output_dir()
    yield {"home": home, "linc_home": linc_h, "tmp": sys_tmp, "root": root, "base": tmp_path}
    action_log.REGISTRY.reset()
    _reset_core_output_dir()


# ---------------------------------------------------------------------------
# 1. [contract] Recordings honor NERVE_ARTIFACTS_ROOT without touching HOME
# ---------------------------------------------------------------------------


def test_contract_recordings_under_artifacts_root(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] Android guest, emulator, and iOS sim recordings write under <root>/recordings."""
    root = isolated_env["root"]
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(root))

    # Android guest recording stop (faked adb pull)
    android_ctrl = AndroidController()
    android_ctrl._recording_processes = {}
    android_ctrl.ensure_ready = MagicMock(return_value={"ready": True, "reason": "ready"})

    def fake_pull(cmd: list[str], **kwargs: object) -> MagicMock:
        if "pull" in cmd:
            local_path = Path(cmd[cmd.index("pull") + 2])
            local_path.write_bytes(mp4_bytes())
        return MagicMock(returncode=0, stderr="")

    proc_mock = MagicMock(communicate=MagicMock(return_value=("", "")))
    with (
        patch("subprocess.Popen", return_value=proc_mock),
        patch("subprocess.run", side_effect=fake_pull),
        patch("time.sleep"),
    ):
        android_ctrl.start_recording("ANDROID1")
        guest_path = Path(android_ctrl.stop_recording("ANDROID1"))

    assert guest_path.is_relative_to(root / "recordings")
    assert guest_path.exists()

    # Android emulator recording start
    emu_ctrl = AndroidController()
    emu_ctrl._recording_processes = {}
    emu_ctrl.ensure_ready = MagicMock(return_value={"ready": True, "reason": "ready"})
    with (
        patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")),
        patch("time.sleep"),
    ):
        emu_ctrl._start_emulator_recording("emulator-5554")
        emu_path = Path(emu_ctrl._recording_processes["emulator-5554"]["host_path"])

    assert emu_path.is_relative_to(root / "recordings")

    # iOS simulator recording start
    ios_ctrl = IOSController()
    ios_ctrl._simulator_serials.add(SIM_UUID)
    devices_json = json.dumps(
        {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-17-5": [
                    {"udid": SIM_UUID, "name": "iPhone 15", "state": "Booted"}
                ]
            }
        }
    )
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    with (
        patch.object(ios_ctrl, "_run", return_value=MagicMock(stdout=devices_json)),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("time.sleep"),
    ):
        ios_ctrl.start_recording(SIM_UUID)
        ios_path = Path(ios_ctrl._recording_paths[SIM_UUID])

    assert ios_path.is_relative_to(root / "recordings")
    assert stat.S_IMODE((root / "recordings").stat().st_mode) == 0o700
    _assert_tree_empty(isolated_env["home"])
    _assert_tree_empty(isolated_env["linc_home"])
    _assert_tree_empty(isolated_env["tmp"])


# ---------------------------------------------------------------------------
# 2. [contract] All writers and scratch temp files land under <root>/
# ---------------------------------------------------------------------------


def test_contract_all_writers_confined_under_artifacts_root(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] Screenshot, state, logcat, action log, call log, devicectl under <root>."""
    root = isolated_env["root"]
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(root))
    monkeypatch.setenv("NERVE_USAGE_LOG", "redacted")

    png = _tiny_png_bytes()
    fake_android = MagicMock()
    fake_android.discover.return_value = []
    fake_android.screenshot.return_value = png
    fake_android.get_foreground.return_value = {"package": "com.example.app"}
    fake_android.get_orientation.return_value = "portrait"
    fake_android.get_screen_size.return_value = {"width": 16, "height": 16}
    fake_android.elements.return_value = [{"text": "OK", "bounds": [0, 0, 10, 10]}]
    fake_android.logcat.return_value = "\n".join(f"line {i}" for i in range(260))

    fake_ios = MagicMock()
    fake_ios.discover.return_value = []
    monkeypatch.setattr(_core, "_android", fake_android)
    monkeypatch.setattr(_core, "_ios", fake_ios)
    monkeypatch.setattr(_core, "_device_mgr", MagicMock())
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: (dev, "android"))
    monkeypatch.setattr(_core, "_check_guard", lambda dev: None)

    # 1. Screenshot
    shot = _nerve_screenshot_sync("DEV1")
    shot_path = Path(shot["screenshot"])
    assert shot_path.is_relative_to(root / "output")
    assert shot_path.exists()

    # 2. UI dump / composite state
    st = _nerve_state_sync("DEV1")
    state_path = Path(st["screenshot"])
    assert state_path.is_relative_to(root / "output")
    assert state_path.exists()

    # 3. Logcat spill
    log_res = _nerve_logcat_sync("DEV1", lines=260)
    log_path = Path(log_res["logcat"])
    assert log_path.is_relative_to(root / "output")
    assert log_path.exists()

    # 4. Action log started with relative name
    al_res = _nerve_actionlog_start_sync("DEV1", "run-01.jsonl")
    assert al_res["success"] is True
    al_path = Path(al_res["path"])
    assert al_path.is_relative_to(root / "actionlogs")
    assert al_path.exists()
    _nerve_actionlog_stop_sync("DEV1")

    # 5. MCP call log entry
    observability.log_mcp_call("linc-nerve", "nerve_screenshot", {"device": "DEV1"}, 5.0, True)
    call_log = root / "mcp-calls.jsonl"
    assert call_log.exists()

    # 6. devicectl JSON temp file
    seen_json_paths: list[Path] = []

    def fake_devicectl(cmd: list[str], **kwargs: object) -> MagicMock:
        if "--json-output" in cmd:
            p = Path(cmd[cmd.index("--json-output") + 1])
            seen_json_paths.append(p)
            p.write_text('{"result": {"apps": [], "devices": []}}', encoding="utf-8")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_devicectl):
        _devicectl_json(PHYS_SERIAL, "apps")
        IOSController()._discover_physical_devicectl()

    assert len(seen_json_paths) == 2
    for p in seen_json_paths:
        assert p.is_relative_to(root / "tmp")

    # Isolated TMPDIR & LINC_HOME must remain untouched
    _assert_tree_empty(isolated_env["home"])
    _assert_tree_empty(isolated_env["linc_home"])
    _assert_tree_empty(isolated_env["tmp"])


# ---------------------------------------------------------------------------
# 3. [contract] Engagement ON with bad/unset root fails closed everywhere
# ---------------------------------------------------------------------------


def _setup_bad_config(case: str, base: Path, monkeypatch: pytest.MonkeyPatch) -> Path | None:
    """Apply one invalid NERVE_ENGAGEMENT / NERVE_ARTIFACTS_ROOT scenario."""
    if case == "unset":
        monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-01")
        monkeypatch.delenv("NERVE_ARTIFACTS_ROOT", raising=False)
        return None
    if case == "empty_root":
        monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-01")
        monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", "")
        return None
    if case == "relative":
        monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-01")
        monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", "vault")
        return None
    if case == "missing":
        monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-01")
        monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(base / "does-not-exist"))
        return None
    if case == "regular_file":
        f = base / "not-a-dir"
        f.write_text("hi", encoding="utf-8")
        monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-01")
        monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(f))
        return None
    if case == "readonly_dir":
        d = base / "ro-vault"
        d.mkdir()
        d.chmod(0o555)
        monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-01")
        monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(d))
        return d
    if case == "symlink_leaf":
        target = base / "real-target"
        target.mkdir()
        link = base / "sym-vault"
        link.symlink_to(target)
        monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-01")
        monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(link))
        return target
    if case.startswith("symlink_leaf_trailing_"):
        # A trailing "/", "//" or "/." makes lstat() follow the leaf link, so the
        # leaf-only S_ISLNK test never fired (PR #286 review blocker).
        suffix = {"slash": "/", "double_slash": "//", "dot": "/."}[
            case.removeprefix("symlink_leaf_trailing_")
        ]
        target = base / "real-target"
        target.mkdir()
        link = base / "sym-vault"
        link.symlink_to(target)
        monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-01")
        monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", f"{link}{suffix}")
        return target
    if case == "symlink_ancestor":
        # Fail closed on a symlink in ANY component, not just the leaf:
        # /Volumes/vault-link/acme with vault-link -> an unencrypted dir escapes too.
        real_parent = base / "real-parent"
        (real_parent / "vault").mkdir(parents=True)
        link_parent = base / "link-parent"
        link_parent.symlink_to(real_parent)
        monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-01")
        monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(link_parent / "vault"))
        return real_parent / "vault"
    if case == "invalid_eng_id":
        valid = base / "valid-vault"
        valid.mkdir()
        monkeypatch.setenv("NERVE_ENGAGEMENT", "bad id!")
        monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(valid))
        return valid
    if case == "invalid_eng_id_unset_root":
        monkeypatch.setenv("NERVE_ENGAGEMENT", "eng/slash")
        monkeypatch.delenv("NERVE_ARTIFACTS_ROOT", raising=False)
        return None
    if case == "root_only_missing":
        monkeypatch.delenv("NERVE_ENGAGEMENT", raising=False)
        monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(base / "missing-only"))
        return None
    raise AssertionError(f"unknown case: {case}")


@pytest.mark.parametrize(
    "case",
    [
        "unset",
        "empty_root",
        "relative",
        "missing",
        "regular_file",
        "readonly_dir",
        "symlink_leaf",
        "symlink_leaf_trailing_slash",
        "symlink_leaf_trailing_double_slash",
        "symlink_leaf_trailing_dot",
        "symlink_ancestor",
        "invalid_eng_id",
        "invalid_eng_id_unset_root",
        "root_only_missing",
    ],
)
def test_contract_engagement_misconfigured_refuses_all_writers(
    case: str,
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] Bad engagement/root config fails closed with engagement_misconfigured."""
    extra_dir = _setup_bad_config(case, isolated_env["base"], monkeypatch)
    sample_mp4 = _write_mp4(isolated_env["base"] / "fixture.mp4")

    fake_android = MagicMock()
    fake_android.screenshot.return_value = _tiny_png_bytes()
    fake_android.logcat.return_value = "\n".join(f"line {i}" for i in range(60))
    fake_android.elements.return_value = [{"text": "OK", "bounds": [0, 0, 10, 10]}]
    monkeypatch.setattr(_core, "_android", fake_android)
    monkeypatch.setattr(_core, "_ios", MagicMock())
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: (dev, "android"))
    monkeypatch.setattr(_core, "_check_guard", lambda dev: None)

    try:
        results = [
            _nerve_screenshot_sync("DEV1"),
            _nerve_state_sync("DEV1"),
            _nerve_diff_sync("DEV1"),
            _nerve_logcat_sync("DEV1", lines=60),
            _nerve_inspect_sync("DEV1", "com.example.app"),
            _nerve_record_start_sync("DEV1"),
            _nerve_record_stop_sync("DEV1"),
            _nerve_record_fixed_sync("DEV1", duration_seconds=1),
            _nerve_extract_frames_sync(str(sample_mp4), "0.1"),
            _nerve_actionlog_start_sync("DEV1", "al.jsonl"),
            _nerve_replay_sync("DEV1", "al.jsonl"),
            _nerve_navigate_screenshot_sync("DEV1", "precisionlevel", "home"),
            _nerve_usage_stats_sync(),
        ]

        for res in results:
            assert res.get("error_category") == "engagement_misconfigured", (
                f"Expected engagement_misconfigured for case={case}, got {res}"
            )

        # log_mcp_call must refuse without touching LINC_HOME or crashing tool wrappers
        observability.log_mcp_call("linc-nerve", "nerve_screenshot", {}, 1.0, False, "err")

        _assert_tree_empty(isolated_env["home"])
        _assert_tree_empty(isolated_env["linc_home"])
        _assert_tree_empty(isolated_env["tmp"])
        if extra_dir is not None and extra_dir.exists():
            assert list(extra_dir.iterdir()) == []
    finally:
        ro = isolated_env["base"] / "ro-vault"
        if ro.exists():
            ro.chmod(0o700)


# ---------------------------------------------------------------------------
# 4. [contract] Hostile root paths & out-of-root symlinks / traversals
# ---------------------------------------------------------------------------


def test_contract_hostile_root_and_escape_paths_refused(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] Reject .. / trailing newline in root and escape paths in actionlog/frames."""
    root = isolated_env["root"]
    outside = isolated_env["base"] / "outside"
    outside.mkdir()
    sample_mp4 = _write_mp4(root / "recordings" / "clip.mp4")

    # Root with '..' segments rejected
    dotdot_root = str(isolated_env["base"] / "vault" / ".." / "vault")
    monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-hostile")
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", dotdot_root)
    assert _nerve_screenshot_sync("DEV1").get("error_category") == "engagement_misconfigured"

    # Root with trailing newline rejected
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", f"{root}\n")
    assert _nerve_screenshot_sync("DEV1").get("error_category") == "engagement_misconfigured"

    # Valid root + engagement ON: escape attempts refused
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(root))

    # Action log: .. escape and planted symlink inside <root>/actionlogs
    actionlogs = root / "actionlogs"
    actionlogs.mkdir(mode=0o700, parents=True, exist_ok=True)
    sym_dir = actionlogs / "escape_dir"
    sym_dir.symlink_to(outside)
    sym_file = actionlogs / "escape_file.jsonl"
    sym_file.symlink_to(outside / "escape_file.jsonl")

    res_dotdot = _nerve_actionlog_start_sync("DEV1", "../escape.jsonl")
    assert "error" in res_dotdot
    res_symdir = _nerve_actionlog_start_sync("DEV1", "escape_dir/sub.jsonl")
    assert "error" in res_symdir
    res_symfile = _nerve_actionlog_start_sync("DEV1", "escape_file.jsonl")
    assert "error" in res_symfile
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(outside))
    res_env = _nerve_actionlog_start_sync("DEV1", str(outside / "env.jsonl"))
    assert "error" in res_env
    monkeypatch.delenv("NERVE_ACTIONLOG_ROOTS", raising=False)

    # frames out_dir: .. escape, planted symlink inside root, and outside TMPDIR dir refused
    rec_sym = root / "recordings" / "sym_out"
    rec_sym.symlink_to(outside)

    bad_out_dotdot = _nerve_extract_frames_sync(
        str(sample_mp4), "0.1", out_dir=f"{root}/recordings/../../outside"
    )
    assert "error" in bad_out_dotdot

    bad_out_sym = _nerve_extract_frames_sync(
        str(sample_mp4), "0.1", out_dir=str(rec_sym / "frames")
    )
    assert "error" in bad_out_sym

    bad_out_tmp = _nerve_extract_frames_sync(
        str(sample_mp4), "0.1", out_dir=str(isolated_env["tmp"] / "frames")
    )
    assert "error" in bad_out_tmp

    _assert_tree_empty(outside)
    _assert_tree_empty(isolated_env["tmp"])


# ---------------------------------------------------------------------------
# 5. [contract] LINC_HOME pin with no root & no engagement
# ---------------------------------------------------------------------------


def test_contract_linc_home_pin_and_backward_compat_defaults(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] No root/engagement: Android honors LINC_HOME, other defaults pinned."""
    lh = isolated_env["linc_home"]

    # Android guest & emulator recordings honor LINC_HOME instead of ~/.linc
    ctrl = AndroidController()
    ctrl._recording_processes = {}
    ctrl.ensure_ready = MagicMock(return_value={"ready": True, "reason": "ready"})

    def fake_pull(cmd: list[str], **kwargs: object) -> MagicMock:
        if "pull" in cmd:
            Path(cmd[cmd.index("pull") + 2]).write_bytes(mp4_bytes())
        return MagicMock(returncode=0, stderr="")

    proc_mock = MagicMock(communicate=MagicMock(return_value=("", "")))
    with (
        patch("subprocess.Popen", return_value=proc_mock),
        patch("subprocess.run", side_effect=fake_pull),
        patch("time.sleep"),
    ):
        ctrl.start_recording("ANDROID1")
        guest_path = Path(ctrl.stop_recording("ANDROID1"))

    with (
        patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")),
        patch("time.sleep"),
    ):
        ctrl._start_emulator_recording("emulator-5554")
        emu_path = Path(ctrl._recording_processes["emulator-5554"]["host_path"])

    expected_recordings = lh / "nerve" / "recordings"
    assert guest_path.parent == expected_recordings
    assert emu_path.parent == expected_recordings
    _assert_tree_empty(isolated_env["home"])

    # Pin every other default path
    assert observability._default_log_path() == lh / "mcp-calls.jsonl"
    assert usage_stats._default_log_path() == lh / "mcp-calls.jsonl"
    assert action_log.actionlog_roots({}) == [lh / "nerve" / "actionlogs"]
    assert required_linc_dirs() == (str(lh), str(expected_recordings))
    monkeypatch.setenv(mcp_server._THREAD_DUMP_ENV, "1")
    with mcp_server._thread_dump_file() as f:
        f.write("dump\n")
    dump_path = lh / "nerve" / "nerve_threads.txt"
    assert dump_path.exists()
    assert stat.S_IMODE(dump_path.stat().st_mode) == 0o600
    out_dir, lock = output_retention.create_locked_output_dir()
    try:
        assert Path(out_dir).resolve().parent == Path(isolated_env["tmp"]).resolve()
    finally:
        if lock is not None:
            lock.close()
    video = isolated_env["base"] / "vid" / "a.mp4"
    assert frames.allowed_out_roots(video) == [
        video.parent.resolve(),
        lh.resolve(),
        isolated_env["tmp"].resolve(),
    ]


# ---------------------------------------------------------------------------
# 6. [test] Zero ~/.linc/nerve/recordings literals + 0700 dirs / 0600 files
# ---------------------------------------------------------------------------


def test_no_hardcoded_recordings_path_and_strict_modes(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[test] No ~/.linc/nerve/recordings in src; created dirs are 0700 and files 0600."""
    if (REPO_ROOT / "src").is_dir() and (REPO_ROOT / ".git").exists():
        grep_res = subprocess.run(
            ["git", "grep", "-n", "~/.linc/nerve/recordings", "--", "src"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert grep_res.returncode == 1 and grep_res.stdout == "", (
            f"Hardcoded ~/.linc/nerve/recordings remains:\n{grep_res.stdout}"
        )
    else:
        import linc_nerve

        pkg_root = (
            REPO_ROOT / "src" / "linc_nerve"
            if (REPO_ROOT / "src" / "linc_nerve").is_dir()
            else Path(linc_nerve.__file__).resolve().parent
        )
        hits = [
            f"{p.relative_to(pkg_root)}:{i}:{line}"
            for p in sorted(pkg_root.rglob("*.py"))
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if "~/.linc/nerve/recordings" in line
        ]
        assert not hits, "Hardcoded ~/.linc/nerve/recordings remains:\n" + "\n".join(hits)

    root = isolated_env["root"]
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(root))
    monkeypatch.setenv("NERVE_USAGE_LOG", "redacted")

    fake_android = MagicMock()
    fake_android.screenshot.return_value = _tiny_png_bytes()
    fake_android.logcat.return_value = "\n".join(f"line {i}" for i in range(260))
    monkeypatch.setattr(_core, "_android", fake_android)
    monkeypatch.setattr(_core, "_ios", MagicMock())
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: (dev, "android"))
    monkeypatch.setattr(_core, "_check_guard", lambda dev: None)

    from linc_nerve.core import engagement

    shot_path = Path(_nerve_screenshot_sync("DEV1")["screenshot"])
    logcat_path = Path(_nerve_logcat_sync("DEV1", lines=260)["logcat"])
    al_path = Path(_nerve_actionlog_start_sync("DEV1", "mode.jsonl")["path"])
    _nerve_actionlog_stop_sync("DEV1")
    observability.log_mcp_call("linc-nerve", "nerve_screenshot", {}, 1.0, True)
    call_log = root / "mcp-calls.jsonl"
    rec_dir = engagement.recordings_dir()
    tmp_dir = Path(engagement.scratch_dir() or "")

    clip = _write_mp4(rec_dir / "clip.mp4")
    frames_res = _nerve_extract_frames_sync(str(clip), "0.1")
    frame_path = Path(frames_res["frames"][0]["path"])

    for d in (rec_dir, root / "output", root / "actionlogs", tmp_dir, frame_path.parent):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, f"Expected 0700 for {d}"

    for f in (shot_path, logcat_path, al_path, call_log, frame_path):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f"Expected 0600 for {f}"


# ---------------------------------------------------------------------------
# 7. [test] nerve cleanup and stale-output sweep are scoped to <root>/output
# ---------------------------------------------------------------------------


def test_cleanup_and_sweep_confined_to_artifacts_root(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[test] Sweep and nerve cleanup only remove dirs under <root>/output, never TMPDIR."""
    root = isolated_env["root"]
    sys_tmp = isolated_env["tmp"]
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(root))

    stale_outside = sys_tmp / "nerve-output-outside"
    stale_outside.mkdir()
    (stale_outside / "keep.txt").write_text("keep", encoding="utf-8")
    old = 1_000_000.0
    os.utime(stale_outside / "keep.txt", (old, old))
    os.utime(stale_outside, (old, old))

    out_parent = root / "output"
    out_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stale_inside = out_parent / "nerve-output-inside"
    stale_inside.mkdir()
    (stale_inside / "drop.txt").write_text("drop", encoding="utf-8")
    os.utime(stale_inside / "drop.txt", (old, old))
    os.utime(stale_inside, (old, old))

    # Stale sweep during _output_dir() creation
    monkeypatch.setenv("NERVE_OUTPUT_SWEEP", "1")
    monkeypatch.setenv("NERVE_OUTPUT_MAX_AGE_S", "60")
    cur_dir = Path(_core._output_dir())
    assert cur_dir.is_relative_to(out_parent)
    assert not stale_inside.exists()
    assert stale_outside.exists()

    # Recreate stale_inside and run `nerve cleanup --all` via CLI
    stale_inside2 = out_parent / "nerve-output-inside-2"
    stale_inside2.mkdir()
    (stale_inside2 / "drop2.txt").write_text("drop2", encoding="utf-8")

    runner = CliRunner()
    import signal

    prev_sigint = signal.getsignal(signal.SIGINT)
    try:
        cli_res = runner.invoke(main, ["cleanup", "--all"])
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
    assert cli_res.exit_code == 0, cli_res.output
    payload = json.loads(cli_res.output)
    assert any("nerve-output-inside-2" in p for p in payload["removed"])
    assert not stale_inside2.exists()
    assert stale_outside.exists()


def test_async_wrappers_http_and_ios_scratch_under_engagement(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Async wrappers, HTTP router, .agent.png, and iOS scratch paths honor rules."""
    import asyncio
    import io
    import signal

    from linc_nerve.core import engagement
    from linc_nerve.daemon.http_router import route_request
    from linc_nerve.mcp import sse_server

    root = isolated_env["root"]

    # engagement_id boundary checks
    assert engagement.is_engagement_on({"NERVE_ENGAGEMENT": ""}) is False
    assert engagement.engagement_id({"NERVE_ENGAGEMENT": "eng-2026.1_a"}) == "eng-2026.1_a"
    with pytest.raises(engagement.EngagementMisconfiguredError):
        engagement.engagement_id({"NERVE_ENGAGEMENT": "x" * 65})

    # Async wrapper refusal without writing mcp-calls.jsonl or devices.db
    monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-1")
    monkeypatch.delenv("NERVE_ARTIFACTS_ROOT", raising=False)
    monkeypatch.setattr(_core, "_device_mgr", None)

    tool = _core.server._tool_manager.get_tool("nerve_screenshot")
    assert tool is not None
    prev_sigint = signal.getsignal(signal.SIGINT)
    try:
        res_async = asyncio.run(tool.fn(device="DEV1"))
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
    assert res_async.get("error_category") == "engagement_misconfigured"

    # HTTP route_request refusal
    registry = sse_server._get_tool_registry()
    http_res = route_request("POST", "/tools/nerve_screenshot", {"device": "DEV1"}, registry)
    assert http_res["body"]["result"]["error_category"] == "engagement_misconfigured"
    _assert_tree_empty(isolated_env["home"])
    _assert_tree_empty(isolated_env["linc_home"])
    _assert_tree_empty(isolated_env["tmp"])

    # Valid root: oversized screenshot -> .agent.png written as 0600 inside <root>/output
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(root))
    big_buf = io.BytesIO()
    Image.new("RGB", (2400, 1200), (30, 60, 90)).save(big_buf, format="PNG")
    big_png = big_buf.getvalue()

    fake_android = MagicMock()
    fake_android.screenshot.return_value = big_png
    monkeypatch.setattr(_core, "_android", fake_android)
    monkeypatch.setattr(_core, "_ios", MagicMock())
    monkeypatch.setattr(_core, "_device_mgr", MagicMock())
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: (dev, "android"))
    monkeypatch.setattr(_core, "_check_guard", lambda dev: None)

    prev_sigint = signal.getsignal(signal.SIGINT)
    try:
        blocks = asyncio.run(tool.fn(device="DEV1"))
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
    agent_path = Path(blocks[0]["screenshot"])
    assert agent_path.name.endswith(".agent.png")
    assert agent_path.is_relative_to(root / "output")
    assert stat.S_IMODE(agent_path.stat().st_mode) == 0o600
    assert (root / "mcp-calls.jsonl").exists()

    # iOS simctl screenshot & physical app listing use <root>/tmp
    ios_ctrl = IOSController()
    seen_tmp: list[Path] = []

    def fake_run_binary(*args: object) -> MagicMock:
        out_p = Path(str(args[-1]))
        seen_tmp.append(out_p)
        out_p.write_bytes(big_png)
        return MagicMock(returncode=0)

    def fake_devicectl(*args: object, **kwargs: object) -> MagicMock:
        args_list = list(args)
        out_p = Path(str(args_list[args_list.index("--json-output") + 1]))
        seen_tmp.append(out_p)
        out_p.write_text(
            '{"result": {"apps": [{"bundleIdentifier": "a.b", "executableName": "Ab"}]}}',
            encoding="utf-8",
        )
        return MagicMock(returncode=0)

    with (
        patch.object(ios_ctrl, "_run_binary", side_effect=fake_run_binary),
        patch.object(ios_ctrl, "_run_devicectl", side_effect=fake_devicectl),
    ):
        ios_ctrl._screenshot_simctl(SIM_UUID)
        assert ios_ctrl._list_apps_physical(PHYS_SERIAL) == ["a.b"]
        assert ios_ctrl._resolve_bundle_id_physical(PHYS_SERIAL, "a.b") == "Ab"

    assert len(seen_tmp) == 3
    for p in seen_tmp:
        assert p.is_relative_to(root / "tmp")
    _assert_tree_empty(isolated_env["tmp"])
