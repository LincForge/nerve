"""Connectivity probe (linc-nerve-up9): parsers, controller mixin, ensure_ready gate, tool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.core import action_log
from linc_nerve.core.android import AndroidController
from linc_nerve.core.android_network import _CONNECTIVITY_CMD, _PING_CMD, _NetworkMixin
from linc_nerve.core.device_state import (
    NetworkInfo,
    Transport,
    apply_network_requirement,
    parse_connectivity,
    parse_ping,
)
from linc_nerve.mcp.state_tools import _nerve_ensure_ready_sync, _nerve_network_sync

# Real captures (scrubbed), 2026-10-04 on the mini — see fixtures/connectivity/README.md.
_FIXTURES = Path(__file__).parent / "fixtures" / "connectivity"
OFFLINE = (_FIXTURES / "tablet_offline.txt").read_text()  # Tab Active3, Android 12
TABLET_ONLINE = (_FIXTURES / "tablet_wifi_online.txt").read_text()  # Tab Active3, Android 12
ONLINE = (_FIXTURES / "phone_wifi_online.txt").read_text()  # Galaxy S24, Android 16
WATCH = (_FIXTURES / "watch_wifi_companion_proxy.txt").read_text()  # Pixel Watch 2, Android 13
PING_OK = (_FIXTURES / "ping_one_received.txt").read_text()  # Galaxy S24

_READY = {
    "awake": True,
    "unlocked": True,
    "battery_pct": 88,
    "charging": True,
    "ready": True,
    "reason": "ready",
    "actions_taken": ["extend_timeout", "stayon"],
    "platform": "android",
}

_OFFLINE_INFO = NetworkInfo(Transport.NONE, False, False)
_ONLINE_INFO = NetworkInfo(Transport.WIFI, True, True)
_UNKNOWN_INFO = NetworkInfo(Transport.UNKNOWN, None, None)


# ---------------------------------------------------------------------------
# parse_connectivity / parse_ping / NetworkInfo
# ---------------------------------------------------------------------------


def test_parse_connectivity_tablet_offline() -> None:
    info = parse_connectivity(OFFLINE)
    assert info.transport is Transport.NONE
    assert info.default_network is False
    assert info.validated is False
    assert info.online is False


def test_parse_connectivity_phone_wifi_online() -> None:
    info = parse_connectivity(ONLINE)
    assert info.transport is Transport.WIFI
    assert info.default_network is True
    assert info.validated is True
    assert info.online is True


def test_parse_connectivity_samsung_tablet_wifi_online() -> None:
    # Samsung's Android 12 record layout: ``Score(60 ; ...)`` and no ``created=`` stamp.
    info = parse_connectivity(TABLET_ONLINE)
    assert info.transport is Transport.WIFI
    assert info.default_network is True
    assert info.validated is True
    assert info.online is True


def test_parse_connectivity_garbage_is_unknown() -> None:
    info = parse_connectivity("garbage")
    assert info.transport is Transport.UNKNOWN
    assert info.default_network is None
    assert info.validated is None
    assert info.online is None


def test_parse_connectivity_picks_the_default_networks_record() -> None:
    # The watch prints two validated networks: Wi-Fi (231, the default) and the
    # phone companion proxy (251, Transports: BLUETOOTH). The id decides the record.
    assert parse_connectivity(WATCH).transport is Transport.WIFI
    info = parse_connectivity(
        WATCH.replace("Active default network: 231", "Active default network: 251")
    )
    assert info.transport is Transport.BLUETOOTH
    assert info.default_network is True
    assert info.validated is True


def test_parse_connectivity_default_record_not_validated() -> None:
    dump = ONLINE.replace("&VALIDATED&", "&", 1)
    info = parse_connectivity(dump)
    assert info.transport is Transport.WIFI
    assert info.validated is False
    assert info.online is False


def test_parse_connectivity_default_id_without_record_is_unvalidated_unknown() -> None:
    info = parse_connectivity("Active default network: 555\n\nCurrent Networks:\n")
    assert info.transport is Transport.UNKNOWN
    assert info.default_network is True
    assert info.validated is None
    assert info.online is None


def test_parse_connectivity_record_without_capabilities() -> None:
    dump = (
        "Active default network: 7\n  NetworkAgentInfo{network{7}  nc{[ Transports: ETHERNET ]}}\n"
    )
    info = parse_connectivity(dump)
    assert info.transport is Transport.ETHERNET
    assert info.default_network is True
    assert info.validated is None


@pytest.mark.parametrize(
    ("transports", "expected"),
    [
        ("WIFI", Transport.WIFI),
        ("CELLULAR", Transport.CELLULAR),
        ("BLUETOOTH", Transport.BLUETOOTH),
        ("WIFI|VPN", Transport.VPN),
        ("VPN", Transport.VPN),
        ("SATELLITE", Transport.UNKNOWN),
    ],
)
def test_parse_connectivity_transport_mapping(transports: str, expected: Transport) -> None:
    dump = (
        "Active default network: 9\n"
        f"  NetworkAgentInfo{{network{{9}}  nc{{[ Transports: {transports} "
        "Capabilities: INTERNET&VALIDATED ]}}\n"
    )
    assert parse_connectivity(dump).transport is expected


def test_parse_ping_samples() -> None:
    ok = PING_OK
    lost = "1 packets transmitted, 0 received, 100% packet loss, time 0ms"
    assert parse_ping(ok) is True
    assert parse_ping(lost) is False
    assert parse_ping("ping: permission denied (are you root?)") is None


def test_network_info_online_three_valued() -> None:
    assert NetworkInfo(Transport.WIFI, True, False).online is False
    assert NetworkInfo(Transport.WIFI, None, True).online is None
    assert NetworkInfo(Transport.WIFI, True, None).online is None


def test_network_info_as_dict() -> None:
    assert NetworkInfo(Transport.WIFI, True, True, reachable=False).as_dict() == {
        "transport": "wifi",
        "default_network": True,
        "validated": True,
        "reachable": False,
        "online": True,
    }


# ---------------------------------------------------------------------------
# _NetworkMixin.network_info
# ---------------------------------------------------------------------------


def _ctrl(
    outputs: dict[str, str] | None = None, exc: Exception | None = None
) -> AndroidController:
    ctrl = AndroidController()

    def fake_shell(serial, cmd, timeout=15):
        if exc is not None:
            raise exc
        return (outputs or {})[cmd]

    ctrl._shell = MagicMock(side_effect=fake_shell)
    return ctrl


def test_android_controller_has_network_mixin() -> None:
    assert issubclass(AndroidController, _NetworkMixin)


def test_network_info_runs_only_connectivity_cmd() -> None:
    ctrl = _ctrl({_CONNECTIVITY_CMD: ONLINE})
    info = ctrl.network_info("S")
    assert [c.args[1] for c in ctrl._shell.call_args_list] == [_CONNECTIVITY_CMD]
    assert info.online is True and info.reachable is None


def test_network_info_probe_runs_connectivity_then_ping() -> None:
    ping_ok = "1 packets transmitted, 1 received, 0% packet loss"
    ctrl = _ctrl({_CONNECTIVITY_CMD: ONLINE, _PING_CMD: ping_ok})
    info = ctrl.network_info("S", probe=True)
    assert [c.args[1] for c in ctrl._shell.call_args_list] == [_CONNECTIVITY_CMD, _PING_CMD]
    assert info.reachable is True


def test_network_info_probe_skips_ping_without_default_network() -> None:
    ctrl = _ctrl({_CONNECTIVITY_CMD: OFFLINE})
    info = ctrl.network_info("S", probe=True)
    assert [c.args[1] for c in ctrl._shell.call_args_list] == [_CONNECTIVITY_CMD]
    assert info.reachable is None and info.online is False


def test_network_info_shell_raising_is_all_unknown() -> None:
    info = _ctrl(exc=RuntimeError("adb gone")).network_info("S", probe=True)
    assert info == _UNKNOWN_INFO


def test_network_info_ping_raising_keeps_connectivity() -> None:
    ctrl = _ctrl({_CONNECTIVITY_CMD: ONLINE})
    info = ctrl.network_info("S", probe=True)
    assert info.online is True and info.reachable is None


def test_shell_strings_are_fixed_constants() -> None:
    assert _CONNECTIVITY_CMD == "dumpsys connectivity"
    assert _PING_CMD == "ping -c 1 -w 3 8.8.8.8"


# ---------------------------------------------------------------------------
# apply_network_requirement (pure)
# ---------------------------------------------------------------------------


def test_apply_requirement_off_only_adds_network_key() -> None:
    out = apply_network_requirement(dict(_READY), _OFFLINE_INFO, require=False)
    assert {k: out[k] for k in _READY} == _READY
    assert out["network"]["online"] is False


def test_apply_requirement_does_not_mutate_input() -> None:
    verdict = dict(_READY)
    apply_network_requirement(verdict, _OFFLINE_INFO, require=True)
    assert verdict == _READY


# ---------------------------------------------------------------------------
# _nerve_ensure_ready_sync
# ---------------------------------------------------------------------------


def _android_core(
    mock_core: MagicMock, verdict: dict, network=None, network_exc=None
) -> MagicMock:
    mock_core._resolve_device.return_value = (object(), "android")
    mock_core._check_guard.return_value = None
    ctrl = MagicMock()
    ctrl.ensure_ready.return_value = dict(verdict)
    if network_exc is not None:
        ctrl.network_info.side_effect = network_exc
    else:
        ctrl.network_info.return_value = network
    mock_core._get_controller.return_value = ctrl
    return ctrl


@patch("linc_nerve.mcp.state_tools._core")
def test_flag_off_keeps_verdict_and_adds_network(mock_core) -> None:
    ctrl = _android_core(mock_core, _READY, _OFFLINE_INFO)
    out = _nerve_ensure_ready_sync("S")
    assert out["ready"] is True and out["reason"] == "ready"
    for key, value in _READY.items():
        assert out[key] == value, key
    assert out["network"]["online"] is False
    assert out["network"]["transport"] == "none"
    ctrl.network_info.assert_called_once_with("S", probe=False)


@patch("linc_nerve.mcp.state_tools._core")
def test_flag_on_offline_is_not_ready_no_network(mock_core) -> None:
    _android_core(mock_core, _READY, _OFFLINE_INFO)
    out = _nerve_ensure_ready_sync("S", require_network=True)
    assert out["ready"] is False
    assert out["reason"].startswith("no_network")
    assert "transport none" in out["reason"]


@patch("linc_nerve.mcp.state_tools._core")
def test_flag_on_online_is_ready(mock_core) -> None:
    _android_core(mock_core, _READY, _ONLINE_INFO)
    out = _nerve_ensure_ready_sync("S", require_network=True)
    assert out["ready"] is True and out["reason"] == "ready"
    assert out["network"]["online"] is True


@patch("linc_nerve.mcp.state_tools._core")
def test_flag_on_unknown_is_network_unknown(mock_core) -> None:
    _android_core(mock_core, _READY, _UNKNOWN_INFO)
    out = _nerve_ensure_ready_sync("S", require_network=True)
    assert out["ready"] is False
    assert out["reason"].startswith("network_unknown")


@patch("linc_nerve.mcp.state_tools._core")
def test_flag_on_already_not_ready_keeps_reason(mock_core) -> None:
    locked = {**_READY, "ready": False, "reason": "locked (keyguard up)", "unlocked": False}
    _android_core(mock_core, locked, _OFFLINE_INFO)
    out = _nerve_ensure_ready_sync("S", require_network=True)
    assert out["ready"] is False and out["reason"] == "locked (keyguard up)"


@patch("linc_nerve.mcp.state_tools._core")
def test_network_info_raising_flag_off_leaves_verdict(mock_core) -> None:
    _android_core(mock_core, _READY, network_exc=RuntimeError("boom"))
    out = _nerve_ensure_ready_sync("S")
    assert out["ready"] is True and out["reason"] == "ready"
    assert out["network"]["online"] is None and out["network"]["transport"] == "unknown"


@patch("linc_nerve.mcp.state_tools._core")
def test_adb_unauthorized_skips_network_read(mock_core) -> None:
    unauth = {**_READY, "ready": False, "reason": "adb_unauthorized"}
    ctrl = _android_core(mock_core, unauth, _ONLINE_INFO)
    out = _nerve_ensure_ready_sync("S", require_network=True)
    ctrl.network_info.assert_not_called()
    assert out["reason"] == "adb_unauthorized"
    assert out["network"]["transport"] == "unknown" and out["network"]["online"] is None


@patch("linc_nerve.mcp.state_tools._core")
def test_controller_error_verdict_gets_no_network(mock_core) -> None:
    ctrl = _android_core(mock_core, {"error": "x"}, _ONLINE_INFO)
    out = _nerve_ensure_ready_sync("S", require_network=True)
    assert out == {"error": "x"}
    ctrl.network_info.assert_not_called()


@patch("linc_nerve.mcp.state_tools._core")
def test_non_networkinfo_return_is_treated_unknown(mock_core) -> None:
    _android_core(mock_core, _READY, network=MagicMock())
    out = _nerve_ensure_ready_sync("S")
    assert out["network"]["online"] is None


@patch("linc_nerve.mcp.state_tools._core")
def test_ios_simulator_ignores_require_network(mock_core) -> None:
    mock_core._resolve_device.return_value = (object(), "ios")
    ios = MagicMock()
    ios._is_physical.return_value = False
    mock_core._ensure_initialized.return_value = (MagicMock(), ios)
    out = _nerve_ensure_ready_sync("SIM", require_network=True)
    assert out["ready"] is True and out["reason"] == "simulator (no lock)"
    assert out["network"]["note"] == "not checked on iOS"
    assert out["network"]["online"] is None


@patch("linc_nerve.mcp.state_tools._core")
def test_ios_physical_ignores_require_network(mock_core) -> None:
    mock_core._resolve_device.return_value = (object(), "ios")
    ios = MagicMock()
    ios._is_physical.return_value = True
    mock_core._ensure_initialized.return_value = (MagicMock(), ios)
    out = _nerve_ensure_ready_sync("PHYS", require_network=True)
    assert out["ready"] is True and out["report_only"] is True
    assert out["network"]["note"] == "not checked on iOS"


# ---------------------------------------------------------------------------
# _nerve_network_sync
# ---------------------------------------------------------------------------

_SEVEN = {"device", "platform", "transport", "default_network", "validated", "reachable", "online"}


@patch("linc_nerve.mcp.state_tools._core")
def test_network_tool_android_returns_seven_keys_read_only(mock_core) -> None:
    mock_core._resolve_device.return_value = (object(), "android")
    ctrl = MagicMock()
    ctrl.network_info.return_value = _ONLINE_INFO
    mock_core._get_controller.return_value = ctrl
    out = _nerve_network_sync("S")
    assert set(out) == _SEVEN
    assert out["online"] is True and out["transport"] == "wifi" and out["device"] == "S"
    ctrl.network_info.assert_called_once_with("S", probe=False)
    called = {c[0].split(".")[0] for c in ctrl.method_calls}
    assert called == {"network_info"}
    ctrl.ensure_ready.assert_not_called()


@patch("linc_nerve.mcp.state_tools._core")
def test_network_tool_probe_passes_through(mock_core) -> None:
    mock_core._resolve_device.return_value = (object(), "android")
    ctrl = MagicMock()
    ctrl.network_info.return_value = NetworkInfo(Transport.WIFI, True, True, reachable=False)
    mock_core._get_controller.return_value = ctrl
    out = _nerve_network_sync("S", probe=True)
    ctrl.network_info.assert_called_once_with("S", probe=True)
    assert out["reachable"] is False


@patch("linc_nerve.mcp.state_tools._core")
def test_network_tool_controller_raising_is_unknown(mock_core) -> None:
    mock_core._resolve_device.return_value = (object(), "android")
    ctrl = MagicMock()
    ctrl.network_info.side_effect = RuntimeError("boom")
    mock_core._get_controller.return_value = ctrl
    out = _nerve_network_sync("S")
    assert set(out) == _SEVEN and out["transport"] == "unknown" and out["online"] is None


@patch("linc_nerve.mcp.state_tools._core")
def test_network_tool_ios_is_unknown_with_note(mock_core) -> None:
    mock_core._resolve_device.return_value = (object(), "ios")
    out = _nerve_network_sync("SIM")
    assert set(out) >= _SEVEN
    assert out["platform"] == "ios" and out["note"] == "not checked on iOS"
    assert out["online"] is None
    mock_core._get_controller.assert_not_called()


@patch("linc_nerve.mcp.state_tools._core")
def test_network_tool_unknown_device_errors(mock_core) -> None:
    mock_core._resolve_device.return_value = (None, "Device 'X' not found.")
    assert _nerve_network_sync("X") == {"error": "Device 'X' not found."}


def test_nerve_network_is_readonly_in_action_log() -> None:
    assert "nerve_network" in action_log.READONLY_TOOLS
