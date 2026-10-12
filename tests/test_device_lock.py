"""Tests for per-device command locking in AndroidController."""

from __future__ import annotations

import contextlib
import threading
import time
from unittest.mock import MagicMock

import pytest

from linc_nerve.core.android import AndroidController


class TestDeviceLock:
    def test_lock_created_per_device(self) -> None:
        ctrl = AndroidController()
        lock_a = ctrl._device_lock("DEVICE_A")
        lock_b = ctrl._device_lock("DEVICE_B")
        assert lock_a is not lock_b
        # Same device returns same lock
        assert ctrl._device_lock("DEVICE_A") is lock_a

    def test_lock_prevents_concurrent_shell(self) -> None:
        """Two threads targeting the same device should serialize."""
        ctrl = AndroidController()
        ctrl._scrcpy_available = False

        mock_device = MagicMock()
        order: list[str] = []
        barrier = threading.Barrier(2, timeout=5)

        def slow_shell(cmd: str, timeout: int = 15) -> str:
            name = cmd.split()[-1]  # "input tap 1" -> "1"
            order.append(f"start-{name}")
            # Signal the other thread so they race for the lock
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait()
            time.sleep(0.05)
            order.append(f"end-{name}")
            return ""

        mock_device.shell = slow_shell
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        t1 = threading.Thread(target=ctrl._shell, args=("DEV", "input tap 1"))
        t2 = threading.Thread(target=ctrl._shell, args=("DEV", "input tap 2"))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Because of the lock, one must fully complete before the other starts
        # The order should be: start-X, end-X, start-Y, end-Y
        assert len(order) == 4
        # Verify serialization: the second command starts after the first ends
        start_indices = [i for i, x in enumerate(order) if x.startswith("start")]
        end_indices = [i for i, x in enumerate(order) if x.startswith("end")]
        # First end must come before second start
        assert end_indices[0] < start_indices[1]

    def test_different_devices_not_blocked(self) -> None:
        """Threads targeting different devices should run concurrently."""
        ctrl = AndroidController()
        ctrl._scrcpy_available = False

        threading.Event()
        both_started = threading.Event()
        start_count = 0
        lock = threading.Lock()

        def shell_track(cmd: str, timeout: int = 15) -> str:
            nonlocal start_count
            with lock:
                start_count += 1
                if start_count >= 2:
                    both_started.set()
            # Wait until both threads have started (proves concurrency)
            both_started.wait(timeout=5)
            return ""

        mock_dev_a = MagicMock()
        mock_dev_a.shell = shell_track
        mock_dev_b = MagicMock()
        mock_dev_b.shell = shell_track

        mock_client = MagicMock()
        mock_client.device.side_effect = lambda serial: (
            mock_dev_a if serial == "DEV_A" else mock_dev_b
        )
        ctrl._client = mock_client

        t1 = threading.Thread(target=ctrl._shell, args=("DEV_A", "input tap 1"))
        t2 = threading.Thread(target=ctrl._shell, args=("DEV_B", "input tap 2"))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Both should have started — proves no cross-device blocking
        assert both_started.is_set()


class TestHardTimeout:
    def test_shell_hard_timeout_kills_hung_command(self) -> None:
        """_shell() should raise TimeoutError even when d.shell() ignores timeout."""
        ctrl = AndroidController()
        ctrl._scrcpy_available = False

        def hanging_shell(cmd: str, timeout: int = 15) -> str:
            # Simulate adbutils ignoring timeout and blocking forever
            time.sleep(60)
            return "should never return"

        mock_device = MagicMock()
        mock_device.shell = hanging_shell

        # Patch _get_device to always return our mock (survives client reconnects)
        ctrl._get_device = MagicMock(return_value=mock_device)  # type: ignore[method-assign]

        import pytest

        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            ctrl._shell("DEV", "uiautomator dump /dev/tty", timeout=1)
        elapsed = time.monotonic() - t0
        # Should complete within ~3s x 3 attempts = ~9s, not 60s
        # With timeout=1, hard limit is 3s per attempt, 3 attempts = ~9s + backoff
        assert elapsed < 15, f"Hard timeout took {elapsed:.1f}s — should be <15s"

    def test_shell_hard_timeout_returns_on_success(self) -> None:
        """Normal d.shell() calls should work fine through hard timeout."""
        ctrl = AndroidController()
        ctrl._scrcpy_available = False

        mock_device = MagicMock()
        mock_device.shell.return_value = "output"
        ctrl._get_device = MagicMock(return_value=mock_device)  # type: ignore[method-assign]

        result = ctrl._shell("DEV", "getprop ro.product.model", timeout=5)
        assert result == "output"

    def test_shell_hard_timeout_propagates_errors(self) -> None:
        """Non-timeout errors from d.shell() should propagate normally."""
        ctrl = AndroidController()
        ctrl._scrcpy_available = False

        mock_device = MagicMock()
        mock_device.shell.side_effect = OSError("Connection refused")
        ctrl._get_device = MagicMock(return_value=mock_device)  # type: ignore[method-assign]

        import pytest

        with pytest.raises(OSError, match="Connection refused"):
            ctrl._shell("DEV", "getprop ro.product.model", timeout=5)


class TestHardTimeoutHelper:
    """Tests for the unified _hard_timeout daemon-thread wrapper."""

    def test_hard_timeout_returns_result(self) -> None:
        ctrl = AndroidController()
        result = ctrl._hard_timeout(lambda: 42, timeout=5, label="test")
        assert result == 42

    def test_hard_timeout_raises_on_hang(self) -> None:
        import pytest

        ctrl = AndroidController()

        def hang() -> str:
            time.sleep(60)
            return "nope"

        t0 = time.monotonic()
        with pytest.raises(TimeoutError, match="hard-timed out"):
            ctrl._hard_timeout(hang, timeout=1, label="hang-test")
        assert time.monotonic() - t0 < 3

    def test_hard_timeout_propagates_exception(self) -> None:
        import pytest

        ctrl = AndroidController()

        def boom() -> None:
            raise ValueError("kaboom")

        with pytest.raises(ValueError, match="kaboom"):
            ctrl._hard_timeout(boom, timeout=5, label="boom-test")

    def test_hard_timeout_with_args_kwargs(self) -> None:
        ctrl = AndroidController()

        def adder(a: int, b: int, extra: int = 0) -> int:
            return a + b + extra

        result = ctrl._hard_timeout(
            adder, args=(1, 2), kwargs={"extra": 10}, timeout=5, label="args-test"
        )
        assert result == 13

    def test_screencap_no_deadlock_on_timeout(self) -> None:
        """_screencap_screenshot must not deadlock when d.screenshot() hangs.

        Regression test for the ThreadPoolExecutor shutdown(wait=True) bug:
        when d.screenshot() hangs, the context manager exit blocks forever
        while holding the device lock.
        """

        ctrl = AndroidController()
        ctrl._scrcpy_available = False

        mock_device = MagicMock()
        mock_device.screenshot.side_effect = lambda: time.sleep(60)
        ctrl._get_device = MagicMock(return_value=mock_device)  # type: ignore[method-assign]

        # _raw_screencap fallback — simulate it returning data
        ctrl._raw_screencap = MagicMock(return_value=b"\x89PNG" + b"\x00" * 30000)  # type: ignore[method-assign]

        t0 = time.monotonic()
        data = ctrl._screencap_screenshot("DEV")
        elapsed = time.monotonic() - t0

        # Must complete via fallback, not hang on shutdown(wait=True)
        assert elapsed < 20, f"Took {elapsed:.1f}s — likely deadlocked"
        assert data.startswith(b"\x89PNG")
        ctrl._raw_screencap.assert_called_once_with("DEV")


class TestSocketTimeout:
    def test_client_created_with_socket_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AdbClient should be initialized with socket_timeout=10 (pin: default server)."""
        monkeypatch.delenv("ANDROID_ADB_SERVER_PORT", raising=False)
        monkeypatch.delenv("ADB_SERVER_SOCKET", raising=False)
        ctrl = AndroidController()
        import unittest.mock

        with unittest.mock.patch("adbutils.AdbClient") as mock_cls:
            ctrl._get_client()
            mock_cls.assert_called_once_with(host="127.0.0.1", port=5037, socket_timeout=10)

    def test_client_honours_android_adb_server_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ANDROID_ADB_SERVER_PORT points the persistent client at another server (7aaba3c3)."""
        monkeypatch.delenv("ADB_SERVER_SOCKET", raising=False)
        monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5099")
        ctrl = AndroidController()
        import unittest.mock

        with unittest.mock.patch("adbutils.AdbClient") as mock_cls:
            ctrl._get_client()
            mock_cls.assert_called_once_with(host="127.0.0.1", port=5099, socket_timeout=10)

    def test_client_honours_adb_server_socket_over_the_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5099")
        monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:localhost:5098")
        ctrl = AndroidController()
        import unittest.mock

        with unittest.mock.patch("adbutils.AdbClient") as mock_cls:
            ctrl._get_client()
            mock_cls.assert_called_once_with(host="localhost", port=5098, socket_timeout=10)
