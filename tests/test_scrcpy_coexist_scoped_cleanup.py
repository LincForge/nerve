"""Tests for ScrcpyManager orphan cleanup and forward sweep (linc-nerve-he7c, linc-nerve-8r0b)."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys

import pytest

from linc_nerve.core import ios as _ios_preload  # noqa: F401
from linc_nerve.core import scrcpy as scrcpy_mod
from linc_nerve.core import scrcpy_manager
from linc_nerve.core.adb_path import ADB_PATH
from linc_nerve.core.scrcpy import SCRCPY_VERSION, ScrcpyManager

EXPECTED_DEVICE_PATH = f"/data/local/tmp/nerve-scrcpy-server-v{SCRCPY_VERSION}.jar"
UPSTREAM_CMDLINE = (
    "adb -s R3CFAKE00001 shell CLASSPATH=/data/local/tmp/scrcpy-server.jar "
    "app_process / com.genymobile.scrcpy.Server 5.0.1 scid=1a2b3c4d"
)
NERVE_CMDLINE = (
    f"adb -s R3CFAKE00001 shell CLASSPATH={EXPECTED_DEVICE_PATH} "
    "app_process / com.genymobile.scrcpy.Server 5.0.1 scid=1a2b3c4d"
)


def _nerve_client_cmdline(scid_hex: str = "6e650001", serial: str = "R3CFAKE00001") -> str:
    return (
        f"{ADB_PATH} -s {serial} shell CLASSPATH={EXPECTED_DEVICE_PATH} "
        f"app_process / com.genymobile.scrcpy.Server {SCRCPY_VERSION} "
        f"scid={scid_hex} tunnel_forward=true"
    )


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    return proc.pid


def _install_kill_recorder(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record non-zero signals sent via ``os.kill``, delegating signal 0 to real ``os.kill``."""
    real_kill = os.kill
    signals_sent: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            real_kill(pid, 0)
            return
        signals_sent.append((pid, int(sig)))

    monkeypatch.setattr(os, "kill", fake_kill)
    return signals_sent


class TestLiveParentPreservedAndOrphanReapedContract:
    """[contract] linc-nerve-8r0b: ScrcpyManager spares live-parent clients and their forwards."""

    def test_live_parent_client_not_signaled_and_forward_left_in_place(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] AC1: with ps faked to list a nerve scrcpy-server adb client
        (scid=6e650001) whose ppid is a live pid, constructing ScrcpyManager sends it
        no signal and leaves forward localabstract:scrcpy_6e650001 in place."""
        live_ppid = os.getpid()
        ps_stdout = f"  4242 {live_ppid} {_nerve_client_cmdline('6e650001')}\n"
        forward_stdout = "R3CFAKE00001 tcp:27183 localabstract:scrcpy_6e650001\n"

        recorded_cmds: list[list[str]] = []
        signals_sent = _install_kill_recorder(monkeypatch)

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded_cmds.append(list(cmd))
            if cmd and cmd[0] == "ps":
                return subprocess.CompletedProcess(cmd, 0, stdout=ps_stdout, stderr="")
            if cmd == [ADB_PATH, "forward", "--list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=forward_stdout, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(scrcpy_mod, "adb_server_config_error", lambda: None)

        ScrcpyManager()

        pkill_calls = [cmd for cmd in recorded_cmds if cmd and cmd[0] == "pkill"]
        remove_calls = [cmd for cmd in recorded_cmds if "--remove" in cmd]
        assert pkill_calls == [], f"must never call pkill, got {pkill_calls!r}"
        assert signals_sent == [], (
            f"must send no signal to live-parent client, got {signals_sent!r}"
        )
        assert remove_calls == [], (
            f"must leave localabstract:scrcpy_6e650001 forward in place, got {remove_calls!r}"
        )

    @pytest.mark.parametrize("ppid_kind", ["ppid_1", "dead_ppid"])
    def test_orphan_client_with_ppid_1_or_dead_ppid_reaped_and_forward_removed(
        self, monkeypatch: pytest.MonkeyPatch, ppid_kind: str
    ) -> None:
        """[contract] AC2: a client with ppid 1 (or a dead ppid) and its forward
        ARE reaped and removed (orphan behaviour kept)."""
        orphan_ppid = 1 if ppid_kind == "ppid_1" else _dead_pid()
        live_ppid = os.getpid()
        ps_stdout = (
            "\n".join(
                [
                    f"  4242 {live_ppid} {_nerve_client_cmdline('6e650001')}",
                    f"  5151 {orphan_ppid} {_nerve_client_cmdline('6e650002')}",
                ]
            )
            + "\n"
        )
        forward_stdout = (
            "\n".join(
                [
                    "R3CFAKE00001 tcp:27183 localabstract:scrcpy_6e650001",
                    "R3CFAKE00001 tcp:27184 localabstract:scrcpy_6e650002",
                ]
            )
            + "\n"
        )

        recorded_cmds: list[list[str]] = []
        log_calls: list[tuple[str, dict[str, object]]] = []
        signals_sent = _install_kill_recorder(monkeypatch)

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded_cmds.append(list(cmd))
            if cmd and cmd[0] == "ps":
                return subprocess.CompletedProcess(cmd, 0, stdout=ps_stdout, stderr="")
            if cmd == [ADB_PATH, "forward", "--list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=forward_stdout, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(scrcpy_mod, "adb_server_config_error", lambda: None)
        monkeypatch.setattr(
            scrcpy_mod.logger,
            "info",
            lambda event, **kw: log_calls.append((event, kw)),
        )

        ScrcpyManager()

        assert signals_sent == [(5151, int(signal.SIGTERM))]
        remove_calls = [cmd for cmd in recorded_cmds if "--remove" in cmd]
        assert remove_calls == [
            [ADB_PATH, "-s", "R3CFAKE00001", "forward", "--remove", "tcp:27184"]
        ]
        assert ("scrcpy_orphans_cleaned", {"count": 1}) in log_calls
        assert ("scrcpy_orphan_forwards_removed", {"count": 1}) in log_calls

    def test_orphan_already_exited_before_sigterm_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If an orphan client exits between ps and os.kill (ProcessLookupError),
        cleanup tolerates it without raising."""
        ps_stdout = f"  5151 1 {_nerve_client_cmdline('6e650002')}\n"

        def fake_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError(3, "No such process")

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd and cmd[0] == "ps":
                return subprocess.CompletedProcess(cmd, 0, stdout=ps_stdout, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(scrcpy_mod, "adb_server_config_error", lambda: None)

        ScrcpyManager()

    @pytest.mark.parametrize(
        "ps_failure",
        [
            FileNotFoundError("ps"),
            subprocess.TimeoutExpired(cmd=["ps", "-axo", "pid=,ppid=,args="], timeout=5),
            OSError("ps unavailable"),
            "nonzero_rc",
        ],
        ids=["missing_binary", "timeout_expired", "os_error", "nonzero_rc"],
    )
    def test_ps_failure_or_timeout_reaps_nothing_and_removes_no_forwards(
        self, monkeypatch: pytest.MonkeyPatch, ps_failure: object
    ) -> None:
        """[contract] AC3: ps raising FileNotFoundError or timing out: no kill,
        no forward removal, manager still constructible."""
        forward_stdout = "R3CFAKE00001 tcp:27183 localabstract:scrcpy_6e650001\n"
        recorded_cmds: list[list[str]] = []
        warning_calls: list[tuple[str, dict[str, object]]] = []
        signals_sent = _install_kill_recorder(monkeypatch)

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded_cmds.append(list(cmd))
            if cmd and cmd[0] in ("ps", "pkill", "pgrep"):
                if isinstance(ps_failure, Exception):
                    raise ps_failure
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ps error")
            if cmd == [ADB_PATH, "forward", "--list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=forward_stdout, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(scrcpy_mod, "adb_server_config_error", lambda: None)
        monkeypatch.setattr(
            scrcpy_mod.logger,
            "warning",
            lambda event, **kw: warning_calls.append((event, kw)),
        )

        mgr = ScrcpyManager()

        assert mgr.active_streams == {}
        assert signals_sent == []
        remove_calls = [cmd for cmd in recorded_cmds if "--remove" in cmd]
        assert remove_calls == []
        assert any(event == "scrcpy_orphan_scan_skipped" for event, _ in warning_calls)


class TestScopedOrphanCleanupAndForwardSweepContract:
    """[contract] _cleanup_orphans and _sweep_orphan_forwards touch only nerve's own markers."""

    def test_scoped_orphan_cleanup_and_forward_sweep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unquoted_dots_cmdline = NERVE_CMDLINE.replace(".3.4.jar", "X3Y4Zjar")
        current_proc_cmdline = f"python3 -m pytest -k {EXPECTED_DEVICE_PATH}"
        ps_listing = "\n".join(
            [
                f"  4242 1 {NERVE_CMDLINE}",
                f"  4243 1 {UPSTREAM_CMDLINE}",
                f"  4244 1 {unquoted_dots_cmdline}",
                f"  4245 1 {current_proc_cmdline}",
            ]
        )
        forward_listing = "\n".join(
            [
                "R3CFAKE00001 tcp:27183 localabstract:scrcpy_6e656a2f",
                "R3CFAKE00001 tcp:27184 localabstract:scrcpy_1a2b3c4d",
                "R3CFAKE00001 tcp:27185 localabstract:scrcpy_zzzz",
                "R3CFAKE00001 tcp:27186",
                "R3CFAKE\u00e9 tcp:27187 localabstract:scrcpy_6e656a2f",
                "R3CFAKE00001 tcp:27188 localabstract:scrcpy_6e656a2f extra_field",
            ]
        )
        recorded_calls: list[tuple[list[str], dict[str, object]]] = []
        log_calls: list[tuple[str, dict[str, object]]] = []
        signals_sent = _install_kill_recorder(monkeypatch)

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded_calls.append((list(cmd), dict(kwargs)))
            if cmd and cmd[0] == "ps":
                return subprocess.CompletedProcess(cmd, 0, stdout=ps_listing + "\n", stderr="")
            if cmd == [ADB_PATH, "forward", "--list"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=forward_listing + "\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(scrcpy_mod, "adb_server_config_error", lambda: None)
        monkeypatch.setattr(
            scrcpy_mod.logger,
            "info",
            lambda event, **kw: log_calls.append((event, kw)),
        )

        ScrcpyManager()

        # 1. Orphan-enumeration command and regex pattern contract
        ps_calls = [(argv, kw) for argv, kw in recorded_calls if argv and argv[0] == "ps"]
        assert ps_calls == [
            (
                ["ps", "-axo", "pid=,ppid=,args="],
                {"capture_output": True, "text": True, "timeout": 5},
            )
        ]
        assert signals_sent == [(4242, int(signal.SIGTERM))]

        pattern = scrcpy_manager._nerve_client_pattern()
        assert re.search(pattern, UPSTREAM_CMDLINE) is None, (
            f"orphan pattern {pattern!r} must never match upstream cmdline {UPSTREAM_CMDLINE!r}"
        )
        assert re.search(pattern, NERVE_CMDLINE) is not None, (
            f"orphan pattern {pattern!r} must match nerve cmdline {NERVE_CMDLINE!r}"
        )
        assert re.search(pattern, unquoted_dots_cmdline) is None, (
            f"orphan pattern {pattern!r} must escape regex dots in device path"
        )
        assert re.search(pattern, current_proc_cmdline) is None, (
            f"orphan pattern {pattern!r} must never match non-adb current process"
        )

        # 2. Forward sweep contract: removes ONLY the nerve forward and logs kept count
        list_calls = [
            (argv, kw) for argv, kw in recorded_calls if argv == [ADB_PATH, "forward", "--list"]
        ]
        assert list_calls == [
            (
                [ADB_PATH, "forward", "--list"],
                {"capture_output": True, "text": True, "timeout": 5},
            )
        ]

        remove_calls = [(argv, kw) for argv, kw in recorded_calls if "--remove" in argv]
        assert remove_calls == [
            (
                [ADB_PATH, "-s", "R3CFAKE00001", "forward", "--remove", "tcp:27183"],
                {"capture_output": True, "timeout": 5},
            )
        ]

        assert ("scrcpy_orphans_cleaned", {"count": 1}) in log_calls
        assert ("scrcpy_orphan_forwards_removed", {"count": 1}) in log_calls
        kept_logs = [kw for event, kw in log_calls if event == "scrcpy_foreign_forwards_kept"]
        assert len(kept_logs) == 1
        kept_count = kept_logs[0].get("count")
        assert isinstance(kept_count, int)
        assert kept_count >= 3
        assert kept_count == 5

        assert scrcpy_manager.ScrcpyManager is ScrcpyManager
        assert (
            scrcpy_manager._NERVE_FORWARD_REMOTE_RE.fullmatch("localabstract:scrcpy_6e656a2f")
            is not None
        )
        assert scrcpy_manager._parse_nerve_forward_line(
            "R3CFAKE00001 tcp:27183 localabstract:scrcpy_6e656a2f"
        ) == ("R3CFAKE00001", "tcp:27183")
        assert scrcpy_manager._parse_nerve_forward_line("malformed") is None
        assert callable(ScrcpyManager._cleanup_orphans)
        assert callable(ScrcpyManager._sweep_orphan_forwards)

    def test_sweep_orphan_forwards_scid_range_boundaries_and_log_suppression(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        boundary_listing = "\n".join(
            [
                "R3CFAKE00001 tcp:27180 localabstract:scrcpy_6e650000",
                "R3CFAKE00001 tcp:27181 localabstract:scrcpy_6e65ffff",
                "R3CFAKE00001 tcp:27182 localabstract:scrcpy_6e64ffff",
                "R3CFAKE00001 tcp:27183 localabstract:scrcpy_6e660000",
                "R3CFAKE00001 tcp:27184 localabstract:scrcpy_6E656A2F",
                "R3CFAKE00001 tcp:27185 localabstract:scrcpy_6e656a2",
                "R3CFAKE00001 tcp:27186 localabstract:scrcpy_6e656a2f0",
                "R3CFAKE00001 tcp:27187 localabstract:scrcpy_6e65zzzz",
            ]
        )
        removed_locals: list[str] = []
        log_calls: list[tuple[str, dict[str, object]]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd and cmd[0] == "pkill":
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
            if cmd == [ADB_PATH, "forward", "--list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=boundary_listing, stderr="")
            if "--remove" in cmd:
                removed_locals.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(scrcpy_mod, "adb_server_config_error", lambda: None)
        monkeypatch.setattr(
            scrcpy_mod.logger,
            "info",
            lambda event, **kw: log_calls.append((event, kw)),
        )

        ScrcpyManager()

        assert removed_locals == ["tcp:27180", "tcp:27181"]
        assert log_calls == [
            ("scrcpy_orphan_forwards_removed", {"count": 2}),
            ("scrcpy_foreign_forwards_kept", {"count": 6}),
        ]

        # When only nerve forwards are present, scrcpy_foreign_forwards_kept is not logged
        log_calls.clear()
        removed_locals.clear()
        boundary_listing = "R3CFAKE00001 tcp:27183 localabstract:scrcpy_6e656a2f\n"
        ScrcpyManager()
        assert removed_locals == ["tcp:27183"]
        assert log_calls == [("scrcpy_orphan_forwards_removed", {"count": 1})]

    def test_hostile_ps_and_forward_lines_skipped_and_counted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[test] AC4: hostile ps/forward lines (non-numeric pid, missing scid,
        non-hex scid, non-ASCII) are skipped and counted; foreign upstream scrcpy
        forwards are still kept."""
        hostile_ps = "\n".join(
            [
                f"  notapid 1 {_nerve_client_cmdline('6e650001')}",
                f"  6001 notappid {_nerve_client_cmdline('6e650002')}",
                f"  0 1 {_nerve_client_cmdline('6e650003')}",
                "  only_two_fields",
                (
                    f"  6002 1 {ADB_PATH} -s R3CFAKE00001 shell "
                    f"CLASSPATH={EXPECTED_DEVICE_PATH} app_process / "
                    f"com.genymobile.scrcpy.Server {SCRCPY_VERSION} tunnel_forward=true"
                ),
                f"  6003 1 {_nerve_client_cmdline('6e65zzzz')}",
                f"  6004 1 {_nerve_client_cmdline('6E650001')}",
                f"  6005 1 {_nerve_client_cmdline('6e65001')}",
                f"  6006 1 {_nerve_client_cmdline('6e650001', serial='R3CFAKE\u00e9')}",
                f"  6007 1 {UPSTREAM_CMDLINE}",
                f"  6008 1 python3 -m pytest -k {EXPECTED_DEVICE_PATH}",
            ]
        )
        hostile_forwards = "\n".join(
            [
                "R3CFAKE00001 tcp:27184 localabstract:scrcpy_1a2b3c4d",
                "R3CFAKE00001 tcp:27185 localabstract:scrcpy_zzzz",
                "R3CFAKE00001 tcp:27186 localabstract:scrcpy_",
                "R3CFAKE00001 tcp:27187",
                "R3CFAKE\u00e9 tcp:27188 localabstract:scrcpy_6e656a2f",
            ]
        )
        log_calls: list[tuple[str, dict[str, object]]] = []
        removed: list[list[str]] = []
        signals_sent = _install_kill_recorder(monkeypatch)

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd and cmd[0] == "ps":
                return subprocess.CompletedProcess(cmd, 0, stdout=hostile_ps + "\n", stderr="")
            if cmd == [ADB_PATH, "forward", "--list"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=hostile_forwards + "\n", stderr=""
                )
            if "--remove" in cmd:
                removed.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(scrcpy_mod, "adb_server_config_error", lambda: None)
        monkeypatch.setattr(
            scrcpy_mod.logger,
            "info",
            lambda event, **kw: log_calls.append((event, kw)),
        )

        ScrcpyManager()

        assert signals_sent == []
        assert removed == []
        assert ("scrcpy_ps_lines_skipped", {"count": 9}) in log_calls
        assert ("scrcpy_foreign_forwards_kept", {"count": 5}) in log_calls


class TestOrphanKillMissingOrTimeoutPin:
    """[test] Missing or timed-out ps leaves ScrcpyManager constructible and removes nothing."""

    @pytest.mark.parametrize(
        "orphan_exc",
        [
            FileNotFoundError("ps"),
            subprocess.TimeoutExpired(cmd=["ps", "-axo", "pid=,ppid=,args="], timeout=5),
        ],
        ids=["missing_binary", "timeout_expired"],
    )
    def test_pkill_missing_or_timeout_leaves_manager_constructible(
        self, monkeypatch: pytest.MonkeyPatch, orphan_exc: Exception
    ) -> None:
        adb_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd and cmd[0] in ("ps", "pkill", "pgrep"):
                raise orphan_exc
            adb_calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(scrcpy_mod, "adb_server_config_error", lambda: None)

        mgr = ScrcpyManager()
        assert mgr.active_streams == {}
        assert adb_calls == []


class TestConftestSubprocessGuard:
    """[test] AC5: the autouse conftest guard fails any unfaked subprocess call to pkill or adb."""

    @pytest.mark.parametrize(
        "cmd",
        [
            ["pkill", "-f", "adb.*nerve-scrcpy-server"],
            ["/usr/bin/pkill", "-f", "anything"],
            ["adb", "forward", "--list"],
            [ADB_PATH, "devices"],
        ],
        ids=["pkill", "usr_bin_pkill", "adb", "adb_path"],
    )
    def test_unfaked_pkill_or_adb_run_and_popen_fail(self, cmd: list[str]) -> None:
        with pytest.raises(AssertionError, match="forbidden in tests"):
            subprocess.run(cmd, capture_output=True, timeout=1)
        with pytest.raises(AssertionError, match="forbidden in tests"):
            subprocess.Popen(cmd)

    def test_guard_is_a_popen_subclass_so_annotations_and_isinstance_work(self) -> None:
        """[test] AC5: the guard replaces ``subprocess.Popen`` with a subclass, not a
        function, so ``subprocess.Popen[bytes]`` annotations and isinstance checks hold."""
        assert isinstance(subprocess.Popen, type)
        assert subprocess.Popen[bytes] is not None
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=5)
        assert isinstance(proc, subprocess.Popen)


class TestSurvivingScidsPassedExplicitlyContract:
    """[contract] linc-nerve-8r0b blocker 2: surviving scids flow by return value, not a global."""

    def test_no_module_global_carries_surviving_scids(self) -> None:
        assert not hasattr(scrcpy_manager, "_last_surviving_scids")

    def _forward_recorder(
        self, monkeypatch: pytest.MonkeyPatch, forward_stdout: str
    ) -> list[list[str]]:
        removed: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd == [ADB_PATH, "forward", "--list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=forward_stdout, stderr="")
            if "--remove" in cmd:
                removed.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(scrcpy_mod, "adb_server_config_error", lambda: None)
        return removed

    def test_sweep_with_none_removes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """[contract] ``_sweep_orphan_forwards(None)`` (scan failed) removes no forward."""
        removed = self._forward_recorder(
            monkeypatch, "R3CFAKE00001 tcp:27183 localabstract:scrcpy_6e650001\n"
        )
        ScrcpyManager._sweep_orphan_forwards(None)
        assert removed == []

    def test_sweep_keeps_forward_in_surviving_set_regardless_of_other_managers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] a concurrent manager's construction cannot clear the surviving set
        another sweep was handed: the set is an argument, not shared state."""
        removed = self._forward_recorder(
            monkeypatch,
            "R3CFAKE00001 tcp:27183 localabstract:scrcpy_6e650001\n"
            "R3CFAKE00001 tcp:27184 localabstract:scrcpy_6e650002\n",
        )
        surviving = frozenset({0x6E650001})
        ScrcpyManager._sweep_orphan_forwards(surviving)
        assert removed == [[ADB_PATH, "-s", "R3CFAKE00001", "forward", "--remove", "tcp:27184"]]

    def test_cleanup_returns_surviving_scids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ps_stdout = (
            f"  4242 {os.getpid()} {_nerve_client_cmdline('6e650001')}\n"
            f"  5151 1 {_nerve_client_cmdline('6e650002')}\n"
        )
        _install_kill_recorder(monkeypatch)
        monkeypatch.setattr(
            scrcpy_mod.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=ps_stdout, stderr=""),
        )
        assert ScrcpyManager._cleanup_orphans() == {0x6E650001}


class TestOrphanClassificationBoundaries:
    """[contract] boundary cases for parent liveness and pid parsing (mutation pins)."""

    def _run(
        self, monkeypatch: pytest.MonkeyPatch, ps_stdout: str, *, parent_alive: bool
    ) -> tuple[list[tuple[int, int]], set[int] | None]:
        signals_sent = _install_kill_recorder(monkeypatch)
        monkeypatch.setattr(scrcpy_manager, "pid_alive", lambda pid: parent_alive)
        monkeypatch.setattr(
            scrcpy_mod.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=ps_stdout, stderr=""),
        )
        surviving = ScrcpyManager._cleanup_orphans()
        return signals_sent, surviving

    def test_live_parent_with_ppid_2_is_spared(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """[contract] only ppid <= 1 counts as re-parented; a live ppid 2 (kthreadd /
        launchd child) keeps its client alive and its scid surviving."""
        signals_sent, surviving = self._run(
            monkeypatch, f"  4242 2 {_nerve_client_cmdline('6e650001')}\n", parent_alive=True
        )
        assert signals_sent == []
        assert surviving == {0x6E650001}

    def test_dead_parent_with_ppid_2_is_reaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        signals_sent, surviving = self._run(
            monkeypatch, f"  4242 2 {_nerve_client_cmdline('6e650001')}\n", parent_alive=False
        )
        assert signals_sent == [(4242, int(signal.SIGTERM))]
        assert surviving == set()

    def test_client_with_pid_1_is_parsed_not_skipped(self) -> None:
        """[contract] pid 1 is a valid pid; only pid 0 is rejected."""
        line = f"  1 {os.getpid()} {_nerve_client_cmdline('6e650001')}"
        assert scrcpy_manager._parse_ps_nerve_line(line) == (1, os.getpid(), 0x6E650001)

    def test_non_nerve_line_classified_as_none(self) -> None:
        assert scrcpy_manager._parse_ps_nerve_line(f"  4243 1 {UPSTREAM_CMDLINE}") is None

    def test_adb_path_with_whitespace_fails_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """[contract] an adb path containing whitespace can never be matched in ps argv,
        so the scan is skipped and the sweep removes nothing (fail safe)."""
        monkeypatch.setattr(scrcpy_mod, "ADB_PATH", "/opt/Android SDK/platform-tools/adb")
        calls: list[list[str]] = []
        signals_sent = _install_kill_recorder(monkeypatch)

        def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        assert ScrcpyManager._cleanup_orphans() is None
        assert calls == []
        assert signals_sent == []

    @pytest.mark.parametrize(
        ("token", "ok"),
        [("6e650001", True), ("6E650001", False), ("6e65001", False), ("6e6500011", False)],
    )
    def test_scid_hex_regex_accepts_only_eight_lowercase_hex(self, token: str, ok: bool) -> None:
        assert (scrcpy_manager._SCID_HEX_RE.fullmatch(token) is not None) is ok
