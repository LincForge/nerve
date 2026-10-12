"""Tests for process existence / liveness predicate (pid_alive)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from linc_nerve.core import adb_health, device_manager
from linc_nerve.core.procs import pid_alive


def test_pid_alive_self() -> None:
    """pid_alive(os.getpid()) is True."""
    assert pid_alive(os.getpid()) is True


def test_pid_alive_dead_subprocess() -> None:
    """A spawned-and-waited subprocess pid is False."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert pid_alive(proc.pid) is False


@pytest.mark.parametrize("pid", [0, -1, -99])
def test_pid_alive_non_positive_does_not_call_kill(monkeypatch, pid: int) -> None:
    """0 and negative pids are False without calling os.kill."""

    def _fail_kill(target_pid: int, sig: int) -> None:
        raise AssertionError(f"os.kill called with pid={target_pid}, sig={sig}")

    monkeypatch.setattr(os, "kill", _fail_kill)
    assert pid_alive(pid) is False


def test_permission_error_means_alive(monkeypatch) -> None:
    """With os.kill monkeypatched to raise PermissionError, pid is alive across all three."""

    def _raise_eperm(pid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", _raise_eperm)
    assert pid_alive(12345) is True
    assert device_manager._pid_alive(12345) is True
    assert adb_health._pid_alive(12345) is True


def test_process_lookup_error_means_dead(monkeypatch) -> None:
    """With os.kill monkeypatched to raise ProcessLookupError, pid is dead across all three."""

    def _raise_esrch(pid: int, sig: int) -> None:
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(os, "kill", _raise_esrch)
    assert pid_alive(12345) is False
    assert device_manager._pid_alive(12345) is False
    assert adb_health._pid_alive(12345) is False


def test_other_oserror_means_dead(monkeypatch) -> None:
    """Any other OSError is treated as False across all three."""

    def _raise_other(pid: int, sig: int) -> None:
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(os, "kill", _raise_other)
    assert pid_alive(12345) is False
    assert device_manager._pid_alive(12345) is False
    assert adb_health._pid_alive(12345) is False
