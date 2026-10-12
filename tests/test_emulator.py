"""Tests for the emulator lifecycle manager."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.core.adb_path import ADB_PATH
from linc_nerve.core.emulator import _PORT_MAX, _PORT_MIN, EmulatorInfo, EmulatorManager


@pytest.fixture(autouse=True)
def _default_emulator_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests below assert on subprocess argv and don't care about resolver
    precedence (that's covered exhaustively in test_adb_path.py) — pin
    `resolve_emulator_path()` to the literal "emulator" so existing argv
    assertions keep testing what they already test. Tests exercising
    resolution/unresolved-state explicitly re-patch this within the test body."""
    monkeypatch.setattr("linc_nerve.core.emulator.resolve_emulator_path", lambda: "emulator")


class TestEmulatorInfo:
    def test_dataclass_defaults(self) -> None:
        info = EmulatorInfo(avd_name="Pixel_7_API_34")
        assert info.avd_name == "Pixel_7_API_34"
        assert info.serial is None
        assert info.port is None
        assert info.state == "stopped"
        assert info.api_level == ""
        assert info.started_at is None

    def test_to_dict_stopped(self) -> None:
        info = EmulatorInfo(avd_name="Pixel_7_API_34")
        d = info.to_dict()
        assert d == {"avd_name": "Pixel_7_API_34", "state": "stopped"}
        assert "serial" not in d
        assert "port" not in d

    def test_to_dict_running(self) -> None:
        info = EmulatorInfo(
            avd_name="Pixel_7_API_34",
            serial="emulator-5554",
            port=5554,
            state="running",
            api_level="34",
            started_at=1700000000.0,
        )
        d = info.to_dict()
        assert d["serial"] == "emulator-5554"
        assert d["port"] == 5554
        assert d["state"] == "running"
        assert d["api_level"] == "34"
        assert d["started_at"] == 1700000000.0


class TestAllocatedPorts:
    def test_parses_emulator_ports(self) -> None:
        mgr = EmulatorManager()
        adb_output = "List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n"
        with patch.object(mgr, "_run") as mock_run:
            mock_run.return_value = MagicMock(stdout=adb_output)
            ports = mgr._allocated_ports()
        assert ports == {5554, 5556}

    def test_ignores_physical_devices(self) -> None:
        mgr = EmulatorManager()
        adb_output = "List of devices attached\nFAKESERIAL0001\tdevice\nemulator-5554\tdevice\n"
        with patch.object(mgr, "_run") as mock_run:
            mock_run.return_value = MagicMock(stdout=adb_output)
            ports = mgr._allocated_ports()
        assert ports == {5554}

    def test_empty_when_no_devices(self) -> None:
        mgr = EmulatorManager()
        adb_output = "List of devices attached\n"
        with patch.object(mgr, "_run") as mock_run:
            mock_run.return_value = MagicMock(stdout=adb_output)
            ports = mgr._allocated_ports()
        assert ports == set()

    def test_port_allocation_finds_free(self) -> None:
        """With 5554 in use, next free port should be 5556."""
        mgr = EmulatorManager()
        adb_output = "List of devices attached\nemulator-5554\tdevice\n"
        with patch.object(mgr, "_run") as mock_run:
            mock_run.return_value = MagicMock(stdout=adb_output)
            ports = mgr._allocated_ports()
        assert 5554 in ports
        assert 5556 not in ports


class TestListAvds:
    def test_parses_avd_output(self) -> None:
        mgr = EmulatorManager()
        with patch.object(mgr, "_run") as mock_run:
            # emulator -list-avds
            mock_run.side_effect = [
                MagicMock(stdout="Pixel_7_API_34\nPixel_Watch\n"),
                # adb devices
                MagicMock(stdout="List of devices attached\n"),
            ]
            avds = mgr.list_avds()

        assert len(avds) == 2
        assert avds[0].avd_name == "Pixel_7_API_34"
        assert avds[0].state == "stopped"
        assert avds[1].avd_name == "Pixel_Watch"
        assert avds[1].state == "stopped"

    def test_cross_references_running(self) -> None:
        mgr = EmulatorManager()

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[0] == "emulator":
                return MagicMock(stdout="Pixel_7_API_34\nPixel_Watch\n")
            if cmd == [ADB_PATH, "devices"]:
                return MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n")
            if "getprop" in cmd and "ro.boot.qemu.avd_name" in cmd:
                return MagicMock(stdout="Pixel_7_API_34\n")
            if "getprop" in cmd and "ro.kernel.qemu.avd_name" in cmd:
                return MagicMock(stdout="")
            return MagicMock(stdout="")

        with patch.object(mgr, "_run", side_effect=run_side_effect):
            avds = mgr.list_avds()

        running = [a for a in avds if a.state == "running"]
        stopped = [a for a in avds if a.state == "stopped"]
        assert len(running) == 1
        assert running[0].avd_name == "Pixel_7_API_34"
        assert running[0].serial == "emulator-5554"
        assert running[0].port == 5554
        assert len(stopped) == 1
        assert stopped[0].avd_name == "Pixel_Watch"

    def test_uses_resolved_emulator_path_not_bare_literal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_get_installed_avds` must exec the RESOLVED emulator path (linc-nerve#118),
        not a hard-coded bare "emulator" — the launchd-PATH bug this issue fixes.
        Fails pre-fix: emulator.py:104 hard-codes `["emulator", "-list-avds"]`."""
        monkeypatch.setattr(
            "linc_nerve.core.emulator.resolve_emulator_path",
            lambda: "/opt/homebrew/share/android-commandlinetools/emulator/emulator",
        )
        mgr = EmulatorManager()
        with patch.object(mgr, "_run") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout="Pixel_7_API_34\n"),
                MagicMock(stdout="List of devices attached\n"),
            ]
            mgr.list_avds()

        first_call_cmd = mock_run.call_args_list[0][0][0]
        assert (
            first_call_cmd[0]
            == "/opt/homebrew/share/android-commandlinetools/emulator/emulator"
        )
        assert "-list-avds" in first_call_cmd

    def test_returns_empty_list_when_emulator_binary_unresolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When `resolve_emulator_path()` returns None, `_get_installed_avds` must
        return `[]` WITHOUT attempting a subprocess call (there's no binary to
        exec) — the unresolved state is silent-degrade-to-empty at this layer;
        surfacing it to the caller is `nerve_emulator_list`'s job
        (mcp/emulator_tools.py), not this manager's."""
        monkeypatch.setattr("linc_nerve.core.emulator.resolve_emulator_path", lambda: None)
        mgr = EmulatorManager()
        with patch.object(mgr, "_run") as mock_run:
            avds = mgr._get_installed_avds()

        assert avds == []
        mock_run.assert_not_called()


class TestStart:
    def test_builds_correct_command(self) -> None:
        mgr = EmulatorManager()

        # Mock list_avds to return an available AVD
        with (
            patch.object(
                mgr,
                "list_avds",
                return_value=[EmulatorInfo(avd_name="Pixel_7_API_34", state="stopped")],
            ),
            patch.object(mgr, "_allocated_ports", return_value=set()),
            patch("linc_nerve.core.emulator.subprocess.Popen") as mock_popen,
            patch.object(mgr, "_run") as mock_run,
        ):
            # First _run call: adb devices (wait for appearance)
            # Second _run call: adb shell getprop sys.boot_completed
            mock_run.side_effect = [
                # adb devices — device appeared
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
                # boot_completed
                MagicMock(stdout="1\n"),
            ]

            result = mgr.start("Pixel_7_API_34", timeout=10)

        # Verify Popen was called with correct args
        popen_args = mock_popen.call_args[0][0]
        assert popen_args[0] == "emulator"
        assert "-avd" in popen_args
        assert "Pixel_7_API_34" in popen_args
        assert "-no-audio" in popen_args
        assert "-no-boot-anim" in popen_args
        assert "-port" in popen_args
        assert "5554" in popen_args
        assert "-no-window" not in popen_args
        assert "-snapshot" not in popen_args

        assert result.serial == "emulator-5554"
        assert result.state == "running"
        assert result.port == 5554

    def test_headless_adds_no_window(self) -> None:
        mgr = EmulatorManager()
        with (
            patch.object(
                mgr,
                "list_avds",
                return_value=[EmulatorInfo(avd_name="Pixel_7_API_34", state="stopped")],
            ),
            patch.object(mgr, "_allocated_ports", return_value=set()),
            patch("linc_nerve.core.emulator.subprocess.Popen") as mock_popen,
            patch.object(mgr, "_run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
                MagicMock(stdout="1\n"),
            ]

            mgr.start("Pixel_7_API_34", headless=True, timeout=10)

        popen_args = mock_popen.call_args[0][0]
        assert "-no-window" in popen_args

    def test_with_snapshot(self) -> None:
        mgr = EmulatorManager()
        with (
            patch.object(
                mgr,
                "list_avds",
                return_value=[EmulatorInfo(avd_name="Pixel_7_API_34", state="stopped")],
            ),
            patch.object(mgr, "_allocated_ports", return_value=set()),
            patch("linc_nerve.core.emulator.subprocess.Popen") as mock_popen,
            patch.object(mgr, "_run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
                MagicMock(stdout="1\n"),
            ]

            mgr.start("Pixel_7_API_34", snapshot="clean_state", timeout=10)

        popen_args = mock_popen.call_args[0][0]
        assert "-snapshot" in popen_args
        idx = popen_args.index("-snapshot")
        assert popen_args[idx + 1] == "clean_state"

    def test_rejects_unknown_avd(self) -> None:
        mgr = EmulatorManager()
        with (
            patch.object(
                mgr,
                "list_avds",
                return_value=[EmulatorInfo(avd_name="Pixel_7_API_34", state="stopped")],
            ),
            pytest.raises(ValueError, match="not found"),
        ):
            mgr.start("Nonexistent_AVD")

    def test_skips_start_if_already_running(self) -> None:
        mgr = EmulatorManager()
        running_info = EmulatorInfo(
            avd_name="Pixel_7_API_34",
            serial="emulator-5554",
            port=5554,
            state="running",
        )
        with (
            patch.object(mgr, "list_avds", return_value=[running_info]),
            patch("linc_nerve.core.emulator.subprocess.Popen") as mock_popen,
        ):
            result = mgr.start("Pixel_7_API_34")

        mock_popen.assert_not_called()
        assert result.serial == "emulator-5554"
        assert result.state == "running"


class TestStop:
    def test_sends_emu_kill(self) -> None:
        mgr = EmulatorManager()
        with patch.object(mgr, "_run") as mock_run:
            # emu kill, then adb devices shows it's gone
            mock_run.side_effect = [
                MagicMock(stdout="OK\n"),  # emu kill
                MagicMock(stdout="List of devices attached\n"),  # adb devices — gone
            ]

            result = mgr.stop("emulator-5554")

        assert result is True
        # First call should be emu kill
        first_call = mock_run.call_args_list[0]
        assert first_call[0][0] == [ADB_PATH, "-s", "emulator-5554", "emu", "kill"]

    def test_rejects_non_emulator_serial(self) -> None:
        mgr = EmulatorManager()
        result = mgr.stop("FAKESERIAL0001")
        assert result is False


class TestFindEmulatorPid:
    def test_finds_pid(self) -> None:
        mgr = EmulatorManager()
        with patch.object(mgr, "_run") as mock_run:
            mock_run.return_value = MagicMock(stdout="12345\n")
            pid = mgr._find_emulator_pid("emulator-5554")
        assert pid == 12345

    def test_no_pid_found(self) -> None:
        mgr = EmulatorManager()
        with patch.object(mgr, "_run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            pid = mgr._find_emulator_pid("emulator-5554")
        assert pid is None


class TestPreflightChecks:
    def test_rejects_unknown_avd(self) -> None:
        mgr = EmulatorManager()
        with (
            patch.object(
                mgr,
                "list_avds",
                return_value=[EmulatorInfo(avd_name="Pixel_7_API_34", state="stopped")],
            ),
            pytest.raises(ValueError, match="not found"),
        ):
            mgr._preflight_checks("Nonexistent_AVD")

    def test_returns_running_emulator(self) -> None:
        mgr = EmulatorManager()
        running = EmulatorInfo(
            avd_name="Pixel_7_API_34",
            serial="emulator-5554",
            port=5554,
            state="running",
        )
        with patch.object(mgr, "list_avds", return_value=[running]):
            result = mgr._preflight_checks("Pixel_7_API_34")
        assert isinstance(result, EmulatorInfo)
        assert result.serial == "emulator-5554"

    def test_allocates_first_free_port(self) -> None:
        mgr = EmulatorManager()
        with (
            patch.object(
                mgr,
                "list_avds",
                return_value=[EmulatorInfo(avd_name="Pixel_7_API_34", state="stopped")],
            ),
            patch.object(mgr, "_allocated_ports", return_value={5554}),
        ):
            result = mgr._preflight_checks("Pixel_7_API_34")
        assert result == 5556

    def test_raises_when_no_ports(self) -> None:
        mgr = EmulatorManager()
        all_ports = set(range(_PORT_MIN, _PORT_MAX + 1, 2))
        with (
            patch.object(
                mgr,
                "list_avds",
                return_value=[EmulatorInfo(avd_name="Pixel_7_API_34", state="stopped")],
            ),
            patch.object(mgr, "_allocated_ports", return_value=all_ports),
            pytest.raises(RuntimeError, match="No free emulator ports"),
        ):
            mgr._preflight_checks("Pixel_7_API_34")


class TestLaunchEmulator:
    def test_stores_pid(self) -> None:
        mgr = EmulatorManager()
        with patch("linc_nerve.core.emulator.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=42)
            serial = mgr._launch_emulator("Pixel_7_API_34", 5554, False, None)
        assert serial == "emulator-5554"
        assert mgr._pids["emulator-5554"] == 42

    def test_uses_resolved_emulator_path_not_bare_literal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_launch_emulator` must exec the RESOLVED emulator path (linc-nerve#118),
        not a hard-coded bare "emulator". Fails pre-fix: emulator.py:210 hard-codes
        `cmd = ["emulator", ...]`."""
        monkeypatch.setattr(
            "linc_nerve.core.emulator.resolve_emulator_path",
            lambda: "/opt/homebrew/share/android-commandlinetools/emulator/emulator",
        )
        mgr = EmulatorManager()
        with patch("linc_nerve.core.emulator.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=1)
            mgr._launch_emulator("Pixel_7_API_34", 5554, False, None)
        args = mock_popen.call_args[0][0]
        assert args[0] == "/opt/homebrew/share/android-commandlinetools/emulator/emulator"

    def test_raises_when_emulator_binary_unresolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No emulator binary anywhere -> raise RuntimeError naming the probed
        locations, rather than exec-ing a bare "emulator" and getting an opaque
        FileNotFoundError from deep inside subprocess.Popen."""
        monkeypatch.setattr("linc_nerve.core.emulator.resolve_emulator_path", lambda: None)
        mgr = EmulatorManager()
        with (
            patch("linc_nerve.core.emulator.subprocess.Popen") as mock_popen,
            pytest.raises(RuntimeError, match="emulator binary not found"),
        ):
            mgr._launch_emulator("Pixel_7_API_34", 5554, False, None)
        mock_popen.assert_not_called()

    def test_headless_command(self) -> None:
        mgr = EmulatorManager()
        with patch("linc_nerve.core.emulator.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=1)
            mgr._launch_emulator("Pixel_7_API_34", 5554, True, None)
        args = mock_popen.call_args[0][0]
        assert "-no-window" in args

    def test_snapshot_command(self) -> None:
        mgr = EmulatorManager()
        with patch("linc_nerve.core.emulator.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=1)
            mgr._launch_emulator("Pixel_7_API_34", 5554, False, "snap")
        args = mock_popen.call_args[0][0]
        assert "-snapshot" in args
        idx = args.index("-snapshot")
        assert args[idx + 1] == "snap"


class TestWaitForDevice:
    def test_succeeds(self) -> None:
        mgr = EmulatorManager()
        with patch.object(mgr, "_run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="List of devices attached\nemulator-5554\tdevice\n"
            )
            mgr._wait_for_device("emulator-5554", time.monotonic() + 10)

    def test_timeout(self) -> None:
        mgr = EmulatorManager()
        with patch.object(mgr, "_run") as mock_run:
            mock_run.return_value = MagicMock(stdout="List of devices attached\n")
            with pytest.raises(TimeoutError, match="did not appear"):
                mgr._wait_for_device("emulator-5554", time.monotonic() - 1)


class TestWaitForBoot:
    def test_succeeds(self) -> None:
        mgr = EmulatorManager()
        with patch.object(mgr, "_run") as mock_run:
            mock_run.return_value = MagicMock(stdout="1\n")
            mgr._wait_for_boot("emulator-5554", time.monotonic() + 10, time.monotonic())

    def test_timeout(self) -> None:
        mgr = EmulatorManager()
        with patch.object(mgr, "_run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            with pytest.raises(TimeoutError, match="did not finish booting"):
                mgr._wait_for_boot("emulator-5554", time.monotonic() - 1, time.monotonic())


class TestStopUsesStoredPid:
    def test_uses_stored_pid(self) -> None:
        mgr = EmulatorManager()
        mgr._pids["emulator-5554"] = 999
        with (
            patch.object(mgr, "_run") as mock_run,
            patch.object(mgr, "_find_emulator_pid") as mock_find,
            patch("os.kill") as mock_kill,
        ):
            # emu kill, then adb devices still shows it (forces PID fallback)
            mock_run.side_effect = [
                MagicMock(stdout="OK\n"),  # emu kill
                # adb devices — still present (loop iterations)
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
                MagicMock(stdout="List of devices attached\nemulator-5554\tdevice\n"),
            ]

            result = mgr.stop("emulator-5554")

        assert result is True
        mock_find.assert_not_called()
        mock_kill.assert_called_once()
        assert mock_kill.call_args[0][0] == 999
        assert "emulator-5554" not in mgr._pids
