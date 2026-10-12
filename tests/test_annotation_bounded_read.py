"""Tests for unguarded action-log tap annotation bounded read (linc-nerve-30om).

Acceptance criteria:
- [test] with a fake AndroidController whose `_shell` for "uiautomator dump /dev/tty" raises
  TimeoutError and records (cmd, timeout), an unguarded action-log session +
  `_nerve_tap_sync("S", 10, 10)`: the dump is attempted exactly once with timeout 3,
  `_recover_from_dump_hang` and `_kill_uiautomation_holders` are never called (patched spies),
  the tap's `input tap` still runs, and the log line has target None
  (fails on origin/main: recovery runs).
- [test] non-XML dump output ("Killed") on the quick path raises UiHierarchyUnavailableError
  from `elements(serial, quick_timeout_s=3)` without force-stopping holders.
- [test] pin: a GUARDED session's tap on the same fake still goes through the full read
  (recovery spy called) and is refused as target_unreadable.
- [test] pin: `elements(serial)` with no new keyword calls `_shell` with the default timeout
  and still runs recovery on a hang (existing tests in tests/ for _dump_hierarchy stay green).
- [test] an iOS-shaped reader without the keyword is called without it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from linc_nerve.core import action_log
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import UiHierarchyUnavailableError
from linc_nerve.mcp import _core, input_tools
from linc_nerve.mcp.actionlog_tools import (
    _nerve_actionlog_start_sync,
    _nerve_actionlog_stop_sync,
)


class FakeAndroidController(AndroidController):
    """An AndroidController test double with a controllable _shell seam."""

    def __init__(self) -> None:
        super().__init__()
        self.shell_calls: list[tuple[str, int]] = []
        self.all_cmds: list[str] = []
        self.dump_outputs: list[str] = []
        self.foreground: str = "com.example"

    def get_foreground(self, serial: str) -> dict[str, str]:
        return {"package": self.foreground, "activity": ".MainActivity"}

    def _shell(self, serial: str, cmd: str, timeout: int = 15) -> str:
        self.all_cmds.append(cmd)
        if cmd == "uiautomator dump /dev/tty":
            self.shell_calls.append((cmd, timeout))
            if self.dump_outputs:
                return self.dump_outputs.pop(0)
            raise TimeoutError("device wedged")
        return ""


class FakeIOSController:
    """An iOS-shaped controller whose elements() method has no quick_timeout_s keyword."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def elements(self, serial: str) -> list[dict[str, Any]]:
        self.calls.append(serial)
        return [{"class": "XCUIElementTypeButton", "label": "OK"}]


def _action_entries(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if json.loads(line).get("type") == "action"]


def test_unguarded_tap_annotation_bounded_read_no_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unguarded tap annotation attempts dump once with timeout 3 and no recovery."""
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(tmp_path))
    fake = FakeAndroidController()
    path = tmp_path / "unguarded.jsonl"
    action_log.REGISTRY.reset()

    recover_target = fake._recover_from_dump_hang
    kill_target = fake._kill_uiautomation_holders
    with (
        patch.object(_core, "_resolve_device", return_value=(object(), "android")),
        patch.object(_core, "_get_controller", return_value=fake),
        patch.object(_core, "_check_guard", return_value=None),
        patch.object(fake, "_recover_from_dump_hang", wraps=recover_target) as spy_recover,
        patch.object(fake, "_kill_uiautomation_holders", wraps=kill_target) as spy_kill,
    ):
        started = _nerve_actionlog_start_sync("S", str(path))
        assert started["success"] is True

        result = input_tools._nerve_tap_sync("S", 10, 10)
        assert result == {"success": True, "action": "tap", "x": 10, "y": 10}

        # The dump is attempted exactly once with timeout 3
        assert fake.shell_calls == [("uiautomator dump /dev/tty", 3)]

        # Neither recovery nor holder force-stop was called
        spy_recover.assert_not_called()
        spy_kill.assert_not_called()

        # The tap's input tap still ran
        assert "input tap 10 10" in fake.all_cmds

        # The log line has target None
        entries = _action_entries(path)
        assert len(entries) == 1
        assert entries[0]["target"] is None

        _nerve_actionlog_stop_sync("S")

    action_log.REGISTRY.reset()


def test_quick_path_non_xml_output_raises_without_killing_holders() -> None:
    """Non-XML dump output on quick path raises without force-stopping holders."""
    fake = FakeAndroidController()
    fake.dump_outputs = ["Killed\n"]

    kill_target = fake._kill_uiautomation_holders
    with patch.object(fake, "_kill_uiautomation_holders", wraps=kill_target) as spy_kill:
        with pytest.raises(UiHierarchyUnavailableError) as exc_info:
            fake.elements("S", quick_timeout_s=3)

        assert "non-XML output" in str(exc_info.value) or "Killed" in str(exc_info.value)
        spy_kill.assert_not_called()


def test_pin_guarded_tap_still_runs_recovery_and_refuses_target_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A GUARDED session's tap on the same fake still goes through full read + recovery."""
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(tmp_path))
    fake = FakeAndroidController()
    path = tmp_path / "guarded.jsonl"
    action_log.REGISTRY.reset()

    recover_target = fake._recover_from_dump_hang
    with (
        patch.object(_core, "_resolve_device", return_value=(object(), "android")),
        patch.object(_core, "_get_controller", return_value=fake),
        patch.object(_core, "_check_guard", return_value=None),
        patch.object(fake, "_recover_from_dump_hang", wraps=recover_target) as spy_recover,
    ):
        started = _nerve_actionlog_start_sync("S", str(path), target_package="com.example")
        assert started["success"] is True

        result = input_tools._nerve_tap_sync("S", 10, 10)

        # In guarded mode, recovery spy was called because it does full uncapped read
        spy_recover.assert_called()

        # Refused as target_unreadable
        assert result.get("guard") == "target_unreadable"
        assert result.get("validation_error") is True

        # input tap never ran
        assert "input tap 10 10" not in fake.all_cmds

        _nerve_actionlog_stop_sync("S")

    action_log.REGISTRY.reset()


def test_pin_elements_no_keyword_runs_recovery_on_hang() -> None:
    """elements(serial) with no new keyword calls _shell with default timeout and runs recovery."""
    fake = FakeAndroidController()

    recover_target = fake._recover_from_dump_hang
    with patch.object(fake, "_recover_from_dump_hang", wraps=recover_target) as spy_recover:
        with pytest.raises(UiHierarchyUnavailableError):
            fake.elements("S")

        # Initial dump called with default timeout 15
        assert fake.shell_calls[0] == ("uiautomator dump /dev/tty", 15)
        spy_recover.assert_called()


def test_ios_reader_without_keyword_called_without_it() -> None:
    """An iOS-shaped reader without the quick_timeout_s keyword is called without it."""
    fake_ios = FakeIOSController()

    with (
        patch.object(_core, "_resolve_device", return_value=(object(), "ios")),
        patch.object(_core, "_get_controller", return_value=fake_ios),
    ):
        read = _core._read_tree("S", guarded=False)
        assert isinstance(read, tuple)
        tree, _capped = read
        assert tree == [{"class": "XCUIElementTypeButton", "label": "OK"}]
        assert fake_ios.calls == ["S"]
