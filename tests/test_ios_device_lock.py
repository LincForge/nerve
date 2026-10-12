"""Tests for per-device command locking in IOSController."""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from linc_nerve.core.ios import IOSController


class TestIOSDeviceLock:
    def test_lock_created_per_device(self) -> None:
        ctrl = IOSController()
        lock_a = ctrl._device_lock("SIM_A")
        lock_b = ctrl._device_lock("SIM_B")
        assert lock_a is not lock_b
        # Same device returns same lock
        assert ctrl._device_lock("SIM_A") is lock_a

    def test_same_device_serializes(self) -> None:
        """Two threads targeting the same device should serialize."""
        ctrl = IOSController()
        serial = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"  # Simulator UUID

        order: list[str] = []
        barrier = threading.Barrier(2, timeout=5)

        def mock_run(*args: Any, timeout: Any = 30, check: Any = True) -> Any:
            # Extract a tag from the command args
            tag = args[-1] if args else "?"
            order.append(f"start-{tag}")
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait()
            time.sleep(0.05)
            order.append(f"end-{tag}")
            return MagicMock(stdout="", returncode=0)

        ctrl._run = mock_run  # type: ignore[method-assign]

        def do_type(text: str) -> None:
            ctrl.type_text(serial, text)

        t1 = threading.Thread(target=do_type, args=("hello",))
        t2 = threading.Thread(target=do_type, args=("world",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Because of the lock, one must fully complete before the other starts
        assert len(order) == 4
        start_indices = [i for i, x in enumerate(order) if x.startswith("start")]
        end_indices = [i for i, x in enumerate(order) if x.startswith("end")]
        assert end_indices[0] < start_indices[1]

    def test_different_devices_concurrent(self) -> None:
        """Threads targeting different devices should run concurrently."""
        ctrl = IOSController()
        serial_a = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"
        serial_b = "5D326F10-4BBG-509G-C9BG-GF0G9C4C6288"

        both_started = threading.Event()
        start_count = 0
        count_lock = threading.Lock()

        def mock_run(*args: Any, timeout: Any = 30, check: Any = True) -> Any:
            nonlocal start_count
            with count_lock:
                start_count += 1
                if start_count >= 2:
                    both_started.set()
            both_started.wait(timeout=5)
            return MagicMock(stdout="", returncode=0)

        ctrl._run = mock_run  # type: ignore[method-assign]

        def do_type(serial: str, text: str) -> None:
            ctrl.type_text(serial, text)

        t1 = threading.Thread(target=do_type, args=(serial_a, "hello"))
        t2 = threading.Thread(target=do_type, args=(serial_b, "world"))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert both_started.is_set()

    def test_timeout_raises(self) -> None:
        """Lock timeout should raise TimeoutError."""
        ctrl = IOSController()
        serial = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"

        # Hold the lock in one thread
        lock = ctrl._device_lock(serial)
        lock.acquire()

        try:
            with (
                pytest.raises(TimeoutError, match="Could not acquire device lock"),
                ctrl._device_lock_timeout(serial, timeout=0.1),
            ):
                pass  # Should never reach here
        finally:
            lock.release()
