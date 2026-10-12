from unittest.mock import MagicMock

import pytest

from linc_nerve.core.android import AndroidController


def _ctrl(shell_map, state="device"):
    """Build a controller whose _shell returns canned output by command substring."""
    ctrl = AndroidController()

    def fake_shell(serial, cmd, timeout=15):
        for key, val in shell_map.items():
            if key in cmd:
                return val() if callable(val) else val
        return ""

    ctrl._shell = MagicMock(side_effect=fake_shell)
    ctrl._adb_state = MagicMock(return_value=state)
    return ctrl


def test_already_ready_takes_no_actions():
    ctrl = _ctrl(
        {
            "getprop": "default",
            "dumpsys power": "mWakefulness=Awake",
            "dumpsys window": "mKeyguardShowing=false",
            "dumpsys battery": "level: 90\nscale: 100\nstatus: 2",
        }
    )
    r = ctrl.ensure_ready("S")
    assert r["ready"] is True and r["actions_taken"] == ["extend_timeout", "stayon"]
    assert r["awake"] is True and r["unlocked"] is True and r["battery_pct"] == 90


def test_asleep_device_gets_wakeup():
    reads = iter(["mWakefulness=Asleep", "mWakefulness=Awake"])
    ctrl = _ctrl(
        {
            "getprop": "default",
            "dumpsys power": lambda: next(reads),
            "dumpsys window": "mKeyguardShowing=false",
            "dumpsys battery": "level: 90\nscale: 100\nstatus: 2",
        }
    )
    r = ctrl.ensure_ready("S")
    assert "wakeup" in r["actions_taken"]
    ctrl._shell.assert_any_call("S", "input keyevent WAKEUP")


def test_locked_nonsecure_gets_dismiss_keyguard():
    reads = iter(["mKeyguardShowing=true", "mKeyguardShowing=false"])
    ctrl = _ctrl(
        {
            "getprop": "default",
            "dumpsys power": "mWakefulness=Awake",
            "dumpsys window": lambda: next(reads),
            "dumpsys battery": "level: 90\nscale: 100\nstatus: 2",
        }
    )
    r = ctrl.ensure_ready("S")
    assert "dismiss_keyguard" in r["actions_taken"] and r["unlocked"] is True
    ctrl._shell.assert_any_call("S", "wm dismiss-keyguard")


def test_wear_os_skips_dismiss_keyguard():
    ctrl = _ctrl(
        {
            "getprop": "watch",
            "dumpsys power": "mWakefulness=Awake",
            "dumpsys window": "mKeyguardShowing=true",
            "dumpsys battery": "level: 90\nscale: 100\nstatus: 2",
        }
    )
    r = ctrl.ensure_ready("S")
    assert "dismiss_keyguard" not in r["actions_taken"]
    for call in ctrl._shell.call_args_list:
        assert "wm dismiss-keyguard" not in call.args[1]


def test_dismiss_keyguard_polls_until_settled(monkeypatch):
    # Samsung A12 race: keyguard still showing on the first re-read, clears on the second.
    import linc_nerve.core.android as andmod

    monkeypatch.setattr(andmod.time, "sleep", lambda *_: None)
    reads = iter(
        [
            "isKeyguardShowing=true",  # initial
            "isKeyguardShowing=true",  # settle read 1 — still locked
            "isKeyguardShowing=false",  # settle read 2 — cleared
        ]
    )
    ctrl = _ctrl(
        {
            "getprop": "default",
            "dumpsys power": "mWakefulness=Awake",
            "dumpsys window": lambda: next(reads),
            "dumpsys battery": "level: 90\nscale: 100\nstatus: 2",
        }
    )
    r = ctrl.ensure_ready("S")
    assert r["unlocked"] is True and "dismiss_keyguard" in r["actions_taken"]


def test_adb_unauthorized_short_circuits():
    ctrl = _ctrl({}, state="unauthorized")
    r = ctrl.ensure_ready("S")
    assert r["ready"] is False and r["reason"] == "adb_unauthorized"
    ctrl._shell.assert_not_called()


def test_start_recording_refuses_when_not_ready():
    ctrl = AndroidController()
    ctrl.ensure_ready = MagicMock(return_value={"ready": False, "reason": "asleep (wake failed)"})
    with pytest.raises(RuntimeError, match="not ready"):
        ctrl.start_recording("S")


def test_start_recording_proceeds_when_ready(monkeypatch):
    ctrl = AndroidController()
    ctrl.ensure_ready = MagicMock(return_value={"ready": True, "reason": "ready"})
    monkeypatch.setattr("linc_nerve.core.android.subprocess.Popen", lambda *a, **k: MagicMock())
    monkeypatch.setattr("linc_nerve.core.android.time.sleep", lambda *_: None)
    ctrl.start_recording("S")
    ctrl.ensure_ready.assert_called_once_with("S")
    assert "S" in ctrl._recording_processes


def test_ready_path_sets_stayon():
    ctrl = _ctrl(
        {
            "getprop": "default",
            "dumpsys power": "mWakefulness=Awake",
            "dumpsys window": "mKeyguardShowing=false",
            "dumpsys battery": "level: 90\nscale: 100\nstatus: 2",
        }
    )
    r = ctrl.ensure_ready("S")
    assert "stayon" in r["actions_taken"]
    ctrl._shell.assert_any_call("S", "svc power stayon true")


def test_screen_size_parses_wm_size():
    ctrl = _ctrl({"wm size": "Physical size: 1080x2280"})
    assert ctrl._screen_size("S") == (1080, 2280)


def test_screen_size_none_on_garbage():
    ctrl = _ctrl({"wm size": "error"})
    assert ctrl._screen_size("S") is None


def test_swipe_fallback_when_dismiss_keyguard_insufficient(monkeypatch):
    # Galaxy S10 case: wm dismiss-keyguard does NOT clear; the swipe does.
    import linc_nerve.core.android as andmod

    monkeypatch.setattr(andmod.time, "sleep", lambda *_: None)
    swiped: list[int] = []

    def fake_shell(serial, cmd, timeout=15):
        if "input touchscreen swipe" in cmd:
            swiped.append(1)
            return ""
        if "wm size" in cmd:
            return "Physical size: 1080x2280"
        if "getprop" in cmd:
            return "default"
        if "dumpsys power" in cmd:
            return "mWakefulness=Awake"
        if "dumpsys window" in cmd:
            return "isKeyguardShowing=false" if swiped else "isKeyguardShowing=true"
        if "dumpsys battery" in cmd:
            return "level: 90\nscale: 100\nstatus: 2"
        return ""

    ctrl = AndroidController()
    ctrl._shell = MagicMock(side_effect=fake_shell)
    ctrl._adb_state = MagicMock(return_value="device")
    r = ctrl.ensure_ready("S")
    assert r["ready"] is True and r["unlocked"] is True
    assert "swipe_dismiss" in r["actions_taken"]
    ctrl._shell.assert_any_call("S", "input touchscreen swipe 540 1938 540 342 300")


def test_swipe_fallback_both_fail_reports_locked(monkeypatch):
    import linc_nerve.core.android as andmod

    monkeypatch.setattr(andmod.time, "sleep", lambda *_: None)
    ctrl = _ctrl(
        {
            "getprop": "default",
            "dumpsys power": "mWakefulness=Awake",
            "wm size": "Physical size: 1080x2280",
            "dumpsys window": "isKeyguardShowing=true",  # never clears
            "dumpsys battery": "level: 90\nscale: 100\nstatus: 2",
        }
    )
    r = ctrl.ensure_ready("S")
    assert r["ready"] is False and r["reason"] == "locked (keyguard up)"
    assert "swipe_dismiss" in r["actions_taken"]  # swipe WAS attempted
    ctrl._shell.assert_any_call("S", "input touchscreen swipe 540 1938 540 342 300")


def test_dismiss_keyguard_success_skips_swipe(monkeypatch):
    import linc_nerve.core.android as andmod

    monkeypatch.setattr(andmod.time, "sleep", lambda *_: None)
    reads = iter(["isKeyguardShowing=true", "isKeyguardShowing=false"])
    ctrl = _ctrl(
        {
            "getprop": "default",
            "dumpsys power": "mWakefulness=Awake",
            "wm size": "Physical size: 1080x2280",
            "dumpsys window": lambda: next(reads),
            "dumpsys battery": "level: 90\nscale: 100\nstatus: 2",
        }
    )
    r = ctrl.ensure_ready("S")
    assert r["unlocked"] is True and "swipe_dismiss" not in r["actions_taken"]
    for call in ctrl._shell.call_args_list:
        assert "input touchscreen swipe" not in call.args[1]


def test_swipe_skipped_when_screen_size_unknown(monkeypatch):
    import linc_nerve.core.android as andmod

    monkeypatch.setattr(andmod.time, "sleep", lambda *_: None)
    ctrl = _ctrl(
        {
            "getprop": "default",
            "dumpsys power": "mWakefulness=Awake",
            "wm size": "error: could not get size",  # unparseable -> None
            "dumpsys window": "isKeyguardShowing=true",  # never clears
            "dumpsys battery": "level: 90\nscale: 100\nstatus: 2",
        }
    )
    r = ctrl.ensure_ready("S")
    assert r["ready"] is False  # no crash, honest verdict
    for call in ctrl._shell.call_args_list:
        assert "input touchscreen swipe" not in call.args[1]


# --- UiAutomation leak gate (44d68b62) ---
# A UiAutomation registration left behind by a killed instrumentation hangs
# every subsequent `am instrument` and gets `uiautomator dump` SIGKILLed.
# ensure_ready must surface it (self-healing killable holders) instead of
# letting BVT callers discover it as an unexplained hang.

_READY_MAP = {
    "getprop": "default",
    "dumpsys power": "mWakefulness=Awake",
    "dumpsys window": "mKeyguardShowing=false",
    "dumpsys battery": "level: 90\nscale: 100\nstatus: 2",
}
_UIA_LINE = "Ui Automation[eventTypes=TYPES_ALL_MASK, notificationTimeout=0]"


def test_leak_with_live_holder_self_heals_and_stays_ready():
    acc_reads = iter([_UIA_LINE, "Registered clients:{}"])  # leaked -> clean after heal
    ctrl = _ctrl(
        {
            **_READY_MAP,
            "dumpsys accessibility": lambda: next(acc_reads),
            "ps -A": "NAME\ndev.mobile.maestro\n",
        }
    )
    r = ctrl.ensure_ready("S")
    assert r["ready"] is True
    assert "uiautomation_heal:dev.mobile.maestro" in r["actions_taken"]
    ctrl._shell.assert_any_call("S", "am force-stop dev.mobile.maestro")


def test_leak_with_no_killable_holder_reports_reboot():
    ctrl = _ctrl(
        {
            **_READY_MAP,
            "dumpsys accessibility": _UIA_LINE,
            "ps -A": "NAME\ncom.android.systemui\n",  # no allowlisted holder alive
        }
    )
    r = ctrl.ensure_ready("S")
    assert r["ready"] is False
    assert r["reason"].startswith("uiautomation_leaked")
    assert "reboot" in r["reason"]
    # must not have set stayon on a device it declared not ready
    for call in ctrl._shell.call_args_list:
        assert "svc power stayon" not in call.args[1]


def test_leak_persisting_after_heal_reports_reboot_with_stopped_pkgs():
    ctrl = _ctrl(
        {
            **_READY_MAP,
            "dumpsys accessibility": _UIA_LINE,  # never clears
            "ps -A": "NAME\ndev.mobile.maestro\n",
        }
    )
    r = ctrl.ensure_ready("S")
    assert r["ready"] is False
    assert r["reason"].startswith("uiautomation_leaked")
    assert "dev.mobile.maestro" in r["reason"] and "reboot" in r["reason"]


def test_clean_device_skips_leak_recovery():
    ctrl = _ctrl({**_READY_MAP, "dumpsys accessibility": "Registered clients:{}"})
    r = ctrl.ensure_ready("S")
    assert r["ready"] is True
    for call in ctrl._shell.call_args_list:
        assert "am force-stop" not in call.args[1]


def test_leak_check_skipped_when_device_not_ready_anyway():
    # An asleep/locked device already has an actionable reason; the leak gate
    # must not spend a dumpsys on it (and must not mask that reason).
    ctrl = _ctrl(
        {
            "getprop": "default",
            "dumpsys power": "mWakefulness=Asleep",  # never wakes
            "dumpsys window": "mKeyguardShowing=false",
            "dumpsys battery": "level: 90\nscale: 100\nstatus: 2",
            "dumpsys accessibility": _UIA_LINE,
        }
    )
    r = ctrl.ensure_ready("S")
    assert r["ready"] is False and "asleep" in r["reason"]
    for call in ctrl._shell.call_args_list:
        assert "dumpsys accessibility" not in call.args[1]
