"""[contract] Wave 4: refused ADB_SERVER_SOCKET / ANDROID_ADB_SERVER_PORT stops every
emulator, doctor, and scrcpy adb subprocess with one explicit error and zero spawns
(bead linc-nerve-k2yj).
"""

from __future__ import annotations

import subprocess as sp
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from linc_nerve.cli import doctor as doctor_mod
from linc_nerve.cli.doctor import (
    FixAction,
    _execute_fix,
    doctor,
    gather_probes,
    plan_fixes,
    run_checks,
)
from linc_nerve.core.adb_path import ADB_PATH
from linc_nerve.core.emulator import EmulatorManager
from linc_nerve.core.scrcpy import ScrcpyStream
from linc_nerve.mcp import _core, emulator_tools

REFUSED_ENV_CASES = [
    ("ADB_SERVER_SOCKET", "tcp:203.0.113.5:5037"),
    ("ANDROID_ADB_SERVER_PORT", "99999"),
    ("ANDROID_ADB_SERVER_PORT", "abc"),
]


@pytest.fixture(autouse=True)
def _clean_adb_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADB_SERVER_SOCKET", raising=False)
    monkeypatch.delenv("ANDROID_ADB_SERVER_PORT", raising=False)
    monkeypatch.setattr("linc_nerve.core.emulator.resolve_emulator_path", lambda: "emulator")
    monkeypatch.setattr("linc_nerve.mcp.emulator_tools.resolve_emulator_path", lambda: "emulator")
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda _p: ADB_PATH)
    monkeypatch.setattr(doctor_mod, "_probe_daemon_health", lambda _port: None)
    monkeypatch.setattr(doctor_mod, "_probe_xcode_clt", lambda: "/Library/Developer/CLT")
    # Hermetic clock: emulator wait loops never sleep for real, and a guard that
    # failed open cannot stall a trial on a polling deadline.
    clock = [1000.0]

    def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    fake_time = SimpleNamespace(monotonic=lambda: clock[0], sleep=fake_sleep, time=time.time)
    monkeypatch.setattr("linc_nerve.core.emulator.time", fake_time)


def _install_subprocess_recorders(
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[str]]:
    """Patch subprocess.run and subprocess.Popen to record every spawned argv."""
    spawned: list[list[str]] = []
    launched = False

    def fake_run(cmd: list[str], **_kwargs: Any) -> sp.CompletedProcess[str]:
        nonlocal launched
        argv = [str(x) for x in cmd]
        spawned.append(argv)
        if argv and argv[0] == "emulator" and "-list-avds" in argv:
            return sp.CompletedProcess(argv, 0, stdout="Pixel_7_API_34\n", stderr="")
        if argv == [ADB_PATH, "devices"]:
            devices_out = (
                "List of devices attached\nemulator-5554\tdevice\n"
                if launched
                else "List of devices attached\n"
            )
            return sp.CompletedProcess(argv, 0, stdout=devices_out, stderr="")
        if argv[:4] == [ADB_PATH, "-s", "emulator-5554", "emu"] and "kill" in argv:
            launched = False
            return sp.CompletedProcess(argv, 0, stdout="OK\n", stderr="")
        if "sys.boot_completed" in argv:
            return sp.CompletedProcess(argv, 0, stdout="1\n", stderr="")
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    def fake_popen(cmd: list[str], **_kwargs: Any) -> MagicMock:
        nonlocal launched
        argv = [str(x) for x in cmd]
        spawned.append(argv)
        launched = True
        proc = MagicMock()
        proc.pid = 42424
        return proc

    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr(sp, "Popen", fake_popen)
    return spawned


def _adb_spawns(spawned: list[list[str]]) -> list[list[str]]:
    return [argv for argv in spawned if argv and argv[0] == ADB_PATH]


@pytest.mark.parametrize(("env_var", "env_val"), REFUSED_ENV_CASES)
def test_refused_adb_env_spawns_zero_adb_subprocesses_across_emulator_doctor_scrcpy(
    monkeypatch: pytest.MonkeyPatch,
    env_var: str,
    env_val: str,
) -> None:
    """[contract] With refused ADB_SERVER_SOCKET / ANDROID_ADB_SERVER_PORT:
    EmulatorManager().list_avds(), .start('<avd>'), .stop('emulator-5554'),
    nerve_emulator_list, doctor's adb check (via gather_probes + run_checks),
    `nerve doctor --fix` planning/execution, and scrcpy teardown spawn ZERO
    argv whose [0] is the adb path; emulator results carry the refusal reason;
    doctor's adb CheckResult is 'fail' naming the variable with hint
    'unset or point it at 127.0.0.1/localhost'.
    """
    monkeypatch.setenv(env_var, env_val)
    spawned = _install_subprocess_recorders(monkeypatch)

    mgr = EmulatorManager()

    # 1. EmulatorManager.list_avds() + nerve_emulator_list + helpers
    avds = mgr.list_avds()
    assert _adb_spawns(spawned) == []
    assert len(avds) == 1
    assert avds[0].avd_name == "Pixel_7_API_34"
    assert avds[0].state == "stopped"
    assert env_var in (avds[0].adb_refused or "")
    assert avds[0].to_dict()["running"] is False
    assert env_var in avds[0].to_dict()["adb_refused"]

    with pytest.raises((ValueError, RuntimeError)):
        mgr._run([ADB_PATH, "devices"])

    monkeypatch.setattr(_core, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(_core, "_emulator", mgr)
    mock_ios = MagicMock()
    mock_ios._discover_simulators.return_value = []
    monkeypatch.setattr(_core, "_ios", mock_ios)

    list_payload = emulator_tools._nerve_emulator_list_sync()
    assert env_var in list_payload.get("adb_refused", "")
    assert list_payload["virtual_devices"][0]["running"] is False

    # 2. EmulatorManager.start("<avd>")
    with pytest.raises((ValueError, RuntimeError)) as start_exc:
        mgr.start("Pixel_7_API_34")
    assert env_var in str(start_exc.value)

    start_payload = emulator_tools._nerve_emulator_start_sync("Pixel_7_API_34")
    assert env_var in start_payload.get("error", "")

    # 3. EmulatorManager.stop("emulator-5554")
    with pytest.raises((ValueError, RuntimeError)) as stop_exc:
        mgr.stop("emulator-5554")
    assert env_var in str(stop_exc.value)

    stop_payload = emulator_tools._nerve_emulator_stop_sync("emulator-5554")
    assert env_var in stop_payload.get("error", "")

    # 4. doctor's adb check (via the same entry the CLI uses) + --fix planning
    probes = gather_probes()
    results = run_checks(probes)
    adb_check = next(r for r in results if r.name == "adb")
    assert adb_check.status == "fail"
    assert env_var in adb_check.detail
    assert adb_check.hint == "unset or point it at 127.0.0.1/localhost"

    fixes = plan_fixes(results)
    assert not any(f.kind == "adb-start-server" for f in fixes)

    # Also verify _execute_fix and CLI `nerve doctor --fix` never spawn adb
    _execute_fix(FixAction(kind="adb-start-server", description="adb start-server"))
    CliRunner().invoke(doctor, ["--fix", "--dry-run"])

    # 5. scrcpy teardown skips forward removal
    stream = ScrcpyStream("emulator-5554", port=27183)
    stream.stop()
    from linc_nerve.core.scrcpy import ScrcpyManager

    ScrcpyManager._sweep_orphan_forwards(set())

    assert _adb_spawns(spawned) == []


def test_unset_adb_env_still_spawns_adb_devices_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] Pin: with the vars unset, the same calls still spawn `adb devices`
    (and scrcpy teardown removes the forward) exactly as today.
    """
    spawned = _install_subprocess_recorders(monkeypatch)
    monkeypatch.setattr(
        doctor_mod, "_adb_server_probe", lambda _addr: doctor_mod.AdbProbe.LISTENING
    )

    mgr = EmulatorManager()

    # list_avds -> spawns `adb devices`
    spawned.clear()
    mgr.list_avds()
    assert [ADB_PATH, "devices"] in _adb_spawns(spawned)

    # start("<avd>") -> spawns `adb devices`
    spawned.clear()
    mgr.start("Pixel_7_API_34", timeout=5)
    assert [ADB_PATH, "devices"] in _adb_spawns(spawned)

    # stop("emulator-5554") -> spawns `adb devices` (and `adb -s emulator-5554 emu kill`)
    spawned.clear()
    assert mgr.stop("emulator-5554") is True
    assert [ADB_PATH, "devices"] in _adb_spawns(spawned)

    # doctor gather_probes -> spawns `adb devices`
    spawned.clear()
    probes = gather_probes()
    results = run_checks(probes)
    adb_check = next(r for r in results if r.name == "adb")
    assert adb_check.status == "ok"
    assert [ADB_PATH, "devices"] in _adb_spawns(spawned)

    # scrcpy teardown -> spawns `adb -s emulator-5554 forward --remove tcp:27183`
    spawned.clear()
    stream = ScrcpyStream("emulator-5554", port=27183)
    stream.stop()
    assert [
        ADB_PATH,
        "-s",
        "emulator-5554",
        "forward",
        "--remove",
        "tcp:27183",
    ] in _adb_spawns(spawned)
