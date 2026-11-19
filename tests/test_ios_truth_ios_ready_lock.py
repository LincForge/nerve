"""Tests for physical iOS readiness lockState detection (linc-nerve-48ox)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from linc_nerve.core import ios as ios_mod
from linc_nerve.core.ios import IOSController, XcodeNotReadyError
from linc_nerve.mcp import _core, state_tools
from linc_nerve.mcp.state_tools import _nerve_ensure_ready_sync

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
UNLOCKED_FIXTURE_PATH = FIXTURES_DIR / "devicectl_lockstate_observed.json"
LOCKED_FIXTURE_PATH = FIXTURES_DIR / "devicectl_lockstate_derived.json"

PHYS_UDID = "00008110-0000000000000001"
SIM_UDID = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"

CORE_DEVICE_LOCKED_STDERR = (
    "ERROR: Unable to retrieve lockState for device (CoreDeviceError error 1002: "
    "The device is locked and passcode protected.)"
)

DEVICECTL_STDOUT_HEADER = (
    "Current device lock state:\n- Passcode required: true\n- Unlocked since boot: false\n\n"
)


def _unlocked_fixture_text() -> str:
    return UNLOCKED_FIXTURE_PATH.read_text(encoding="utf-8")


def _locked_fixture_text() -> str:
    return LOCKED_FIXTURE_PATH.read_text(encoding="utf-8")


def _wire_ios_controller(
    monkeypatch: pytest.MonkeyPatch,
    *,
    device: str,
    run_devicectl: Any,
) -> tuple[IOSController, list[tuple[tuple[str, ...], dict[str, Any]]]]:
    monkeypatch.setattr(ios_mod, "WDAManager", MagicMock)
    monkeypatch.setattr(ios_mod, "WdaMjpegManager", MagicMock)
    ctrl = IOSController()
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def _fake_run_devicectl(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return run_devicectl(*args, **kwargs)

    monkeypatch.setattr(ctrl, "_run_devicectl", _fake_run_devicectl)
    monkeypatch.setattr(_core, "_ensure_initialized", lambda: (MagicMock(), ctrl))
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: ({"serial": dev}, "ios"))
    monkeypatch.setattr(_core, "_check_guard", lambda dev: None)
    return ctrl, calls


def test_locked_fixture_returns_not_ready_ios_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] LOCKED fixture JSON -> ready=False, reason='ios_locked', lock_state='locked'."""
    locked_json = _locked_fixture_text()
    _, calls = _wire_ios_controller(
        monkeypatch,
        device=PHYS_UDID,
        run_devicectl=lambda *a, **kw: subprocess.CompletedProcess(
            args=["xcrun", "devicectl", *a],
            returncode=0,
            stdout=locked_json,
            stderr="",
        ),
    )

    out = _nerve_ensure_ready_sync(device=PHYS_UDID)

    assert out["ready"] is False
    assert out["reason"] == "ios_locked"
    assert out["lock_state"] == "locked"
    assert out["unlocked"] is False
    assert out["report_only"] is False
    assert out["platform"] == "ios"
    assert out["awake"] is None
    assert out["battery_pct"] is None
    assert out["charging"] is None
    assert out["actions_taken"] == []
    assert out["network"]["note"] == "not checked on iOS"
    assert calls == [
        (
            ("device", "info", "lockState", "--device", PHYS_UDID, "--json-output", "-"),
            {"timeout": 15, "check": False},
        )
    ]


def test_unlocked_fixture_returns_ready_unlocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] UNLOCKED fixture JSON -> ready=True, report_only=False, lock_state='unlocked'."""
    unlocked_json = _unlocked_fixture_text()
    _, calls = _wire_ios_controller(
        monkeypatch,
        device=PHYS_UDID,
        run_devicectl=lambda *a, **kw: subprocess.CompletedProcess(
            args=["xcrun", "devicectl", *a],
            returncode=0,
            stdout=unlocked_json,
            stderr="",
        ),
    )

    out = _nerve_ensure_ready_sync(device=PHYS_UDID)

    assert out["ready"] is True
    assert out["reason"] == "ok"
    assert out["lock_state"] == "unlocked"
    assert out["unlocked"] is True
    assert out["report_only"] is False
    assert out["platform"] == "ios"
    assert out["awake"] is None
    assert out["battery_pct"] is None
    assert out["charging"] is None
    assert out["actions_taken"] == []
    assert out["network"]["note"] == "not checked on iOS"
    assert calls == [
        (
            ("device", "info", "lockState", "--device", PHYS_UDID, "--json-output", "-"),
            {"timeout": 15, "check": False},
        )
    ]


def test_parse_lock_state_valid_and_prefixed_fixtures() -> None:
    """[test] parse_lock_state parses raw and header-prefixed devicectl lockState outputs."""
    assert ios_mod.parse_lock_state(_locked_fixture_text()) == "locked"
    assert ios_mod.parse_lock_state(_unlocked_fixture_text()) == "unlocked"
    assert ios_mod.parse_lock_state(DEVICECTL_STDOUT_HEADER + _locked_fixture_text()) == "locked"
    assert (
        ios_mod.parse_lock_state(DEVICECTL_STDOUT_HEADER + _unlocked_fixture_text()) == "unlocked"
    )

    # Device unlocked once since boot and later re-locked -> locked
    relocked = json.loads(_unlocked_fixture_text())
    relocked["result"]["passcodeRequired"] = True
    relocked["result"]["unlockedSinceBoot"] = True
    assert ios_mod.parse_lock_state(json.dumps(relocked)) == "locked"

    # Device before first unlock -> locked even if passcodeRequired were false
    bfu = json.loads(_unlocked_fixture_text())
    bfu["result"]["passcodeRequired"] = False
    bfu["result"]["unlockedSinceBoot"] = False
    assert ios_mod.parse_lock_state(json.dumps(bfu)) == "locked"


def _fixture_with_replaced_bool(field: str, replacement: Any) -> str:
    data = json.loads(_unlocked_fixture_text())
    data["result"][field] = replacement
    return json.dumps(data)


def _fixture_with_both_bools_replaced(replacement: Any) -> str:
    data = json.loads(_locked_fixture_text())
    data["result"]["passcodeRequired"] = replacement
    data["result"]["unlockedSinceBoot"] = replacement
    return json.dumps(data)


@pytest.mark.parametrize(
    "hostile_input",
    [
        pytest.param("", id="empty_string"),
        pytest.param("   \n\t  ", id="whitespace_only"),
        pytest.param("not json", id="not_json"),
        pytest.param("[]", id="json_array"),
        pytest.param("[{}]", id="json_array_with_object"),
        pytest.param("{}", id="empty_object"),
        pytest.param('{"result": null}', id="result_null"),
        pytest.param('{"result": []}', id="result_list"),
        pytest.param('{"result": "locked"}', id="result_string"),
        pytest.param('{"result": {}}', id="result_empty_dict"),
        pytest.param('{"result": {"passcodeRequired": false}}', id="missing_unlocked_since_boot"),
        pytest.param('{"result": {"unlockedSinceBoot": true}}', id="missing_passcode_required"),
        pytest.param(
            _fixture_with_replaced_bool("passcodeRequired", None),
            id="passcode_required_null",
        ),
        pytest.param(
            _fixture_with_replaced_bool("passcodeRequired", "yes"),
            id="passcode_required_str_yes",
        ),
        pytest.param(
            _fixture_with_replaced_bool("passcodeRequired", 1),
            id="passcode_required_int_1",
        ),
        pytest.param(
            _fixture_with_replaced_bool("passcodeRequired", 0),
            id="passcode_required_int_0",
        ),
        pytest.param(
            _fixture_with_replaced_bool("unlockedSinceBoot", None),
            id="unlocked_since_boot_null",
        ),
        pytest.param(
            _fixture_with_replaced_bool("unlockedSinceBoot", "yes"),
            id="unlocked_since_boot_str_yes",
        ),
        pytest.param(
            _fixture_with_replaced_bool("unlockedSinceBoot", 1),
            id="unlocked_since_boot_int_1",
        ),
        pytest.param(
            _fixture_with_replaced_bool("unlockedSinceBoot", 0),
            id="unlocked_since_boot_int_0",
        ),
        pytest.param(
            _fixture_with_both_bools_replaced(None),
            id="both_bools_null",
        ),
        pytest.param(
            _fixture_with_both_bools_replaced("yes"),
            id="both_bools_str_yes",
        ),
        pytest.param(
            _fixture_with_both_bools_replaced(1),
            id="both_bools_int_1",
        ),
        pytest.param("x" * (1024 * 1024), id="one_megabyte_plain_string"),
        pytest.param(
            _unlocked_fixture_text() + (" " * (1024 * 1024)),
            id="one_megabyte_padded_json",
        ),
        pytest.param(
            '{"result": {"passcodeRequired": NaN, "unlockedSinceBoot": true}}',
            id="json_nan_in_result",
        ),
        pytest.param(
            '{"info": {"jsonVersion": NaN}, '
            '"result": {"passcodeRequired": false, "unlockedSinceBoot": true}}',
            id="json_nan_in_info",
        ),
        pytest.param(
            '{"info": {"jsonVersion": Infinity}, '
            '"result": {"passcodeRequired": false, "unlockedSinceBoot": true}}',
            id="json_infinity",
        ),
    ],
)
def test_contract_parse_lock_state_hostile_inputs_return_unknown(hostile_input: str) -> None:
    """[contract] parse_lock_state over hostile inputs returns 'unknown' and never raises."""
    assert ios_mod.parse_lock_state(hostile_input) == "unknown"


def test_contract_parse_lock_state_size_boundary_and_non_str() -> None:
    """[contract] parse_lock_state enforces max size boundary and rejects non-str inputs."""
    base = _unlocked_fixture_text()
    max_chars = ios_mod._MAX_LOCKSTATE_CHARS
    exact = base + (" " * (max_chars - len(base)))
    over_by_one = exact + " "
    assert len(exact) == max_chars
    assert len(over_by_one) == max_chars + 1
    assert ios_mod.parse_lock_state(exact) == "unlocked"
    assert ios_mod.parse_lock_state(over_by_one) == "unknown"
    assert ios_mod.parse_lock_state(None) == "unknown"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "failure_mode",
    [
        pytest.param("timeout_expired", id="timeout_expired"),
        pytest.param("os_error", id="os_error"),
        pytest.param("xcode_not_ready", id="xcode_not_ready"),
        pytest.param("nonzero_coredevice_locked_text", id="nonzero_coredevice_locked_text"),
        pytest.param("nonzero_with_unlocked_stdout", id="nonzero_with_unlocked_stdout"),
        pytest.param("negative_returncode_with_unlocked_stdout", id="negative_returncode"),
    ],
)
def test_contract_failed_devicectl_yields_report_only_unknown_verdict(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    """[contract] Failed devicectl -> report_only + lock_state='unknown' (ready=True)."""

    def _failing_devicectl(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if failure_mode == "timeout_expired":
            raise subprocess.TimeoutExpired(cmd=["xcrun", "devicectl", *args], timeout=15)
        if failure_mode == "os_error":
            raise OSError("xcrun devicectl failed to spawn")
        if failure_mode == "xcode_not_ready":
            raise XcodeNotReadyError()
        if failure_mode == "nonzero_coredevice_locked_text":
            return subprocess.CompletedProcess(
                args=["xcrun", "devicectl", *args],
                returncode=1,
                stdout=CORE_DEVICE_LOCKED_STDERR,
                stderr=CORE_DEVICE_LOCKED_STDERR,
            )
        if failure_mode == "nonzero_with_unlocked_stdout":
            return subprocess.CompletedProcess(
                args=["xcrun", "devicectl", *args],
                returncode=1,
                stdout=_unlocked_fixture_text(),
                stderr=CORE_DEVICE_LOCKED_STDERR,
            )
        return subprocess.CompletedProcess(
            args=["xcrun", "devicectl", *args],
            returncode=-1,
            stdout=_unlocked_fixture_text(),
            stderr="",
        )

    _wire_ios_controller(monkeypatch, device=PHYS_UDID, run_devicectl=_failing_devicectl)

    out = _nerve_ensure_ready_sync(device=PHYS_UDID)

    assert out["ready"] is True
    assert out["report_only"] is True
    assert out["reason"] == "ios_physical_state_not_detectable"
    assert out["lock_state"] == "unknown"
    assert out["platform"] == "ios"
    assert out["awake"] is None
    assert out["unlocked"] is None
    assert out["battery_pct"] is None
    assert out["charging"] is None
    assert out["actions_taken"] == []
    assert out["network"]["note"] == "not checked on iOS"


def test_contract_ios_verdict_never_raises_when_lock_state_raises_or_returns_unexpected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] _ios_verdict falls back to 'unknown' if lock_state raises or is invalid."""
    fake_ios = MagicMock()
    fake_ios._is_physical.return_value = True
    fake_ios.lock_state.side_effect = RuntimeError("unexpected controller failure")
    monkeypatch.setattr(_core, "_ensure_initialized", lambda: (MagicMock(), fake_ios))
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: ({"serial": dev}, "ios"))

    out = _nerve_ensure_ready_sync(device=PHYS_UDID)
    assert out["ready"] is True
    assert out["report_only"] is True
    assert out["reason"] == "ios_physical_state_not_detectable"
    assert out["lock_state"] == "unknown"

    fake_ios.lock_state.side_effect = None
    fake_ios.lock_state.return_value = "bogus_state"
    out2 = _nerve_ensure_ready_sync(device=PHYS_UDID)
    assert out2["ready"] is True
    assert out2["report_only"] is True
    assert out2["reason"] == "ios_physical_state_not_detectable"
    assert out2["lock_state"] == "unknown"


def test_simulator_udid_never_calls_devicectl_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] Simulator UDID never calls devicectl and preserves simulator verdict (pin)."""
    _, calls = _wire_ios_controller(
        monkeypatch,
        device=SIM_UDID,
        run_devicectl=lambda *a, **kw: pytest.fail("Simulator must never call _run_devicectl"),
    )

    out = _nerve_ensure_ready_sync(device=SIM_UDID)

    assert calls == []
    assert out == {
        "ready": True,
        "reason": "simulator (no lock)",
        "platform": "ios",
        "awake": True,
        "unlocked": True,
        "battery_pct": None,
        "charging": True,
        "actions_taken": [],
        "network": state_tools._ios_network(),
    }


def test_ensure_ready_docstring_documents_ios_lock_state_and_ios_locked() -> None:
    """[test] _nerve_ensure_ready_sync Returns docstring documents lock_state and ios_locked."""
    doc = _nerve_ensure_ready_sync.__doc__ or ""
    returns_section = doc.split("Returns:", 1)[-1]
    assert "lock_state" in returns_section
    assert "ios_locked" in returns_section
