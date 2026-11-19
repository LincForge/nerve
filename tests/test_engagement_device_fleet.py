"""Contract and unit tests for device-fleet.yaml personal-device refusal and
fail-closed engagement device gating (bead linc-nerve-qu9a).
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from linc_nerve.cli import doctor as doctor_mod
from linc_nerve.cli.doctor import doctor, gather_probes, run_checks
from linc_nerve.core import engagement
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.ios import IOSController
from linc_nerve.mcp import _core
from linc_nerve.mcp import server as mcp_mod
from linc_nerve.mcp.discovery_tools import _nerve_discover_sync
from linc_nerve.mcp.recording_tools import _nerve_record_start_sync
from linc_nerve.mcp.reservation_tools import _nerve_reserve_sync
from linc_nerve.mcp.screenshot_tools import _nerve_screenshot_sync

PERSONAL_SERIAL = "SYNTH-PERSONAL-001"
CLEARED_SERIAL = "SYNTH-LAB-002"
IOS_UDID = "00008110-0011223344556677"
WIFI_HW_PERSONAL = "SYNTHWIFI003"
# mDNS adb-over-wifi transport name: adb-<ro.serialno>-<suffix>._adb-tls-connect._tcp
WIFI_TRANSPORT = f"adb-{WIFI_HW_PERSONAL}-AbCdEf._adb-tls-connect._tcp"


@pytest.fixture
def isolated_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolate HOME, LINC_HOME, TMPDIR, env vars, and mcp_mod singletons."""
    home = tmp_path / "home"
    linc = tmp_path / "linc"
    sys_tmp = tmp_path / "tmp"
    vault = tmp_path / "vault"
    for p in (home, linc, sys_tmp, vault):
        p.mkdir(mode=0o700)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LINC_HOME", str(linc))
    monkeypatch.setenv("TMPDIR", str(sys_tmp))
    for var in (
        "NERVE_ENGAGEMENT",
        "NERVE_ARTIFACTS_ROOT",
        "NERVE_ALLOW_PERSONAL_SERIALS",
        "NERVE_ACTIONLOG_ROOTS",
    ):
        monkeypatch.delenv(var, raising=False)

    if mcp_mod._device_mgr is not None:
        mcp_mod._device_mgr.close()
    mcp_mod._android = None
    mcp_mod._ios = None
    mcp_mod._state_mgr = None
    mcp_mod._metrics = None
    mcp_mod._device_mgr = None
    mcp_mod._emulator = None
    mcp_mod._cached_devices = []
    mcp_mod._cache_timestamp = 0.0
    mcp_mod._cleanup_done = False

    yield {
        "base": tmp_path,
        "home": home,
        "linc": linc,
        "tmp": sys_tmp,
        "vault": vault,
        "fleet": linc / "device-fleet.yaml",
    }

    if mcp_mod._device_mgr is not None:
        mcp_mod._device_mgr.close()
        mcp_mod._device_mgr = None


def _wire_fake_controllers(
    tmp_path: Path,
    *,
    android_devices: list[NerveDevice] | None = None,
    ios_devices: list[NerveDevice] | None = None,
) -> tuple[MagicMock, MagicMock, DeviceManager]:
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = android_devices or []
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = ios_devices or []
    mgr = DeviceManager(db_path=tmp_path / "devices.db", controller=mock_android)
    mcp_mod._android = mock_android
    mcp_mod._ios = mock_ios
    mcp_mod._device_mgr = mgr
    return mock_android, mock_ios, mgr


def _write_fleet(path: Path, content: str, *, bump_mtime: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prior_mtime = path.stat().st_mtime if path.exists() else None
    path.write_text(content, encoding="utf-8")
    if bump_mtime and prior_mtime is not None:
        new_mtime = max(prior_mtime + 2.0, path.stat().st_mtime + 2.0)
        os.utime(path, (new_mtime, new_mtime))


def _assert_no_workaround_hint(message: str) -> None:
    lowered = message.lower()
    assert "nerve_allow_personal_serials" not in lowered
    assert "allowlist" not in lowered
    assert "set personal: false" not in lowered
    assert "unset nerve_engagement" not in lowered


# ---------------------------------------------------------------------------
# [contract] Personal device refusal + zero adb calls
# ---------------------------------------------------------------------------


def test_contract_personal_device_refused_with_zero_adb_calls(
    isolated_fleet: dict[str, Path],
) -> None:
    """[contract] personal: true refuses _resolve_device, reserve, and recording
    start with error_category='personal_device' and zero calls to adb."""
    _write_fleet(
        isolated_fleet["fleet"],
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: true\n",
    )
    mock_android, _mock_ios, mgr = _wire_fake_controllers(
        isolated_fleet["base"],
        android_devices=[
            NerveDevice(
                serial=PERSONAL_SERIAL,
                platform=Platform.ANDROID,
                device_type=DeviceType.PHONE,
                model="Personal Phone",
            )
        ],
    )

    dev, platform_or_err = _core._resolve_device(PERSONAL_SERIAL)
    assert dev is None
    assert "personal" in platform_or_err.lower()
    _assert_no_workaround_hint(platform_or_err)

    res_reserve = _nerve_reserve_sync(PERSONAL_SERIAL, purpose="test")
    assert res_reserve.get("error_category") == "personal_device"
    _assert_no_workaround_hint(res_reserve["error"])
    assert mgr.pool() == []

    # Direct DeviceManager.reserve also refuses without persisting.
    direct_res = mgr.reserve(PERSONAL_SERIAL, purpose="direct")
    assert isinstance(direct_res, str)
    assert "personal" in direct_res.lower()
    assert mgr.pool() == []

    res_rec = _nerve_record_start_sync(PERSONAL_SERIAL)
    assert res_rec.get("error_category") == "personal_device"
    _assert_no_workaround_hint(res_rec["error"])

    res_shot = _nerve_screenshot_sync(PERSONAL_SERIAL)
    assert res_shot.get("error_category") == "personal_device"

    # Zero adb calls for the refused personal device.
    assert mock_android.mock_calls == []


# ---------------------------------------------------------------------------
# [contract] Engagement ON fail-closed states
# ---------------------------------------------------------------------------


def _enable_engagement(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    monkeypatch.setenv("NERVE_ENGAGEMENT", "client-eng-01")
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(vault))


@pytest.mark.parametrize(
    "setup_kind",
    [
        "absent",
        "invalid_yaml",
        "yaml_list",
        "yaml_scalar",
        "empty_file",
        "too_large",
        "directory",
        "missing_device",
        "missing_personal_key",
        "personal_str_false",
        "personal_str_true",
        "personal_int_zero",
        "personal_int_one",
        "personal_null",
        "personal_list",
        "personal_dict",
    ],
)
def test_contract_engagement_on_fails_closed_when_not_explicit_false(
    isolated_fleet: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    setup_kind: str,
) -> None:
    """[contract] In engagement mode, anything short of loaded + personal: false
    (exact Python bool False) refuses every device call cleanly."""
    _enable_engagement(monkeypatch, isolated_fleet["vault"])
    fleet_path = isolated_fleet["fleet"]

    if setup_kind == "absent":
        pass
    elif setup_kind == "invalid_yaml":
        _write_fleet(fleet_path, "devices:\n  - [broken: : :")
    elif setup_kind == "yaml_list":
        _write_fleet(fleet_path, f"- serial: {CLEARED_SERIAL}\n  personal: false\n")
    elif setup_kind == "yaml_scalar":
        _write_fleet(fleet_path, f'"{CLEARED_SERIAL}"\n')
    elif setup_kind == "empty_file":
        _write_fleet(fleet_path, "# comment only\n")
    elif setup_kind == "too_large":
        payload = (
            f"devices:\n  - serial: {CLEARED_SERIAL}\n    personal: false\n"
            + "# "
            + ("x" * (1_100_000))
            + "\n"
        )
        _write_fleet(fleet_path, payload)
    elif setup_kind == "directory":
        fleet_path.mkdir(parents=True)
    elif setup_kind == "missing_device":
        _write_fleet(
            fleet_path,
            "devices:\n  - serial: OTHER-DEV\n    personal: false\n",
        )
    elif setup_kind == "missing_personal_key":
        _write_fleet(
            fleet_path,
            f"devices:\n  - serial: {CLEARED_SERIAL}\n    label: lab\n",
        )
    else:
        raw_val = {
            "personal_str_false": '"false"',
            "personal_str_true": '"true"',
            "personal_int_zero": "0",
            "personal_int_one": "1",
            "personal_null": "null",
            "personal_list": "[]",
            "personal_dict": "{}",
        }[setup_kind]
        _write_fleet(
            fleet_path,
            f"devices:\n  - serial: {CLEARED_SERIAL}\n    personal: {raw_val}\n",
        )

    mock_android, _mock_ios, mgr = _wire_fake_controllers(
        isolated_fleet["base"],
        android_devices=[
            NerveDevice(
                serial=CLEARED_SERIAL,
                platform=Platform.ANDROID,
                device_type=DeviceType.PHONE,
            )
        ],
    )

    assert engagement.device_refusal([CLEARED_SERIAL]) is not None
    dev, msg = _core._resolve_device(CLEARED_SERIAL)
    assert dev is None
    _assert_no_workaround_hint(msg)

    res_reserve = _nerve_reserve_sync(CLEARED_SERIAL)
    assert "error" in res_reserve
    assert res_reserve.get("error_category") in {
        "engagement_misconfigured",
        "personal_device",
    }
    assert mgr.pool() == []

    res_rec = _nerve_record_start_sync(CLEARED_SERIAL)
    assert "error" in res_rec
    assert res_rec.get("error_category") in {
        "engagement_misconfigured",
        "personal_device",
    }

    if setup_kind in {
        "absent",
        "invalid_yaml",
        "yaml_list",
        "yaml_scalar",
        "empty_file",
        "too_large",
        "directory",
    }:
        assert mock_android.mock_calls == []


def test_contract_engagement_on_unreadable_chmod_fails_closed(
    isolated_fleet: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] chmod 000 on device-fleet.yaml is reported 'unreadable' and fails closed."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("chmod 000 is readable by root")

    _enable_engagement(monkeypatch, isolated_fleet["vault"])
    fleet_path = isolated_fleet["fleet"]
    _write_fleet(
        fleet_path,
        f"devices:\n  - serial: {CLEARED_SERIAL}\n    personal: false\n",
    )
    # Prime cache while readable, then chmod 000 without modifying contents/size.
    assert engagement.load_fleet_registry().state == "loaded"
    assert engagement.device_refusal([CLEARED_SERIAL]) is None

    fleet_path.chmod(0o000)
    try:
        reg = engagement.load_fleet_registry()
        assert reg.state == "unreadable"
        assert engagement.device_refusal([CLEARED_SERIAL]) is not None
        _wire_fake_controllers(isolated_fleet["base"])
        res = _nerve_reserve_sync(CLEARED_SERIAL)
        assert "error" in res
        assert res.get("error_category") == "engagement_misconfigured"
    finally:
        fleet_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_contract_engagement_on_allows_explicit_personal_false(
    isolated_fleet: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleared device (personal: false bool) works normally in engagement mode."""
    _enable_engagement(monkeypatch, isolated_fleet["vault"])
    _write_fleet(
        isolated_fleet["fleet"],
        f"devices:\n  - serial: {CLEARED_SERIAL}\n    personal: false\n",
    )
    _wire_fake_controllers(
        isolated_fleet["base"],
        android_devices=[
            NerveDevice(
                serial=CLEARED_SERIAL,
                platform=Platform.ANDROID,
                device_type=DeviceType.PHONE,
            )
        ],
    )

    assert engagement.device_refusal([CLEARED_SERIAL]) is None
    dev, platform = _core._resolve_device(CLEARED_SERIAL)
    assert dev is not None and dev.serial == CLEARED_SERIAL
    assert platform == "android"
    res = _nerve_reserve_sync(CLEARED_SERIAL, purpose="engagement")
    assert res["success"] is True


# ---------------------------------------------------------------------------
# [contract] NERVE_ALLOW_PERSONAL_SERIALS override vs engagement mode
# ---------------------------------------------------------------------------


def test_contract_allow_personal_override_ignored_when_engagement_on(
    isolated_fleet: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] engagement ON + NERVE_ALLOW_PERSONAL_SERIALS=S still refuses S;
    engagement OFF + allowlist resolves S."""
    _write_fleet(
        isolated_fleet["fleet"],
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: true\n",
    )
    mock_android, _mock_ios, mgr = _wire_fake_controllers(
        isolated_fleet["base"],
        android_devices=[
            NerveDevice(
                serial=PERSONAL_SERIAL,
                platform=Platform.ANDROID,
                device_type=DeviceType.PHONE,
            )
        ],
    )

    # Engagement ON: override ignored.
    _enable_engagement(monkeypatch, isolated_fleet["vault"])
    monkeypatch.setenv("NERVE_ALLOW_PERSONAL_SERIALS", PERSONAL_SERIAL)
    dev, msg = _core._resolve_device(PERSONAL_SERIAL)
    assert dev is None
    assert "personal" in msg.lower()
    assert _nerve_reserve_sync(PERSONAL_SERIAL).get("error_category") == "personal_device"
    assert _nerve_record_start_sync(PERSONAL_SERIAL).get("error_category") == "personal_device"
    assert mgr.pool() == []
    assert mock_android.mock_calls == []

    # Engagement OFF: allowlist permits S (also test whitespace + case-insensitive).
    monkeypatch.delenv("NERVE_ENGAGEMENT", raising=False)
    monkeypatch.setenv("NERVE_ALLOW_PERSONAL_SERIALS", f" other , {PERSONAL_SERIAL.lower()} ")
    dev_off, platform_off = _core._resolve_device(PERSONAL_SERIAL)
    assert dev_off is not None and platform_off == "android"
    res_reserve = _nerve_reserve_sync(PERSONAL_SERIAL, purpose="owner-override")
    assert res_reserve["success"] is True


# ---------------------------------------------------------------------------
# [contract] Hostile NERVE_ENGAGEMENT env values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_val", "expected_off"),
    [
        (None, True),
        ("", True),
        (" ", False),
        ("../x", False),
        ("x" * 200, False),
        ("eng-1\n", False),
    ],
    ids=["unset", "empty", "space", "traversal", "len_200", "trailing_newline"],
)
def test_contract_hostile_engagement_env(
    isolated_fleet: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    raw_val: str | None,
    expected_off: bool,
) -> None:
    """[contract] '' and unset mean OFF; every other invalid value means
    ON-and-misconfigured (refused with error_category='engagement_misconfigured')."""
    if raw_val is None:
        monkeypatch.delenv("NERVE_ENGAGEMENT", raising=False)
    else:
        monkeypatch.setenv("NERVE_ENGAGEMENT", raw_val)
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(isolated_fleet["vault"]))

    mock_android, _mock_ios, mgr = _wire_fake_controllers(
        isolated_fleet["base"],
        android_devices=[
            NerveDevice(
                serial=CLEARED_SERIAL,
                platform=Platform.ANDROID,
                device_type=DeviceType.PHONE,
            )
        ],
    )

    if expected_off:
        assert engagement.is_engagement_on() is False
        dev, platform = _core._resolve_device(CLEARED_SERIAL)
        assert dev is not None and platform == "android"
        assert _nerve_reserve_sync(CLEARED_SERIAL)["success"] is True
    else:
        with pytest.raises(engagement.EngagementMisconfiguredError):
            engagement.is_engagement_on()
        assert engagement.device_refusal([CLEARED_SERIAL]) is not None
        dev, msg = _core._resolve_device(CLEARED_SERIAL)
        assert dev is None
        assert msg
        for res in (
            _nerve_reserve_sync(CLEARED_SERIAL),
            _nerve_record_start_sync(CLEARED_SERIAL),
            _nerve_screenshot_sync(CLEARED_SERIAL),
            _nerve_discover_sync(),
        ):
            assert res.get("error_category") == "engagement_misconfigured"
        assert mgr.pool() == []
        assert mock_android.mock_calls == []


# ---------------------------------------------------------------------------
# [contract] Identifier matching: casefold/strip, ios:, garmin:, wifi hw_serial
# ---------------------------------------------------------------------------


def test_contract_identifier_matching_case_space_nested_ios_garmin_and_wifi(
    isolated_fleet: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] Case/whitespace normalization, nested ios:/garmin: sections,
    hardware_id key, and wifi transport carrying resolved hardware_serial."""
    _write_fleet(
        isolated_fleet["fleet"],
        f"""
devices:
  - serial: "  synth-mixed-CASE  "
    personal: true
  - id: "LAB-DEV-100"
    hardware_id: "HW-SYNTH-99"
    personal: false
ios:
  phones:
    founder_phone:
      id: "  {IOS_UDID.lower()}  "
      personal: true
  labs:
    - serial: "00008110-LABSIM0000000001"
      personal: false
garmin:
  watches:
    - id: "GARMIN-SYNTH-7"
      hardware_id: "{WIFI_HW_PERSONAL}"
      personal: true
""",
    )

    # Case & whitespace normalization.
    assert engagement.device_refusal(["SYNTH-MIXED-case"]) is not None
    assert engagement.device_refusal(["  synth-mixed-case  "]) is not None

    # Nested ios: matches UDID (case-insensitive).
    ios_personal = NerveDevice(
        serial=IOS_UDID.upper(),
        platform=Platform.IOS,
        device_type=DeviceType.PHONE,
    )
    _wire_fake_controllers(isolated_fleet["base"], ios_devices=[ios_personal])
    assert _core._resolve_device(IOS_UDID)[0] is None
    assert _nerve_reserve_sync(IOS_UDID).get("error_category") == "personal_device"
    assert _nerve_record_start_sync(IOS_UDID).get("error_category") == "personal_device"

    # Nested garmin: id / hardware_id matches.
    assert engagement.device_refusal(["garmin-synth-7"]) is not None
    assert engagement.device_refusal(["hw-synth-99"]) is None

    # An mDNS wifi transport whose name embeds a hardware serial registered personal ->
    # refused, derived from the transport name alone (no injected attribute, no probe).
    mcp_mod._cached_devices = []
    mcp_mod._cache_timestamp = 0.0
    wifi_dev = NerveDevice(
        serial=WIFI_TRANSPORT,
        platform=Platform.ANDROID,
        device_type=DeviceType.WATCH,
    )
    mock_android, _mock_ios, _mgr = _wire_fake_controllers(
        isolated_fleet["base"], android_devices=[wifi_dev]
    )

    discovered = _nerve_discover_sync()
    assert len(discovered["devices"]) == 1
    assert discovered["devices"][0]["usable"] is False
    assert isinstance(discovered["devices"][0]["refusal"], str)
    assert discovered["devices"][0]["refusal"]
    # Refused via _resolve_device and reserve and record_start using the derived serial.
    dev, _ = _core._resolve_device(WIFI_TRANSPORT)
    assert dev is None
    assert _nerve_reserve_sync(WIFI_TRANSPORT).get("error_category") == "personal_device"
    assert _nerve_record_start_sync(WIFI_TRANSPORT).get("error_category") == "personal_device"
    # No extra adb shell/ro.serialno probes were invoked for gating.
    assert [name for name, *_ in mock_android.mock_calls] == ["discover"]
    assert mock_android.start_recording.mock_calls == []

    # An ip:port wifi transport with no resolved hardware serial fails closed while
    # personal entries exist; when its ro.serialno resolves to an unregistered lab
    # serial it is allowed outside engagement and refused inside engagement.
    mcp_mod._cached_devices = []
    mcp_mod._cache_timestamp = 0.0
    unmapped_wifi = NerveDevice(
        serial="192.0.2.88:5555",
        platform=Platform.ANDROID,
        device_type=DeviceType.WATCH,
    )
    mock_wifi, _mock_ios, _mgr = _wire_fake_controllers(
        isolated_fleet["base"], android_devices=[unmapped_wifi]
    )
    assert _core._resolve_device("192.0.2.88:5555")[0] is None

    # Reset the short failed-probe cache before simulating a reachable lab device at the same IP.
    engagement.clear_hardware_serial_cache()
    mock_wifi.read_hardware_serial.return_value = "SYNTH-LAB-UNREG"
    assert _core._resolve_device("192.0.2.88:5555")[0] is not None
    _enable_engagement(monkeypatch, isolated_fleet["vault"])
    assert _core._resolve_device("192.0.2.88:5555")[0] is None


# ---------------------------------------------------------------------------
# [test] Discover pins, doctor reporting, and live mtime reload
# ---------------------------------------------------------------------------


def test_discover_off_absent_adds_no_keys_and_mixed_list_keeps_others(
    isolated_fleet: dict[str, Path],
) -> None:
    """[test] Engagement OFF + absent registry adds NO keys to discover output;
    when a personal device is present, it gains usable=False + refusal and
    usable devices still appear cleanly with no new keys."""
    dev_a = NerveDevice(
        serial=PERSONAL_SERIAL,
        platform=Platform.ANDROID,
        device_type=DeviceType.PHONE,
        model="Personal",
        os_version="15",
    )
    dev_b = NerveDevice(
        serial=CLEARED_SERIAL,
        platform=Platform.ANDROID,
        device_type=DeviceType.PHONE,
        model="Lab",
        os_version="14",
    )
    _wire_fake_controllers(isolated_fleet["base"], android_devices=[dev_a, dev_b])

    # Registry absent, engagement OFF -> baseline keys only.
    res_absent = _nerve_discover_sync()
    assert res_absent["count"] == 2
    for entry in res_absent["devices"]:
        assert "usable" not in entry
        assert "refusal" not in entry

    # Registry marks dev_a personal -> dev_a marked unusable, dev_b clean.
    _write_fleet(
        isolated_fleet["fleet"],
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: true\n",
    )
    res_mixed = _nerve_discover_sync()
    assert res_mixed["count"] == 2
    by_serial = {d["serial"]: d for d in res_mixed["devices"]}
    assert by_serial[PERSONAL_SERIAL]["usable"] is False
    assert isinstance(by_serial[PERSONAL_SERIAL]["refusal"], str)
    assert by_serial[PERSONAL_SERIAL]["refusal"]
    assert "usable" not in by_serial[CLEARED_SERIAL]
    assert "refusal" not in by_serial[CLEARED_SERIAL]


def _refused_count(detail: str) -> int:
    match = re.search(r"refused=(\d+)", detail)
    assert match is not None, detail
    return int(match.group(1))


def test_doctor_reports_registry_states_and_refused_count(
    isolated_fleet: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] nerve doctor reports device-fleet check for loaded/absent/unreadable,
    engagement state, and the exact refused device count across adb AND iOS."""
    monkeypatch.setattr(doctor_mod, "ADB_PATH", "/sdk/adb")
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda _p: "/sdk/adb")
    monkeypatch.setattr(doctor_mod, "_probe_daemon_health", lambda _p: None)
    monkeypatch.setattr(doctor_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(doctor_mod, "_probe_xcode_clt", lambda: "/Library/Developer/CLT")
    monkeypatch.setattr(doctor_mod, "_probe_ios_serials", lambda: [IOS_UDID])
    monkeypatch.setattr(doctor_mod, "_adb_server_probe", lambda _a: doctor_mod.AdbProbe.CONNECTED)
    adb_out = f"List of devices attached\n{PERSONAL_SERIAL}\tdevice\n{CLEARED_SERIAL}\tdevice\n"
    monkeypatch.setattr(doctor_mod, "_probe_adb_devices", lambda _adb: (adb_out, ""))

    def _fleet_check() -> dict[str, Any]:
        result = CliRunner().invoke(doctor, ["--json"])
        payload = json.loads(result.output)
        return next(c for c in payload["checks"] if c["name"] == "device-fleet")

    # 1. Absent, engagement OFF -> warn, nothing can be refused.
    c_absent = _fleet_check()
    assert c_absent["status"] == "warn"
    assert "absent" in c_absent["detail"].lower()
    assert "engagement=off" in c_absent["detail"].lower()
    assert _refused_count(c_absent["detail"]) == 0

    # 2. Loaded: one personal adb device + one personal iOS device, engagement OFF
    #    -> ok, refused count 2 (the cleared adb device is not counted).
    _write_fleet(
        isolated_fleet["fleet"],
        (
            "devices:\n"
            f"  - serial: {PERSONAL_SERIAL}\n    personal: true\n"
            f"  - serial: {CLEARED_SERIAL}\n    personal: false\n"
            "ios:\n"
            f"  - id: {IOS_UDID}\n    personal: true\n"
        ),
    )
    c_loaded = _fleet_check()
    assert c_loaded["status"] == "ok"
    assert "loaded" in c_loaded["detail"].lower()
    assert "engagement=off" in c_loaded["detail"].lower()
    assert _refused_count(c_loaded["detail"]) == 2

    # 3. Unreadable, engagement OFF -> warn.
    _write_fleet(isolated_fleet["fleet"], "::: [invalid", bump_mtime=True)
    c_bad = _fleet_check()
    assert c_bad["status"] == "warn"
    assert "unreadable" in c_bad["detail"].lower()
    assert _refused_count(c_bad["detail"]) == 0

    # 4. Absent + engagement ON -> fail, refused count 3 (2 adb + 1 iOS).
    isolated_fleet["fleet"].unlink()
    _enable_engagement(monkeypatch, isolated_fleet["vault"])
    c_eng_absent = _fleet_check()
    assert c_eng_absent["status"] == "fail"
    assert "absent" in c_eng_absent["detail"].lower()
    assert "client-eng-01" in c_eng_absent["detail"]
    assert _refused_count(c_eng_absent["detail"]) == 3

    # 5. gather_probes carries the device-fleet check through run_checks.
    probes = gather_probes()
    checks = {r.name: r for r in run_checks(probes)}
    assert "device-fleet" in checks
    assert probes.fleet_refused_count == 3


def test_doctor_skips_ios_probe_when_nothing_can_be_refused(
    isolated_fleet: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] Engagement OFF + no loaded registry: no device can be refused, so the
    doctor never spends an iOS discovery probe on the count."""
    monkeypatch.setattr(doctor_mod, "_probe_ios_serials", MagicMock(return_value=[IOS_UDID]))
    assert doctor_mod._probe_device_fleet(None, "/Library/Developer/CLT")[-1] == 0
    doctor_mod._probe_ios_serials.assert_not_called()  # type: ignore[attr-defined]
    # Without the Xcode command line tools there is no iOS probe either.
    _write_fleet(isolated_fleet["fleet"], f"ios:\n  - id: {IOS_UDID}\n    personal: true\n")
    assert doctor_mod._probe_device_fleet(None, None)[-1] == 0
    doctor_mod._probe_ios_serials.assert_not_called()  # type: ignore[attr-defined]
    assert doctor_mod._probe_device_fleet(None, "/Library/Developer/CLT")[-1] == 1


def test_probe_ios_serials_counts_online_devices_and_swallows_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] The iOS probe lists only online devices and never raises."""
    from linc_nerve.core import ios as ios_mod

    online = NerveDevice(serial=IOS_UDID, platform=Platform.IOS, device_type=DeviceType.PHONE)
    offline = NerveDevice(
        serial="SIM-OFFLINE", platform=Platform.IOS, device_type=DeviceType.PHONE, state="offline"
    )
    fake = MagicMock(spec=IOSController)
    fake.discover.return_value = [online, offline]
    monkeypatch.setattr(ios_mod, "IOSController", lambda: fake)
    assert doctor_mod._probe_ios_serials() == [IOS_UDID]
    fake.discover.side_effect = RuntimeError("xcrun wedged")
    assert doctor_mod._probe_ios_serials() == []


def test_registry_edit_mtime_takes_effect_without_restart(
    isolated_fleet: dict[str, Path],
) -> None:
    """[test] Editing device-fleet.yaml (mtime change) to flip S to personal: true
    takes effect on the next call in the same process."""
    fleet_path = isolated_fleet["fleet"]
    _write_fleet(
        fleet_path,
        f"devices:\n  - serial: {CLEARED_SERIAL}\n    personal: false\n",
    )
    _wire_fake_controllers(
        isolated_fleet["base"],
        android_devices=[
            NerveDevice(
                serial=CLEARED_SERIAL,
                platform=Platform.ANDROID,
                device_type=DeviceType.PHONE,
            )
        ],
    )

    dev, platform = _core._resolve_device(CLEARED_SERIAL)
    assert dev is not None and platform == "android"

    # Edit file with same size/different content and explicit mtime bump.
    _write_fleet(
        fleet_path,
        f"devices:\n  - serial: {CLEARED_SERIAL}\n    personal: true\n",
        bump_mtime=True,
    )

    dev_after, reason = _core._resolve_device(CLEARED_SERIAL)
    assert dev_after is None
    assert "personal" in reason.lower()
    assert _nerve_reserve_sync(CLEARED_SERIAL).get("error_category") == "personal_device"
    assert _nerve_record_start_sync(CLEARED_SERIAL).get("error_category") == "personal_device"


def test_readme_documents_personal_devices_and_override_ignored() -> None:
    """[test] README covers personal devices, all three env vars, and override limit."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "Personal devices" in readme
    for var in (
        "NERVE_ENGAGEMENT",
        "NERVE_ARTIFACTS_ROOT",
        "NERVE_ALLOW_PERSONAL_SERIALS",
    ):
        assert var in readme
    assert "ignored" in readme.lower()


def test_http_devices_and_non_resolve_tools_respect_fleet_policy(
    isolated_fleet: dict[str, Path],
) -> None:
    """/devices mirrors usable/refusal annotations and non-_resolve tools refuse personal."""
    from linc_nerve.core import android as android_mod
    from linc_nerve.core import ios as ios_mod
    from linc_nerve.daemon import http_router
    from linc_nerve.mcp.actionlog_tools import _nerve_actionlog_start_sync
    from linc_nerve.mcp.emulator_tools import (
        _nerve_emulator_stop_sync,
        _nerve_inject_sensor_sync,
    )

    _write_fleet(
        isolated_fleet["fleet"],
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: true\n",
    )
    dev_a = NerveDevice(
        serial=PERSONAL_SERIAL,
        platform=Platform.ANDROID,
        device_type=DeviceType.PHONE,
    )
    dev_b = NerveDevice(
        serial=CLEARED_SERIAL,
        platform=Platform.ANDROID,
        device_type=DeviceType.PHONE,
    )
    _wire_fake_controllers(isolated_fleet["base"], android_devices=[dev_a, dev_b])

    assert _nerve_emulator_stop_sync(PERSONAL_SERIAL).get("error_category") == "personal_device"
    assert _nerve_inject_sensor_sync(PERSONAL_SERIAL).get("error_category") == "personal_device"
    assert (
        _nerve_actionlog_start_sync(PERSONAL_SERIAL, "log.jsonl").get("error_category")
        == "personal_device"
    )

    http_router._DEVICES_CACHE = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(android_mod, "AndroidController", lambda: mcp_mod._android)
        mp.setattr(ios_mod, "IOSController", lambda: mcp_mod._ios)
        body = http_router._build_devices()
    by_serial = {d["serial"]: d for d in body["devices"]}
    assert by_serial[PERSONAL_SERIAL]["usable"] is False
    assert "personal" in by_serial[PERSONAL_SERIAL]["refusal"].lower()
    assert "usable" not in by_serial[CLEARED_SERIAL]
    assert "refusal" not in by_serial[CLEARED_SERIAL]


def test_registry_mtime_only_edit_same_size_takes_effect(
    isolated_fleet: dict[str, Path],
) -> None:
    """[test] A same-size, same-inode content swap that only moves the mtime is
    picked up on the next call (size alone is not the reload key)."""
    fleet_path = isolated_fleet["fleet"]
    cleared = f"devices:\n  - serial: {CLEARED_SERIAL}\n    personal: false\n"
    personal = f"devices:\n  - serial: {CLEARED_SERIAL}\n    personal: true \n"
    assert len(cleared) == len(personal)
    _write_fleet(fleet_path, cleared)
    before = fleet_path.stat()
    assert engagement.device_refusal([CLEARED_SERIAL]) is None

    with open(fleet_path, "r+", encoding="utf-8") as fh:  # in place: same inode
        fh.write(personal)
    os.utime(fleet_path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
    after = fleet_path.stat()
    assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)
    assert after.st_mtime_ns != before.st_mtime_ns

    assert engagement.device_refusal([CLEARED_SERIAL]) is not None


# ---------------------------------------------------------------------------
# [contract] Hostile YAML: recursive aliases, alias bombs, deep nesting
# ---------------------------------------------------------------------------


_HOSTILE_RECURSIVE = {
    "ios": "ios: &a\n  devices:\n    - *a\n",
    "garmin": "garmin: &a\n  watches:\n    - *a\n",
    "ios_list": "ios: &a\n  - *a\n",
    "garmin_deep": "garmin: " + "[" * 5000 + "]" * 5000 + "\n",
}


@pytest.mark.parametrize("kind", sorted(_HOSTILE_RECURSIVE))
def test_contract_recursive_yaml_is_unreadable_not_a_crash(
    isolated_fleet: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """[contract] A self-referential alias or pathologically deep nesting under ios:
    or garmin: never raises RecursionError: when it is the whole registry it reads
    'unreadable' (refused in engagement, USB allowed with warning outside); when a
    valid devices: section sits alongside it, devices: stays loaded and active."""
    lab = NerveDevice(
        serial=CLEARED_SERIAL, platform=Platform.ANDROID, device_type=DeviceType.PHONE
    )
    _wire_fake_controllers(isolated_fleet["base"], android_devices=[lab])

    # 1. Hostile section alone -> whole registry is unreadable, never RecursionError.
    _write_fleet(isolated_fleet["fleet"], _HOSTILE_RECURSIVE[kind])
    reg_only = engagement.load_fleet_registry()
    assert reg_only.state == "unreadable"
    assert reg_only.error
    assert engagement.device_decision([CLEARED_SERIAL]).allowed is True
    assert _core._resolve_device(CLEARED_SERIAL)[0] is lab
    listed = _nerve_discover_sync()
    assert listed["count"] == 1
    assert "usable" not in listed["devices"][0]

    _enable_engagement(monkeypatch, isolated_fleet["vault"])
    decision_eng = engagement.device_decision([CLEARED_SERIAL])
    assert decision_eng.allowed is False
    assert decision_eng.error_category == engagement.ERROR_CATEGORY
    assert "unreadable" in (decision_eng.reason or "")
    assert _core._resolve_device(CLEARED_SERIAL)[0] is None

    # 2. Valid devices: alongside hostile ios:/garmin: -> devices: is kept independently.
    monkeypatch.delenv("NERVE_ENGAGEMENT", raising=False)
    monkeypatch.delenv("NERVE_ARTIFACTS_ROOT", raising=False)
    _write_fleet(
        isolated_fleet["fleet"],
        f"devices:\n  - serial: {CLEARED_SERIAL}\n    personal: false\n"
        f"  - serial: {PERSONAL_SERIAL}\n    personal: true\n" + _HOSTILE_RECURSIVE[kind],
        bump_mtime=True,
    )
    reg_mixed = engagement.load_fleet_registry()
    assert reg_mixed.state == "loaded"
    assert reg_mixed.malformed_count >= 1
    assert engagement.device_decision([PERSONAL_SERIAL]).allowed is False
    assert engagement.device_decision([CLEARED_SERIAL]).allowed is True
    _enable_engagement(monkeypatch, isolated_fleet["vault"])
    assert engagement.device_decision([CLEARED_SERIAL]).allowed is True


def _alias_bomb(section: str, levels: int = 7, fanout: int = 9) -> str:
    lines = [f"l0: &l0 {{serial: {PERSONAL_SERIAL}, personal: true}}"]
    for level in range(1, levels + 1):
        refs = ", ".join([f"*l{level - 1}"] * fanout)
        lines.append(f"l{level}: &l{level} [{refs}]")
    lines.append(f"{section}:\n  bomb: *l{levels}")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("section", ["ios", "garmin"])
def test_contract_alias_bomb_walks_each_shared_node_once(
    isolated_fleet: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    section: str,
) -> None:
    """[contract] A 9^7 YAML alias bomb under ios:/garmin: visits each shared node
    once (bounded work per registry edit) and still honours the personal entry."""
    calls: list[object] = []
    from linc_nerve.core import fleet_entries

    real_extract = fleet_entries._extract_entry

    def _counting_extract(node: Any) -> Any:
        calls.append(node)
        return real_extract(node)

    monkeypatch.setattr(fleet_entries, "_extract_entry", _counting_extract)
    _write_fleet(isolated_fleet["fleet"], _alias_bomb(section))

    decision = engagement.device_decision([PERSONAL_SERIAL])
    assert decision.allowed is False
    assert decision.error_category == engagement.PERSONAL_DEVICE_ERROR_CATEGORY
    assert engagement.load_fleet_registry().state == "loaded"
    assert len(calls) == 1
    assert engagement.device_decision([CLEARED_SERIAL]).allowed is True


def test_contract_numeric_yaml_serials_are_matched_as_strings(
    isolated_fleet: dict[str, Path],
) -> None:
    """[contract] An unquoted numeric serial/id is coerced to its string form, not
    silently dropped; a YAML boolean is never mistaken for a serial."""
    _write_fleet(
        isolated_fleet["fleet"],
        "devices:\n"
        "  - serial: 98765\n    personal: true\n"
        "  - serial: true\n    personal: true\n"
        "ios:\n"
        "  phones:\n    - id: 4242\n      personal: true\n"
        "garmin:\n"
        "  - id: GARMIN-1\n    hardware_id: 31337\n    personal: true\n",
    )
    assert engagement.device_refusal(["98765"]) is not None
    assert engagement.device_refusal(["4242"]) is not None
    assert engagement.device_refusal(["31337"]) is not None
    assert engagement.device_refusal(["true"]) is None
    assert engagement.device_refusal(["1"]) is None


# ---------------------------------------------------------------------------
# [contract] mDNS adb-tls-connect transport names carry the hardware serial
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transport", "derived"),
    [
        ("adb-FAKESERIAL01-AbCdEf._adb-tls-connect._tcp", "FAKESERIAL01"),
        ("adb-FAKESERIAL01-AbCdEf._adb-tls-connect._tcp.", "FAKESERIAL01"),
        ("adb-28031FDH30041N-x9Yz01._adb-tls-connect._tcp", "28031FDH30041N"),
        ("adb-FAKEPERSONAL1-AbCd._adb._tcp", "FAKEPERSONAL1"),
        ("adb-FAKEPERSONAL1-AbCd._adb._tcp.", "FAKEPERSONAL1"),
        ("adb-unknown-AbCdEf._adb-tls-connect._tcp", None),
        ("adb-unknown-AbCd._adb._tcp", None),
        ("adb-0123456789ABCDEF-AbCdEf._adb-tls-connect._tcp", None),
        ("adb--AbCdEf._adb-tls-connect._tcp", None),
        ("adb-FAKESERIAL01._adb-tls-connect._tcp", None),
        ("adb-FAKESERIAL01-AbCdEf._adb-tls-pairing._tcp", None),
        ("192.0.2.55:5555", None),
        ("emulator-5554", None),
        ("", None),
    ],
)
def test_contract_mdns_transport_derives_hardware_serial(
    transport: str, derived: str | None
) -> None:
    """[contract] device_identifiers derives ro.serialno from an mDNS
    ``adb-<SERIAL>-<suffix>._adb-tls-connect._tcp`` name (zero probes); placeholder,
    empty, or non-mDNS names derive nothing."""
    idents = engagement.device_identifiers(transport)
    extra = [i for i in idents if i != transport.strip()]
    assert extra == ([derived] if derived else [])


def test_contract_alternate_identifier_policy_matches_resolve_on_every_path(
    isolated_fleet: dict[str, Path],
) -> None:
    """[contract] Engagement OFF, empty device cache: a personal device registered by
    hardware serial and addressed by its mDNS transport is refused by reserve,
    actionlog_start, emulator_stop and DeviceManager.reserve, exactly like
    _resolve_device, with zero controller calls."""
    from linc_nerve.mcp.actionlog_tools import _nerve_actionlog_start_sync
    from linc_nerve.mcp.emulator_tools import _nerve_emulator_stop_sync

    _write_fleet(
        isolated_fleet["fleet"],
        f"garmin:\n  - id: GARMIN-W2\n    hardware_id: {WIFI_HW_PERSONAL}\n    personal: true\n",
    )
    mock_android, mock_ios, mgr = _wire_fake_controllers(isolated_fleet["base"])
    mcp_mod._cached_devices = []
    mcp_mod._cache_timestamp = 0.0

    assert _core._resolve_device(WIFI_TRANSPORT)[0] is None
    assert _nerve_reserve_sync(WIFI_TRANSPORT).get("error_category") == "personal_device"
    assert (
        _nerve_actionlog_start_sync(WIFI_TRANSPORT, "log.jsonl").get("error_category")
        == "personal_device"
    )
    assert _nerve_emulator_stop_sync(WIFI_TRANSPORT).get("error_category") == "personal_device"
    direct = mgr.reserve(WIFI_TRANSPORT, owner="lane-a")
    assert isinstance(direct, str)
    assert "personal" in direct.lower()
    assert mgr.pool() == []
    assert mgr.check(WIFI_TRANSPORT) is None
    assert mock_android.mock_calls == []
    assert mock_ios.mock_calls == []
