"""Tests for nerve_device_state — device power/idle/standby control.

Pure helpers (validation, command mapping, dumpsys parsing) are tested directly;
the sync dispatcher is tested against a fake controller that records shell calls
and returns scripted dumpsys output.
"""

from __future__ import annotations

import pytest

from linc_nerve.mcp import device_state_tools as ds

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidatePackage:
    @pytest.mark.parametrize(
        "pkg",
        ["com.lincforge.bench", "a", "com.foo.bar_baz", "A.b.C0"],
    )
    def test_accepts_valid_package(self, pkg: str) -> None:
        assert ds._validate_package(pkg) is None

    @pytest.mark.parametrize(
        "pkg",
        [
            "",
            "a;rm -rf /",
            "com.foo && reboot",
            "pkg name",
            "pkg$(whoami)",
            "pkg`id`",
            "pkg|cat",
            "../etc",
        ],
    )
    def test_rejects_injection_and_empty(self, pkg: str) -> None:
        err = ds._validate_package(pkg)
        assert err is not None
        assert "package" in err.lower()


class TestValidateBucket:
    @pytest.mark.parametrize(
        "bucket",
        ["active", "working_set", "frequent", "rare", "restricted"],
    )
    def test_accepts_enum(self, bucket: str) -> None:
        assert ds._validate_bucket(bucket) is None

    @pytest.mark.parametrize("bucket", ["", "RARE", "never", "5; reboot"])
    def test_rejects_non_enum(self, bucket: str) -> None:
        assert ds._validate_bucket(bucket) is not None


# ---------------------------------------------------------------------------
# Command mapping — exact fixed strings, no free-form
# ---------------------------------------------------------------------------


class TestCommandFor:
    def test_static_actions(self) -> None:
        assert ds._command_for("doze_force", "", "") == "dumpsys deviceidle force-idle"
        assert ds._command_for("doze_unforce", "", "") == "dumpsys deviceidle unforce"
        assert ds._command_for("doze_status", "", "") == "dumpsys deviceidle get deep"
        assert ds._command_for("battery_unplug", "", "") == "dumpsys battery unplug"
        assert ds._command_for("battery_reset", "", "") == "dumpsys battery reset"
        assert ds._command_for("power_save_on", "", "") == "settings put global low_power 1"
        assert ds._command_for("power_save_off", "", "") == "settings put global low_power 0"
        assert ds._command_for("power_save_status", "", "") == "settings get global low_power"
        assert ds._command_for("screen_on", "", "") == "input keyevent 224"
        assert ds._command_for("screen_off", "", "") == "input keyevent 223"
        assert ds._command_for("screen_status", "", "") == "dumpsys power | grep 'mWakefulness='"

    def test_package_actions_interpolate_validated_package(self) -> None:
        assert (
            ds._command_for("whitelist_remove", "com.x.y", "")
            == "dumpsys deviceidle whitelist -com.x.y"
        )
        assert ds._command_for("whitelist_check", "com.x.y", "") == "dumpsys deviceidle whitelist"
        assert ds._command_for("standby_get", "com.x.y", "") == "am get-standby-bucket com.x.y"

    def test_standby_set_interpolates_package_and_bucket(self) -> None:
        assert (
            ds._command_for("standby_set", "com.x.y", "rare")
            == "am set-standby-bucket com.x.y rare"
        )

    def test_unknown_action_raises(self) -> None:
        with pytest.raises(ValueError):
            ds._command_for("rm_rf", "", "")


# ---------------------------------------------------------------------------
# Parsers — return (state, parsed_ok); never coerce a miss to a default
# ---------------------------------------------------------------------------


class TestParseDoze:
    def test_idle(self) -> None:
        assert ds._parse_doze_status("IDLE") == ("idle", True)

    def test_active(self) -> None:
        assert ds._parse_doze_status("ACTIVE\n") == ("active", True)

    def test_stepped_states(self) -> None:
        assert ds._parse_doze_status("IDLE_MAINTENANCE")[0] == "idle_maintenance"
        assert ds._parse_doze_status("IDLE_MAINTENANCE")[1] is True

    def test_unparseable(self) -> None:
        _state, ok = ds._parse_doze_status("Error: unknown command")
        assert ok is False


class TestParsePowerSave:
    def test_on(self) -> None:
        assert ds._parse_power_save("1") == (True, True)

    def test_off(self) -> None:
        assert ds._parse_power_save("0\n") == (False, True)

    def test_null_is_off(self) -> None:
        # `settings get` returns literal "null" when unset — that means low_power off.
        assert ds._parse_power_save("null") == (False, True)

    def test_unparseable(self) -> None:
        assert ds._parse_power_save("garbage")[1] is False


class TestParseStandby:
    def test_numeric_bucket(self) -> None:
        # am get-standby-bucket returns the integer bucket value.
        assert ds._parse_standby("40") == ("rare", True)
        assert ds._parse_standby("10") == ("active", True)
        assert ds._parse_standby("45") == ("restricted", True)

    def test_unparseable(self) -> None:
        assert ds._parse_standby("wat")[1] is False


class TestParseWhitelist:
    SAMPLE = "com.android.foo\ncom.google.bar\ncom.lincforge.bench\n"

    def test_present(self) -> None:
        present, ok = ds._parse_whitelist(self.SAMPLE, "com.lincforge.bench")
        assert present is True
        assert ok is True

    def test_absent(self) -> None:
        present, ok = ds._parse_whitelist(self.SAMPLE, "com.other.app")
        assert present is False
        assert ok is True


# ---------------------------------------------------------------------------
# Sync dispatcher — validation gate + shell call + parsed result
# ---------------------------------------------------------------------------


class FakeController:
    """Records _shell calls; returns scripted outputs keyed by command substring."""

    def __init__(self, scripted: dict[str, str] | None = None) -> None:
        self.calls: list[str] = []
        self.scripted = scripted or {}

    def _shell(self, serial: str, cmd: str, timeout: int = 15) -> str:
        self.calls.append(cmd)
        for needle, out in self.scripted.items():
            if needle in cmd:
                return out
        return ""


@pytest.fixture
def patched(monkeypatch):
    """Patch _core resolution helpers so the dispatcher runs without a real device."""
    fake = FakeController()

    def _resolve(serial):
        return object(), "android"

    monkeypatch.setattr(ds._core, "_resolve_device", _resolve)
    monkeypatch.setattr(ds._core, "_check_guard", lambda serial: None)
    monkeypatch.setattr(ds._core, "_get_controller", lambda platform: fake)
    return fake


class TestSyncDispatcher:
    def test_rejects_bad_package_before_any_shell(self, patched) -> None:
        res = ds._nerve_device_state_sync("emu", "standby_set", package="a;rm", bucket="rare")
        assert "error" in res
        assert res.get("validation_error") is True
        assert patched.calls == []  # never touched the device

    def test_rejects_bad_bucket(self, patched) -> None:
        res = ds._nerve_device_state_sync("emu", "standby_set", package="com.x", bucket="nope")
        assert "error" in res
        assert patched.calls == []

    def test_doze_force_runs_command(self, patched) -> None:
        res = ds._nerve_device_state_sync("emu", "doze_force")
        assert res["success"] is True
        assert res["action"] == "doze_force"
        assert "dumpsys deviceidle force-idle" in patched.calls

    def test_doze_status_parses_state(self, monkeypatch) -> None:
        fake = FakeController({"deviceidle get deep": "IDLE\n"})
        monkeypatch.setattr(ds._core, "_resolve_device", lambda s: (object(), "android"))
        monkeypatch.setattr(ds._core, "_check_guard", lambda s: None)
        monkeypatch.setattr(ds._core, "_get_controller", lambda p: fake)
        res = ds._nerve_device_state_sync("emu", "doze_status")
        assert res["success"] is True
        assert res["state"] == "idle"
        assert res["parsed_ok"] is True

    def test_unparseable_status_sets_parsed_ok_false(self, monkeypatch) -> None:
        fake = FakeController({"deviceidle get deep": "Error"})
        monkeypatch.setattr(ds._core, "_resolve_device", lambda s: (object(), "android"))
        monkeypatch.setattr(ds._core, "_check_guard", lambda s: None)
        monkeypatch.setattr(ds._core, "_get_controller", lambda p: fake)
        res = ds._nerve_device_state_sync("emu", "doze_status")
        assert res["parsed_ok"] is False

    def test_standby_set_requires_package(self, patched) -> None:
        res = ds._nerve_device_state_sync("emu", "standby_set", bucket="rare")
        assert "error" in res
        assert patched.calls == []

    def test_ios_is_rejected(self, monkeypatch) -> None:
        monkeypatch.setattr(ds._core, "_resolve_device", lambda s: (object(), "ios"))
        monkeypatch.setattr(ds._core, "_check_guard", lambda s: None)
        res = ds._nerve_device_state_sync("iphone", "doze_force")
        assert "error" in res
        assert "android" in res["error"].lower()

    def test_reset_is_composite(self, patched) -> None:
        res = ds._nerve_device_state_sync("emu", "reset")
        assert res["success"] is True
        # reset must undo battery, doze, power-save, and restore screen.
        joined = " || ".join(patched.calls)
        assert "dumpsys battery reset" in joined
        assert "dumpsys deviceidle unforce" in joined
        assert "settings put global low_power 0" in joined

    def test_unknown_device_returns_error(self, monkeypatch) -> None:
        monkeypatch.setattr(ds._core, "_resolve_device", lambda s: (None, "Device 'x' not found."))
        res = ds._nerve_device_state_sync("x", "doze_force")
        assert "error" in res
