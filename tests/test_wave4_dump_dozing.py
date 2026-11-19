"""Tests for uiautomator dump null-root-node dozing/asleep recovery (linc-nerve-agr9)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import structlog.testing

from linc_nerve.core import android, ios  # noqa: F401
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import UiHierarchyUnavailableError

from linc_nerve.core import android_ui  # isort: skip

_NULL_ROOT_ERROR = "ERROR: null root node returned by UiTestAutomationBridge."
_VALID_HIERARCHY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<hierarchy rotation="0">'
    '<node resource-id="com.app:id/btn" text="OK" '
    'class="android.widget.Button" bounds="[0,0][100,50]" '
    'clickable="true" enabled="true" checked="false" focused="false" />'
    "</hierarchy>"
    "UI hierchary dumped to: /dev/tty\n"
)


def test_null_root_node_on_dozing_display_wakes_once_and_retries() -> None:
    """First dump returns null root node, dumpsys power reports Dozing, after
    input keyevent 224 the dump returns valid hierarchy XML -> elements are
    returned, uiautomator_dump_recovered is logged with reason='display_woken',
    and exactly one keyevent 224 was sent."""
    ctrl = AndroidController()
    calls: list[str] = []
    timeouts: dict[str, list[int]] = {}

    def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
        calls.append(cmd)
        timeouts.setdefault(cmd, []).append(timeout)
        if cmd == "uiautomator dump /dev/tty":
            woken = "input keyevent 224" in calls
            return _VALID_HIERARCHY if woken else _NULL_ROOT_ERROR
        if cmd == "dumpsys power":
            return "mWakefulness=Dozing\n"
        if cmd.startswith("ps"):
            return "com.android.shell\n"
        return ""

    ctrl._shell = MagicMock(side_effect=fake_shell)

    with structlog.testing.capture_logs() as cap_logs:
        elements = ctrl.elements("SER1")

    assert len(elements) == 1
    assert elements[0]["text"] == "OK"
    assert [c for c in calls if c == "input keyevent 224"] == ["input keyevent 224"]
    assert [c for c in calls if c == "dumpsys power"] == ["dumpsys power"]
    assert timeouts["dumpsys power"] == [android_ui._RECOVERY_AUX_TIMEOUT_S]
    assert timeouts["input keyevent 224"] == [android_ui._RECOVERY_AUX_TIMEOUT_S]
    assert len(timeouts["uiautomator dump /dev/tty"]) == 2
    assert timeouts["uiautomator dump /dev/tty"][1] == android_ui._DUMP_DEFAULT_TIMEOUT_S
    recovered = [e for e in cap_logs if e.get("event") == "uiautomator_dump_recovered"]
    assert len(recovered) == 1
    assert recovered[0]["reason"] == "display_woken"
    assert recovered[0]["serial"] == "SER1"


def test_null_root_node_on_asleep_display_case_insensitive_wakes_and_retries() -> None:
    """Case-insensitive 'NULL ROOT NODE' on an Asleep display also wakes once and retries."""
    ctrl = AndroidController()
    calls: list[str] = []

    def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
        calls.append(cmd)
        if cmd == "uiautomator dump /dev/tty":
            woken = "input keyevent 224" in calls
            return _VALID_HIERARCHY if woken else "Error: Null Root Node from bridge"
        if cmd == "dumpsys power":
            return "Display Power: state=OFF\n"
        return ""

    ctrl._shell = MagicMock(side_effect=fake_shell)

    elements = ctrl.elements("SER1")
    assert len(elements) == 1
    assert [c for c in calls if c == "input keyevent 224"] == ["input keyevent 224"]


def test_null_root_node_when_awake_sends_no_keyevent_and_names_dozing_before_holder() -> None:
    """When wakefulness is Awake, no keyevent is sent and the raised error's
    remedy lists 'the display may be dozing/asleep (wake it: nerve key WAKEUP)'
    before the existing UiAutomation holder text."""
    ctrl = AndroidController()
    calls: list[str] = []

    def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
        calls.append(cmd)
        if cmd == "uiautomator dump /dev/tty":
            return _NULL_ROOT_ERROR
        if cmd == "dumpsys power":
            return "mWakefulness=Awake\n"
        return ""

    ctrl._shell = MagicMock(side_effect=fake_shell)

    with pytest.raises(UiHierarchyUnavailableError) as exc_info:
        ctrl.elements("SER1")

    assert "input keyevent 224" not in calls
    assert len([c for c in calls if c == "uiautomator dump /dev/tty"]) == 1
    msg = str(exc_info.value)
    dozing_phrase = "the display may be dozing/asleep (wake it: nerve key WAKEUP)"
    holder_phrase = (
        "another process may hold the device's single UiAutomation "
        "connection (leaked instrumentation/test driver); stop it or "
        "reboot the device"
    )
    assert dozing_phrase in msg
    assert holder_phrase in msg
    assert msg.index(dozing_phrase) < msg.index("UiAutomation")


def test_null_root_node_when_dozing_but_retry_still_fails_wakes_at_most_once() -> None:
    """Invariant: at most one wake + one retry per dump call; if retry still fails,
    remedy lists dozing/asleep before the holder text."""
    ctrl = AndroidController()
    calls: list[str] = []

    def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
        calls.append(cmd)
        if cmd == "uiautomator dump /dev/tty":
            return _NULL_ROOT_ERROR
        if cmd == "dumpsys power":
            return "mWakefulness=Dozing\n"
        return ""

    ctrl._shell = MagicMock(side_effect=fake_shell)

    with pytest.raises(UiHierarchyUnavailableError) as exc_info:
        ctrl.elements("SER1")

    assert [c for c in calls if c == "input keyevent 224"] == ["input keyevent 224"]
    assert len([c for c in calls if c == "uiautomator dump /dev/tty"]) == 2
    msg = str(exc_info.value)
    assert "the display may be dozing/asleep (wake it: nerve key WAKEUP)" in msg
    assert msg.index("dozing/asleep") < msg.index("UiAutomation")


@pytest.mark.parametrize("fail_cmd", ["input keyevent 224", "retry_dump"])
def test_null_root_node_retry_timeout_and_keyevent_error_convert_to_remedy(
    fail_cmd: str,
) -> None:
    """If the post-wake retry dump or the keyevent itself raises a shell error,
    no exception other than UiHierarchyUnavailableError escapes."""
    ctrl = AndroidController()
    calls: list[str] = []

    def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
        calls.append(cmd)
        if cmd == "uiautomator dump /dev/tty":
            if len([c for c in calls if c == "uiautomator dump /dev/tty"]) == 1:
                return _NULL_ROOT_ERROR
            if fail_cmd == "retry_dump":
                raise TimeoutError("retry timed out")
            return _NULL_ROOT_ERROR
        if cmd == "dumpsys power":
            return "mWakefulness=Dozing\n"
        if cmd == "input keyevent 224" and fail_cmd == "input keyevent 224":
            raise RuntimeError("input failed")
        return ""

    ctrl._shell = MagicMock(side_effect=fake_shell)

    with pytest.raises(UiHierarchyUnavailableError) as exc_info:
        ctrl.elements("SER1")
    assert "dozing/asleep" in str(exc_info.value)


@pytest.mark.parametrize("probe_mode", ["raise", "garbage"])
def test_wakefulness_probe_raising_or_garbage_does_not_wake_or_leak(
    probe_mode: str,
) -> None:
    """Contract: wakefulness probe raising / returning garbage -> no keyevent,
    no exception other than the existing UiHierarchyUnavailableError."""
    ctrl = AndroidController()
    calls: list[str] = []

    def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
        calls.append(cmd)
        if cmd == "uiautomator dump /dev/tty":
            return _NULL_ROOT_ERROR
        if cmd == "dumpsys power":
            if probe_mode == "raise":
                raise RuntimeError("dumpsys power exploded")
            return "completely unreadable garbage output\n"
        return ""

    ctrl._shell = MagicMock(side_effect=fake_shell)

    with pytest.raises(UiHierarchyUnavailableError) as exc_info:
        ctrl.elements("SER1")

    assert "input keyevent 224" not in calls
    assert len([c for c in calls if c == "uiautomator dump /dev/tty"]) == 1
    msg = str(exc_info.value)
    assert "dozing/asleep" in msg
    assert msg.index("dozing/asleep") < msg.index("UiAutomation")


def test_patched_monotonic_past_deadline_skips_wake_and_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: patched monotonic past the deadline -> no wake, no retry."""
    ctrl = AndroidController()
    calls: list[str] = []
    clock = [0.0]

    def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
        calls.append(cmd)
        if cmd == "uiautomator dump /dev/tty":
            clock[0] = android_ui._RECOVERY_DEADLINE_S
            return _NULL_ROOT_ERROR
        if cmd == "dumpsys power":
            return "mWakefulness=Dozing\n"
        return ""

    ctrl._shell = MagicMock(side_effect=fake_shell)
    monkeypatch.setattr(android_ui._android.time, "monotonic", lambda: clock[0])

    with pytest.raises(UiHierarchyUnavailableError) as exc_info:
        ctrl.elements("SER1")

    assert "dumpsys power" not in calls
    assert "input keyevent 224" not in calls
    assert len([c for c in calls if c == "uiautomator dump /dev/tty"]) == 1
    assert "dozing/asleep" in str(exc_info.value)


@pytest.mark.parametrize("jump_on", ["dumpsys power", "input keyevent 224"])
def test_patched_monotonic_past_deadline_during_power_or_wake_skips_remaining_steps(
    monkeypatch: pytest.MonkeyPatch,
    jump_on: str,
) -> None:
    """Deadline spent during dumpsys power skips wake; spent during wake skips retry."""
    ctrl = AndroidController()
    calls: list[str] = []
    clock = [0.0]

    def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
        calls.append(cmd)
        if cmd == jump_on:
            clock[0] = android_ui._RECOVERY_DEADLINE_S
        if cmd == "uiautomator dump /dev/tty":
            return _NULL_ROOT_ERROR
        if cmd == "dumpsys power":
            return "mWakefulness=Dozing\n"
        return ""

    ctrl._shell = MagicMock(side_effect=fake_shell)
    monkeypatch.setattr(android_ui._android.time, "monotonic", lambda: clock[0])

    with pytest.raises(UiHierarchyUnavailableError) as exc_info:
        ctrl.elements("SER1")

    assert "dozing/asleep" in str(exc_info.value)
    if jump_on == "dumpsys power":
        assert "input keyevent 224" not in calls
    else:
        assert "input keyevent 224" in calls
    assert len([c for c in calls if c == "uiautomator dump /dev/tty"]) == 1


def test_post_wake_retry_bare_closing_tag_rejected_without_parse_retries() -> None:
    """A post-wake retry returning bare '</hierarchy>' (end_tag == 0) is treated
    as non-XML (end_tag <= 0) without triggering _parse_hierarchy_with_retry."""
    ctrl = AndroidController()
    calls: list[str] = []

    def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
        calls.append(cmd)
        if cmd == "uiautomator dump /dev/tty":
            return "</hierarchy>" if "input keyevent 224" in calls else _NULL_ROOT_ERROR
        if cmd == "dumpsys power":
            return "mWakefulness=Dozing\n"
        return ""

    ctrl._shell = MagicMock(side_effect=fake_shell)

    with pytest.raises(UiHierarchyUnavailableError) as exc_info:
        ctrl.elements("SER1")

    assert len([c for c in calls if c == "uiautomator dump /dev/tty"]) == 2
    assert "dozing/asleep" in str(exc_info.value)


def test_post_timeout_retry_returning_null_root_node_uses_dozing_remedy() -> None:
    """Wiring site (:545-552): when a hang recovery retry returns null-root-node
    output and no holder was killed, the raised error lists dozing/asleep before
    UiAutomation."""
    ctrl = AndroidController()
    calls: list[str] = []

    def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
        calls.append(cmd)
        if cmd == "uiautomator dump /dev/tty":
            if len([c for c in calls if c == "uiautomator dump /dev/tty"]) == 1:
                raise TimeoutError("adb read timeout")
            return _NULL_ROOT_ERROR
        return ""

    ctrl._shell = MagicMock(side_effect=fake_shell)

    with pytest.raises(UiHierarchyUnavailableError) as exc_info:
        ctrl.elements("SER1")

    msg = str(exc_info.value)
    assert "the display may be dozing/asleep (wake it: nerve key WAKEUP)" in msg
    assert msg.index("dozing/asleep") < msg.index("UiAutomation")


def test_null_root_node_with_force_stopped_holder_skips_wake_and_reports_holder() -> None:
    """When a holder WAS force-stopped and retry still returns null-root-node,
    do not probe wakefulness or send WAKEUP; report the force-stopped holder remedy."""
    ctrl = AndroidController()
    calls: list[str] = []

    def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
        calls.append(cmd)
        if cmd == "uiautomator dump /dev/tty":
            return _NULL_ROOT_ERROR
        if cmd.startswith("ps"):
            return "dev.mobile.maestro\n"
        return ""

    ctrl._shell = MagicMock(side_effect=fake_shell)

    with pytest.raises(UiHierarchyUnavailableError) as exc_info:
        ctrl.elements("SER1")

    assert "dumpsys power" not in calls
    assert "input keyevent 224" not in calls
    assert "force-stopped leaked UiAutomation holder(s)" in str(exc_info.value)
