"""Contract tests for the PR #300 review of bead linc-nerve-56eg (R4 finding 2).

1. A malformed PERSONAL entry is never silently dropped: a registry rebuilt by
   line-level recovery, or holding a malformed entry with no usable identifier,
   is *degraded* (warn on every decision, refuse network transports), and the
   identifiers in a chunk that failed to parse are registered as personal unless
   that chunk only ever says ``personal: false``.
2. Recovery is bounded: a parse budget (chunks, bytes, wall clock) past which the
   registry is unreadable (fail closed).
3. Duplicate keys never fail open: duplicate sections are merged, identifiers
   are unioned and any non-false ``personal`` wins.
"""

from __future__ import annotations

import base64
import itertools
import os
import time
from pathlib import Path

import pytest
import structlog

import linc_nerve.core.ios  # noqa: F401  (conftest imports ios_discovery, which needs ios first)
from linc_nerve.core import engagement, fleet_parse

PERSONAL = "FAKEPERSONAL01"
PERSONAL_2 = "FAKEPERSONAL02"
LAB = "FAKELABUSB01"
LAB_2 = "FAKELABUSB02"
IP_TRANSPORT = "10.9.9.9:5555"
_MTIME = itertools.count(1_700_000_000)


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point LINC_HOME at a private dir and clear every policy env var."""
    linc = tmp_path / "linc"
    linc.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LINC_HOME", str(linc))
    for var in ("NERVE_ENGAGEMENT", "NERVE_ARTIFACTS_ROOT", "NERVE_ALLOW_PERSONAL_SERIALS"):
        monkeypatch.delenv(var, raising=False)
    return linc / "device-fleet.yaml"


def _write(path: Path, text: str) -> engagement.FleetRegistry:
    path.write_text(text, encoding="utf-8")
    stamp = next(_MTIME) * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))
    return engagement.load_fleet_registry()


def _lab_then(personal_entry: str) -> str:
    return f"devices:\n  - serial: {LAB}\n    personal: false\n{personal_entry}"


# ---------------------------------------------------------------------------
# 1. A malformed personal entry is refused and the registry is degraded
# ---------------------------------------------------------------------------

_B64_PERSONAL = base64.b64encode(PERSONAL.encode()).decode()

_MALFORMED_PERSONAL_ENTRIES = {
    "notes_mapping_in_value": _lab_then(
        f"  - serial: {PERSONAL}\n    notes: a: b\n    personal: true\n"
    ),
    "misindented_personal_deeper": _lab_then(f"  - serial: {PERSONAL}\n     personal: true\n"),
    "misindented_personal_shallower": _lab_then(f"  - serial: {PERSONAL}\n   personal: true\n"),
    "unclosed_quote": _lab_then(
        f'  - serial: {PERSONAL}\n    notes: "unclosed\n    personal: true\n'
    ),
    "unclosed_quote_serial": _lab_then(f'  - serial: "{PERSONAL}\n    personal: true\n'),
    "personal_before_broken_line_then_serial": _lab_then(
        f"  - personal: true\n    notes: a: b\n    serial: {PERSONAL}\n"
    ),
    "deep_nesting_in_personal_ios_entry": (
        f"ios:\n  phones:\n    - serial: {LAB}\n      personal: false\n"
        f"    - serial: {PERSONAL}\n      personal: true\n"
        f"      extra: {'[' * 3000}{']' * 3000}\n"
    ),
    "bad_line_inside_garmin_block": (
        f"garmin:\n  watches:\n    - id: {LAB}\n      personal: false\n"
        f"    bad line: : :\n    - id: {PERSONAL}\n      personal: true\n"
    ),
    "named_mapping_entry_broken": (
        f"devices:\n  lab:\n    serial: {LAB}\n    personal: false\n"
        f"  mine:\n    serial: {PERSONAL}\n    notes: a: b\n    personal: true\n"
    ),
    "column_zero_list_item": f"- serial: {PERSONAL}\n  personal: true\ndevices: []\n",
    "serial_as_mapping": _lab_then(f"  - serial: {{a: {PERSONAL}}}\n    personal: true\n"),
    "nested_list_item": _lab_then(f"  - - serial: {PERSONAL}\n      personal: true\n"),
    "serial_binary": _lab_then(f"  - serial: !!binary {_B64_PERSONAL}\n    personal: true\n"),
}


@pytest.mark.parametrize(
    "variant", sorted(_MALFORMED_PERSONAL_ENTRIES), ids=sorted(_MALFORMED_PERSONAL_ENTRIES)
)
def test_contract_malformed_personal_entry_is_refused_and_degrades_registry(
    fleet: Path, variant: str
) -> None:
    """[contract] A malformed personal entry next to a good one refuses the personal
    serial AND every network transport, warning on each decision."""
    reg = _write(fleet, _MALFORMED_PERSONAL_ENTRIES[variant])
    assert reg.state == "loaded", reg
    assert reg.malformed_count >= 1, reg

    with structlog.testing.capture_logs() as logs:
        dec = engagement.device_decision([PERSONAL])
        assert dec.allowed is False, f"{variant}: personal serial allowed ({reg})"
        assert dec.error_category == engagement.PERSONAL_DEVICE_ERROR_CATEGORY
        net = engagement.device_decision([IP_TRANSPORT])
        assert net.allowed is False, f"{variant}: network transport allowed ({reg})"
        assert net.error_category == engagement.PERSONAL_DEVICE_ERROR_CATEGORY
        assert engagement.device_decision([LAB]).allowed is True
    warned = [e for e in logs if e.get("event") == "fleet_registry_degraded"]
    assert len(warned) == 3, logs
    assert all(e["log_level"] == "warning" and e["path"] == str(fleet) for e in warned)


@pytest.mark.parametrize(
    "variant", sorted(_MALFORMED_PERSONAL_ENTRIES), ids=sorted(_MALFORMED_PERSONAL_ENTRIES)
)
def test_contract_malformed_personal_entry_never_probes_network_hardware(
    fleet: Path, variant: str
) -> None:
    """[contract] A degraded registry refuses a network transport without spending
    an adb probe on it (the verdict cannot depend on the probe)."""
    _write(fleet, _MALFORMED_PERSONAL_ENTRIES[variant])
    probed: list[str] = []
    dec = engagement.resolve_device_decision(
        IP_TRANSPORT, probe_hw=lambda s: probed.append(s) or "FAKELABUSB01"
    )
    assert dec.allowed is False
    assert probed == []


def test_contract_failed_chunk_saying_personal_false_keeps_lab_usb_usable(fleet: Path) -> None:
    """[contract] An unparsable chunk whose only personal value is ``false`` keeps
    its USB device usable outside engagement mode but stays degraded."""
    reg = _write(fleet, _lab_then(f"  - serial: {LAB_2}\n    notes: a: b\n    personal: false\n"))
    assert reg.state == "loaded" and reg.degraded is True
    assert engagement.device_decision([LAB_2]).allowed is True
    assert engagement.device_decision([IP_TRANSPORT]).allowed is False


@pytest.mark.parametrize("personal_value", ["no", "off", "FALSE"])
def test_contract_failed_chunk_yaml11_false_spellings_are_not_personal(
    fleet: Path, personal_value: str
) -> None:
    """The raw scan honours every YAML 1.1 ``false`` spelling the loader honours."""
    _write(fleet, _lab_then(f"  - serial: {LAB_2}\n    a: b: c\n    personal: {personal_value}\n"))
    assert engagement.device_decision([LAB_2]).allowed is True


@pytest.mark.parametrize(
    "personal_value", ["'false'", '"no"', "", "maybe", "[false]", ".nan", "-.inf"]
)
def test_contract_failed_chunk_non_false_personal_is_personal(
    fleet: Path, personal_value: str
) -> None:
    """[contract] Hostile ``personal`` spellings in an unparsable chunk are personal."""
    _write(fleet, _lab_then(f"  - serial: {LAB_2}\n    a: b: c\n    personal: {personal_value}\n"))
    assert engagement.device_decision([LAB_2]).allowed is False


@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf"])
def test_contract_non_finite_serial_and_personal_fail_closed(fleet: Path, value: str) -> None:
    """[contract] NaN/Inf ``personal`` is personal; a NaN/Inf serial matches its token."""
    reg = _write(fleet, f"devices:\n  - serial: {LAB}\n    personal: {value}\n")
    assert engagement.device_decision([LAB]).allowed is False
    reg = _write(fleet, f"devices:\n  - serial: {value}\n    personal: true\n")
    assert reg.state == "loaded" and reg.malformed_count == 1
    assert engagement.device_decision([value]).allowed is False


def test_contract_failed_chunk_mixing_false_and_true_is_personal(fleet: Path) -> None:
    """[contract] One non-false personal line in a failed chunk marks every
    identifier in it personal (fail closed); sibling chunks keep their own verdict."""
    _write(
        fleet,
        _lab_then(
            f"  - {{serial: {LAB_2}, personal: false\n"
            f"  - serial: [{PERSONAL}, {PERSONAL_2}]\n    personal: false\n"
            "    personal: true\n    x: y: z\n"
        ),
    )
    assert engagement.device_decision([LAB_2]).allowed is True
    for serial in (PERSONAL, PERSONAL_2):
        assert engagement.device_decision([serial]).allowed is False


def test_contract_failed_chunk_without_personal_line_is_personal(fleet: Path) -> None:
    """[contract] An unparsable entry that never says ``personal: false`` is personal."""
    _write(fleet, _lab_then(f"  - serial: {PERSONAL}\n    notes: a: b\n"))
    assert engagement.device_decision([PERSONAL]).allowed is False


def test_contract_failed_chunk_block_list_serial_is_scanned(fleet: Path) -> None:
    """[contract] A block-sequence serial inside an unparsable entry is registered."""
    _write(
        fleet,
        _lab_then(
            f"  - serial:\n      - {PERSONAL}\n      - '{PERSONAL_2}'\n"
            "    notes: a: b\n    personal: true\n"
        ),
    )
    for serial in (PERSONAL, PERSONAL_2):
        assert engagement.device_decision([serial]).allowed is False


def test_contract_failed_chunk_with_blank_and_comment_lines_is_scanned(fleet: Path) -> None:
    """[contract] Blank and comment lines inside a broken entry do not hide it."""
    _write(
        fleet,
        _lab_then(
            f"  - serial:\n\n      # first\n      - {PERSONAL}\n\n      - {PERSONAL_2}\n"
            "    # broken next\n\n    notes: a: b\n    personal: true\n"
        ),
    )
    for serial in (PERSONAL, PERSONAL_2):
        assert engagement.device_decision([serial]).allowed is False


@pytest.mark.parametrize(
    ("serial_yaml", "registered"),
    [(f"!!binary {_B64_PERSONAL}", True), ("!!binary abcde", False), ("&a *b", False)],
)
def test_contract_failed_chunk_binary_and_alias_serials(
    fleet: Path, serial_yaml: str, registered: bool
) -> None:
    """[contract] The raw scan decodes ``!!binary``; undecodable or alias-only values
    register nothing (the registry is degraded, never crashes)."""
    reg = _write(fleet, _lab_then(f"  - serial: {serial_yaml}\n    a: b: c\n    personal: true\n"))
    assert reg.state == "loaded" and reg.degraded is True
    assert engagement.device_decision([PERSONAL]).allowed is (not registered)
    assert engagement.device_decision([IP_TRANSPORT]).allowed is False


def test_valid_non_platform_block_in_broken_file_is_ignored(fleet: Path) -> None:
    """A parsable non-platform block naming an id is not a device (as when the file
    parses whole); a broken one is scanned (fail closed)."""
    _write(
        fleet,
        f"meta:\n  id: {LAB_2}\n  personal: true\n"
        f"notes:\n  id: {PERSONAL}\n  x: y: z\n" + _lab_then("broken: [unclosed\n"),
    )
    assert engagement.device_decision([LAB_2]).allowed is True
    assert engagement.device_decision([PERSONAL]).allowed is False


@pytest.mark.parametrize(
    "text",
    [
        "devices: !!map not-a-mapping\n",
        f"devices:\n  - serial: {PERSONAL}\n    personal: true\n    ? [a, b]\n    : c\n",
    ],
    ids=["map_tag_on_scalar", "unhashable_key"],
)
def test_contract_constructor_errors_never_crash(fleet: Path, text: str) -> None:
    """[contract] Constructor errors (bad ``!!map``, unhashable keys) never raise."""
    reg = _write(fleet, text)
    assert reg.state in ("loaded", "unreadable")
    assert engagement.device_decision([PERSONAL]).allowed is (reg.state == "unreadable")
    assert engagement.device_decision([IP_TRANSPORT]).allowed is False


def test_contract_engagement_mode_refuses_scanned_personal_false_entry(
    fleet: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] In engagement mode an identifier recovered only by the raw scan
    has no trustworthy boolean flag and is refused; parsed entries still clear."""
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    _write(fleet, _lab_then(f"  - serial: {LAB_2}\n    notes: a: b\n    personal: false\n"))
    monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-degraded")
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(vault))
    assert engagement.device_decision([LAB_2]).allowed is False
    assert engagement.device_decision([LAB]).allowed is True
    assert engagement.device_decision([IP_TRANSPORT]).allowed is False


@pytest.mark.parametrize(
    "entry",
    [
        f"  - serial: null\n    personal: true\n    model: {PERSONAL}\n",
        "  - serial: ''\n    personal: true\n",
        "  - serial: {}\n    personal: true\n",
        "  - serial: []\n    personal: true\n",
        "  - serial: true\n    personal: true\n",
        "  - foo: bar\n",
        "  - just-a-string\n",
        "  - serial: !!binary //79\n    personal: true\n",
    ],
)
def test_contract_unidentified_malformed_entry_degrades_parsed_registry(
    fleet: Path, entry: str
) -> None:
    """[contract] A parsable file with a malformed entry carrying no usable
    identifier is degraded: network refused + warning, USB still works."""
    reg = _write(fleet, _lab_then(entry))
    assert reg.state == "loaded" and reg.degraded is True, reg
    assert reg.error
    with structlog.testing.capture_logs() as logs:
        assert engagement.device_decision([IP_TRANSPORT]).allowed is False
        assert engagement.device_decision([LAB]).allowed is True
    assert [e["event"] for e in logs].count("fleet_registry_degraded") == 2


def test_contract_null_serial_with_id_is_not_malformed(fleet: Path) -> None:
    """[contract] ``serial: null`` next to a usable ``id`` (real fleet shape) is
    neither malformed nor degrading; the network-identity gate is unchanged."""
    reg = _write(
        fleet,
        "devices:\n"
        f"  - serial: null\n    id: {PERSONAL}\n    personal: true\n"
        f"  - serial: {LAB}\n    hardware_id: ~\n    personal: false\n"
        "ios:\n"
        f"  - id: {LAB_2}\n    serial: ''\n    personal: false\n",
    )
    assert reg.state == "loaded"
    assert reg.malformed_count == 0
    assert reg.degraded is False
    assert reg.error is None
    assert engagement.device_decision([PERSONAL]).allowed is False
    assert engagement.device_decision([LAB]).allowed is True
    # personal devices are registered: an unresolved network transport is refused,
    # one whose hardware serial resolves to a cleared device is allowed
    assert engagement.device_decision([IP_TRANSPORT]).allowed is False
    assert engagement.device_decision([IP_TRANSPORT, LAB]).allowed is True


def test_clean_registry_is_not_degraded_and_does_not_warn(fleet: Path) -> None:
    """A clean registry never warns and never reports degraded."""
    reg = _write(fleet, _lab_then(f"  - serial: {PERSONAL}\n    personal: true\n"))
    assert reg.degraded is False and reg.malformed_count == 0
    with structlog.testing.capture_logs() as logs:
        engagement.device_decision([LAB])
    assert logs == []


def test_doctor_reports_degraded_registry_and_counts_network_refusal(fleet: Path) -> None:
    """``nerve doctor`` names a degraded registry and counts the refused transport."""
    from linc_nerve.cli import doctor

    _write(fleet, _MALFORMED_PERSONAL_ENTRIES["notes_mapping_in_value"])
    adb = f"List of devices attached\n{IP_TRANSPORT}\tdevice\n{LAB}\tdevice\n"
    state, _path, error, _eng, _eng_err, refused = doctor._probe_device_fleet(adb, None)
    assert state == "degraded"
    assert error
    assert refused == 1
    check = doctor.check_device_fleet(state, str(fleet), error, None, None, refused)
    assert check is not None and check.status == "warn"


# ---------------------------------------------------------------------------
# 2. Recovery is linear and budgeted
# ---------------------------------------------------------------------------


def _bad_entries(n: int, prefix: str = "FAKEBAD") -> str:
    return "".join(
        f"  - serial: {prefix}{i:05d}\n    notes: a: b\n    personal: true\n" for i in range(n)
    )


@pytest.mark.parametrize(("keys", "bad"), [(5000, 200), (2000, 2000)])
def test_contract_recovery_is_bounded_in_time(fleet: Path, keys: int, bad: int) -> None:
    """[contract] The R4 review's quadratic recovery inputs finish quickly and fail
    closed (degraded or unreadable, network refused either way)."""
    text = "".join(f"k{i:05d}: v\n" for i in range(keys)) + "devices:\n" + _bad_entries(bad)
    start = time.perf_counter()
    reg = _write(fleet, text)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"recovery took {elapsed:.1f}s"
    assert reg.state == "unreadable" or reg.degraded is True
    assert engagement.device_decision([IP_TRANSPORT]).allowed is False
    if reg.state == "loaded":
        assert engagement.device_decision(["FAKEBAD00000"]).allowed is False


def test_contract_deep_flow_nesting_is_rejected_cheaply(fleet: Path) -> None:
    """[contract] A near-cap ``[[[`` nest (quadratic in PyYAML's scanner) is refused
    up front, inside a personal entry and as a whole section, in well under a second."""
    deep = "[" * 200_000 + "]" * 200_000
    text = _lab_then(f"  - serial: {PERSONAL}\n    personal: true\n    x: {deep}\n")
    start = time.perf_counter()
    reg = _write(fleet, text + f"ios: {deep}\n")
    assert time.perf_counter() - start < 2.0
    assert reg.state == "loaded" and reg.degraded is True
    assert engagement.device_decision([PERSONAL]).allowed is False
    assert engagement.device_decision([LAB]).allowed is True


def test_flow_nesting_within_cap_still_parses(fleet: Path) -> None:
    """Moderate flow nesting is ordinary YAML and is not mistaken for an attack."""
    nest = "[" * 50 + "1" + "]" * 50
    reg = _write(fleet, _lab_then(f"  - serial: {PERSONAL}\n    personal: true\n    x: {nest}\n"))
    assert reg.state == "loaded" and reg.degraded is False and reg.malformed_count == 0


def test_contract_recovery_chunk_cap_fails_closed(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] Past the chunk cap the registry is unreadable (never partial)."""
    monkeypatch.setattr(fleet_parse, "_MAX_RECOVERY_CHUNKS", 5)
    reg = _write(fleet, "devices:\n" + _bad_entries(10))
    assert reg.state == "unreadable"
    assert "budget" in (reg.error or "")
    assert engagement.device_decision([IP_TRANSPORT]).allowed is False


def test_contract_recovery_byte_budget_fails_closed(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] Past the parse byte budget the registry is unreadable."""
    monkeypatch.setattr(fleet_parse, "_RECOVERY_BYTE_BUDGET", 200)
    reg = _write(fleet, "devices:\n" + _bad_entries(10))
    assert reg.state == "unreadable"
    assert "budget" in (reg.error or "")


def test_contract_recovery_deadline_fails_closed(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] Past the wall-clock deadline the registry is unreadable."""
    ticks = itertools.count(0.0, 10.0)
    monkeypatch.setattr(fleet_parse, "_recovery_clock", lambda: next(ticks))
    reg = _write(fleet, "devices:\n" + _bad_entries(3))
    assert reg.state == "unreadable"
    assert "budget" in (reg.error or "")


def test_recovery_within_budget_keeps_every_good_entry(fleet: Path) -> None:
    """Good entries survive recovery among many bad ones (budget not hit)."""
    good = "".join(f"  - serial: FAKEGOOD{i:04d}\n    personal: false\n" for i in range(300))
    reg = _write(fleet, "devices:\n" + good + _bad_entries(300))
    assert reg.state == "loaded" and reg.degraded is True
    assert engagement.device_decision(["FAKEGOOD0299"]).allowed is True
    assert engagement.device_decision(["FAKEBAD00299"]).allowed is False


# ---------------------------------------------------------------------------
# 3. Duplicate keys never fail open
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "refused", "allowed"),
    [
        (
            f"devices:\n  - serial: {PERSONAL}\n    personal: true\n"
            f"devices:\n  - serial: {LAB}\n    personal: false\n",
            [PERSONAL],
            [LAB],
        ),
        (
            f"devices:\n  - serial: {PERSONAL}\n    personal: true\n    personal: false\n",
            [PERSONAL],
            [],
        ),
        (
            f"devices:\n  - serial: {PERSONAL}\n    personal: false\n    personal: 'x'\n",
            [PERSONAL],
            [],
        ),
        (
            f"devices:\n  - serial: {PERSONAL}\n    serial: {PERSONAL_2}\n"
            f"    serial: {LAB_2}\n    personal: true\n",
            [PERSONAL, PERSONAL_2, LAB_2],
            [],
        ),
        (
            f"ios:\n  phones:\n    - id: {PERSONAL}\n      personal: true\n"
            f"ios:\n  tablets:\n    - id: {LAB}\n      personal: false\n",
            [PERSONAL],
            [LAB],
        ),
        (
            f"ios:\n  phones:\n    - id: {PERSONAL}\n      personal: true\n"
            f"  phones:\n    - id: {LAB}\n      personal: false\n",
            [PERSONAL],
            [LAB],
        ),
        (
            f"garmin:\n  - id: {PERSONAL}\n    personal: true\n"
            f"garmin: {{id: {PERSONAL_2}, personal: true, personal: false}}\n",
            [PERSONAL, PERSONAL_2],
            [],
        ),
    ],
    ids=[
        "duplicate_devices_section",
        "personal_true_then_false",
        "personal_false_then_string",
        "duplicate_serial_unions_identifiers",
        "duplicate_ios_section",
        "duplicate_nested_subsection",
        "duplicate_garmin_section_flow",
    ],
)
def test_contract_duplicate_keys_never_fail_open(
    fleet: Path, text: str, refused: list[str], allowed: list[str]
) -> None:
    """[contract] Duplicate sections merge, identifiers union, non-false personal wins."""
    reg = _write(fleet, text)
    assert reg.state == "loaded", reg
    assert reg.malformed_count >= 1
    for serial in refused:
        assert engagement.device_decision([serial]).allowed is False, serial
    for serial in allowed:
        assert engagement.device_decision([serial]).allowed is True, serial


def test_contract_duplicate_keys_inside_recovered_chunk_never_fail_open(fleet: Path) -> None:
    """[contract] Duplicate-key merging also applies to chunks parsed by recovery."""
    _write(
        fleet,
        f"devices:\n  - serial: {PERSONAL}\n    personal: true\n    personal: false\n"
        "broken: [unclosed\n",
    )
    assert engagement.device_decision([PERSONAL]).allowed is False


def test_merge_key_override_is_not_a_duplicate(fleet: Path) -> None:
    """A YAML merge key overridden by an explicit key keeps YAML semantics."""
    reg = _write(
        fleet,
        "defaults: &def\n  personal: true\n"
        f"devices:\n  - serial: {LAB}\n    <<: *def\n    personal: false\n"
        f"  - serial: {PERSONAL}\n    <<: *def\n",
    )
    assert reg.malformed_count == 0
    assert engagement.device_decision([LAB]).allowed is True
    assert engagement.device_decision([PERSONAL]).allowed is False
