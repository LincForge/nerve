"""Dynamic artifacts-root confinement & completeness contract guard.

Bead ``linc-nerve-yucn`` (epic ``linc-nerve-3m2o``): sets ``NERVE_ENGAGEMENT=test-1``,
``NERVE_ARTIFACTS_ROOT=<tmp>/root``, and isolates ``HOME``, ``LINC_HOME``, ``TMPDIR`` and the
cwd into separate empty directories (``tempfile.tempdir`` is reset so ``TMPDIR`` really
applies; the fixture asserts it). Drives every host-writing surface through public
tool-layer entry points with ``adb`` / ``xcrun`` / ``devicectl`` faked, then:

- snapshots all five trees: every created host file must live under ``<tmp>/root``
  except the explicitly allowlisted ``devices.db``;
- checks every write target seen by a hook on ``open`` / ``io.open`` / ``os.open`` /
  ``os.mkdir`` while the writers run, so a write to any other absolute path is caught
  too (sqlite opens files in C and is not seen; ``devices.db`` is allowlisted anyway).

Also enforces:
- Registry completeness against :func:`discover_write_modules` from the static guard.
- Hostile-serial containment (serials containing ``/``, ``..``, and ``:``).
"""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import os
import signal
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from PIL import Image

from linc_nerve.cli import main as cli_main
from linc_nerve.core import action_log, observability
from linc_nerve.core import android as _android_mod
from linc_nerve.core import ios as _ios_mod  # noqa: F401
from linc_nerve.core import scrcpy as _scrcpy_mod  # noqa: F401
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.ios import IOSController
from linc_nerve.core.state_engine import StateSessionManager
from linc_nerve.mcp import _core
from linc_nerve.mcp import server as mcp_server
from linc_nerve.mcp.actionlog_tools import (
    _nerve_actionlog_start_sync,
    _nerve_actionlog_stop_sync,
)
from linc_nerve.mcp.app_tools import _nerve_apps_sync
from linc_nerve.mcp.diagnostic_tools import _nerve_inspect_sync, _nerve_logcat_sync
from linc_nerve.mcp.discovery_tools import _nerve_discover_sync
from linc_nerve.mcp.navigation_tools import _nerve_navigate_screenshot_sync
from linc_nerve.mcp.recording_tools import (
    _nerve_extract_frames_sync,
    _nerve_record_start_sync,
    _nerve_record_stop_sync,
)
from linc_nerve.mcp.screenshot_tools import (
    _nerve_screenshot_sync,
    _nerve_stream_screenshot_sync,
)
from linc_nerve.mcp.ui_tools import _nerve_diff_sync, _nerve_elements_sync, _nerve_state_sync
from tests._mp4_fixture import write_mp4

GUEST_SERIAL = "ANDROIDGUEST01"
EMU_SERIAL = "emulator-5554"
SIM_UUID = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"
PHYS_UDID = "00008110-0000000000000001"

ALLOWLISTED_LINC_HOME_FILES = frozenset(
    {
        ".local/share/linc-nerve/devices.db",
        ".local/share/linc-nerve/devices.db-wal",
        ".local/share/linc-nerve/devices.db-shm",
    }
)


def _tiny_png_bytes(
    size: tuple[int, int] = (32, 32),
    color: tuple[int, int, int] = (10, 20, 30),
) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _snapshot_tree(root: Path) -> set[str]:
    """Return relative POSIX file paths currently existing under *root*."""
    if not root.exists():
        return set()
    return {
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()
    }


def _snapshot_all(trees: dict[str, Path]) -> dict[str, set[str]]:
    return {name: _snapshot_tree(path) for name, path in trees.items()}


def _is_within_dir(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


_OS_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


class _WriteRecorder:
    """Record the target of every successful host write while installed.

    Hooks ``builtins.open`` / ``io.open`` (which ``Path.open``/``write_*``, PIL and
    shutil use), ``os.open`` (``tempfile.mkstemp``, ``engagement.write_private_bytes``)
    and ``os.mkdir`` (``Path.mkdir``, ``os.makedirs``, ``tempfile.mkdtemp``). Paths are
    resolved against the cwd at write time, so writes anywhere on disk are visible,
    not just inside the snapshotted trees. sqlite opens files in C and is not seen.
    """

    def __init__(self) -> None:
        self.targets: list[Path] = []

    def _add(self, target: object) -> None:
        if isinstance(target, (str, bytes, os.PathLike)):
            self.targets.append(Path(os.fsdecode(target)).resolve(strict=False))

    def install(self, mp: pytest.MonkeyPatch) -> None:
        real_open, real_os_open, real_mkdir = builtins.open, os.open, os.mkdir

        def _open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            handle = real_open(file, mode, *args, **kwargs)
            if any(ch in mode for ch in "wax+"):
                self._add(file)
            return handle

        def _os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            fd = real_os_open(path, flags, *args, **kwargs)
            if flags & _OS_WRITE_FLAGS:
                self._add(path)
            return fd

        def _mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
            real_mkdir(path, *args, **kwargs)
            self._add(path)

        mp.setattr(builtins, "open", _open)
        mp.setattr(io, "open", _open)
        mp.setattr(os, "open", _os_open)
        mp.setattr(os, "mkdir", _mkdir)

    def outside(self, root: Path) -> list[Path]:
        """Return recorded write targets that are not inside *root*."""
        return [p for p in self.targets if not _is_within_dir(p, root)]


class _FakeProcess:
    """Minimal Popen double for screenrecord / simctl recordVideo."""

    def __init__(self, out_file: Path | None = None, payload: bytes = b"") -> None:
        self.returncode: int | None = None
        self.stderr = io.BytesIO(b"")
        self.stdout = io.BytesIO(b"")
        self._out_file = out_file
        self._payload = payload

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self._finalize()

    def terminate(self) -> None:
        self._finalize()

    def kill(self) -> None:
        self._finalize()

    def wait(self, timeout: float | None = None) -> int:
        self._finalize()
        return 0

    def communicate(self, input: Any = None, timeout: float | None = None) -> tuple[bytes, bytes]:
        self._finalize()
        return (b"", b"")

    def _finalize(self) -> None:
        if self._out_file is not None and not self._out_file.exists():
            self._out_file.parent.mkdir(parents=True, exist_ok=True)
            self._out_file.write_bytes(self._payload)
        self.returncode = 0


class _DynamicHarness:
    """In-process faked ADB/xcrun/devicectl harness wired through _core singletons."""

    def __init__(
        self,
        trees: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        mp4_payload: bytes,
    ) -> None:
        self.trees = trees
        self.root = trees["root"]
        self.monkeypatch = monkeypatch
        self.mp4_payload = mp4_payload
        self.small_png = _tiny_png_bytes((32, 32), (10, 20, 30))
        self.alt_png = _tiny_png_bytes((32, 32), (220, 40, 80))
        self.big_png = _tiny_png_bytes((2400, 1200), (30, 60, 90))
        self.use_big_png = False
        self.observed_scratch_paths: list[Path] = []
        self._emu_host_paths: dict[str, Path] = {}

        self._reset_singletons()
        self.android = AndroidController()
        self.ios = IOSController()
        self._wire_controllers()

    def _reset_singletons(self) -> None:
        mp = self.monkeypatch
        mp.setattr(_core, "_OUTPUT_DIR", None)
        if getattr(_core, "_OUTPUT_LOCK", None) is not None:
            _core._OUTPUT_LOCK.close()
        mp.setattr(_core, "_OUTPUT_LOCK", None)
        mp.setattr(_core, "_cached_devices", [])
        mp.setattr(_core, "_cache_timestamp", _core._INVALIDATED)
        mp.setattr(_core, "_state_mgr", StateSessionManager())
        action_log.REGISTRY.reset()

    def _wire_controllers(self) -> None:
        mp = self.monkeypatch
        # Ensure DeviceManager creates its allowlisted devices.db inside isolated LINC_HOME.
        self.device_mgr = DeviceManager()
        mp.setattr(_core, "_android", self.android)
        mp.setattr(_core, "_ios", self.ios)
        mp.setattr(_core, "_device_mgr", self.device_mgr)
        mp.setattr(_core, "_ensure_initialized", lambda: (self.android, self.ios))
        mp.setattr(_core, "_metrics", MagicMock())
        mp.setattr(_core, "_check_guard", lambda dev: None)
        mp.setattr(_android_mod.time, "sleep", lambda _s: None)

        # Avoid touching any host ADB daemon on TCP 5037.
        mp.setattr(self.android, "_get_client", lambda: None)
        mp.setattr(self.android, "_scrcpy_available", False)
        mp.setattr(self.android, "ensure_ready", lambda serial: {"ready": True, "reason": "ok"})

        def _fake_resolve(device: str) -> tuple[str, str]:
            if device in (SIM_UUID, PHYS_UDID) or device.startswith("ios/"):
                return device, "ios"
            return device, "android"

        mp.setattr(_core, "_resolve_device", _fake_resolve)

        # Register known simulator/physical UDIDs cleanly on IOSController.
        self.ios._simulator_serials.add(SIM_UUID)
        self.ios._physical_serials.add(PHYS_UDID)

        # Android in-memory behaviors that would otherwise invoke ADB TCP sockets.
        mp.setattr(
            self.android,
            "screenshot",
            lambda *a, **k: self.big_png if self.use_big_png else self.small_png,
        )
        mp.setattr(
            self.android,
            "elements",
            lambda *a, **k: [
                {
                    "text": "Submit",
                    "resource_id": "com.example:id/submit",
                    "bounds": [10, 20, 110, 60],
                    "clickable": True,
                }
            ],
        )
        mp.setattr(
            self.android,
            "get_foreground",
            lambda *a, **k: {"package": "com.example.App", "activity": ".Main"},
        )
        mp.setattr(self.android, "get_orientation", lambda *a, **k: "portrait")
        mp.setattr(self.android, "launch", lambda *a, **k: None)
        mp.setattr(self.android, "discover", self._fake_android_discover)
        mp.setattr(
            self.android,
            "logcat",
            lambda *a, lines=100, **kw: "\n".join(f"log line {i}" for i in range(lines)),
        )
        mp.setattr(self.android, "_shell", self._fake_android_run_raw)

        # Fake Scrcpy stream frame for _nerve_stream_screenshot_sync.
        fake_scrcpy = MagicMock()
        fake_scrcpy.screenshot.return_value = self.small_png
        self.android._scrcpy = fake_scrcpy
        mp.setattr(self.android, "_get_scrcpy", lambda: fake_scrcpy)

        # Fake subprocess.run / subprocess.Popen globally so every controller uses our doubles.
        mp.setattr(subprocess, "run", self._fake_subprocess_run)
        mp.setattr(subprocess, "Popen", self._fake_popen)
        mp.setattr(_android_mod.subprocess, "run", self._fake_subprocess_run)
        mp.setattr(_android_mod.subprocess, "Popen", self._fake_popen)

    def register_hostile_ios_sim(self, serial: str) -> None:
        """Allow *serial* to route cleanly to IOSController simulator recording."""
        self.ios._simulator_serials.add(serial)

    def _fake_android_discover(self) -> list[NerveDevice]:
        return [
            NerveDevice(
                serial=GUEST_SERIAL,
                platform=Platform.ANDROID,
                device_type=DeviceType.PHONE,
                model="Fake Guest",
                os_version="14",
                state="device",
            )
        ]

    def _fake_android_run_raw(self, *args: str, timeout: int = 10) -> MagicMock:
        return MagicMock(returncode=0, stdout="", stderr="")

    def _record_temp_path(self, raw_path: str) -> Path:
        p = Path(raw_path)
        self.observed_scratch_paths.append(p)
        return p

    def _fake_subprocess_run(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        cmd_str = [str(c) for c in cmd]
        if len(cmd_str) >= 3 and cmd_str[0] == "xcrun":
            return self._handle_xcrun(cmd_str)
        if cmd_str[0] == "log":
            return subprocess.CompletedProcess(
                cmd_str, 0, stdout="2026-01-01 ExampleApp syslog entry\n", stderr=""
            )
        if "emu" in cmd_str and "screenrecord" in cmd_str:
            serial = cmd_str[cmd_str.index("-s") + 1] if "-s" in cmd_str else EMU_SERIAL
            if "start" in cmd_str:
                host_path = Path(cmd_str[cmd_str.index("start") + 1])
                host_path.parent.mkdir(parents=True, exist_ok=True)
                host_path.write_bytes(self.mp4_payload)
                self._emu_host_paths[serial] = host_path
            elif "stop" in cmd_str:
                p = self._emu_host_paths.get(serial)
                if p is not None and not p.exists():
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(self.mp4_payload)
            return subprocess.CompletedProcess(cmd_str, 0, stdout="OK\n", stderr="")
        if "pull" in cmd_str:
            local_path = Path(cmd_str[cmd_str.index("pull") + 2])
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(self.mp4_payload)
            return subprocess.CompletedProcess(cmd_str, 0, stdout="1 file pulled\n", stderr="")
        if "get-state" in cmd_str:
            return subprocess.CompletedProcess(cmd_str, 0, stdout="device\n", stderr="")
        if "rm" in cmd_str or "pgrep" in cmd_str or "pkill" in cmd_str or "kill" in cmd_str:
            return subprocess.CompletedProcess(cmd_str, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd_str, 0, stdout="", stderr="")

    def _handle_xcrun(self, cmd: list[str]) -> subprocess.CompletedProcess:
        sub = cmd[1]
        if sub == "simctl":
            if cmd[2:4] == ["list", "devices"]:
                sims = [
                    {"udid": s, "name": "Fake iPhone", "state": "Booted", "isAvailable": True}
                    for s in sorted(self.ios._simulator_serials)
                ]
                payload = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-18-0": sims}}
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
            if cmd[2] == "io" and "screenshot" in cmd:
                png_path = self._record_temp_path(cmd[-1])
                png_path.parent.mkdir(parents=True, exist_ok=True)
                png_path.write_bytes(self.small_png)
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        if sub == "devicectl":
            if "--json-output" in cmd:
                json_path = self._record_temp_path(cmd[cmd.index("--json-output") + 1])
                json_path.parent.mkdir(parents=True, exist_ok=True)
                if "list" in cmd:
                    data: dict[str, Any] = {
                        "result": {
                            "devices": [
                                {
                                    "hardwareProperties": {
                                        "udid": PHYS_UDID,
                                        "deviceType": "iPhone",
                                        "marketingName": "Fake iPhone",
                                    },
                                    "deviceProperties": {
                                        "name": "Fake iPhone",
                                        "osVersionNumber": "18.0",
                                    },
                                    "connectionProperties": {"tunnelState": "connected"},
                                }
                            ]
                        }
                    }
                elif "processes" in cmd:
                    data = {
                        "result": {
                            "runningProcesses": [
                                {"executable": "file:///ExampleApp", "processIdentifier": 1234}
                            ]
                        }
                    }
                else:
                    data = {
                        "result": {
                            "apps": [
                                {
                                    "bundleIdentifier": "com.example.App",
                                    "executableName": "ExampleApp",
                                    "name": "Example",
                                    "version": "1.0",
                                    "url": "file:///App.app",
                                }
                            ]
                        }
                    }
                json_path.write_text(json.dumps(data), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if sub == "xctrace":
            return subprocess.CompletedProcess(cmd, 0, stdout="== Devices ==\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def _fake_popen(self, cmd: list[str], **kwargs: Any) -> _FakeProcess:
        cmd_str = [str(c) for c in cmd]
        if "recordVideo" in cmd_str:
            out_path = Path(cmd_str[-1])
            return _FakeProcess(out_file=out_path, payload=self.mp4_payload)
        return _FakeProcess()


# ---------------------------------------------------------------------------
# Public tool-layer drivers & registry
# ---------------------------------------------------------------------------


def _drive_recording_android_guest(h: _DynamicHarness) -> None:
    res_start = _nerve_record_start_sync(GUEST_SERIAL)
    assert res_start.get("status") == "success", res_start
    res_stop = _nerve_record_stop_sync(GUEST_SERIAL)
    assert res_stop.get("status") == "success", res_stop


def _drive_recording_android_emulator(h: _DynamicHarness) -> None:
    res_start = _nerve_record_start_sync(EMU_SERIAL)
    assert res_start.get("status") == "success", res_start
    res_stop = _nerve_record_stop_sync(EMU_SERIAL)
    assert res_stop.get("status") == "success", res_stop


def _drive_recording_ios_sim(h: _DynamicHarness) -> None:
    res_start = _nerve_record_start_sync(SIM_UUID)
    assert res_start.get("status") == "success", res_start
    res_stop = _nerve_record_stop_sync(SIM_UUID)
    assert res_stop.get("status") == "success", res_stop


def _drive_screenshot_android(h: _DynamicHarness) -> None:
    res = _nerve_screenshot_sync(GUEST_SERIAL)
    assert "error" not in res, res
    res_stream = _nerve_stream_screenshot_sync(GUEST_SERIAL)
    assert "error" not in res_stream, res_stream


def _drive_screenshot_ios_sim(h: _DynamicHarness) -> None:
    res = _nerve_screenshot_sync(SIM_UUID)
    assert "error" not in res, res


def _drive_discover_ios_physical(h: _DynamicHarness) -> None:
    res = _nerve_discover_sync()
    assert "error" not in res, res


def _drive_ios_app_list(h: _DynamicHarness) -> None:
    res = _nerve_apps_sync(PHYS_UDID)
    assert res.get("count", 0) >= 1, res


def _drive_ios_logs_physical(h: _DynamicHarness) -> None:
    res = _nerve_logcat_sync(PHYS_UDID, tag="com.example.App", lines=10)
    assert "error" not in res, res


def _drive_ios_inspect_physical(h: _DynamicHarness) -> None:
    res = _nerve_inspect_sync(PHYS_UDID, "com.example.App")
    assert "error" not in res, res


def _drive_ui_dump(h: _DynamicHarness) -> None:
    s_res = _nerve_state_sync(GUEST_SERIAL)
    assert "error" not in s_res, s_res
    e_res = _nerve_elements_sync(GUEST_SERIAL)
    assert "error" not in e_res, e_res
    # Flip screenshot bytes so diff detects a change and writes a diff PNG.
    orig = h.small_png
    try:
        h.small_png = h.alt_png
        d_res = _nerve_diff_sync(GUEST_SERIAL)
        assert "error" not in d_res, d_res
    finally:
        h.small_png = orig


def _drive_navigate_screenshot(h: _DynamicHarness) -> None:
    res = _nerve_navigate_screenshot_sync(GUEST_SERIAL, "sniperpulse", "settings")
    assert "error" not in res, res


def _drive_logcat_spill(h: _DynamicHarness) -> None:
    res = _nerve_logcat_sync(GUEST_SERIAL, lines=300)
    assert "error" not in res, res
    assert "logcat" in res and Path(res["logcat"]).is_file(), res


def _drive_action_log(h: _DynamicHarness) -> None:
    started = _nerve_actionlog_start_sync(GUEST_SERIAL, "session.jsonl")
    assert started.get("success") is True, started
    _nerve_screenshot_sync(GUEST_SERIAL)
    stopped = _nerve_actionlog_stop_sync(GUEST_SERIAL)
    assert stopped.get("success") is True, stopped
    assert stopped.get("entries", 0) >= 1


def _drive_frame_extraction(h: _DynamicHarness) -> None:
    clip = h.root / "clip.mp4"
    clip.write_bytes(h.mp4_payload)
    res = _nerve_extract_frames_sync(str(clip), "0.0,0.2")
    assert "error" not in res, res
    assert res.get("frames"), res


def _drive_usage_log_and_budget(h: _DynamicHarness) -> None:
    tool = _core.server._tool_manager.get_tool("nerve_screenshot")
    assert tool is not None
    h.use_big_png = True
    prev_sigint = signal.getsignal(signal.SIGINT)
    try:
        blocks = asyncio.run(tool.fn(device=GUEST_SERIAL))
    finally:
        h.use_big_png = False
        signal.signal(signal.SIGINT, prev_sigint)
    assert blocks and isinstance(blocks[0], dict), blocks


def _drive_thread_dump(h: _DynamicHarness) -> None:
    with mcp_server._thread_dump_file() as fh:
        assert fh is not None


def _drive_cli_screenshot_output(h: _DynamicHarness) -> None:
    out_file = h.root / "cli_shot.png"
    shot_file = h.root / "cli_src.png"
    shot_file.write_bytes(h.small_png)
    h.monkeypatch.setattr(
        cli_main,
        "_call",
        lambda tool, payload: {"screenshot": str(shot_file)},
    )
    prev_sigint = signal.getsignal(signal.SIGINT)
    try:  # the CLI installs its own SIGINT handler; keep later tests' process state intact
        res = CliRunner().invoke(
            cli_main.main,
            ["screenshot", GUEST_SERIAL, "-o", str(out_file)],
        )
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
    assert res.exit_code == 0, res.output
    assert out_file.is_file()


@dataclass(frozen=True)
class WriterDriverSpec:
    """One registry entry pairing source modules covered with its public driver."""

    modules: tuple[str, ...]
    driver: Callable[[_DynamicHarness], None]


DRIVEN_WRITERS: dict[str, WriterDriverSpec] = {
    "recording_android_guest": WriterDriverSpec(
        modules=("core/android_sensor.py",),
        driver=_drive_recording_android_guest,
    ),
    "recording_android_emulator": WriterDriverSpec(
        modules=("core/android_sensor.py",),
        driver=_drive_recording_android_emulator,
    ),
    "recording_ios_sim": WriterDriverSpec(
        modules=("core/ios.py",),
        driver=_drive_recording_ios_sim,
    ),
    "screenshot": WriterDriverSpec(
        modules=("mcp/screenshot_tools.py", "mcp/_core.py", "core/output_retention.py"),
        driver=_drive_screenshot_android,
    ),
    "screenshot_ios_sim": WriterDriverSpec(
        modules=("core/ios_interaction.py",),
        driver=_drive_screenshot_ios_sim,
    ),
    "discover_ios_physical": WriterDriverSpec(
        modules=("core/ios_discovery.py",),
        driver=_drive_discover_ios_physical,
    ),
    "ios_app_list": WriterDriverSpec(
        modules=("core/ios_apps_logs.py",),
        driver=_drive_ios_app_list,
    ),
    "ios_logs_physical": WriterDriverSpec(
        modules=("core/ios_logs.py",),
        driver=_drive_ios_logs_physical,
    ),
    "ios_inspect_physical": WriterDriverSpec(
        modules=("mcp/diagnostic_tools.py",),
        driver=_drive_ios_inspect_physical,
    ),
    "ui_dump": WriterDriverSpec(
        modules=("mcp/ui_tools.py",),
        driver=_drive_ui_dump,
    ),
    "navigate_screenshot": WriterDriverSpec(
        modules=("mcp/navigation_tools.py",),
        driver=_drive_navigate_screenshot,
    ),
    "logcat_spill": WriterDriverSpec(
        modules=("mcp/diagnostic_tools.py",),
        driver=_drive_logcat_spill,
    ),
    "action_log": WriterDriverSpec(
        modules=("core/action_log.py", "mcp/actionlog_tools.py"),
        driver=_drive_action_log,
    ),
    "frame_extraction": WriterDriverSpec(
        modules=("core/frames.py",),
        driver=_drive_frame_extraction,
    ),
    "usage_log_decorator": WriterDriverSpec(
        modules=("core/observability.py",),
        driver=_drive_usage_log_and_budget,
    ),
    "thread_dump": WriterDriverSpec(
        modules=("mcp/server.py",),
        driver=_drive_thread_dump,
    ),
    "cli_screenshot_output": WriterDriverSpec(
        modules=("cli/main.py",),
        driver=_drive_cli_screenshot_output,
    ),
}


def _unallowlisted_writes(recorder: _WriteRecorder, trees: dict[str, Path]) -> list[str]:
    """Recorded write targets outside the root, minus the allowlisted devices.db store."""
    allowed = {(trees["linc_home"] / rel).resolve() for rel in ALLOWLISTED_LINC_HOME_FILES}
    allowed |= {p.parent for p in allowed} | {p.parent.parent for p in allowed}
    return sorted(str(p) for p in recorder.outside(trees["root"]) if p not in allowed)


@pytest.fixture
def isolated_trees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point HOME, LINC_HOME, TMPDIR, NERVE_ARTIFACTS_ROOT and cwd at five empty directories.

    ``tests/conftest.py`` calls ``tempfile.gettempdir()`` at import, which caches
    ``tempfile.tempdir`` as the host temp; setting ``TMPDIR`` alone is then ignored.
    The cache is cleared here and the isolation is asserted, so the TMPDIR snapshot
    can actually observe a leak.
    """
    trees = {name: tmp_path / name for name in ("root", "home", "linc_home", "sys_tmp", "cwd")}
    for d in trees.values():
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(trees["home"]))
    monkeypatch.setenv("LINC_HOME", str(trees["linc_home"]))
    monkeypatch.setenv("TMPDIR", str(trees["sys_tmp"]))
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.chdir(trees["cwd"])
    if Path(tempfile.gettempdir()).resolve() != trees["sys_tmp"].resolve():
        raise AssertionError(
            f"TMPDIR isolation failed: tempfile.gettempdir()={tempfile.gettempdir()!r}"
        )
    if Path.home().resolve() != trees["home"].resolve():
        raise AssertionError(f"HOME isolation failed: Path.home()={Path.home()!r}")
    # Engagement mode refuses devices the fleet registry does not list as personal: false
    # (linc-nerve-qu9a), so register the synthetic test devices before the snapshot.
    (trees["linc_home"] / "device-fleet.yaml").write_text(
        "devices:\n"
        f"  - serial: {GUEST_SERIAL}\n    personal: false\n"
        f"  - serial: {EMU_SERIAL}\n    personal: false\n"
        "ios:\n  test:\n"
        f"    - serial: {SIM_UUID}\n      personal: false\n"
        f"    - serial: {PHYS_UDID}\n      personal: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NERVE_ENGAGEMENT", "test-1")
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(trees["root"]))
    monkeypatch.setenv("NERVE_USAGE_LOG", "redacted")
    monkeypatch.setenv("NERVE_THREAD_DUMP", "1")
    monkeypatch.delenv("NERVE_ACTIONLOG_ROOTS", raising=False)
    monkeypatch.delenv("NERVE_OUTPUT_SWEEP", raising=False)
    # Keep observability path checks aligned if an older module reads ~/.linc.
    _ = observability.usage_log_mode()
    try:
        yield {**trees, "base": tmp_path}
    finally:
        lock = getattr(_core, "_OUTPUT_LOCK", None)
        if lock is not None:
            lock.close()
            _core._OUTPUT_LOCK = None


@pytest.fixture
def sample_mp4(tmp_path: Path) -> bytes:
    """Create tiny valid H.264/MPEG4 MP4 bytes once via PyAV (no ffmpeg binary)."""
    mp4_path = tmp_path / "fixture.mp4"
    write_mp4(mp4_path, frames=5)
    data = mp4_path.read_bytes()
    mp4_path.unlink()
    return data


def test_no_writer_escapes_artifacts_root(
    isolated_trees: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    sample_mp4: bytes,
) -> None:
    """[contract] Every driven writer writes under <tmp>/root (except allowlisted devices.db)."""
    watched = {k: isolated_trees[k] for k in ("root", "home", "linc_home", "sys_tmp", "cwd")}
    before = _snapshot_all(watched)
    harness = _DynamicHarness(isolated_trees, monkeypatch, sample_mp4)
    recorder = _WriteRecorder()

    with monkeypatch.context() as hooks:
        recorder.install(hooks)
        for name, spec in DRIVEN_WRITERS.items():
            try:
                spec.driver(harness)
            except Exception as exc:  # pragma: no cover - diagnostic context on failure
                raise AssertionError(f"Driver {name!r} failed: {exc}") from exc

    after = _snapshot_all(watched)
    created = {name: after[name] - before[name] for name in watched}

    bad_scratch = [
        str(p)
        for p in harness.observed_scratch_paths
        if not _is_within_dir(p, isolated_trees["root"])
    ]
    unexpected_linc_home = created["linc_home"] - ALLOWLISTED_LINC_HOME_FILES

    failures: list[str] = []
    if created["home"]:
        failures.append(f"Files written under isolated HOME: {sorted(created['home'])}")
    if created["sys_tmp"]:
        failures.append(f"Files written under isolated TMPDIR: {sorted(created['sys_tmp'])}")
    if created["cwd"]:
        failures.append(f"Files written under isolated cwd: {sorted(created['cwd'])}")
    if unexpected_linc_home:
        failures.append(
            f"Unexpected files written under isolated LINC_HOME: {sorted(unexpected_linc_home)}"
        )
    if bad_scratch:
        failures.append(f"Transient scratch temp files escaped root: {bad_scratch}")
    if len([p for p in recorder.targets if _is_within_dir(p, isolated_trees["root"])]) < 10:
        failures.append(f"Write hook saw too few writes under root: {recorder.targets}")
    escaped_writes = _unallowlisted_writes(recorder, isolated_trees)
    if escaped_writes:
        failures.append(f"Writes outside root (open/os.open/mkdir hook): {escaped_writes}")
    if not any(p.startswith("recordings/") for p in created["root"]):
        failures.append(f"Expected recordings/ under root, got: {sorted(created['root'])}")
    if not any(p.startswith("output/") for p in created["root"]):
        failures.append(f"Expected output/ under root, got: {sorted(created['root'])}")
    if not any(p.startswith("actionlogs/") for p in created["root"]):
        failures.append(f"Expected actionlogs/ under root, got: {sorted(created['root'])}")
    if "mcp-calls.jsonl" not in created["root"]:
        failures.append(f"Expected mcp-calls.jsonl under root, got: {sorted(created['root'])}")
    assert not failures, "\n".join(failures)


def _drive_hostile_serials(root: Path, guest: str, emu: str, ios: str) -> None:
    """Screenshot plus all three recording writers, each with a hostile serial."""
    # 2. Hostile serial through _nerve_screenshot_sync.
    shot_res = _nerve_screenshot_sync(guest)
    assert "error" not in shot_res, shot_res
    assert _is_within_dir(Path(shot_res["screenshot"]), root)

    # 3. Hostile serial through all three recording writers.
    for serial in (guest, emu, ios):
        start_res = _nerve_record_start_sync(serial)
        assert start_res.get("status") == "success", (serial, start_res)
        stop_res = _nerve_record_stop_sync(serial)
        assert stop_res.get("status") == "success", (serial, stop_res)
        saved = Path(stop_res["file_path"])
        assert _is_within_dir(saved, root / "recordings"), (
            f"Recording for hostile serial {serial!r} escaped root/recordings: {saved}"
        )


def test_hostile_serial_cannot_escape_artifacts_root(
    isolated_trees: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    sample_mp4: bytes,
) -> None:
    """[contract] Hostile serials containing /, .., and : remain confined inside <tmp>/root."""
    outside_sentinel = isolated_trees["base"] / "outside"
    outside_sentinel.mkdir(parents=True, exist_ok=True)
    watched = {
        "root": isolated_trees["root"],
        "home": isolated_trees["home"],
        "linc_home": isolated_trees["linc_home"],
        "sys_tmp": isolated_trees["sys_tmp"],
        "cwd": isolated_trees["cwd"],
        "outside": outside_sentinel,
    }
    before = _snapshot_all(watched)
    harness = _DynamicHarness(isolated_trees, monkeypatch, sample_mp4)

    # 1. Pin existing _next_output_path safe_serial replacement for /, .., :.
    out_path = Path(_core._next_output_path("dev/../outside:5555", "screenshot"))
    assert _is_within_dir(out_path, isolated_trees["root"]), out_path
    assert ".." in out_path.name and "/" not in out_path.name and ":" not in out_path.name

    hostile_guest = "guest/../../outside/escape:5555"
    hostile_emu = "emulator-../../outside/escape:5555"
    hostile_ios = "ios/../../outside/escape:5555"
    harness.register_hostile_ios_sim(hostile_ios)
    recorder = _WriteRecorder()
    with monkeypatch.context() as hooks:
        recorder.install(hooks)
        _drive_hostile_serials(isolated_trees["root"], hostile_guest, hostile_emu, hostile_ios)

    escaped_writes = _unallowlisted_writes(recorder, isolated_trees)
    assert escaped_writes == [], f"Hostile serial wrote outside root: {escaped_writes}"
    after = _snapshot_all(watched)
    assert after["home"] == before["home"]
    assert after["sys_tmp"] == before["sys_tmp"]
    assert after["cwd"] == before["cwd"]
    assert after["outside"] == before["outside"], (
        f"Hostile serial wrote outside sentinel dir: {after['outside'] - before['outside']}"
    )
    unexpected_lh = (after["linc_home"] - before["linc_home"]) - ALLOWLISTED_LINC_HOME_FILES
    assert unexpected_lh == set(), f"Escaped to LINC_HOME: {sorted(unexpected_lh)}"


def _uncovered_write_modules(src_root: Path | None = None) -> set[str]:
    from tests.test_writer_roots_contract import (
        EXEMPT_WRITE_MODULES,
        SRC_ROOT,
        discover_write_modules,
    )

    root = SRC_ROOT if src_root is None else src_root
    all_writers = discover_write_modules(root)
    exempt = {entry.file for entry in EXEMPT_WRITE_MODULES}
    covered = {mod for spec in DRIVEN_WRITERS.values() for mod in spec.modules}
    return all_writers - exempt - covered


def test_completeness_every_write_module_covered_or_exempt() -> None:
    """[contract] Every AST-discovered host write module is either driven or explicitly exempt."""
    uncovered = _uncovered_write_modules()
    assert not uncovered, (
        "Host write module(s) missing from DRIVEN_WRITERS and EXEMPT_WRITE_MODULES: "
        f"{sorted(uncovered)}"
    )


def test_completeness_fails_for_unknown_new_writer_module(tmp_path: Path) -> None:
    """[contract] Adding a new host-writing module fails completeness until registered."""
    fake_src = tmp_path / "src"
    (fake_src / "mcp").mkdir(parents=True)
    (fake_src / "mcp" / "new_unregistered_writer.py").write_text(
        "from pathlib import Path\n"
        "def save_trace(p: Path) -> None:\n"
        "    p.write_bytes(b'trace')\n",
        encoding="utf-8",
    )
    uncovered = _uncovered_write_modules(fake_src)
    assert "mcp/new_unregistered_writer.py" in uncovered


def test_write_recorder_sees_writes_outside_watched_trees(
    isolated_trees: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] Writes to an arbitrary absolute path are observed, whatever the primitive."""
    elsewhere = isolated_trees["base"] / "elsewhere"
    recorder = _WriteRecorder()
    with monkeypatch.context() as m:
        recorder.install(m)
        (elsewhere / "made").mkdir(parents=True)
        (elsewhere / "a.txt").write_text("x", encoding="utf-8")
        with open(elsewhere / "b.bin", "wb") as fh:
            fh.write(b"x")
        os.close(os.open(elsewhere / "c.bin", os.O_WRONLY | os.O_CREAT, 0o600))
        fd, _ = tempfile.mkstemp(dir=elsewhere)
        os.close(fd)
        with open(elsewhere / "a.txt", encoding="utf-8") as fh:  # read-only: not a write
            fh.read()
    escaped = recorder.outside(isolated_trees["root"])
    names = {p.name for p in escaped}
    assert {"elsewhere", "made", "a.txt", "b.bin", "c.bin"} <= names, escaped
    assert len([p for p in escaped if p.name == "a.txt"]) == 1, escaped
    assert recorder.outside(isolated_trees["base"]) == []
