"""Exact-value tests for the fleet registry read, warning and doctor paths
(bead linc-nerve-56eg).

These compare whole ``FleetRegistry`` / ``DeviceDecision`` values, structured log
events and doctor probe rows, so a mutated field or message cannot pass.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import structlog

import linc_nerve.core.ios  # noqa: F401  (conftest imports ios_discovery, which needs ios first)
from linc_nerve.cli import doctor
from linc_nerve.core import engagement
from linc_nerve.core.engagement import DeviceDecision, FleetRegistry
from linc_nerve.core.fleet_entries import FleetEntry

LAB = "FAKELABUSB01"
DEVICE = "FAKESERIAL01"
IP_TRANSPORT = "10.9.9.9:5555"
_LAB_TEXT = f"devices:\n  - serial: {LAB}\n    personal: false\n"
_LAB_BY_ID = {LAB.casefold(): (FleetEntry((LAB.casefold(),), False),)}
_FAKE_PATH = Path("/nonexistent/linc/device-fleet.yaml")


@pytest.fixture
def fleet_file(tmp_path: Path) -> Path:
    return tmp_path / "device-fleet.yaml"


# ---------------------------------------------------------------------------
# _read_fleet_file
# ---------------------------------------------------------------------------


def test_contract_oversized_read_is_unreadable_exactly(fleet_file: Path) -> None:
    """[contract] A file past the cap (e.g. grown since stat) is unreadable, nothing parsed."""
    fleet_file.write_bytes(b"#" * (engagement._MAX_FLEET_BYTES + 1))
    assert engagement._read_fleet_file(fleet_file) == FleetRegistry(
        "unreadable", fleet_file, {}, f"exceeds {engagement._MAX_FLEET_BYTES} bytes"
    )


class _ReadRecorder:
    """File wrapper recording every ``read`` size requested."""

    def __init__(self, path: Path, sizes: list[int | None]) -> None:
        self._data = path.read_bytes()
        self._sizes = sizes

    def __enter__(self) -> _ReadRecorder:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, size: int | None = -1) -> bytes:
        self._sizes.append(size)
        return self._data if size is None or size < 0 else self._data[:size]


def test_contract_read_is_bounded_to_one_byte_past_the_cap(
    fleet_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] The registry is read with a bound, never slurped whole."""
    fleet_file.write_text(_LAB_TEXT, encoding="utf-8")
    sizes: list[int | None] = []
    monkeypatch.setattr(
        engagement, "open", lambda path, _mode: _ReadRecorder(path, sizes), raising=False
    )
    assert engagement._read_fleet_file(fleet_file) == FleetRegistry(
        "loaded", fleet_file, _LAB_BY_ID
    )
    assert sizes == [engagement._FLEET_READ_LIMIT]
    assert engagement._FLEET_READ_LIMIT == engagement._MAX_FLEET_BYTES + 1
    assert (engagement._FLEET_ENCODING, engagement._UTF8_BOM) == ("utf-8", "\ufeff")


def test_contract_file_of_exactly_the_cap_is_read_whole(fleet_file: Path) -> None:
    """[contract] Exactly ``_MAX_FLEET_BYTES`` is accepted and read to the last byte."""
    tail = _LAB_TEXT.rstrip("\n")  # ends in "personal: false" with no newline
    pad = engagement._MAX_FLEET_BYTES - len(tail) - 1
    fleet_file.write_text("#" * pad + "\n" + tail, encoding="utf-8")
    assert fleet_file.stat().st_size == engagement._MAX_FLEET_BYTES
    assert engagement._read_fleet_file(fleet_file) == FleetRegistry(
        "loaded", fleet_file, _LAB_BY_ID
    )


def test_contract_invalid_utf8_is_unreadable_with_decoder_message(fleet_file: Path) -> None:
    """[contract] Undecodable bytes are unreadable, naming the exception and its text."""
    fleet_file.write_bytes(b"\xff" + _LAB_TEXT.encode())
    with pytest.raises(UnicodeDecodeError) as info:
        (b"\xff" + _LAB_TEXT.encode()).decode("utf-8")
    assert engagement._read_fleet_file(fleet_file) == FleetRegistry(
        "unreadable", fleet_file, {}, f"UnicodeDecodeError: {info.value}"
    )


def test_contract_os_error_is_unreadable_with_exception_name(tmp_path: Path) -> None:
    """[contract] An OSError on open is unreadable, naming the exception type."""
    with pytest.raises(OSError) as info:
        open(tmp_path, "rb")  # noqa: SIM115 - a directory cannot be opened
    assert engagement._read_fleet_file(tmp_path) == FleetRegistry(
        "unreadable", tmp_path, {}, f"{type(info.value).__name__}: {info.value}"
    )


def test_contract_unparseable_registry_keeps_malformed_count(fleet_file: Path) -> None:
    """[contract] An unreadable parse carries the parser's error and malformed count."""
    fleet_file.write_text("ios: scalar\n", encoding="utf-8")
    assert engagement._read_fleet_file(fleet_file) == FleetRegistry(
        "unreadable",
        fleet_file,
        {},
        "no readable platform section in device fleet registry",
        malformed_count=1,
    )


def test_loaded_degraded_registry_fields_are_exact(fleet_file: Path) -> None:
    fleet_file.write_text(_LAB_TEXT + "  - serial: {k: FAKEPERS01}\n    personal: 1\n", "utf-8")
    assert engagement._read_fleet_file(fleet_file) == FleetRegistry(
        "loaded",
        fleet_file,
        {**_LAB_BY_ID, "fakepers01": (FleetEntry(("fakepers01",), True, malformed=True),)},
        error="1 malformed fleet entries carry no usable identifier",
        has_personal=True,
        malformed_count=1,
        degraded=True,
    )


def test_contract_leading_byte_order_marks_are_stripped(fleet_file: Path) -> None:
    """[contract] Every leading U+FEFF is dropped (PyYAML itself skips only one)."""
    fleet_file.write_text("﻿﻿" + _LAB_TEXT, encoding="utf-8")
    assert engagement._read_fleet_file(fleet_file) == FleetRegistry(
        "loaded", fleet_file, _LAB_BY_ID
    )


def test_contract_trailing_byte_order_mark_is_kept(fleet_file: Path) -> None:
    """[contract] A U+FEFF after the content is content, not a BOM: it is parsed (and fails)."""
    fleet_file.write_text(_LAB_TEXT + "﻿", encoding="utf-8")
    reg = engagement._read_fleet_file(fleet_file)
    assert reg.state == "loaded"
    assert reg.by_id == _LAB_BY_ID
    assert reg.degraded is True
    assert reg.malformed_count == 1
    assert reg.error == "recovered after ScannerError with 1 malformed entries"


# ---------------------------------------------------------------------------
# _warn_untrusted_registry / _untrusted_registry_decision
# ---------------------------------------------------------------------------


def test_contract_unreadable_warning_fields_are_exact() -> None:
    """[contract] The unreadable warning names event, path, error and device."""
    reg = FleetRegistry("unreadable", _FAKE_PATH, {}, "boom")
    with structlog.testing.capture_logs() as logs:
        engagement._warn_untrusted_registry(reg, DEVICE)
    assert logs == [
        {
            "event": "fleet_registry_unreadable",
            "path": str(_FAKE_PATH),
            "error": "boom",
            "device": DEVICE,
            "log_level": "warning",
            "domain": "device_control",
        }
    ]


def test_contract_degraded_warning_fields_are_exact() -> None:
    """[contract] The degraded warning also carries the malformed count."""
    reg = FleetRegistry("loaded", _FAKE_PATH, {}, "meh", malformed_count=2, degraded=True)
    with structlog.testing.capture_logs() as logs:
        engagement._warn_untrusted_registry(reg, DEVICE)
    assert logs == [
        {
            "event": "fleet_registry_degraded",
            "path": str(_FAKE_PATH),
            "error": "meh",
            "malformed_count": 2,
            "device": DEVICE,
            "log_level": "warning",
            "domain": "device_control",
        }
    ]


def test_trusted_registry_does_not_warn() -> None:
    with structlog.testing.capture_logs() as logs:
        engagement._warn_untrusted_registry(FleetRegistry("loaded", _FAKE_PATH, {}), DEVICE)
        engagement._warn_untrusted_registry(FleetRegistry("absent", _FAKE_PATH, {}), DEVICE)
    assert logs == []


@pytest.mark.parametrize(
    ("reg", "condition"),
    [
        (FleetRegistry("unreadable", _FAKE_PATH, {}, "boom"), "unreadable"),
        (FleetRegistry("loaded", _FAKE_PATH, {}, "meh", degraded=True), "degraded"),
    ],
    ids=["unreadable", "degraded"],
)
def test_contract_untrusted_registry_refuses_network_with_exact_reason(
    reg: FleetRegistry, condition: str
) -> None:
    """[contract] Network transports are refused, naming the registry condition."""
    assert engagement._untrusted_registry_decision(reg, [IP_TRANSPORT], DEVICE) == DeviceDecision(
        False,
        f"Device '{DEVICE}' identity could not be verified; "
        f"fleet registry is {condition} ({_FAKE_PATH}), refusing network transport.",
        engagement.PERSONAL_DEVICE_ERROR_CATEGORY,
    )
    assert engagement._untrusted_registry_decision(reg, [LAB], DEVICE) == DeviceDecision(True)


# ---------------------------------------------------------------------------
# doctor._probe_device_fleet
# ---------------------------------------------------------------------------

_ADB = f"List of devices attached\n{IP_TRANSPORT}\tdevice\n{LAB}\tdevice\n"


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Callable[..., tuple]:
    """Run the doctor probe against a fake registry and engagement state.

    ``resolve_device_decision`` refuses every serial and records each call, so a
    branch that should short-circuit is visible as a non-zero count or a call.
    """
    calls: list[str] = []

    def run(reg: FleetRegistry, eng_id: str | None = None, eng_error: str | None = None) -> tuple:
        def fake_engagement_id() -> str | None:
            if eng_error is not None:
                raise engagement.EngagementMisconfiguredError(eng_error)
            return eng_id

        def refuse(serial: str = "", *_a: object, **_k: object) -> DeviceDecision:
            calls.append(serial)
            return DeviceDecision(False, "refused", engagement.ERROR_CATEGORY)

        monkeypatch.setattr(engagement, "load_fleet_registry", lambda: reg)
        monkeypatch.setattr(engagement, "engagement_id", fake_engagement_id)
        monkeypatch.setattr(engagement, "resolve_device_decision", refuse)
        monkeypatch.setattr(doctor, "_probe_ios_serials", lambda: ["FAKEIOS01"])
        return (*doctor._probe_device_fleet(_ADB, None), tuple(calls))

    return run


def test_contract_doctor_absent_without_engagement_probes_nothing(probe: Callable) -> None:
    """[contract] Absent registry, engagement off: nothing can be refused, nothing probed."""
    reg = FleetRegistry("absent", _FAKE_PATH, {})
    assert probe(reg) == ("absent", str(_FAKE_PATH), None, None, None, 0, ())


def test_contract_doctor_unreadable_without_engagement_counts_network_only(
    probe: Callable,
) -> None:
    """[contract] Unreadable registry, engagement off: only network transports refused."""
    reg = FleetRegistry("unreadable", _FAKE_PATH, {}, "boom")
    assert probe(reg) == ("unreadable", str(_FAKE_PATH), "boom", None, None, 1, ())


@pytest.mark.parametrize("state", ["absent", "unreadable"])
def test_contract_doctor_with_engagement_resolves_every_adb_serial(
    probe: Callable, state: str
) -> None:
    """[contract] Engagement on: every online adb serial goes through the policy."""
    reg = FleetRegistry(state, _FAKE_PATH, {}, "boom")  # type: ignore[arg-type]
    assert probe(reg, eng_id="eng-1") == (
        state,
        str(_FAKE_PATH),
        "boom",
        "eng-1",
        None,
        2,
        (IP_TRANSPORT, LAB),
    )


@pytest.mark.parametrize("state", ["absent", "unreadable"])
def test_contract_doctor_misconfigured_engagement_resolves_every_adb_serial(
    probe: Callable, state: str
) -> None:
    """[contract] A misconfigured engagement is reported and never short-circuits."""
    reg = FleetRegistry(state, _FAKE_PATH, {})  # type: ignore[arg-type]
    assert probe(reg, eng_error="bad engagement") == (
        state,
        str(_FAKE_PATH),
        None,
        None,
        "bad engagement",
        2,
        (IP_TRANSPORT, LAB),
    )


def test_doctor_degraded_registry_reports_degraded_state(probe: Callable) -> None:
    reg = FleetRegistry("loaded", _FAKE_PATH, {}, "meh", degraded=True)
    assert probe(reg) == ("degraded", str(_FAKE_PATH), "meh", None, None, 2, (IP_TRANSPORT, LAB))


def test_doctor_loaded_registry_reports_loaded_state(probe: Callable) -> None:
    reg = FleetRegistry("loaded", _FAKE_PATH, {})
    assert probe(reg) == ("loaded", str(_FAKE_PATH), None, None, None, 2, (IP_TRANSPORT, LAB))


# ---------------------------------------------------------------------------
# device_decision: the untrusted-registry lines name the device
# ---------------------------------------------------------------------------


def _decide(tmp_path: Path, text: str, ident: str, **env: str) -> tuple[DeviceDecision, list]:
    linc = tmp_path / "linc"
    linc.mkdir(exist_ok=True)
    (linc / "device-fleet.yaml").write_text(text, encoding="utf-8")
    environ = {"LINC_HOME": str(linc), **env}
    with structlog.testing.capture_logs() as logs:
        decision = engagement.device_decision([ident], environ=environ)
    return decision, logs


def _refusal(path: Path, condition: str) -> DeviceDecision:
    return DeviceDecision(
        False,
        f"Device '{IP_TRANSPORT}' identity could not be verified; "
        f"fleet registry is {condition} ({path}), refusing network transport.",
        engagement.PERSONAL_DEVICE_ERROR_CATEGORY,
    )


def test_contract_unreadable_registry_decision_names_the_device(tmp_path: Path) -> None:
    """[contract] Outside engagement the refusal and the warning name the device."""
    decision, logs = _decide(tmp_path, "ios: scalar\n", IP_TRANSPORT)
    path = tmp_path / "linc" / "device-fleet.yaml"
    assert decision == _refusal(path, "unreadable")
    assert [(e["event"], e["device"]) for e in logs] == [
        ("fleet_registry_unreadable", IP_TRANSPORT)
    ]


def test_contract_degraded_registry_in_engagement_refuses_network_by_name(
    tmp_path: Path,
) -> None:
    """[contract] In engagement mode a degraded registry refuses the network transport."""
    text = _LAB_TEXT + "  - serial: {k: FAKEPERS01}\n"
    decision, logs = _decide(tmp_path, text, IP_TRANSPORT, NERVE_ENGAGEMENT="eng-1")
    path = tmp_path / "linc" / "device-fleet.yaml"
    assert decision == _refusal(path, "degraded")
    assert [(e["event"], e["device"]) for e in logs] == [("fleet_registry_degraded", IP_TRANSPORT)]
