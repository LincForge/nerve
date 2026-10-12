"""Tests for ADB retry with backoff (IMP-013) and error categories (IMP-010)."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
import structlog.testing

from linc_nerve.core import adb_health
from linc_nerve.core.adb_health import STALE_AFTER_SECONDS, AdbTransportStatus
from linc_nerve.core.adb_path import ADB_PATH
from linc_nerve.core.android import (
    _BACKOFF_BASE,
    _DISTINCT_DEVICE_RESTART,
    _MAX_RETRIES,
    AndroidController,
    DeviceCircuitBreaker,
)
from linc_nerve.core.metrics import (
    CommandMetric,
    ErrorCategory,
    MetricsCollector,
    classify_error,
)


def _make_controller() -> AndroidController:
    """Create a controller with scrcpy disabled for unit tests."""
    ctrl = AndroidController()
    ctrl._scrcpy_available = False
    return ctrl


def _wire_mock(ctrl: AndroidController, mock_device: MagicMock) -> None:
    """Wire a mock device into the controller by patching _get_device."""
    ctrl._get_device = MagicMock(return_value=mock_device)  # type: ignore[method-assign]
    # Also prevent real ADB client creation on reconnect
    ctrl._get_client = MagicMock()  # type: ignore[method-assign]


# --- IMP-010: Error Category Classification ---


class TestErrorClassification:
    def test_timeout_error(self) -> None:
        assert classify_error(TimeoutError("timed out")) == ErrorCategory.TIMEOUT

    def test_connection_reset(self) -> None:
        assert classify_error(ConnectionResetError("reset")) == ErrorCategory.CONNECTION

    def test_os_error_connection(self) -> None:
        assert classify_error(OSError("connection refused")) == ErrorCategory.CONNECTION

    def test_os_error_broken_pipe(self) -> None:
        assert classify_error(OSError("Broken pipe")) == ErrorCategory.CONNECTION

    def test_os_error_timeout(self) -> None:
        assert classify_error(OSError("timed out")) == ErrorCategory.TIMEOUT

    def test_os_error_permission(self) -> None:
        assert classify_error(OSError("Permission denied")) == ErrorCategory.PERMISSION

    def test_os_error_generic(self) -> None:
        # Generic OSError without recognizable keywords → CONNECTION
        assert classify_error(OSError("socket error")) == ErrorCategory.CONNECTION

    def test_device_not_found(self) -> None:
        assert classify_error(RuntimeError("device not found")) == ErrorCategory.DEVICE_NOT_FOUND

    def test_offline_device(self) -> None:
        assert classify_error(RuntimeError("device offline")) == ErrorCategory.DEVICE_NOT_FOUND

    def test_permission_denied(self) -> None:
        assert classify_error(RuntimeError("unauthorized")) == ErrorCategory.PERMISSION

    def test_unknown_error(self) -> None:
        assert classify_error(ValueError("something weird")) == ErrorCategory.UNKNOWN


class TestCommandMetricErrorCategory:
    def test_metric_with_category(self) -> None:
        m = CommandMetric(
            command="input",
            serial="ABC",
            duration_ms=5.0,
            success=False,
            error="timed out",
            error_category=ErrorCategory.TIMEOUT,
        )
        assert m.error_category == ErrorCategory.TIMEOUT

    def test_metric_without_category(self) -> None:
        m = CommandMetric(command="input", serial="ABC", duration_ms=5.0, success=True)
        assert m.error_category is None


class TestStatsErrorBreakdown:
    def test_error_breakdown_in_stats(self) -> None:
        mc = MetricsCollector()
        mc.record(
            CommandMetric(
                command="input",
                serial="A",
                duration_ms=10.0,
                success=False,
                error="timeout",
                error_category=ErrorCategory.TIMEOUT,
            )
        )
        mc.record(
            CommandMetric(
                command="input",
                serial="A",
                duration_ms=10.0,
                success=False,
                error="reset",
                error_category=ErrorCategory.CONNECTION,
            )
        )
        mc.record(
            CommandMetric(
                command="input",
                serial="A",
                duration_ms=10.0,
                success=True,
            )
        )
        stats = mc.stats()
        breakdown = stats["input"]["errors_by_category"]
        assert breakdown["timeout"] == 1
        assert breakdown["connection"] == 1

    def test_no_breakdown_when_all_success(self) -> None:
        mc = MetricsCollector()
        mc.record(
            CommandMetric(
                command="input",
                serial="A",
                duration_ms=10.0,
                success=True,
            )
        )
        stats = mc.stats()
        assert "errors_by_category" not in stats["input"]


# --- IMP-013: Retry with Backoff ---


class TestShellRetry:
    def test_succeeds_on_first_try(self) -> None:
        ctrl = _make_controller()
        mock_device = MagicMock()
        mock_device.shell.return_value = "ok"
        _wire_mock(ctrl, mock_device)

        result = ctrl._shell("ABC", "echo hi")
        assert result == "ok"
        assert mock_device.shell.call_count == 1

    def test_retries_on_timeout(self) -> None:
        ctrl = _make_controller()
        mock_device = MagicMock()
        mock_device.shell.side_effect = [TimeoutError("timed out"), "ok"]
        _wire_mock(ctrl, mock_device)

        with patch("time.sleep"):  # Skip actual backoff
            result = ctrl._shell("ABC", "echo hi")

        assert result == "ok"
        assert mock_device.shell.call_count == 2

    def test_retries_on_os_error(self) -> None:
        ctrl = _make_controller()
        mock_device = MagicMock()
        mock_device.shell.side_effect = [OSError("broken pipe"), "ok"]
        _wire_mock(ctrl, mock_device)

        with patch("time.sleep"):
            result = ctrl._shell("ABC", "echo hi")

        assert result == "ok"
        assert mock_device.shell.call_count == 2

    def test_retries_on_connection_reset(self) -> None:
        ctrl = _make_controller()
        mock_device = MagicMock()
        mock_device.shell.side_effect = [ConnectionResetError("reset"), "ok"]
        _wire_mock(ctrl, mock_device)

        with patch("time.sleep"):
            result = ctrl._shell("ABC", "echo hi")

        assert result == "ok"

    def test_exhausts_retries_then_raises(self) -> None:
        ctrl = _make_controller()
        mock_device = MagicMock()
        # All attempts fail
        mock_device.shell.side_effect = TimeoutError("timed out")
        _wire_mock(ctrl, mock_device)

        with patch("time.sleep"), pytest.raises(TimeoutError):
            ctrl._shell("ABC", "echo hi")

        assert mock_device.shell.call_count == _MAX_RETRIES + 1

    def test_backoff_timing(self) -> None:
        ctrl = _make_controller()
        mock_device = MagicMock()
        mock_device.shell.side_effect = [
            TimeoutError("t1"),
            TimeoutError("t2"),
            "ok",
        ]
        _wire_mock(ctrl, mock_device)

        sleep_calls: list[float] = []
        with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            ctrl._shell("ABC", "echo hi")

        # Backoff: 0.5s, 1.0s
        assert sleep_calls == [_BACKOFF_BASE, _BACKOFF_BASE * 2]

    def test_clears_device_failures_on_success(self) -> None:
        ctrl = _make_controller()
        ctrl._circuit_breakers["ABC"] = DeviceCircuitBreaker(failure_count=2)
        mock_device = MagicMock()
        mock_device.shell.return_value = "ok"
        _wire_mock(ctrl, mock_device)

        ctrl._shell("ABC", "echo hi")
        assert "ABC" not in ctrl._circuit_breakers

    def test_increments_device_failures(self) -> None:
        ctrl = _make_controller()
        mock_device = MagicMock()
        mock_device.shell.side_effect = TimeoutError("t")
        _wire_mock(ctrl, mock_device)

        with patch("time.sleep"), pytest.raises(TimeoutError):
            ctrl._shell("ABC", "echo hi")

        # Each attempt records a failure: total = MAX_RETRIES + 1
        assert ctrl._circuit_breakers["ABC"].failure_count == _MAX_RETRIES + 1

    def test_records_metric_with_error_category(self) -> None:
        ctrl = _make_controller()
        mc = MetricsCollector()
        ctrl._metrics = mc
        mock_device = MagicMock()
        mock_device.shell.side_effect = TimeoutError("timed out")
        _wire_mock(ctrl, mock_device)

        with patch("time.sleep"), pytest.raises(TimeoutError):
            ctrl._shell("ABC", "input tap 100 200")

        # Should record one metric (after all retries exhausted)
        recent = mc.recent(n=10)
        assert len(recent) == 1
        assert recent[0]["success"] is False
        assert recent[0]["error"] == "timed out"

    def test_records_metric_on_success_after_retry(self) -> None:
        ctrl = _make_controller()
        mc = MetricsCollector()
        ctrl._metrics = mc
        mock_device = MagicMock()
        mock_device.shell.side_effect = [TimeoutError("t"), "ok"]
        _wire_mock(ctrl, mock_device)

        with patch("time.sleep"):
            ctrl._shell("ABC", "input tap 100 200")

        recent = mc.recent(n=10)
        assert len(recent) == 1
        assert recent[0]["success"] is True


class TestAdbServerRestart:
    @pytest.fixture(autouse=True)
    def _reset_transport(self) -> Generator[None]:
        adb_health.reset_transport_status()
        yield
        adb_health.reset_transport_status()

    def test_restart_triggered_after_distinct_device_failures(self) -> None:
        """With no wedge evidence recorded, distinct-device failures skip restart."""
        ctrl = _make_controller()
        mock_device = MagicMock()
        mock_device.shell.side_effect = TimeoutError("t")
        _wire_mock(ctrl, mock_device)

        adb_health.reset_transport_status()

        # Fail on N-1 distinct devices — no restart yet
        with (
            patch("time.sleep"),
            patch("subprocess.run") as mock_run,
            structlog.testing.capture_logs() as cap_logs,
        ):
            for i in range(_DISTINCT_DEVICE_RESTART - 1):
                with pytest.raises(TimeoutError):
                    ctrl._shell(f"DEV{i}", "echo hi")
            assert mock_run.call_count == 0

            # Fail on one more distinct device → skips restart because no wedge evidence
            with pytest.raises(TimeoutError):
                ctrl._shell("DEV_FINAL", "echo hi")

            adb_calls = [call.args[0] for call in mock_run.call_args_list]
            assert [ADB_PATH, "start-server"] not in adb_calls
            skipped = [e for e in cap_logs if e.get("event") == "adb_server_restart_skipped"]
            assert len(skipped) == 1
            assert skipped[0]["distinct_devices"] == _DISTINCT_DEVICE_RESTART
            assert skipped[0]["reason"] == "no_wedge_evidence"

    def test_restart_triggered_with_fresh_wedge_evidence(self) -> None:
        """With fresh watchdog wedge evidence, distinct-device failures trigger restart."""
        ctrl = _make_controller()
        mock_device = MagicMock()
        mock_device.shell.side_effect = TimeoutError("t")
        _wire_mock(ctrl, mock_device)

        status = AdbTransportStatus(
            responsive=False,
            wedged=True,
            device_count=None,
            probe_seconds=8.0,
            server_pid=1234,
            server_cpu_percent=99.0,
            detail="busy_spin",
            checked_at=time.time(),
        )
        adb_health.record_transport_status(status)

        with (
            patch("time.sleep"),
            patch.object(ctrl, "_restart_adb_server") as mock_restart,
        ):
            for i in range(_DISTINCT_DEVICE_RESTART - 1):
                with pytest.raises(TimeoutError):
                    ctrl._shell(f"DEV{i}", "echo hi")
            assert mock_restart.call_count == 0

            with pytest.raises(TimeoutError):
                ctrl._shell("DEV_FINAL", "echo hi")

            mock_restart.assert_called_once()

    def test_restart_skipped_when_wedge_evidence_is_stale(self) -> None:
        """A wedged reading older than STALE_AFTER_SECONDS does not trigger restart."""
        ctrl = _make_controller()
        mock_device = MagicMock()
        mock_device.shell.side_effect = TimeoutError("t")
        _wire_mock(ctrl, mock_device)

        status = AdbTransportStatus(
            responsive=False,
            wedged=True,
            device_count=None,
            probe_seconds=8.0,
            server_pid=1234,
            server_cpu_percent=99.0,
            detail="busy_spin",
            checked_at=time.time() - (STALE_AFTER_SECONDS + 5.0),
        )
        adb_health.record_transport_status(status)

        with (
            patch("time.sleep"),
            patch.object(ctrl, "_restart_adb_server") as mock_restart,
            structlog.testing.capture_logs() as cap_logs,
        ):
            for i in range(_DISTINCT_DEVICE_RESTART):
                with pytest.raises(TimeoutError):
                    ctrl._shell(f"DEV{i}", "echo hi")

            assert mock_restart.call_count == 0
            skipped = [e for e in cap_logs if e.get("event") == "adb_server_restart_skipped"]
            assert len(skipped) == 1
            assert skipped[0]["reason"] == "no_wedge_evidence"

    def test_restart_skipped_when_responsive_non_wedged(self) -> None:
        """A responsive (non-wedged) reading does not trigger restart."""
        ctrl = _make_controller()
        mock_device = MagicMock()
        mock_device.shell.side_effect = TimeoutError("t")
        _wire_mock(ctrl, mock_device)

        status = AdbTransportStatus(
            responsive=True,
            wedged=False,
            device_count=2,
            probe_seconds=0.1,
            server_pid=1234,
            server_cpu_percent=0.1,
            detail="ok",
            checked_at=time.time(),
        )
        adb_health.record_transport_status(status)

        with (
            patch("time.sleep"),
            patch.object(ctrl, "_restart_adb_server") as mock_restart,
            structlog.testing.capture_logs() as cap_logs,
        ):
            for i in range(_DISTINCT_DEVICE_RESTART):
                with pytest.raises(TimeoutError):
                    ctrl._shell(f"DEV{i}", "echo hi")

            assert mock_restart.call_count == 0
            skipped = [e for e in cap_logs if e.get("event") == "adb_server_restart_skipped"]
            assert len(skipped) == 1
            assert skipped[0]["reason"] == "no_wedge_evidence"

    def test_recent_failures_cleared_after_skipped_restart(self) -> None:
        """_recent_failures is emptied when restart is skipped so counter does not re-trip."""
        ctrl = _make_controller()
        adb_health.reset_transport_status()

        for i in range(_DISTINCT_DEVICE_RESTART):
            ctrl._record_device_failure(f"DEV{i}")

        assert len(ctrl._recent_failures) == 0

    def test_adb_restart_evidence_predicate(self) -> None:
        """_adb_restart_evidence returns 'wedged' only when status is wedged and fresh."""
        from linc_nerve.core.android_reliability import _adb_restart_evidence

        now = 1000.0
        # None status
        assert _adb_restart_evidence(None, now) is None

        # Responsive / not wedged
        status_ok = AdbTransportStatus(
            responsive=True,
            wedged=False,
            device_count=1,
            probe_seconds=0.1,
            server_pid=123,
            server_cpu_percent=0.0,
            detail="ok",
            checked_at=now,
        )
        assert _adb_restart_evidence(status_ok, now) is None

        # Wedged and fresh
        status_wedged = AdbTransportStatus(
            responsive=False,
            wedged=True,
            device_count=None,
            probe_seconds=8.0,
            server_pid=123,
            server_cpu_percent=99.0,
            detail="busy_spin",
            checked_at=now - (STALE_AFTER_SECONDS - 1.0),
        )
        assert _adb_restart_evidence(status_wedged, now) == "wedged"

        # Wedged exactly at boundary
        status_boundary = AdbTransportStatus(
            responsive=False,
            wedged=True,
            device_count=None,
            probe_seconds=8.0,
            server_pid=123,
            server_cpu_percent=99.0,
            detail="busy_spin",
            checked_at=now - STALE_AFTER_SECONDS,
        )
        assert _adb_restart_evidence(status_boundary, now) == "wedged"

        # Wedged but stale
        status_stale = AdbTransportStatus(
            responsive=False,
            wedged=True,
            device_count=None,
            probe_seconds=8.0,
            server_pid=123,
            server_cpu_percent=99.0,
            detail="busy_spin",
            checked_at=now - (STALE_AFTER_SECONDS + 0.1),
        )
        assert _adb_restart_evidence(status_stale, now) is None

    def test_restart_signals_the_server_pid_never_kill_server(self) -> None:
        """The busy-spin recovery path: SIGTERM/SIGKILL the adb server pid.

        `adb kill-server` is itself an adb client — against a server spinning in
        `fdevent_context_poll::Loop()` it blocks forever, so the old recovery
        could not have repaired the exact outage it existed for (#131).
        """
        from linc_nerve.core import adb_health

        ctrl = _make_controller()
        with (
            patch.object(adb_health, "find_adb_server_pid", return_value=4242),
            patch.object(adb_health, "recover_adb_server", return_value=True) as mock_recover,
        ):
            ctrl._restart_adb_server()

        mock_recover.assert_called_once_with(4242)

    def test_restart_resets_client(self) -> None:
        ctrl = _make_controller()
        old_client = MagicMock()
        ctrl._client = old_client

        with patch("subprocess.run"):
            ctrl._restart_adb_server()

        assert ctrl._client is None

    def test_restart_failure_is_logged_not_raised(self) -> None:
        ctrl = _make_controller()

        with patch("subprocess.run", side_effect=OSError("no adb")):
            # Should not raise
            ctrl._restart_adb_server()


def _install_subprocess(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a CompletedProcess mock for subprocess.run."""
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


class TestInstallSubprocess:
    """install() shells out to `adb install` (streamed pathway).

    Replaces the legacy adbutils push+pm-install path that returned bare
    "FAIL" on Lenovo Android 15 — see improvement a387b40b.
    """

    def test_install_invokes_adb_install_subprocess(self) -> None:
        ctrl = _make_controller()
        with patch("subprocess.run", return_value=_install_subprocess(0, "Success\n")) as run:
            ctrl.install("ABC", "/tmp/app.apk")

        # First call may be the lazy adb-server kick; find the install call.
        install_calls = [c for c in run.call_args_list if c.args and "install" in c.args[0]]
        assert install_calls, f"no install call in {run.call_args_list}"
        cmd = install_calls[0].args[0]
        assert cmd == [ADB_PATH, "-s", "ABC", "install", "-r", "-t", "/tmp/app.apk"]

    def test_install_success_returns_normally(self) -> None:
        ctrl = _make_controller()
        with patch("subprocess.run", return_value=_install_subprocess(0, "Success\n")):
            ctrl.install("ABC", "/tmp/app.apk")  # no exception

    def test_install_surface_full_stderr_on_failure(self) -> None:
        """Bare 'FAIL' must NOT be the user-visible error — full stderr required."""
        ctrl = _make_controller()
        cp = _install_subprocess(
            1,
            stdout="FAIL\n",
            stderr=(
                "adb: failed to install /tmp/app.apk: cmd: Failure calling "
                "service package: Broken pipe"
            ),
        )
        with patch("subprocess.run", return_value=cp), pytest.raises(RuntimeError) as exc_info:
            ctrl.install("ABC", "/tmp/app.apk")

        msg = str(exc_info.value)
        assert "Broken pipe" in msg, f"stderr not surfaced: {msg!r}"
        assert "exit=1" in msg

    def test_install_failure_when_no_success_token(self) -> None:
        """Even with exit code 0, missing 'Success' is a failure."""
        ctrl = _make_controller()
        cp = _install_subprocess(0, stdout="FAIL\n", stderr="")
        with patch("subprocess.run", return_value=cp), pytest.raises(RuntimeError):
            ctrl.install("ABC", "/tmp/app.apk")

    def test_install_retries_on_timeout(self) -> None:
        ctrl = _make_controller()
        good = _install_subprocess(0, "Success\n")
        with (
            patch(
                "subprocess.run",
                side_effect=[subprocess.TimeoutExpired(cmd="adb", timeout=120), good],
            ),
            patch("time.sleep"),
        ):
            ctrl.install("ABC", "/tmp/app.apk")

    def test_install_exhausts_retries(self) -> None:
        import subprocess as _sp

        ctrl = _make_controller()
        with (
            patch(
                "subprocess.run",
                side_effect=_sp.TimeoutExpired(cmd="adb", timeout=120),
            ),
            patch("time.sleep"),
            pytest.raises(TimeoutError),
        ):
            ctrl.install("ABC", "/tmp/app.apk")

    def test_install_records_metric_on_failure(self) -> None:
        ctrl = _make_controller()
        mc = MetricsCollector()
        ctrl._metrics = mc
        cp = _install_subprocess(1, "", "boom")
        with patch("subprocess.run", return_value=cp), pytest.raises(RuntimeError):
            ctrl.install("ABC", "/tmp/app.apk")

        stats = mc.stats()
        assert "install" in stats
        assert stats["install"]["error_rate"] == 1.0
