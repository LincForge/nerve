"""Contract and property tests for R4 finding 2 fleet registry hardening
(bead linc-nerve-56eg).
"""

from __future__ import annotations

import base64
import os
import random
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from linc_nerve.core import engagement
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.ios import IOSController
from linc_nerve.mcp import _core
from linc_nerve.mcp import server as mcp_mod
from linc_nerve.mcp.reservation_tools import _nerve_reserve_sync

PERSONAL_SERIAL = "FAKEPERSONAL01"
PERSONAL_SERIAL_2 = "FAKEPERSONAL02"
LAB_USB = "FAKELABUSB01"
LAB_IOS = "00008110-0000000000000099"
IP_TRANSPORT = "10.9.9.9:5555"
MDNS_NON_TLS = f"adb-{LAB_USB}-AbCd._adb._tcp"
MDNS_TLS = f"adb-{LAB_USB}-AbCd._adb-tls-connect._tcp"


@pytest.fixture
def isolated_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolate HOME, LINC_HOME, TMPDIR, env vars, and MCP singletons."""
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


def _wire_controllers(
    tmp_path: Path, devices: list[NerveDevice]
) -> tuple[MagicMock, DeviceManager]:
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = [d for d in devices if d.platform == Platform.ANDROID]
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = [d for d in devices if d.platform == Platform.IOS]
    mgr = DeviceManager(db_path=tmp_path / "devices.db", controller=mock_android)
    mcp_mod._android = mock_android
    mcp_mod._ios = mock_ios
    mcp_mod._device_mgr = mgr
    return mock_android, mgr


def _write_fleet(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# [contract] R4 malformed variants never let a personal device through
# ---------------------------------------------------------------------------


_R4_MALFORMED_VARIANTS = [
    (
        "malformed_line_after",
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: true\n"
        f"  - serial: {LAB_USB}\n    personal: false\n"
        "broken_section: [unclosed\n",
        PERSONAL_SERIAL,
    ),
    (
        "malformed_line_before",
        f"stray_unclosed: [1, 2\ndevices:\n  - serial: {PERSONAL_SERIAL}\n    personal: true\n",
        PERSONAL_SERIAL,
    ),
    (
        "malformed_entry_in_same_list",
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: true\n"
        "  - [broken: : :\n"
        f"  - serial: {LAB_USB}\n    personal: false\n",
        PERSONAL_SERIAL,
    ),
    (
        "deep_nesting_under_ios_flow",
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: true\n"
        f"  - serial: {LAB_USB}\n    personal: false\n"
        "ios: " + "[" * 5000 + "]" * 5000 + "\n",
        PERSONAL_SERIAL,
    ),
    (
        "deep_nesting_under_ios_mapping",
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: true\n"
        "ios:\n"
        + "".join(f"{'  ' * (i + 1)}k{i}:\n" for i in range(600))
        + f"{'  ' * 601}leaf: 1\n",
        PERSONAL_SERIAL,
    ),
    (
        "devices_as_mapping_single",
        f"devices:\n  serial: {PERSONAL_SERIAL}\n  personal: true\n",
        PERSONAL_SERIAL,
    ),
    (
        "devices_as_mapping_named",
        f"devices:\n  my_phone:\n    serial: {PERSONAL_SERIAL}\n    personal: true\n"
        f"  lab_phone:\n    serial: {LAB_USB}\n    personal: false\n",
        PERSONAL_SERIAL,
    ),
    (
        "serial_as_list",
        f"devices:\n  - serial:\n      - {PERSONAL_SERIAL}\n      - {PERSONAL_SERIAL_2}\n"
        "    personal: true\n",
        PERSONAL_SERIAL_2,
    ),
    (
        "personal_str_true",
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: 'true'\n",
        PERSONAL_SERIAL,
    ),
    (
        "personal_str_yes",
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: 'yes'\n",
        PERSONAL_SERIAL,
    ),
    (
        "personal_int_one",
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: 1\n",
        PERSONAL_SERIAL,
    ),
    (
        "personal_int_zero",
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: 0\n",
        PERSONAL_SERIAL,
    ),
    (
        "personal_list",
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: [true]\n",
        PERSONAL_SERIAL,
    ),
    (
        "personal_str_false",
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: 'false'\n",
        PERSONAL_SERIAL,
    ),
    (
        "personal_null",
        f"devices:\n  - serial: {PERSONAL_SERIAL}\n    personal: null\n",
        PERSONAL_SERIAL,
    ),
    (
        "octal_unquoted_serial",
        "devices:\n  - serial: 012345\n    personal: true\n",
        "012345",
    ),
    (
        "octal_leading_double_zero_serial",
        "devices:\n  - serial: 0012345\n    personal: true\n",
        "0012345",
    ),
    # R4b Medium 1: an unclosed flow mapping registered only the line's FIRST identifier key.
    (
        "flow_entry_unclosed_second_key_serial",
        f"devices:\n  - {{id: my-phone, serial: {PERSONAL_SERIAL}, personal: true\n",
        PERSONAL_SERIAL,
    ),
    (
        "flow_entry_unclosed_ios_hardware_id_after_id",
        "ios:\n  - {id: my-iphone, hardware_id: 00008110-000A1B2C3D4E5F6A, personal: true\n",
        "00008110-000A1B2C3D4E5F6A",
    ),
]


@pytest.mark.parametrize(
    ("variant", "yaml_text", "target_serial"),
    _R4_MALFORMED_VARIANTS,
    ids=[v[0] for v in _R4_MALFORMED_VARIANTS],
)
def test_contract_r4_malformed_variants_refuse_personal_device(
    isolated_fleet: dict[str, Path],
    variant: str,
    yaml_text: str,
    target_serial: str,
) -> None:
    """[contract] Every R4 malformed variant keeps personal devices refused and counts badness."""
    _write_fleet(isolated_fleet["fleet"], yaml_text)
    reg = engagement.load_fleet_registry()
    assert reg.state == "loaded", f"{variant}: expected loaded, got {reg}"
    assert reg.malformed_count >= 1, f"{variant}: expected malformed_count >= 1, got {reg}"

    dec = engagement.device_decision([target_serial])
    assert dec.allowed is False, f"{variant}: {target_serial} slipped through"
    assert dec.error_category == engagement.PERSONAL_DEVICE_ERROR_CATEGORY

    personal_dev = NerveDevice(
        serial=target_serial, platform=Platform.ANDROID, device_type=DeviceType.PHONE
    )
    lab_dev = NerveDevice(serial=LAB_USB, platform=Platform.ANDROID, device_type=DeviceType.PHONE)
    mock_android, mgr = _wire_controllers(isolated_fleet["base"], [personal_dev, lab_dev])

    dev, reason = _core._resolve_device(target_serial)
    assert dev is None
    assert "personal" in reason.lower()
    assert _nerve_reserve_sync(target_serial).get("error_category") == "personal_device"
    assert isinstance(mgr.reserve(target_serial), str)
    assert mgr.pool() == []
    assert mock_android.mock_calls == []


def test_contract_malformed_entry_keeps_good_sibling_entries_and_sections(
    isolated_fleet: dict[str, Path],
) -> None:
    """Good sections and entries remain usable when sibling entries/sections are malformed."""
    _write_fleet(
        isolated_fleet["fleet"],
        f"""
devices:
  - "not-a-mapping"
  - serial: true
    personal: true
  - foo: bar
  - serial: {LAB_USB}
    personal: false
ios: &cyc
  - *cyc
garmin:
  watches:
    - id: FAKEGARMIN01
      personal: true
""",
    )
    reg = engagement.load_fleet_registry()
    assert reg.state == "loaded"
    assert reg.malformed_count >= 4
    assert engagement.device_decision([LAB_USB]).allowed is True
    assert engagement.device_decision(["FAKEGARMIN01"]).error_category == "personal_device"


def test_contract_octal_unquoted_personal_false_does_not_clear_decimal_value(
    isolated_fleet: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unquoted octal-looking serial (012345) matches '012345', never '5349'."""
    _write_fleet(
        isolated_fleet["fleet"],
        "devices:\n  - serial: 012345\n    personal: false\n",
    )
    monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-octal")
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(isolated_fleet["vault"]))

    assert engagement.device_decision(["012345"]).allowed is True
    assert engagement.device_decision(["5349"]).allowed is False


def test_contract_cyclic_serial_list_and_cyclic_entry_do_not_crash(
    isolated_fleet: dict[str, Path],
) -> None:
    """Cyclic YAML aliases inside an entry or serial list never raise RecursionError."""
    _write_fleet(
        isolated_fleet["fleet"],
        f"devices:\n"
        f"  - &entry\n"
        f"    serial: {PERSONAL_SERIAL}\n"
        f"    personal: true\n"
        f"    self: *entry\n"
        f"  - serial: &slist\n"
        f"      - {PERSONAL_SERIAL_2}\n"
        f"      - *slist\n"
        f"    personal: true\n",
    )
    reg = engagement.load_fleet_registry()
    assert reg.state == "loaded"
    assert reg.malformed_count >= 2
    assert engagement.device_decision([PERSONAL_SERIAL]).error_category == "personal_device"
    assert engagement.device_decision([PERSONAL_SERIAL_2]).error_category == "personal_device"


# ---------------------------------------------------------------------------
# [contract] Unreadable registry outside engagement mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unreadable_kind",
    [
        "directory",
        "not_mapping_list",
        "not_mapping_scalar",
        "empty_comment",
        "too_big",
        "deep_nesting_root",
        "deep_nesting_only_ios",
        "recursive_only_ios",
        "decode_error",
        "invalid_yaml",
        "dangling_symlink",
        "chmod_000",
    ],
)
def test_contract_unreadable_outside_engagement_refuses_network_allows_usb_warns_every_call(
    isolated_fleet: dict[str, Path],
    unreadable_kind: str,
) -> None:
    """[contract] Outside engagement mode, an unreadable fleet registry refuses
    network transports, allows USB devices, never raises RecursionError, and
    emits a structured warning on EVERY decision (not once)."""
    fleet = isolated_fleet["fleet"]
    cleanup_chmod = False

    if unreadable_kind == "directory":
        fleet.mkdir(parents=True)
    elif unreadable_kind == "not_mapping_list":
        _write_fleet(fleet, f"- serial: {LAB_USB}\n  personal: false\n")
    elif unreadable_kind == "not_mapping_scalar":
        _write_fleet(fleet, '"just a scalar"\n')
    elif unreadable_kind == "empty_comment":
        _write_fleet(fleet, "# comment only\n")
    elif unreadable_kind == "too_big":
        _write_fleet(fleet, f"devices:\n  - serial: {LAB_USB}\n# " + ("x" * 1_100_000))
    elif unreadable_kind == "deep_nesting_root":
        _write_fleet(fleet, "[" * 5000 + "]" * 5000 + "\n")
    elif unreadable_kind == "deep_nesting_only_ios":
        _write_fleet(fleet, "ios: " + "[" * 5000 + "]" * 5000 + "\n")
    elif unreadable_kind == "recursive_only_ios":
        _write_fleet(fleet, "ios: &a\n  devices:\n    - *a\n")
    elif unreadable_kind == "decode_error":
        _write_fleet(fleet, b"\xff\xfe\x00\x80invalid-utf8")
    elif unreadable_kind == "invalid_yaml":
        _write_fleet(fleet, "::: [invalid")
    elif unreadable_kind == "dangling_symlink":
        fleet.symlink_to(isolated_fleet["base"] / "does-not-exist.yaml")
    elif unreadable_kind == "chmod_000":
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("chmod 000 is readable by root")
        _write_fleet(fleet, f"devices:\n  - serial: {LAB_USB}\n    personal: false\n")
        fleet.chmod(0o000)
        cleanup_chmod = True

    try:
        reg = engagement.load_fleet_registry()
        assert reg.state == "unreadable", f"{unreadable_kind}: {reg}"

        with structlog.testing.capture_logs() as cap_logs:
            # 1. Network transports refused (ip:port, host:port, _adb._tcp, _adb-tls-connect._tcp)
            for net_serial in (IP_TRANSPORT, "watch.local:5555", MDNS_NON_TLS, MDNS_TLS):
                dec_net = engagement.resolve_device_decision(net_serial)
                assert dec_net.allowed is False, f"{unreadable_kind}: {net_serial} allowed"
                assert dec_net.error_category == engagement.PERSONAL_DEVICE_ERROR_CATEGORY

            # 2. USB & iOS & emulator devices keep working
            for usb_serial in (LAB_USB, LAB_IOS, "emulator-5554"):
                dec_usb = engagement.device_decision([usb_serial])
                assert dec_usb.allowed is True, f"{unreadable_kind}: {usb_serial} blocked"

            # 3. Repeated calls still emit warnings (not once-per-process)
            dec_repeat = engagement.device_decision([LAB_USB])
            assert dec_repeat.allowed is True

        warnings = [
            entry
            for entry in cap_logs
            if entry.get("log_level") == "warning"
            and entry.get("event") == "fleet_registry_unreadable"
        ]
        assert len(warnings) == 8, f"{unreadable_kind}: expected 8 warnings, got {len(warnings)}"
        for w in warnings:
            assert w.get("path") == str(fleet)

        # End-to-end MCP/controller check: network refused with zero adb probes, USB allowed
        usb_dev = NerveDevice(
            serial=LAB_USB, platform=Platform.ANDROID, device_type=DeviceType.PHONE
        )
        net_dev = NerveDevice(
            serial=IP_TRANSPORT, platform=Platform.ANDROID, device_type=DeviceType.WATCH
        )
        mock_android, _mgr = _wire_controllers(isolated_fleet["base"], [usb_dev, net_dev])
        res_net = _nerve_reserve_sync(IP_TRANSPORT)
        assert res_net.get("error_category") == "personal_device"
        mock_android.read_hardware_serial.assert_not_called()
        assert _core._resolve_device(LAB_USB)[0] is usb_dev
    finally:
        if cleanup_chmod:
            fleet.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# Property test over random YAML-ish fleet documents
# ---------------------------------------------------------------------------


def _render_random_fleet_doc(rng: random.Random) -> tuple[str | bytes, list[str]]:
    """Build one random YAML-ish fleet document + identifiers that have truthy personal."""
    if rng.random() < 0.05:
        return b"\xff\xfe\x80random-bad-bytes", []

    sections = ["devices", "ios", "garmin"]
    rng.shuffle(sections)
    lines: list[str] = []
    truthy_personal_ids: list[str] = []

    truthy_vals = ["true", "'true'", "'yes'", "'1'", "1", "['yes']", "[1]", "'on'"]
    falsy_or_non_false = ["false", "'false'", "0", "null", "[]", "{}"]

    chosen = sections[: rng.randint(1, 3)]
    if rng.random() < 0.2:
        chosen.append(chosen[0])  # duplicate top-level section
    for sec in chosen:
        mode = rng.choice(["list", "mapping", "bad_scalar", "deep", "alias_cycle"])
        if mode == "bad_scalar":
            lines.append(f"{sec}: just-a-string")
            continue
        if mode == "deep":
            lines.append(f"{sec}: " + "[" * 600 + "]" * 600)
            continue
        if mode == "alias_cycle":
            lines.append(f"{sec}: &c_{sec}\n  - *c_{sec}")
            continue

        is_mapping_devices = sec == "devices" and mode == "mapping"
        lines.append(f"{sec}:")
        n_entries = rng.randint(1, 5)
        for idx in range(n_entries):
            prefix = f"  item_{idx}:" if (is_mapping_devices or mode == "mapping") else "  -"
            if rng.random() < 0.15:
                lines.append(f"{prefix} [broken: : :")
                continue
            ident = f"FAKERND_{sec}_{idx}_{rng.randint(100, 999)}"
            if rng.random() < 0.2:
                ident = f"0{rng.randint(100, 777)}"  # octal-looking unquoted

            serial_kind = rng.choice(
                ["str", "int_or_octal", "list", "multi_list", "mapping", "binary", "nested"]
            )
            key_name = rng.choice(["serial", "id", "hardware_id"])
            if serial_kind == "str":
                serial_yaml = f'"{ident}"'
                listed = [ident]
            elif serial_kind == "int_or_octal":
                serial_yaml = ident if ident.isdigit() else str(rng.randint(1000, 99999))
                listed = [serial_yaml]
            elif serial_kind in ("list", "nested"):
                serial_yaml = f"[{ident}]"
                listed = [ident]
            elif serial_kind == "mapping":
                serial_yaml = f"{{a: {ident}}}"
                listed = [ident]
            elif serial_kind == "binary":
                serial_yaml = "!!binary " + base64.b64encode(ident.encode()).decode()
                listed = [ident]
            else:
                ident2 = f"FAKERND2_{sec}_{idx}"
                serial_yaml = f"[{ident}, {ident2}]"
                listed = [ident, ident2]

            if rng.random() < 0.65:
                p_vals = [rng.choice(truthy_vals)]
                is_truthy_personal = True
            else:
                p_vals = [rng.choice(falsy_or_non_false)]
                is_truthy_personal = p_vals[0] != "false"
            if rng.random() < 0.15:  # duplicate key: any non-false value wins
                p_vals = [p_vals[0], "false"] if is_truthy_personal else ["false", "true"]
                is_truthy_personal = True

            named = is_mapping_devices or mode == "mapping"
            nested = serial_kind == "nested" and not named
            pad = "      " if nested else "    "
            if named:
                entry = [f"  item_{idx}:", f"    {key_name}: {serial_yaml}"]
            elif nested:
                entry = [f"  - - {key_name}: {serial_yaml}"]
            else:
                entry = [f"  - {key_name}: {serial_yaml}"]
            entry.extend(f"{pad}personal: {val}" for val in p_vals)
            if rng.random() < 0.3:  # a malformed line INSIDE the entry
                bad_line = rng.choice(
                    [
                        f"{pad}notes: a: b",
                        f'{pad}notes: "unclosed',
                        f"{pad}bad line: : :",
                        f"{pad}deep: {'[' * 1200}{']' * 1200}",
                        f"{pad} personal: {p_vals[-1]}",  # mis-indented duplicate
                    ]
                )
                entry.insert(rng.randint(1, len(entry)), bad_line)
            lines.extend(entry)

            if is_truthy_personal:
                truthy_personal_ids.extend(listed)

        if rng.random() < 0.25:
            lines.append("malformed_top_line [unclosed")

    return "\n".join(lines) + "\n", truthy_personal_ids


def test_property_random_yaml_ish_fleet_never_raises_or_allows_truthy_personal(
    isolated_fleet: dict[str, Path],
) -> None:
    """Property test: over random YAML-ish fleet docs, device_decision never raises
    and never allows an identifier listed with a truthy non-false personal."""
    rng = random.Random(20261118)
    fleet = isolated_fleet["fleet"]

    checked_personal = 0
    for trial in range(300):
        doc, personal_ids = _render_random_fleet_doc(rng)
        _write_fleet(fleet, doc)
        os.utime(fleet, ns=(1_700_000_000_000_000_000 + trial * 1_000_000_000,) * 2)

        reg = engagement.load_fleet_registry()
        assert reg.state in ("loaded", "unreadable")

        for ident in personal_ids:
            dec = engagement.device_decision([ident])
            assert dec.allowed is False, f"trial={trial} allowed {ident} in:\n{doc!r}"
            assert dec.error_category == engagement.PERSONAL_DEVICE_ERROR_CATEGORY
            checked_personal += 1

        # Network transport never slips through an unreadable or degraded registry
        dec_net = engagement.device_decision([IP_TRANSPORT])
        if reg.state == "unreadable" or reg.degraded:
            assert dec_net.allowed is False, f"trial={trial} network allowed in:\n{doc!r}"
        # Arbitrary USB device decision never raises
        dec_usb = engagement.device_decision([LAB_USB])
        assert isinstance(dec_usb.allowed, bool)

    assert checked_personal > 200


def test_recovery_and_scalar_edge_cases(isolated_fleet: dict[str, Path]) -> None:
    """Nested sub-section recovery, cross-section anchors, comments/indented junk,
    float/date/dict serials, empty section, and doctor count when unreadable."""
    from linc_nerve.cli import doctor as doctor_mod

    # 1. Nested sub-section recovery + anchors + comments + leading indented junk
    _write_fleet(
        isolated_fleet["fleet"],
        f"""
  stray_indented_prefix
# header comment
defaults: &def
  personal: true
devices:
  # comment inside devices
  - serial: {PERSONAL_SERIAL}
    <<: *def
  - [broken
  stray_unaligned_token
ios:
  phones:
    - serial: FAKEIOS01
      personal: true
    - [broken
broken_trailing: [unclosed
""",
    )
    reg = engagement.load_fleet_registry()
    assert reg.state == "loaded"
    assert reg.malformed_count >= 5
    assert engagement.device_decision([PERSONAL_SERIAL]).error_category == "personal_device"
    assert engagement.device_decision(["FAKEIOS01"]).error_category == "personal_device"

    # 2. Float, date, dict serials + empty section + scalar section
    _write_fleet(
        isolated_fleet["fleet"],
        """
devices:
  - serial: 123.5
    personal: true
  - serial: 2026-11-18
    personal: true
  - serial:
      nested: nope
    personal: true
ios:
garmin: "not-a-container"
""",
    )
    os.utime(isolated_fleet["fleet"], ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))
    reg2 = engagement.load_fleet_registry()
    assert reg2.state == "loaded"
    assert reg2.malformed_count >= 4
    assert engagement.device_decision(["123.5"]).error_category == "personal_device"
    assert engagement.device_decision(["2026-11-18"]).error_category == "personal_device"

    # 3. Doctor counts network adb serial as refused when registry is unreadable
    _write_fleet(isolated_fleet["fleet"], "::: [invalid")
    os.utime(isolated_fleet["fleet"], ns=(1_900_000_000_000_000_000, 1_900_000_000_000_000_000))
    adb_out = f"List of devices attached\n{IP_TRANSPORT}\tdevice\n{LAB_USB}\tdevice\n"
    state, _p, _err, _eng, _eng_err, refused = doctor_mod._probe_device_fleet(adb_out, None)
    assert state == "unreadable"
    assert refused == 1
