"""Unit tests for the adb-refusal guards (bead linc-nerve-k2yj).

Each guard is pinned on both sides: with a refusal reason it returns or raises
before any adb argv is spawned, and without one it reaches the exact argv it
spawned before the guard existed. Every test is hermetic: the refusal source
(``adb_server_config_error``) is patched per module and ``subprocess`` is
replaced by a recorder, so no test can reach a real adb binary or sleep. The
end-to-end env-var contract lives in tests/test_wave4_adb_refusal.py.
"""

from __future__ import annotations

import subprocess as sp
from typing import Any
from unittest.mock import MagicMock

import pytest

from linc_nerve.cli import doctor as doctor_mod
from linc_nerve.core import emulator as emulator_mod
from linc_nerve.core import scrcpy as scrcpy_mod
from linc_nerve.core.adb_path import ADB_PATH
from linc_nerve.core.emulator import (
    AdbServerRefusedError,
    EmulatorInfo,
    EmulatorManager,
    _apply_refused_dict_fields,
    _guard_adb_command,
    _require_adb_server_allowed,
    _tag_adb_refused,
)
from linc_nerve.core.scrcpy import ScrcpyStream
from linc_nerve.core.scrcpy_manager import ScrcpyManager
from linc_nerve.mcp import _core, emulator_tools

REASON = "ADB_SERVER_SOCKET='tcp:203.0.113.5:5037' is refused: host must be loopback"
HINT = "unset or point it at 127.0.0.1/localhost"


def _refuse(monkeypatch: pytest.MonkeyPatch, module: Any, reason: str | None) -> None:
    monkeypatch.setattr(module, "adb_server_config_error", lambda: reason)


class _Recorder:
    """Stand-in for ``subprocess.run``: records argv + kwargs, never spawns."""

    def __init__(self, stdout: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.stdout = stdout

    def __call__(self, cmd: list[str], **kwargs: Any) -> sp.CompletedProcess[str]:
        self.calls.append(([str(c) for c in cmd], kwargs))
        return sp.CompletedProcess(cmd, 0, stdout=self.stdout, stderr="")

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


# --- core/emulator.py ---------------------------------------------------------


def test_refused_error_is_both_value_and_runtime_error() -> None:
    assert issubclass(AdbServerRefusedError, ValueError)
    assert issubclass(AdbServerRefusedError, RuntimeError)


def test_require_adb_server_allowed_raises_naming_action_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse(monkeypatch, emulator_mod, REASON)
    with pytest.raises(AdbServerRefusedError) as exc:
        _require_adb_server_allowed("stop emulator 'emulator-5554'")
    assert str(exc.value) == f"Cannot stop emulator 'emulator-5554': {REASON}"


def test_require_adb_server_allowed_passes_when_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse(monkeypatch, emulator_mod, None)
    assert _require_adb_server_allowed("anything") is None


def test_guard_adb_command_refuses_only_adb_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse(monkeypatch, emulator_mod, REASON)
    with pytest.raises(AdbServerRefusedError) as exc:
        _guard_adb_command([ADB_PATH, "devices"])
    assert str(exc.value) == f"Cannot run adb command: {REASON}"
    assert _guard_adb_command(["emulator", "-list-avds"]) is None
    assert _guard_adb_command([]) is None


def test_guard_adb_command_allows_adb_argv_when_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse(monkeypatch, emulator_mod, None)
    assert _guard_adb_command([ADB_PATH, "devices"]) is None


def test_apply_refused_dict_fields_marks_not_running_with_reason() -> None:
    d = {"avd_name": "a"}
    out = _apply_refused_dict_fields(d, REASON)
    assert out is d
    assert out == {"avd_name": "a", "running": False, "adb_refused": REASON}


def test_apply_refused_dict_fields_leaves_dict_untouched_when_fine() -> None:
    d = {"avd_name": "a"}
    assert _apply_refused_dict_fields(d, None) == {"avd_name": "a"}


def test_emulator_info_to_dict_carries_refusal_only_when_set() -> None:
    assert EmulatorInfo(avd_name="a").adb_refused is None
    assert "adb_refused" not in EmulatorInfo(avd_name="a").to_dict()
    refused = EmulatorInfo(avd_name="a", adb_refused=REASON).to_dict()
    assert refused == {
        "avd_name": "a",
        "state": "stopped",
        "running": False,
        "adb_refused": REASON,
    }


def test_tag_adb_refused_stamps_every_entry_and_returns_same_list() -> None:
    infos = [EmulatorInfo(avd_name="a"), EmulatorInfo(avd_name="b")]
    out = _tag_adb_refused(infos, REASON)
    assert out is infos
    assert [i.adb_refused for i in infos] == [REASON, REASON]
    assert [i.adb_refused for i in _tag_adb_refused(infos, None)] == [None, None]


def test_run_refuses_adb_argv_before_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse(monkeypatch, emulator_mod, REASON)
    rec = _Recorder()
    monkeypatch.setattr(emulator_mod.subprocess, "run", rec)
    with pytest.raises(AdbServerRefusedError):
        EmulatorManager()._run([ADB_PATH, "devices"])
    assert rec.calls == []
    EmulatorManager()._run(["emulator", "-list-avds"])
    assert rec.argvs == [["emulator", "-list-avds"]]


def test_run_spawns_adb_argv_when_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse(monkeypatch, emulator_mod, None)
    rec = _Recorder()
    monkeypatch.setattr(emulator_mod.subprocess, "run", rec)
    EmulatorManager()._run([ADB_PATH, "devices"], timeout=7, check=False)
    assert rec.calls == [
        (
            [ADB_PATH, "devices"],
            {"capture_output": True, "text": True, "timeout": 7, "check": False},
        )
    ]


def test_running_unless_refused_skips_adb_when_refused() -> None:
    mgr = EmulatorManager()
    mgr._get_running_emulators = MagicMock(return_value={"Pixel": "emulator-5554"})
    assert mgr._running_unless_refused(REASON) == {}
    mgr._get_running_emulators.assert_not_called()
    assert mgr._running_unless_refused(None) == {"Pixel": "emulator-5554"}


def test_list_avds_refused_lists_avds_stopped_with_reason_and_no_adb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse(monkeypatch, emulator_mod, REASON)
    mgr = EmulatorManager()
    mgr._get_installed_avds = MagicMock(return_value=["Pixel_7"])
    mgr._get_running_emulators = MagicMock(side_effect=AssertionError("adb consulted"))
    infos = mgr.list_avds()
    assert [(i.avd_name, i.state, i.adb_refused) for i in infos] == [
        ("Pixel_7", "stopped", REASON)
    ]


def test_list_avds_not_refused_reports_running_without_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse(monkeypatch, emulator_mod, None)
    mgr = EmulatorManager()
    mgr._get_installed_avds = MagicMock(return_value=["Pixel_7"])
    mgr._get_running_emulators = MagicMock(return_value={"Pixel_7": "emulator-5556"})
    infos = mgr.list_avds()
    assert [(i.avd_name, i.state, i.serial, i.adb_refused) for i in infos] == [
        ("Pixel_7", "running", "emulator-5556", None)
    ]


def test_start_refused_raises_before_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse(monkeypatch, emulator_mod, REASON)
    mgr = EmulatorManager()
    mgr._preflight_checks = MagicMock(side_effect=AssertionError("preflight ran"))
    with pytest.raises(AdbServerRefusedError) as exc:
        mgr.start("Pixel_7")
    assert str(exc.value) == f"Cannot start emulator 'Pixel_7': {REASON}"


def test_stop_refused_raises_before_any_command(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse(monkeypatch, emulator_mod, REASON)
    mgr = EmulatorManager()
    mgr._run = MagicMock(side_effect=AssertionError("command ran"))
    with pytest.raises(AdbServerRefusedError) as exc:
        mgr.stop("emulator-5554")
    assert str(exc.value) == f"Cannot stop emulator 'emulator-5554': {REASON}"


# --- cli/doctor.py ------------------------------------------------------------


def test_check_adb_config_error_fails_with_reason_and_hint() -> None:
    result = doctor_mod.check_adb(ADB_PATH, True, "List of devices attached\n", True, REASON)
    assert (result.name, result.status, result.detail, result.hint) == (
        "adb",
        "fail",
        REASON,
        HINT,
    )


def test_check_adb_without_config_error_is_unchanged() -> None:
    result = doctor_mod.check_adb(ADB_PATH, True, "List of devices attached\n", True, None)
    assert result.status == "ok"


def test_run_checks_forwards_probe_config_error() -> None:
    probes = doctor_mod.ProbeReport(
        adb_path=ADB_PATH,
        adb_is_executable=True,
        adb_devices_output="List of devices attached\n",
        scrcpy_server_exists=True,
        scrcpy_server_path="/x",
        wda_project_dir="",
        wda_checkout_exists=False,
        wda_team_id="",
        is_macos=False,
        xcode_clt_path=None,
        daemon_health=None,
        installed_version="0.0.0",
        adb_config_error=REASON,
    )
    adb = next(r for r in doctor_mod.run_checks(probes) if r.name == "adb")
    assert (adb.status, adb.detail, adb.hint) == ("fail", REASON, HINT)


def test_execute_fix_start_server_refused_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse(monkeypatch, doctor_mod, REASON)
    rec = _Recorder()
    monkeypatch.setattr(doctor_mod.subprocess, "run", rec)
    doctor_mod._execute_fix(doctor_mod.FixAction(kind="adb-start-server", description="x"))
    assert rec.calls == []


def test_execute_fix_start_server_spawns_when_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse(monkeypatch, doctor_mod, None)
    rec = _Recorder()
    monkeypatch.setattr(doctor_mod.subprocess, "run", rec)
    doctor_mod._execute_fix(doctor_mod.FixAction(kind="adb-start-server", description="x"))
    assert rec.argvs == [[ADB_PATH, "start-server"]]


def test_run_adb_probe_refused_touches_neither_socket_nor_adb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse(monkeypatch, doctor_mod, REASON)
    rec = _Recorder()
    monkeypatch.setattr(doctor_mod.subprocess, "run", rec)

    def no_probe(_addr: Any) -> Any:
        raise AssertionError("listening probe ran")

    assert doctor_mod._run_adb_probe(ADB_PATH, True, no_probe) == (True, None)
    assert rec.calls == []


def test_run_adb_probe_not_refused_runs_adb_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse(monkeypatch, doctor_mod, None)
    rec = _Recorder(stdout="List of devices attached\n")
    monkeypatch.setattr(doctor_mod.subprocess, "run", rec)
    listening = doctor_mod.AdbProbe.LISTENING
    assert doctor_mod._run_adb_probe(ADB_PATH, True, lambda _a: listening) == (
        True,
        "List of devices attached\n",
    )
    assert rec.argvs == [[ADB_PATH, "devices"]]


def test_gather_probes_records_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse(monkeypatch, doctor_mod, REASON)
    monkeypatch.setattr(doctor_mod, "_run_adb_probe", lambda *_a: (True, None))
    monkeypatch.setattr(doctor_mod, "_probe_xcode_clt", lambda: None)
    monkeypatch.setattr(doctor_mod, "_probe_daemon_health", lambda _port: None)
    assert doctor_mod.gather_probes().adb_config_error == REASON


# --- core/scrcpy.py + core/scrcpy_manager.py ----------------------------------


def test_remove_adb_forward_refused_spawns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse(monkeypatch, scrcpy_mod, REASON)
    rec = _Recorder()
    monkeypatch.setattr(scrcpy_mod.subprocess, "run", rec)
    scrcpy_mod._remove_adb_forward("emulator-5554", 27183)
    assert rec.calls == []


def test_remove_adb_forward_spawns_exact_argv_when_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse(monkeypatch, scrcpy_mod, None)
    rec = _Recorder()
    monkeypatch.setattr(scrcpy_mod.subprocess, "run", rec)
    scrcpy_mod._remove_adb_forward("emulator-5554", 27183)
    assert rec.calls == [
        (
            [ADB_PATH, "-s", "emulator-5554", "forward", "--remove", "tcp:27183"],
            {"capture_output": True, "timeout": 5},
        )
    ]


@pytest.mark.parametrize(
    "error",
    [
        sp.TimeoutExpired(["adb"], 5),
        FileNotFoundError("adb"),
        PermissionError("adb"),  # an OSError that is not FileNotFoundError
    ],
    ids=["timeout", "file-not-found", "os-error"],
)
def test_remove_adb_forward_swallows_spawn_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _refuse(monkeypatch, scrcpy_mod, None)

    def boom(*_a: Any, **_k: Any) -> None:
        raise error

    monkeypatch.setattr(scrcpy_mod.subprocess, "run", boom)
    assert scrcpy_mod._remove_adb_forward("emulator-5554", 27183) is None


def test_teardown_removes_this_streams_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        scrcpy_mod, "_remove_adb_forward", lambda serial, port: calls.append((serial, port))
    )
    ScrcpyStream("emulator-5554", port=27183)._teardown_transport()
    assert calls == [("emulator-5554", 27183)]


def test_sweep_orphan_forwards_refused_spawns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse(monkeypatch, scrcpy_mod, REASON)
    rec = _Recorder()
    monkeypatch.setattr(scrcpy_mod.subprocess, "run", rec)
    ScrcpyManager._sweep_orphan_forwards(set())
    assert rec.calls == []


def test_sweep_orphan_forwards_lists_forwards_when_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse(monkeypatch, scrcpy_mod, None)
    rec = _Recorder()
    monkeypatch.setattr(scrcpy_mod.subprocess, "run", rec)
    ScrcpyManager._sweep_orphan_forwards(set())
    assert rec.argvs[0] == [ADB_PATH, "forward", "--list"]


# --- mcp/emulator_tools.py ----------------------------------------------------


def _list_payload(
    monkeypatch: pytest.MonkeyPatch, reason: str | None, infos: list[EmulatorInfo]
) -> dict[str, Any]:
    _refuse(monkeypatch, emulator_tools, reason)
    mgr = MagicMock()
    mgr.list_avds.return_value = infos
    ios = MagicMock()
    ios._discover_simulators.return_value = []
    monkeypatch.setattr(_core, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(_core, "_emulator", mgr)
    monkeypatch.setattr(_core, "_ios", ios)
    monkeypatch.setattr(emulator_tools, "_android_sdk_status_fields", lambda: {})
    return emulator_tools._nerve_emulator_list_sync()


def test_emulator_list_refused_surfaces_top_level_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _list_payload(
        monkeypatch, REASON, [EmulatorInfo(avd_name="Pixel_7", adb_refused=REASON)]
    )
    assert payload["adb_refused"] == REASON
    assert [d["running"] for d in payload["virtual_devices"]] == [False]


def test_emulator_list_not_refused_has_no_reason_and_running_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _list_payload(
        monkeypatch,
        None,
        [
            EmulatorInfo(avd_name="Pixel_7", serial="emulator-5554", state="running"),
            EmulatorInfo(avd_name="Pixel_8"),
        ],
    )
    assert "adb_refused" not in payload
    assert [d["running"] for d in payload["virtual_devices"]] == [True, False]
