"""Tests for `nerve doctor` — environment + daemon diagnostics (issue #80).

Every check is a pure function over an injected ProbeReport, so the whole
suite runs with no devices, no network, and no filesystem access.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from linc_nerve.cli.doctor import (
    CheckResult,
    FixAction,
    ProbeReport,
    apply_fixes,
    check_adb,
    check_daemon,
    check_device_auth,
    check_linc_layout,
    check_scrcpy_server,
    check_wda_project,
    check_wda_team_id,
    check_xcode_clt,
    doctor,
    overall_ok,
    plan_fixes,
    run_checks,
)
from linc_nerve.core.observability import linc_home

# ---------------------------------------------------------------------------
# Probe fixtures
# ---------------------------------------------------------------------------


def _green_probes(**overrides: object) -> ProbeReport:
    """A ProbeReport where every check passes."""
    base = dict(
        adb_path="/opt/sdk/platform-tools/adb",
        adb_is_executable=True,
        adb_devices_output="List of devices attached\nR5CT30ABC\tdevice\n",
        scrcpy_server_exists=True,
        scrcpy_server_path="/pkg/vendor/scrcpy-server-v3.3.4",
        wda_project_dir="/home/u/.linc/wda/src",
        wda_checkout_exists=True,
        wda_team_id="ABCDE12345",
        is_macos=True,
        xcode_clt_path="/Library/Developer/CommandLineTools",
        daemon_health={"ok": True, "package_version": "0.1.0"},
        installed_version="0.1.0",
        daemon_port=8111,
        linc_home_exists=True,
        missing_linc_dirs=(),
    )
    base.update(overrides)
    return ProbeReport(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Individual checks — pure functions, ok + fail paths, fix-it hints
# ---------------------------------------------------------------------------


class TestCheckAdb:
    def test_ok(self) -> None:
        r = check_adb("/sdk/adb", True, "List of devices attached\nabc\tdevice\n")
        assert r.status == "ok"
        assert r.name == "adb"

    def test_missing_binary_fails_with_hint(self) -> None:
        r = check_adb("adb", False, None)
        assert r.status == "fail"
        assert r.hint  # one-line fix-it hint required
        assert "platform-tools" in r.hint or "ANDROID" in r.hint

    def test_server_unresponsive_fails_with_hint(self) -> None:
        r = check_adb("/sdk/adb", True, None)
        assert r.status == "fail"
        assert "adb start-server" in r.hint


class TestCheckDeviceAuth:
    def test_all_authorized_ok(self) -> None:
        out = "List of devices attached\nabc\tdevice\ndef\tdevice\n"
        assert check_device_auth(out).status == "ok"

    def test_unauthorized_devices_listed(self) -> None:
        out = "List of devices attached\nabc\tunauthorized\ndef\tdevice\n"
        r = check_device_auth(out)
        assert r.status == "fail"
        assert "abc" in r.detail
        assert r.hint  # accept the RSA prompt

    def test_no_adb_output_skips(self) -> None:
        assert check_device_auth(None).status == "skip"


class TestCheckScrcpyServer:
    def test_present_ok(self) -> None:
        assert check_scrcpy_server(True, "/pkg/vendor/scrcpy-server-v3.3.4").status == "ok"

    def test_missing_fails_with_hint(self) -> None:
        r = check_scrcpy_server(False, "/pkg/vendor/scrcpy-server-v3.3.4")
        assert r.status == "fail"
        assert r.hint


class TestCheckWda:
    def test_project_dir_ok(self) -> None:
        assert check_wda_project("/x/wda", True).status == "ok"

    def test_project_dir_unset_warns_with_hint(self) -> None:
        r = check_wda_project("", False)
        assert r.status == "warn"
        assert "WDA_PROJECT_DIR" in r.hint

    def test_project_dir_set_but_missing_checkout_fails(self) -> None:
        r = check_wda_project("/x/wda", False)
        assert r.status == "fail"
        assert "clone" in r.hint.lower() or "checkout" in r.hint.lower()

    def test_team_id_set_ok(self) -> None:
        assert check_wda_team_id("ABCDE12345").status == "ok"

    def test_team_id_unset_warns_not_fails(self) -> None:
        r = check_wda_team_id("")
        assert r.status == "warn"
        assert "WDA_TEAM_ID" in r.hint


class TestCheckXcodeClt:
    def test_present_ok(self) -> None:
        assert check_xcode_clt(True, "/Library/Developer/CommandLineTools").status == "ok"

    def test_missing_fails_with_hint(self) -> None:
        r = check_xcode_clt(True, None)
        assert r.status == "fail"
        assert "xcode-select --install" in r.hint

    def test_non_macos_skips(self) -> None:
        assert check_xcode_clt(False, None).status == "skip"


class TestCheckDaemon:
    def test_reachable_matching_version_ok(self) -> None:
        r = check_daemon({"ok": True, "package_version": "0.1.0"}, "0.1.0", 8111)
        assert r.status == "ok"

    def test_unreachable_warns_with_hint(self) -> None:
        r = check_daemon(None, "0.1.0", 8111)
        assert r.status == "warn"
        assert "8111" in r.detail or "8111" in r.hint

    def test_stale_daemon_version_mismatch_fails_with_kickstart_hint(self) -> None:
        r = check_daemon({"ok": True, "package_version": "0.0.9"}, "0.1.0", 8111)
        assert r.status == "fail"
        assert "0.0.9" in r.detail and "0.1.0" in r.detail
        assert "kickstart" in r.hint

    def test_unknown_version_warns(self) -> None:
        r = check_daemon({"ok": True, "package_version": "unknown"}, "0.1.0", 8111)
        assert r.status == "warn"


class TestCheckLincLayout:
    def test_sane_ok(self) -> None:
        assert check_linc_layout(True, ()).status == "ok"

    def test_missing_home_warns_with_hint(self) -> None:
        r = check_linc_layout(False, ("~/.linc",))
        assert r.status == "warn"
        assert r.hint

    def test_missing_subdirs_listed(self) -> None:
        r = check_linc_layout(True, ("~/.linc/nerve/recordings",))
        assert r.status == "warn"
        assert "recordings" in r.detail


# ---------------------------------------------------------------------------
# Aggregation + exit-code contract
# ---------------------------------------------------------------------------


class TestRunChecks:
    def test_green_probes_all_ok(self) -> None:
        results = run_checks(_green_probes())
        assert results, "run_checks returned no results"
        assert all(r.status in ("ok", "skip") for r in results)
        assert overall_ok(results)

    def test_warn_does_not_fail_overall(self) -> None:
        results = run_checks(_green_probes(wda_team_id=""))
        assert any(r.status == "warn" for r in results)
        assert overall_ok(results)

    def test_any_fail_flips_overall(self) -> None:
        results = run_checks(_green_probes(scrcpy_server_exists=False))
        assert not overall_ok(results)

    def test_every_failure_carries_a_hint(self) -> None:
        probes = _green_probes(
            adb_path="adb",
            adb_is_executable=False,
            adb_devices_output=None,
            scrcpy_server_exists=False,
            wda_project_dir="",
            wda_checkout_exists=False,
            xcode_clt_path=None,
            daemon_health=None,
            linc_home_exists=False,
            missing_linc_dirs=("~/.linc",),
        )
        for r in run_checks(probes):
            if r.status == "fail":
                assert r.hint, f"check {r.name} failed without a fix-it hint"


# ---------------------------------------------------------------------------
# Bootstrap fix planning + dry-run
# ---------------------------------------------------------------------------


class TestPlanFixes:
    def test_green_results_plan_nothing(self) -> None:
        assert plan_fixes(run_checks(_green_probes())) == []

    def test_missing_linc_dirs_planned(self) -> None:
        results = run_checks(
            _green_probes(linc_home_exists=False, missing_linc_dirs=("~/.linc",))
        )
        fixes = plan_fixes(results)
        assert any(f.kind == "mkdir" for f in fixes)

    def test_adb_server_down_planned(self) -> None:
        results = run_checks(_green_probes(adb_devices_output=None))
        fixes = plan_fixes(results)
        assert any(f.kind == "adb-start-server" for f in fixes)

    def test_dry_run_applies_nothing(self) -> None:
        executed: list[FixAction] = []
        fixes = [FixAction(kind="mkdir", description="create ~/.linc", args=("~/.linc",))]
        applied = apply_fixes(fixes, dry_run=True, executor=lambda f: executed.append(f))
        assert executed == []
        assert applied == []

    def test_apply_executes_when_not_dry_run(self) -> None:
        executed: list[FixAction] = []
        fixes = [FixAction(kind="mkdir", description="create ~/.linc", args=("~/.linc",))]
        applied = apply_fixes(fixes, dry_run=False, executor=lambda f: executed.append(f))
        assert executed == fixes
        assert applied == fixes


# ---------------------------------------------------------------------------
# CLI contract — exit codes, --json, --fix --dry-run
# ---------------------------------------------------------------------------


class TestDoctorCli:
    def test_exit_zero_all_green(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "linc_nerve.cli.doctor.gather_probes", lambda **kw: _green_probes()
        )
        result = CliRunner().invoke(doctor, [])
        assert result.exit_code == 0

    def test_exit_one_on_failure_with_cross_and_hint(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "linc_nerve.cli.doctor.gather_probes",
            lambda **kw: _green_probes(scrcpy_server_exists=False),
        )
        result = CliRunner().invoke(doctor, [])
        assert result.exit_code == 1
        assert "✗" in result.output
        assert "hint:" in result.output.lower()

    def test_json_output_parses(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "linc_nerve.cli.doctor.gather_probes",
            lambda **kw: _green_probes(scrcpy_server_exists=False),
        )
        result = CliRunner().invoke(doctor, ["--json"])
        payload = json.loads(result.output)
        assert payload["ok"] is False
        names = {c["name"] for c in payload["checks"]}
        assert "daemon" in names
        assert "scrcpy-server" in {c["name"] for c in payload["checks"] if c["status"] == "fail"}
        failing = [c for c in payload["checks"] if c["status"] == "fail"]
        assert all(c["hint"] for c in failing)

    def test_fix_dry_run_prints_plan_without_executing(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "linc_nerve.cli.doctor.gather_probes",
            lambda **kw: _green_probes(
                linc_home_exists=False, missing_linc_dirs=("~/.linc",)
            ),
        )
        executed: list[FixAction] = []
        monkeypatch.setattr(
            "linc_nerve.cli.doctor._execute_fix", lambda f: executed.append(f)
        )
        result = CliRunner().invoke(doctor, ["--fix", "--dry-run"])
        assert "dry-run" in result.output.lower()
        assert executed == []

    def test_check_result_is_frozen(self) -> None:
        r = CheckResult(name="x", status="ok", detail="d")
        try:
            r.status = "fail"  # type: ignore[misc]
            raise AssertionError("CheckResult should be immutable")
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# Stranger-ready: daemon, WDA and ~/.linc layout are optional (warn, not fail)
# ---------------------------------------------------------------------------


def test_wda_unset_is_warn_not_fail_for_android_only_users() -> None:
    r = check_wda_project("", False)
    assert (r.name, r.status) == ("wda-project", "warn")
    assert r.detail == (
        "WDA_PROJECT_DIR not set — iOS tap/swipe/elements unavailable (Android works without it)"
    )


def test_wda_set_but_missing_still_fails() -> None:
    assert check_wda_project("/nope", False).status == "fail"


def test_unreachable_daemon_is_warn_cli_falls_back_in_process() -> None:
    r = check_daemon(None, "0.1.0", 8111)
    assert r.status == "warn"
    assert "in-process" in r.detail
    assert r.detail == (
        "no nerve daemon on 127.0.0.1:8111 — CLI calls run in-process (the daemon is optional)"
    )
    assert r.hint == (
        "Start it: uv run nerve daemon --port 8111 "
        "(or launchctl kickstart -k gui/$UID/com.linc.nerve-daemon)"
    )


def test_stale_daemon_still_fails() -> None:
    assert check_daemon({"package_version": "0.0.9"}, "0.1.0", 8111).status == "fail"


def test_missing_linc_layout_is_warn_created_on_first_use() -> None:
    r = check_linc_layout(False, ("~/.linc", "~/.linc/nerve/recordings"))
    assert (r.name, r.status) == ("linc-layout", "warn")
    assert r.detail == (
        "missing directories: ~/.linc, ~/.linc/nerve/recordings — nerve creates them on first use"
    )
    assert r.hint == "mkdir -p ~/.linc ~/.linc/nerve/recordings (or `nerve doctor --fix`)"


def test_missing_linc_layout_warn_still_plans_mkdir_fix() -> None:
    home = linc_home()
    dirs = (str(home), str(home / "nerve" / "recordings"))
    results = [check_linc_layout(False, (str(home),))]
    assert plan_fixes(results) == [
        FixAction(
            kind="mkdir",
            description=f"create directories: {', '.join(dirs)}",
            args=dirs,
        )
    ]


def test_ok_linc_layout_plans_no_mkdir() -> None:
    assert plan_fixes([check_linc_layout(True, ())]) == []


def test_fresh_android_only_host_is_green_overall() -> None:
    """A stranger's first run: adb works, no daemon, no WDA, no ~/.linc yet → exit 0."""
    probes = _green_probes(
        wda_project_dir="",
        wda_checkout_exists=False,
        wda_team_id="",
        daemon_health=None,
        linc_home_exists=False,
        missing_linc_dirs=("~/.linc", "~/.linc/nerve/recordings"),
    )
    results = run_checks(probes)
    statuses = {r.name: r.status for r in results}
    assert statuses == {
        "adb": "ok",
        "device-auth": "ok",
        "scrcpy-server": "ok",
        "wda-project": "warn",
        "wda-team-id": "warn",
        "xcode-clt": "ok",
        "daemon": "warn",
        "linc-layout": "warn",
    }
    assert overall_ok(results)


def test_fresh_host_cli_exits_zero_and_still_offers_fix(monkeypatch) -> None:
    monkeypatch.setattr(
        "linc_nerve.cli.doctor.gather_probes",
        lambda **kw: _green_probes(
            daemon_health=None, linc_home_exists=False, missing_linc_dirs=("~/.linc",)
        ),
    )
    executed: list[FixAction] = []
    monkeypatch.setattr("linc_nerve.cli.doctor._execute_fix", lambda f: executed.append(f))
    result = CliRunner().invoke(doctor, ["--fix", "--dry-run"])
    assert result.exit_code == 0
    assert "✗" not in result.output
    home = linc_home()
    assert f"create directories: {home}, {home / 'nerve' / 'recordings'}" in result.output
    assert executed == []


# ---------------------------------------------------------------------------
# Probe-started adb server cleanup + `nerve doctor --human`
# ---------------------------------------------------------------------------


class TestAdbProbeServerHygiene:
    @staticmethod
    def _stub_other_probes(monkeypatch) -> None:
        from linc_nerve.cli import doctor as doctor_mod

        monkeypatch.setattr(doctor_mod, "ADB_PATH", "/sdk/adb")
        monkeypatch.setattr(doctor_mod.shutil, "which", lambda _p: "/sdk/adb")
        monkeypatch.setattr(doctor_mod, "_probe_daemon_health", lambda _port: None)
        monkeypatch.setattr(doctor_mod, "_probe_xcode_clt", lambda: "/Library/Developer/CLT")

    def test_not_listening_issues_adb_kill_server_after_adb_devices(self, monkeypatch) -> None:
        import subprocess as sp

        from linc_nerve.cli import doctor as doctor_mod

        self._stub_other_probes(monkeypatch)
        seen_addresses: list[object] = []
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_listening(address: object) -> doctor_mod.AdbProbe:
            seen_addresses.append(address)
            assert calls == [], "_adb_server_probe must run BEFORE _probe_adb_devices"
            return doctor_mod.AdbProbe.REFUSED

        def fake_run(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
            calls.append((list(cmd), dict(kwargs)))
            return sp.CompletedProcess(
                cmd,
                0,
                stdout="List of devices attached\n",
                stderr="* daemon started successfully\n",
            )

        monkeypatch.setattr(doctor_mod, "_adb_server_probe", fake_listening)
        monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

        probes = doctor_mod.gather_probes()
        assert len(seen_addresses) == 1
        assert probes.adb_server_listening is False
        assert [c[0] for c in calls] == [
            ["/sdk/adb", "devices"],
            ["/sdk/adb", "kill-server"],
        ]
        assert calls[1][1].get("timeout") == 10
        assert calls[1][1].get("capture_output") is True
        assert calls[1][1].get("text") is True
        assert calls[1][1].get("check") is False

        adb_check = next(r for r in run_checks(probes) if r.name == "adb")
        assert adb_check.status == "ok"
        assert (
            adb_check.detail
            == "adb responsive (/sdk/adb) (server started for the check and stopped)"
        )

    def test_already_listening_issues_no_kill_server(self, monkeypatch) -> None:
        import subprocess as sp

        from linc_nerve.cli import doctor as doctor_mod

        self._stub_other_probes(monkeypatch)
        calls: list[list[str]] = []

        monkeypatch.setattr(
            doctor_mod, "_adb_server_probe", lambda _addr: doctor_mod.AdbProbe.LISTENING
        )

        def fake_run(cmd: list[str], **_kwargs: object) -> sp.CompletedProcess[str]:
            calls.append(list(cmd))
            return sp.CompletedProcess(
                cmd,
                0,
                stdout="List of devices attached\n",
                stderr="* daemon started successfully\n",
            )

        monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

        probes = doctor_mod.gather_probes()
        assert probes.adb_server_listening is True
        assert calls == [["/sdk/adb", "devices"]]

        adb_check = next(r for r in run_checks(probes) if r.name == "adb")
        assert adb_check.status == "ok"
        assert adb_check.detail == "adb responsive (/sdk/adb)"
        assert (
            check_adb("/sdk/adb", True, "List of devices attached\n").detail
            == "adb responsive (/sdk/adb)"
        )

    def test_kill_server_carries_android_adb_server_port_env(self, monkeypatch) -> None:
        import subprocess as sp

        from linc_nerve.cli import doctor as doctor_mod

        self._stub_other_probes(monkeypatch)
        monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5199")
        probed_ports: list[int] = []
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_listening(address: object) -> doctor_mod.AdbProbe:
            probed_ports.append(getattr(address, "port", -1))
            return doctor_mod.AdbProbe.REFUSED

        def fake_run(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
            calls.append((list(cmd), dict(kwargs)))
            return sp.CompletedProcess(
                cmd,
                0,
                stdout="List of devices attached\n",
                stderr="* daemon started successfully\n",
            )

        monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

        doctor_mod.gather_probes(listening_probe=fake_listening)
        assert probed_ports == [5199]
        assert [c[0] for c in calls] == [
            ["/sdk/adb", "devices"],
            ["/sdk/adb", "kill-server"],
        ]
        kill_env = calls[1][1].get("env")
        assert isinstance(kill_env, dict)
        assert kill_env.get("ANDROID_ADB_SERVER_PORT") == "5199"

    def test_non_executable_adb_skips_devices_and_kill_server(self, monkeypatch) -> None:
        from linc_nerve.cli import doctor as doctor_mod

        self._stub_other_probes(monkeypatch)
        monkeypatch.setattr(doctor_mod, "ADB_PATH", "adb")
        monkeypatch.setattr(doctor_mod.shutil, "which", lambda _p: None)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            doctor_mod.subprocess,
            "run",
            lambda cmd, **_kw: calls.append(list(cmd)),
        )

        probes = doctor_mod.gather_probes(
            listening_probe=lambda _addr: doctor_mod.AdbProbe.REFUSED
        )
        assert probes.adb_is_executable is False
        assert probes.adb_devices_output is None
        assert probes.adb_server_listening is True
        assert calls == []

    def test_invalid_adb_server_env_reads_unknown_never_refused(self, monkeypatch) -> None:
        """[contract] An ADB_SERVER_SOCKET nerve rejects is UNKNOWN, so doctor never kills."""
        from linc_nerve.cli import doctor as doctor_mod

        monkeypatch.setenv("ADB_SERVER_SOCKET", "invalid-spec")
        result = doctor_mod._probe_adb_listening(lambda _a: doctor_mod.AdbProbe.REFUSED)
        assert result is doctor_mod.AdbProbe.UNKNOWN
        assert doctor_mod.should_stop(result, "* daemon started successfully") is False

    def test_stop_probe_started_server_swallows_errors(self, monkeypatch) -> None:
        from linc_nerve.cli import doctor as doctor_mod

        def boom(*_a: object, **_kw: object) -> None:
            raise OSError("kill-server failed")

        monkeypatch.setattr(doctor_mod.subprocess, "run", boom)
        doctor_mod._stop_probe_started_server("/sdk/adb")

    def test_fix_adb_start_server_executes_after_gather_probes(
        self, monkeypatch
    ) -> None:
        import subprocess as sp

        from linc_nerve.cli import doctor as doctor_mod

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: object) -> sp.CompletedProcess[str]:
            calls.append(list(cmd))
            return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(doctor_mod, "ADB_PATH", "/sdk/adb")
        monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            doctor_mod,
            "gather_probes",
            lambda **_kw: _green_probes(adb_devices_output=None),
        )
        result = CliRunner().invoke(doctor, ["--fix"])
        assert result.exit_code == 1
        assert calls == [["/sdk/adb", "start-server"]]


class TestDoctorHumanFlag:
    def test_doctor_human_flag_via_group_runner(
        self, monkeypatch, restore_signal_handlers
    ) -> None:
        from linc_nerve.cli.main import main

        monkeypatch.setattr(
            "linc_nerve.cli.doctor.gather_probes", lambda **kw: _green_probes()
        )
        plain = CliRunner().invoke(main, ["doctor"])
        human = CliRunner().invoke(main, ["doctor", "--human"])
        global_human = CliRunner().invoke(main, ["--human", "doctor"])
        assert plain.exit_code == 0
        assert human.exit_code == 0
        assert global_human.exit_code == 0
        assert human.output == plain.output
        assert "all green" in human.output

    def test_doctor_human_flag_exits_one_on_failure(
        self, monkeypatch, restore_signal_handlers
    ) -> None:
        from linc_nerve.cli.main import main

        monkeypatch.setattr(
            "linc_nerve.cli.doctor.gather_probes",
            lambda **kw: _green_probes(scrcpy_server_exists=False),
        )
        human = CliRunner().invoke(main, ["doctor", "--human"])
        assert human.exit_code == 1
        assert "✗" in human.output

    def test_doctor_human_with_json_is_usage_error(
        self, monkeypatch, restore_signal_handlers
    ) -> None:
        from linc_nerve.cli.main import main

        monkeypatch.setattr(
            "linc_nerve.cli.doctor.gather_probes", lambda **kw: _green_probes()
        )
        result = CliRunner().invoke(main, ["doctor", "--human", "--json"])
        assert result.exit_code == 2
        assert "--human" in result.output
        assert "--json" in result.output
