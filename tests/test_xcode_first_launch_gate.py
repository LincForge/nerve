"""Tests for gating iOS/daemon xcrun calls on xcodebuild first-launch (linc-nerve-a2un)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog.testing

from linc_nerve.cli.main import _poll_cycle
from linc_nerve.core import ios as ios_mod
from linc_nerve.core import ios_discovery, wda
from linc_nerve.core.device import DeviceError
from linc_nerve.core.ios import IOSController
from linc_nerve.core.ios_discovery import (
    _FIRST_LAUNCH_CACHE_TTL,
    _FIRST_LAUNCH_READY_MESSAGE,
    _FIRST_LAUNCH_TIMEOUT,
    _FIRST_LAUNCH_TRANSIENT_TTL,
    _FIRST_LAUNCH_WARNING,
    _NON_DARWIN_MESSAGE,
    _XCODEBUILD_FIRST_LAUNCH_CMD,
    XcodeNotReadyError,
    _probe_xcode_first_launch,
    _reset_xcode_first_launch_state,
    xcode_first_launch_ready,
)
from linc_nerve.daemon import wda_manager
from linc_nerve.daemon.wda_manager import DaemonWDAManager
from linc_nerve.mcp.diagnostic_tools import (
    _devicectl_json,
    _get_ios_simulator_pid,
    _inspect_ios,
)


@pytest.fixture(autouse=True)
def _reset_gate_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Ensure every test starts with a cold first-launch gate cache on darwin."""
    monkeypatch.setattr(wda.WDAManager, "_cleanup_orphans", staticmethod(lambda: None))
    monkeypatch.setattr(wda.WDAManager, "_sweep_dead_ports", lambda self: None)
    monkeypatch.setattr(sys, "platform", "darwin")
    _reset_xcode_first_launch_state()
    yield
    _reset_xcode_first_launch_state()


def _install_fake_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gate_returncode: int = 69,
    gate_exc: Exception | None = None,
    devicectl_fails: bool = False,
) -> tuple[list[list[str]], list[dict[str, Any]]]:
    """Install a fake subprocess.run that responds to xcodebuild and records all commands."""
    issued_cmds: list[list[str]] = []
    gate_kwargs: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        issued_cmds.append(list(cmd))
        if cmd == _XCODEBUILD_FIRST_LAUNCH_CMD:
            gate_kwargs.append(kwargs)
            if gate_exc is not None:
                raise gate_exc
            return subprocess.CompletedProcess(
                cmd, returncode=gate_returncode, stdout="", stderr=""
            )
        if cmd[:2] == ["xcrun", "devicectl"]:
            if devicectl_fails:
                raise subprocess.CalledProcessError(1, cmd)
            if "--json-output" in cmd:
                out_path = cmd[cmd.index("--json-output") + 1]
                with open(out_path, "w") as f:
                    json.dump({"result": {"devices": []}}, f)
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        if cmd[:2] == ["xcrun", "simctl"]:
            if len(cmd) >= 6 and cmd[2] == "io" and cmd[4] == "screenshot":
                with open(cmd[-1], "wb") as f:
                    f.write(b"\x89PNG\r\n\x1a\nfakedata")
                return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")  # type: ignore[return-value]
            payload = json.dumps(
                {
                    "devices": {
                        "com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
                            {
                                "name": "iPhone 16",
                                "udid": "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177",
                                "state": "Booted",
                                "isAvailable": True,
                            }
                        ]
                    }
                }
            )
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=payload, stderr="")
        if cmd[:2] == ["xcrun", "xctrace"]:
            xctrace_out = (
                "== Devices ==\n"
                "Test iPhone (18.0) (00008110-0000000000000001)\n"
                "== Simulators ==\n"
            )
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=xctrace_out, stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return issued_cmds, gate_kwargs


def test_constants_match_contract() -> None:
    """Pin the gate command, warning string, 5s timeout, and 5-minute TTL."""
    assert _XCODEBUILD_FIRST_LAUNCH_CMD == ["/usr/bin/xcodebuild", "-checkFirstLaunchStatus"]
    assert (
        _FIRST_LAUNCH_WARNING
        == "Xcode first launch pending: run `sudo xcodebuild -runFirstLaunch`"
    )
    assert _FIRST_LAUNCH_CACHE_TTL == 300.0
    assert _FIRST_LAUNCH_TIMEOUT == 5
    assert ios_discovery._first_launch_cache is None
    assert ios_discovery._first_launch_last_state is None


def test_contract_exit_69_daemon_discovery_pass_issues_zero_xcrun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] With a fake runner where checkFirstLaunchStatus exits 69, one
    daemon discovery pass issues zero commands starting with xcrun.
    """
    issued_cmds, gate_kwargs = _install_fake_runner(monkeypatch, gate_returncode=69)
    android = MagicMock()
    android.discover.return_value = []
    ios = IOSController()
    wda_mgr = DaemonWDAManager(sim_states=wda_manager._default_sim_states)

    diff = _poll_cycle(android, ios, wda_mgr, set())
    sim_states = wda_manager._default_sim_states()
    xctrace_devs = ios._discover_physical_xctrace()

    xcrun_cmds = [cmd for cmd in issued_cmds if cmd and cmd[0] == "xcrun"]
    assert xcrun_cmds == []
    assert diff.raw_devices["ios"] == []
    assert sim_states == {}
    assert xctrace_devs == []
    assert issued_cmds == [_XCODEBUILD_FIRST_LAUNCH_CMD]
    assert gate_kwargs == [
        {"capture_output": True, "text": True, "timeout": 5, "check": False}
    ]


def test_contract_exit_0_existing_xcrun_calls_run_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] Exit 0 -> the existing xcrun calls run unchanged."""
    issued_cmds, gate_kwargs = _install_fake_runner(
        monkeypatch, gate_returncode=0, devicectl_fails=True
    )
    android = MagicMock()
    android.discover.return_value = []
    ios = IOSController()
    wda_mgr = DaemonWDAManager(
        sim_states=wda_manager._default_sim_states,
        wda_products_available=lambda is_physical: False,
    )
    monkeypatch.setenv("WDA_PROJECT_DIR", "/fake/wda")

    with structlog.testing.capture_logs() as cap_logs:
        diff = _poll_cycle(android, ios, wda_mgr, set())

    xcrun_cmds = [cmd for cmd in issued_cmds if cmd and cmd[0] == "xcrun"]
    assert [cmd[:3] for cmd in xcrun_cmds] == [
        ["xcrun", "simctl", "list"],
        ["xcrun", "devicectl", "list"],
        ["xcrun", "xctrace", "list"],
        ["xcrun", "simctl", "list"],
    ]
    assert len(diff.raw_devices["ios"]) == 2
    assert len(gate_kwargs) == 1
    assert not any(e.get("event") == _FIRST_LAUNCH_WARNING for e in cap_logs)


def test_ten_consecutive_passes_with_status_69_log_warning_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] 10 consecutive passes with status 69 log the warning exactly once."""
    issued_cmds, _ = _install_fake_runner(monkeypatch, gate_returncode=69)
    android = MagicMock()
    android.discover.return_value = []
    ios = IOSController()
    wda_mgr = DaemonWDAManager(sim_states=wda_manager._default_sim_states)

    clock = [1000.0]
    monkeypatch.setattr(ios_discovery.time, "monotonic", lambda: clock[0])

    with structlog.testing.capture_logs() as cap_logs:
        for i in range(10):
            # Advance past the 5-minute TTL on half the passes so both cached
            # and re-probed consecutive 69 passes are verified to warn once.
            if i >= 5:
                clock[0] += _FIRST_LAUNCH_CACHE_TTL + 1.0
            _poll_cycle(android, ios, wda_mgr, set())
            wda_manager._default_sim_states()

    warnings = [
        entry
        for entry in cap_logs
        if entry.get("log_level") == "warning" and entry.get("event") == _FIRST_LAUNCH_WARNING
    ]
    assert len(warnings) == 1
    assert [cmd for cmd in issued_cmds if cmd and cmd[0] == "xcrun"] == []


@pytest.mark.parametrize(
    "hostile_exc",
    [
        subprocess.TimeoutExpired(cmd=_XCODEBUILD_FIRST_LAUNCH_CMD, timeout=5),
        FileNotFoundError(2, "No such file or directory", "/usr/bin/xcodebuild"),
    ],
)
def test_hostile_gate_timeout_or_missing_binary_skips_xcrun_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    hostile_exc: Exception,
) -> None:
    """[test] Hostile: the gate command times out / raises FileNotFoundError ->
    treated as not ready, no xcrun, no exception escapes.
    """
    issued_cmds, _ = _install_fake_runner(monkeypatch, gate_exc=hostile_exc)
    android = MagicMock()
    android.discover.return_value = []
    ios = IOSController()
    wda_mgr = DaemonWDAManager(sim_states=wda_manager._default_sim_states)

    with structlog.testing.capture_logs() as cap_logs:
        diff = _poll_cycle(android, ios, wda_mgr, set())
        sim_states = wda_manager._default_sim_states()
        xctrace_devs = ios._discover_physical_xctrace()

    assert diff.raw_devices["ios"] == []
    assert sim_states == {}
    assert xctrace_devs == []
    assert [cmd for cmd in issued_cmds if cmd and cmd[0] == "xcrun"] == []
    warnings = [
        entry
        for entry in cap_logs
        if entry.get("log_level") == "warning" and entry.get("event") == _FIRST_LAUNCH_WARNING
    ]
    assert len(warnings) == 1


def test_non_darwin_skips_without_spawning_xcodebuild_or_xcrun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On non-darwin platforms, iOS discovery issues zero subprocess calls, logs
    the non-darwin info message once across 10 passes, and never mentions runFirstLaunch.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    issued_cmds, _ = _install_fake_runner(monkeypatch, gate_returncode=0)
    ios = IOSController()

    clock = [1000.0]
    monkeypatch.setattr(ios_discovery.time, "monotonic", lambda: clock[0])

    with structlog.testing.capture_logs() as cap_logs:
        for i in range(10):
            if i >= 5:
                clock[0] += _FIRST_LAUNCH_CACHE_TTL + 1.0
            assert ios.discover() == []
            assert wda_manager._default_sim_states() == {}

    assert issued_cmds == []
    assert _NON_DARWIN_MESSAGE == "iOS discovery disabled: not running on macOS"
    info_logs = [
        entry
        for entry in cap_logs
        if entry.get("log_level") == "info" and entry.get("event") == _NON_DARWIN_MESSAGE
    ]
    assert len(info_logs) == 1
    assert not any("runFirstLaunch" in str(entry.get("event", "")) for entry in cap_logs)


def test_cache_ttl_boundary_and_state_change_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verdict is cached for < 300s, re-probed at >= 300s, and warns once per state change."""
    clock = [100.0]
    monkeypatch.setattr(ios_discovery.time, "monotonic", lambda: clock[0])

    returncodes = [69, 0, 69]
    calls: list[list[str]] = []

    def runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": _FIRST_LAUNCH_TIMEOUT,
            "check": False,
        }
        return subprocess.CompletedProcess(
            cmd, returncode=returncodes.pop(0), stdout="", stderr=""
        )

    with structlog.testing.capture_logs() as cap_logs:
        # 1. Cold check -> exit 69 -> False, logs warning #1
        assert xcode_first_launch_ready(runner=runner) is False
        assert len(calls) == 1

        # 2. Within TTL (299.9s elapsed) -> cached False, runner not called
        clock[0] = 100.0 + _FIRST_LAUNCH_CACHE_TTL - 0.1
        assert xcode_first_launch_ready(runner=runner) is False
        assert len(calls) == 1

        # 3. Exact TTL boundary (300.0s elapsed) -> re-probed -> exit 0 -> True
        clock[0] = 100.0 + _FIRST_LAUNCH_CACHE_TTL
        assert xcode_first_launch_ready(runner=runner) is True
        assert len(calls) == 2

        # 4. Within new TTL -> cached True
        clock[0] += 10.0
        assert xcode_first_launch_ready(runner=runner) is True
        assert len(calls) == 2

        # 5. Next TTL expiry -> transitions True -> False (exit 69), logs warning #2
        clock[0] = 100.0 + 2 * _FIRST_LAUNCH_CACHE_TTL
        assert xcode_first_launch_ready(runner=runner) is False
        assert len(calls) == 3

    warnings = [
        entry
        for entry in cap_logs
        if entry.get("log_level") == "warning" and entry.get("event") == _FIRST_LAUNCH_WARNING
    ]
    assert len(warnings) == 2


def test_individual_wiring_sites_blocked_when_gate_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each wiring site individually refuses to call xcrun when checkFirstLaunchStatus != 0."""
    issued_cmds, _ = _install_fake_runner(monkeypatch, gate_returncode=69)
    ios = IOSController()

    _reset_xcode_first_launch_state()
    assert ios._discover_simulators() == []

    _reset_xcode_first_launch_state()
    assert ios._discover_physical_devicectl() == []

    _reset_xcode_first_launch_state()
    assert ios._discover_physical_xctrace() == []

    _reset_xcode_first_launch_state()
    assert wda_manager._default_sim_states() == {}

    assert [cmd for cmd in issued_cmds if cmd and cmd[0] == "xcrun"] == []
    assert len(issued_cmds) == 4
    assert all(cmd == _XCODEBUILD_FIRST_LAUNCH_CMD for cmd in issued_cmds)


def test_probe_helper_directly_handles_subprocess_error() -> None:
    """_probe_xcode_first_launch returns False on CalledProcessError."""
    def failing_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(69, cmd)

    assert _probe_xcode_first_launch(failing_runner) is False


def _seq_runner(
    outcomes: list[int | None | Exception],
) -> tuple[Callable[..., subprocess.CompletedProcess[Any]], list[list[str]]]:
    calls: list[list[str]] = []

    def runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        calls.append(list(cmd))
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return subprocess.CompletedProcess(cmd, returncode=item, stdout="", stderr="")  # type: ignore[arg-type]

    return runner, calls


def test_contract_transient_timeout_cached_for_30s_then_reprobed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] Probe raises subprocess.TimeoutExpired at t=0 -> not ready;
    at t=29.9 cached (runner not called); at t=30.0 re-probed; exit 0 -> ready.
    """
    assert _FIRST_LAUNCH_TRANSIENT_TTL == 30.0
    clock = [0.0]
    monkeypatch.setattr(ios_discovery.time, "monotonic", lambda: clock[0])
    runner, calls = _seq_runner(
        [
            subprocess.TimeoutExpired(cmd=_XCODEBUILD_FIRST_LAUNCH_CMD, timeout=5),
            0,
            subprocess.TimeoutExpired(cmd=_XCODEBUILD_FIRST_LAUNCH_CMD, timeout=5),
        ]
    )

    assert xcode_first_launch_ready(runner=runner) is False
    assert len(calls) == 1
    assert ios_discovery._first_launch_cache_ttl == _FIRST_LAUNCH_TRANSIENT_TTL

    clock[0] = 29.9
    assert xcode_first_launch_ready(runner=runner) is False
    assert len(calls) == 1

    clock[0] = 30.0
    assert xcode_first_launch_ready(runner=runner) is True
    assert len(calls) == 2
    assert ios_discovery._first_launch_cache_ttl == _FIRST_LAUNCH_CACHE_TTL

    clock[0] = 329.9
    assert xcode_first_launch_ready(runner=runner) is True
    assert len(calls) == 2

    clock[0] = 330.0
    assert xcode_first_launch_ready(runner=runner) is False
    assert ios_discovery._first_launch_cache_ttl == _FIRST_LAUNCH_TRANSIENT_TTL
    _reset_xcode_first_launch_state()
    assert ios_discovery._first_launch_cache_ttl == _FIRST_LAUNCH_CACHE_TTL


def test_contract_not_ready_cached_for_300s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] Non-zero exit (69) and FileNotFoundError at t=0 are still cached
    until t=299.9 and re-probed at t=300.0 (unchanged posture).
    """
    clock = [0.0]
    monkeypatch.setattr(ios_discovery.time, "monotonic", lambda: clock[0])
    runner, calls = _seq_runner(
        [69, FileNotFoundError(2, "No such file", "/usr/bin/xcodebuild"), 0]
    )

    assert xcode_first_launch_ready(runner=runner) is False
    assert len(calls) == 1

    for t in (30.0, 299.9):
        clock[0] = t
        assert xcode_first_launch_ready(runner=runner) is False
        assert len(calls) == 1

    clock[0] = 300.0
    assert xcode_first_launch_ready(runner=runner) is False
    assert len(calls) == 2

    for t in (330.0, 599.9):
        clock[0] = t
        assert xcode_first_launch_ready(runner=runner) is False
        assert len(calls) == 2

    clock[0] = 600.0
    assert xcode_first_launch_ready(runner=runner) is True
    assert len(calls) == 3


def test_recovery_info_log_on_69_to_0_and_silent_on_cold_0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] 69 -> 0 sequence logs the recovery info line exactly once;
    a cold start that probes 0 first logs no recovery line.
    """
    clock = [0.0]
    monkeypatch.setattr(ios_discovery.time, "monotonic", lambda: clock[0])
    runner, _ = _seq_runner([69, 0, 0])

    with structlog.testing.capture_logs() as cap_logs:
        assert xcode_first_launch_ready(runner=runner) is False
        clock[0] = _FIRST_LAUNCH_CACHE_TTL
        assert xcode_first_launch_ready(runner=runner) is True
        clock[0] = 2 * _FIRST_LAUNCH_CACHE_TTL
        assert xcode_first_launch_ready(runner=runner) is True

    assert _FIRST_LAUNCH_READY_MESSAGE == "Xcode first launch ready: iOS discovery resumed"
    recovery_logs = [
        entry
        for entry in cap_logs
        if entry.get("log_level") == "info" and entry.get("event") == _FIRST_LAUNCH_READY_MESSAGE
    ]
    assert len(recovery_logs) == 1

    _reset_xcode_first_launch_state()
    cold_runner, _ = _seq_runner([0])
    with structlog.testing.capture_logs() as cold_logs:
        assert xcode_first_launch_ready(runner=cold_runner) is True
    assert cold_logs == []


def test_hostile_permission_error_and_none_returncode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] Hostile: a probe raising PermissionError, then a runner returning
    returncode=None, are each treated as not ready with no exception escaping.
    """
    clock = [0.0]
    monkeypatch.setattr(ios_discovery.time, "monotonic", lambda: clock[0])
    runner, calls = _seq_runner(
        [PermissionError(13, "Permission denied", "/usr/bin/xcodebuild"), None]
    )

    assert xcode_first_launch_ready(runner=runner) is False
    assert len(calls) == 1

    clock[0] = 29.9
    assert xcode_first_launch_ready(runner=runner) is False
    assert len(calls) == 1

    clock[0] = 30.0
    assert xcode_first_launch_ready(runner=runner) is False
    assert len(calls) == 2

    ok_runner, _ = _seq_runner([0, None])
    assert _probe_xcode_first_launch(ok_runner) is True
    assert _probe_xcode_first_launch(ok_runner) is False


SIM_UDID = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"
PHYS_UDID = "00008110-0000000000000001"


def test_contract_user_tool_paths_raise_xcode_not_ready_without_xcrun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] With the gate reset and a fake runner where checkFirstLaunchStatus
    exits 69: IOSController.boot, screenshot (via _run_binary), a _run_devicectl
    caller and start_recording each raise XcodeNotReadyError and the recorded
    subprocess.run/Popen calls contain ZERO argv starting with 'xcrun'.
    """
    assert issubclass(XcodeNotReadyError, DeviceError)
    issued_cmds, _ = _install_fake_runner(monkeypatch, gate_returncode=69)
    popen_cmds: list[list[str]] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        popen_cmds.append(list(cmd))
        proc = MagicMock()
        proc.poll.return_value = None
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ios_mod.time, "sleep", lambda _: None)
    ios = IOSController()
    monkeypatch.setattr(ios, "_get_wda", lambda serial: MagicMock(mjpeg_port=None))

    _reset_xcode_first_launch_state()
    with pytest.raises(XcodeNotReadyError) as boot_exc:
        ios.boot(SIM_UDID)
    assert str(boot_exc.value) == _FIRST_LAUNCH_WARNING

    _reset_xcode_first_launch_state()
    with pytest.raises(XcodeNotReadyError) as shot_exc:
        ios.screenshot(SIM_UDID)
    assert str(shot_exc.value) == _FIRST_LAUNCH_WARNING

    _reset_xcode_first_launch_state()
    with pytest.raises(XcodeNotReadyError) as devctl_exc:
        ios.kill(PHYS_UDID, "com.example.app")
    assert str(devctl_exc.value) == _FIRST_LAUNCH_WARNING

    _reset_xcode_first_launch_state()
    with pytest.raises(XcodeNotReadyError) as rec_exc:
        ios.start_recording(SIM_UDID)
    assert str(rec_exc.value) == _FIRST_LAUNCH_WARNING

    # Also exercise start_recording's own gate check before Popen when
    # _require_recordable has already passed.
    monkeypatch.setattr(ios, "_require_recordable", lambda serial: None)
    _reset_xcode_first_launch_state()
    with pytest.raises(XcodeNotReadyError) as rec_popen_exc:
        ios.start_recording(SIM_UDID)
    assert str(rec_popen_exc.value) == _FIRST_LAUNCH_WARNING

    assert [cmd for cmd in issued_cmds if cmd and cmd[0] == "xcrun"] == []
    assert [cmd for cmd in popen_cmds if cmd and cmd[0] == "xcrun"] == []
    assert popen_cmds == []


def test_contract_inspect_ios_and_log_stream_gated_on_exit_69(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] With checkFirstLaunchStatus exiting 69: _inspect_ios for a
    simulator and a physical serial returns process.running False with zero
    xcrun spawned; get_log_stream_cmd on a simulator UDID raises XcodeNotReadyError.
    """
    issued_cmds, _ = _install_fake_runner(monkeypatch, gate_returncode=69)
    ios = IOSController()

    _reset_xcode_first_launch_state()
    with pytest.raises(XcodeNotReadyError) as dev_json_exc:
        _devicectl_json(PHYS_UDID, "apps")
    assert str(dev_json_exc.value) == _FIRST_LAUNCH_WARNING

    _reset_xcode_first_launch_state()
    assert _get_ios_simulator_pid(SIM_UDID, "com.example.app") is None

    _reset_xcode_first_launch_state()
    sim_result: dict[str, Any] = {}
    _inspect_ios(ios, SIM_UDID, "com.example.app", 20, sim_result)
    assert sim_result["process"] == {
        "pid": None,
        "running": False,
        "package": "com.example.app",
    }

    _reset_xcode_first_launch_state()
    phys_result: dict[str, Any] = {}
    _inspect_ios(ios, PHYS_UDID, "com.example.app", 20, phys_result)
    assert phys_result["process"] == {
        "pid": None,
        "running": False,
        "package": "com.example.app",
    }

    _reset_xcode_first_launch_state()
    with pytest.raises(XcodeNotReadyError) as stream_exc:
        ios.get_log_stream_cmd(SIM_UDID)
    assert str(stream_exc.value) == _FIRST_LAUNCH_WARNING

    # Physical log stream --device branch is not xcrun and stays ungated.
    assert ios.get_log_stream_cmd(PHYS_UDID) == [
        "log",
        "stream",
        "--device",
        PHYS_UDID,
        "--style",
        "syslog",
    ]
    assert [cmd for cmd in issued_cmds if cmd and cmd[0] == "xcrun"] == []


def test_contract_exit_0_user_tool_paths_issue_existing_xcrun_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] Exit 0 -> each site issues its existing xcrun argv unchanged."""
    issued_cmds, _ = _install_fake_runner(monkeypatch, gate_returncode=0)
    popen_cmds: list[list[str]] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        popen_cmds.append(list(cmd))
        proc = MagicMock()
        proc.poll.return_value = None
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ios_mod.time, "sleep", lambda _: None)
    ios = IOSController()
    monkeypatch.setattr(ios, "_get_wda", lambda serial: MagicMock(mjpeg_port=None))

    ios.boot(SIM_UDID)
    assert ["xcrun", "simctl", "boot", SIM_UDID] in issued_cmds

    png = ios.screenshot(SIM_UDID)
    assert png == b"\x89PNG\r\n\x1a\nfakedata"
    assert any(
        cmd[:6] == ["xcrun", "simctl", "io", SIM_UDID, "screenshot", "--type=png"]
        for cmd in issued_cmds
    )

    ios.kill(PHYS_UDID, "com.example.app")
    assert any(
        cmd[:7] == ["xcrun", "devicectl", "device", "info", "apps", "--device", PHYS_UDID]
        for cmd in issued_cmds
    )

    ios.start_recording(SIM_UDID)
    assert ["xcrun", "simctl", "list", "devices", "-j"] in issued_cmds
    assert len(popen_cmds) == 1
    assert popen_cmds[0][:7] == [
        "xcrun",
        "simctl",
        "io",
        SIM_UDID,
        "recordVideo",
        "--codec=h264",
        "--mask=black",
    ]

    assert _get_ios_simulator_pid(SIM_UDID, "com.example.app") is None
    sim_result: dict[str, Any] = {}
    _inspect_ios(ios, SIM_UDID, "com.example.app", 20, sim_result)
    assert ["xcrun", "simctl", "spawn", SIM_UDID, "launchctl", "list"] in issued_cmds

    phys_result: dict[str, Any] = {}
    _inspect_ios(ios, PHYS_UDID, "com.example.app", 20, phys_result)
    assert any(
        cmd[:6] == ["xcrun", "devicectl", "device", "info", "apps", "--device"]
        and cmd[6] == PHYS_UDID
        for cmd in issued_cmds
    )

    assert ios.get_log_stream_cmd(SIM_UDID) == [
        "xcrun",
        "simctl",
        "spawn",
        SIM_UDID,
        "log",
        "stream",
        "--style",
        "syslog",
    ]


def test_hostile_race_ttl_expiry_between_discovery_gate_and_run_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] Hostile race: gate returns True for the discovery check, then the
    TTL expires and the next probe exits 69 -> _discover_simulators returns []
    and no exception escapes.
    """
    clock = [0.0]
    probes = [0, 69]
    issued_cmds: list[list[str]] = []

    def fake_monotonic() -> float:
        val = clock[0]
        clock[0] += _FIRST_LAUNCH_CACHE_TTL + 1.0
        return val

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        issued_cmds.append(list(cmd))
        if cmd == _XCODEBUILD_FIRST_LAUNCH_CMD:
            return subprocess.CompletedProcess(
                cmd, returncode=probes.pop(0), stdout="", stderr=""
            )
        if cmd[:2] == ["xcrun", "xctrace"]:
            raise XcodeNotReadyError()
        raise AssertionError(f"Unexpected command spawned: {cmd}")

    monkeypatch.setattr(ios_discovery.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(subprocess, "run", fake_run)
    ios = IOSController()

    with structlog.testing.capture_logs() as cap_logs:
        assert ios._discover_simulators() == []
        assert issued_cmds == [_XCODEBUILD_FIRST_LAUNCH_CMD, _XCODEBUILD_FIRST_LAUNCH_CMD]

        # Also verify physical discovery catches XcodeNotReadyError without escaping.
        monkeypatch.setattr(
            ios,
            "_discover_physical_devicectl",
            MagicMock(side_effect=XcodeNotReadyError()),
        )
        _reset_xcode_first_launch_state()
        probes.extend([0, 69])
        assert ios._discover_physical() == []

    debug_events = {
        entry.get("event"): entry.get("error")
        for entry in cap_logs
        if entry.get("log_level") == "debug"
    }
    assert debug_events == {
        "ios_discovery_failed": _FIRST_LAUNCH_WARNING,
        "devicectl_discovery_failed": _FIRST_LAUNCH_WARNING,
        "xctrace_discovery_failed": _FIRST_LAUNCH_WARNING,
    }

