"""Contract and edge-case tests for transport_id cache invalidation, allow-list
scope, and fail-closed probe exception handling (bead linc-nerve-86ac).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog.testing

from linc_nerve.core import adb_health, engagement
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.ios import IOSController
from linc_nerve.daemon import http_router
from linc_nerve.mcp import server as mcp_mod
from linc_nerve.mcp.discovery_tools import _nerve_discover_sync
from linc_nerve.mcp.input_tools import _nerve_key_sync
from linc_nerve.mcp.reservation_tools import _nerve_reserve_sync

pytestmark = pytest.mark.usefixtures("restore_signal_handlers")

IP_ADDR = "10.9.9.9:5555"
BENCH_HW = "BENCHFAKE01"
PERSONAL_HW = "FAKEPERSONAL1"
MDNS_PERSONAL = f"adb-{PERSONAL_HW}-AbCd._adb._tcp"


class _FakeAdbDevice:
    def __init__(self, serial: str, shell_fn: Any) -> None:
        self.serial = serial
        self._shell_fn = shell_fn

    def shell(self, cmd: str, timeout: int = 10) -> str:
        return self._shell_fn(self.serial, cmd, timeout)


class _FakeAdbConnection:
    def __init__(self, devices_fn: Any, devices_l_fn: Any) -> None:
        self._devices_fn = devices_fn
        self._devices_l_fn = devices_l_fn
        self._cmd = ""

    def __enter__(self) -> _FakeAdbConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def send_command(self, cmd: str) -> None:
        self._cmd = cmd

    def check_okay(self) -> None:
        return None

    def read_string_block(self) -> str:
        if self._cmd == "host:devices-l":
            res = self._devices_l_fn()
            if isinstance(res, BaseException):
                raise res
            return res
        res = self._devices_fn()
        if isinstance(res, BaseException):
            raise res
        return res


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
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
        "NERVE_OWNER",
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
    http_router._DEVICES_CACHE = None
    engagement.clear_hardware_serial_cache()

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
    engagement.clear_hardware_serial_cache()


def _write_fleet(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _wire_dynamic_android(
    tmp_path: Path,
    *,
    devices_fn: Any,
    devices_l_fn: Any,
    serialno_fn: Any,
) -> tuple[AndroidController, list[tuple[str, str]], DeviceManager]:
    recorded_shells: list[tuple[str, str]] = []

    def _shell(serial: str, cmd: str, timeout: int = 10) -> str:
        recorded_shells.append((serial, cmd))
        if cmd == "getprop ro.serialno":
            val = serialno_fn(serial)
            if isinstance(val, BaseException):
                raise val
            return val
        if cmd == "getprop ro.product.model":
            return "Fake Device"
        if cmd == "getprop ro.build.version.release":
            return "15"
        if cmd == "getprop ro.build.characteristics":
            return "default"
        return ""

    mock_client = MagicMock()
    mock_client.make_connection.side_effect = lambda: _FakeAdbConnection(devices_fn, devices_l_fn)
    mock_client.device.side_effect = lambda serial: _FakeAdbDevice(serial, _shell)

    ctrl = AndroidController()
    ctrl._scrcpy_available = False
    ctrl._client = mock_client

    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    mgr = DeviceManager(db_path=tmp_path / "devices.db", controller=ctrl)
    mcp_mod._android = ctrl
    mcp_mod._ios = mock_ios
    mcp_mod._device_mgr = mgr
    return ctrl, recorded_shells, mgr


def _input_commands(shells: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(s, c) for s, c in shells if c.startswith("input ") or " input " in c]


# ---------------------------------------------------------------------------
# [contract] Acceptance 1: transport_id change invalidates cached identity
# ---------------------------------------------------------------------------


def test_contract_transport_id_change_invalidates_cached_identity_and_refuses_key(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] fake listing: 10.9.9.9:5555 with transport_id 7 resolves to
    BENCHFAKE01 (non-personal); then the same address reports transport_id 8 and
    ro.serialno=FAKEPERSONAL1 (registered personal); the next nerve_key on that
    address is refused and the fake shell receives no input command."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n"
        f"  - serial: {BENCH_HW}\n    personal: false\n"
        f"  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    state: dict[str, Any] = {"tid": "7", "hw": BENCH_HW, "listed": True}

    def _devices() -> str:
        return f"{IP_ADDR}\tdevice\n" if state["listed"] else ""

    def _devices_l() -> str:
        if not state["listed"]:
            return ""
        return (
            f"{IP_ADDR}          device product:fake model:Fake device:fake "
            f"transport_id:{state['tid']}\n"
        )

    _ctrl, shells, _mgr = _wire_dynamic_android(
        isolated_env["base"],
        devices_fn=_devices,
        devices_l_fn=_devices_l,
        serialno_fn=lambda _s: state["hw"],
    )

    # Freeze monotonic clocks so neither the 5s device cache nor the 30s hw cache expires.
    monkeypatch.setattr(engagement, "_hw_cache_clock", lambda: 1000.0)
    monkeypatch.setattr("linc_nerve.mcp._core._clock", lambda: 1000.0)

    discovered = _nerve_discover_sync()
    assert discovered["count"] == 1
    assert "usable" not in discovered["devices"][0]

    # Same transport_id 7 -> allowed, nerve_key reaches fake shell.
    first_key = _nerve_key_sync(IP_ADDR, "HOME")
    assert first_key == {"success": True, "action": "key", "key": "HOME"}
    assert len(_input_commands(shells)) == 1

    # Now the device behind 10.9.9.9:5555 changes: transport_id 8, FAKEPERSONAL1.
    shells.clear()
    state["tid"] = "8"
    state["hw"] = PERSONAL_HW

    second_key = _nerve_key_sync(IP_ADDR, "HOME")
    assert second_key.get("error_category") == "personal_device", second_key
    assert "personal" in second_key["error"].lower(), second_key
    assert _input_commands(shells) == []


# ---------------------------------------------------------------------------
# [contract] Acceptance 2: NERVE_ALLOW_PERSONAL_SERIALS scope
# ---------------------------------------------------------------------------


def test_contract_allow_personal_serials_rejects_network_address_allows_hw_serial(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] NERVE_ALLOW_PERSONAL_SERIALS=10.9.9.9:5555 does not admit the
    personal device on that address, while NERVE_ALLOW_PERSONAL_SERIALS=FAKEPERSONAL1
    still does."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    _ctrl, shells, mgr = _wire_dynamic_android(
        isolated_env["base"],
        devices_fn=lambda: f"{IP_ADDR}\tdevice\n",
        devices_l_fn=lambda: f"{IP_ADDR} device transport_id:7\n",
        serialno_fn=lambda _s: PERSONAL_HW,
    )

    monkeypatch.setenv("NERVE_ALLOW_PERSONAL_SERIALS", IP_ADDR)
    res_addr = _nerve_key_sync(IP_ADDR, "HOME")
    assert res_addr.get("error_category") == "personal_device", res_addr
    assert _nerve_reserve_sync(IP_ADDR).get("error_category") == "personal_device"
    assert isinstance(mgr.reserve(IP_ADDR), str)
    assert _input_commands(shells) == []

    # Same restriction for mDNS transport strings: transport string does not match,
    # only the matched entry/resolved hardware serial does.
    monkeypatch.setenv("NERVE_ALLOW_PERSONAL_SERIALS", MDNS_PERSONAL)
    res_mdns = _nerve_key_sync(MDNS_PERSONAL, "HOME")
    assert res_mdns.get("error_category") == "personal_device", res_mdns

    # Allowing by the hardware serial admits the device when engagement is OFF.
    monkeypatch.setenv("NERVE_ALLOW_PERSONAL_SERIALS", PERSONAL_HW)
    res_hw = _nerve_key_sync(IP_ADDR, "HOME")
    assert res_hw == {"success": True, "action": "key", "key": "HOME"}
    assert len(_input_commands(shells)) == 1


# ---------------------------------------------------------------------------
# [contract] Acceptance 3: probe exception returns refusal, logs 1 warning
# ---------------------------------------------------------------------------


def test_contract_probe_exception_returns_refusal_and_logs_one_warning(
    isolated_env: dict[str, Path],
) -> None:
    """[contract] a probe that raises returns the refusal, not a traceback, and
    logs one structured warning."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )

    def _boom_probe(_serial: str) -> str:
        raise RuntimeError("adb transport exploded")

    with structlog.testing.capture_logs() as logs:
        decision = engagement.resolve_device_decision(
            IP_ADDR,
            probe_hw=_boom_probe,
            probe_transport_ids=lambda: {IP_ADDR: "7"},
        )

    assert decision.allowed is False
    assert decision.error_category == "personal_device"
    assert decision.reason and "identity could not be verified" in decision.reason.lower()
    warnings = [entry for entry in logs if entry.get("log_level") == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["event"] == "network_identity_probe_failed"
    assert warnings[0]["serial"] == IP_ADDR
    assert "adb transport exploded" in warnings[0]["error"]

    # Also verify full MCP tool path (controller.read_hardware_serial raises):
    ctrl, shells, mgr = _wire_dynamic_android(
        isolated_env["base"],
        devices_fn=lambda: f"{IP_ADDR}\tdevice\n",
        devices_l_fn=lambda: f"{IP_ADDR} device transport_id:7\n",
        serialno_fn=lambda _s: BENCH_HW,
    )
    ctrl.read_hardware_serial = MagicMock(side_effect=RuntimeError("controller probe crashed"))  # type: ignore[method-assign]

    with structlog.testing.capture_logs() as tool_logs:
        res = _nerve_key_sync(IP_ADDR, "HOME")

    assert res.get("error_category") == "personal_device", res
    assert "identity could not be verified" in res["error"].lower()
    assert mgr.pool() == []
    assert _input_commands(shells) == []
    tool_warnings = [entry for entry in tool_logs if entry.get("log_level") == "warning"]
    assert len(tool_warnings) == 1


# ---------------------------------------------------------------------------
# Edge cases: unreadable transport_id, vanished device, fleet network entry
# ---------------------------------------------------------------------------


def test_unreadable_transport_id_skips_cache_and_reprobes_every_call(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When transport_id cannot be read (missing in listing or host:devices-l fails),
    the cache is never consulted and each call probes again."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    probe_calls = {"n": 0}
    state: dict[str, Any] = {"devices_l": f"{IP_ADDR} device transport_id:7\n", "hw": BENCH_HW}

    def _serialno(_s: str) -> str:
        probe_calls["n"] += 1
        return state["hw"]

    _ctrl, _shells, _mgr = _wire_dynamic_android(
        isolated_env["base"],
        devices_fn=lambda: f"{IP_ADDR}\tdevice\n",
        devices_l_fn=lambda: state["devices_l"],
        serialno_fn=_serialno,
    )
    monkeypatch.setattr(engagement, "_hw_cache_clock", lambda: 1000.0)
    monkeypatch.setattr("linc_nerve.mcp._core._clock", lambda: 1000.0)

    # Initial discovery caches BENCH_HW under transport_id 7.
    _nerve_discover_sync()
    assert probe_calls["n"] == 1

    # Enforcing calls probe afresh even while transport_id:7 is present (pre + post).
    assert _nerve_key_sync(IP_ADDR, "HOME")["success"] is True
    assert probe_calls["n"] == 3

    # Case 1: Listing omits transport_id -> cache ignored & dropped, probes again.
    state["devices_l"] = f"{IP_ADDR}\tdevice\n"
    state["hw"] = PERSONAL_HW
    res_no_tid = _nerve_key_sync(IP_ADDR, "HOME")
    assert res_no_tid.get("error_category") == "personal_device"
    assert probe_calls["n"] == 4

    # Case 2: host:devices-l raises -> cache still not used, probes again (pre + post checks).
    state["devices_l"] = RuntimeError("adb listing failed")
    state["hw"] = BENCH_HW
    assert _nerve_key_sync(IP_ADDR, "HOME")["success"] is True
    assert probe_calls["n"] == 6


def test_disappeared_transport_drops_cache_even_if_same_transport_id_returns(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a transport disappears from the listing and later reconnects reusing the
    same transport_id (e.g. after adb restart), the old cache entry was dropped."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    state: dict[str, Any] = {"listed": True, "tid": "7", "hw": BENCH_HW}

    def _devices() -> str:
        return f"{IP_ADDR}\tdevice\n" if state["listed"] else ""

    def _devices_l() -> str:
        if not state["listed"]:
            return ""
        return f"{IP_ADDR} device transport_id:{state['tid']}\n"

    _ctrl, shells, mgr = _wire_dynamic_android(
        isolated_env["base"],
        devices_fn=_devices,
        devices_l_fn=_devices_l,
        serialno_fn=lambda _s: state["hw"],
    )
    monkeypatch.setattr(engagement, "_hw_cache_clock", lambda: 1000.0)
    monkeypatch.setattr("linc_nerve.mcp._core._clock", lambda: 1000.0)

    assert _nerve_discover_sync()["count"] == 1

    # Disconnect -> listing no longer lists 10.9.9.9:5555 -> discover() prunes cache.
    state["listed"] = False
    assert _nerve_discover_sync()["count"] == 0

    # Reconnect with a personal device that happens to get the same transport_id=7.
    state["listed"] = True
    state["hw"] = PERSONAL_HW
    # Direct DeviceManager.reserve (no discover path) must not see the pruned BENCH_HW entry.
    direct_refusal = mgr.reserve(IP_ADDR, purpose="post-reconnect")
    assert isinstance(direct_refusal, str)
    assert "personal" in direct_refusal.lower()
    assert mgr.pool() == []
    res = _nerve_key_sync(IP_ADDR, "HOME")
    assert res.get("error_category") == "personal_device", res
    assert _input_commands(shells) == []


def test_allow_personal_ignores_network_address_even_if_in_fleet_and_allows_resolved_hw(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if device-fleet.yaml lists an IP address, NERVE_ALLOW_PERSONAL_SERIALS
    never matches a network address, while the resolved hardware serial does."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {IP_ADDR}\n    personal: true\n",
    )
    monkeypatch.setenv("NERVE_ALLOW_PERSONAL_SERIALS", IP_ADDR)
    dec_ip = engagement.device_decision(
        [IP_ADDR, PERSONAL_HW], resolved_hardware_serial=PERSONAL_HW
    )
    assert dec_ip.allowed is False
    assert dec_ip.error_category == "personal_device"

    monkeypatch.setenv("NERVE_ALLOW_PERSONAL_SERIALS", PERSONAL_HW)
    dec_hw = engagement.device_decision(
        [IP_ADDR, PERSONAL_HW], resolved_hardware_serial=PERSONAL_HW
    )
    assert dec_hw.allowed is True

    # An unrelated non-network string in identifiers cannot unlock a personal entry.
    monkeypatch.setenv("NERVE_ALLOW_PERSONAL_SERIALS", "UNRELATED-USB")
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    dec_other = engagement.device_decision(["UNRELATED-USB", PERSONAL_HW])
    assert dec_other.allowed is False
    assert dec_other.error_category == "personal_device"


def test_parse_adb_transport_ids_edge_cases() -> None:
    """Parser handles standard host:devices-l, headers, spaced mDNS, offline, and conflicts."""
    sample = (
        "List of devices attached\n"
        "* daemon started successfully\n"
        "USB001                 device usb:1-1 product:p model:m device:d transport_id:2\n"
        f"{IP_ADDR}          device product:x model:y device:z transport_id:7\n"
        "adb-FAKESERIAL1-x (2)._adb-tls-connect._tcp   device product:w transport_id:14\n"
        "10.9.9.10:5555         offline transport_id:9\n"
        "10.9.9.11:5555         device product:n\n"
        "10.9.9.12:5555         device transport_id:\n"
        "DUP01                  device transport_id:20\n"
        "DUP01                  device transport_id:21\n"
    )
    parsed = adb_health.parse_adb_transport_ids(sample)
    assert parsed == {
        "USB001": "2",
        IP_ADDR: "7",
        "adb-FAKESERIAL1-x (2)._adb-tls-connect._tcp": "14",
    }


def test_read_transport_ids_and_cache_boundary_guards(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AndroidController.read_transport_ids and cache helpers fail safe on errors/blanks."""
    # Empty / missing transport_id or non-network transport never populates cache.
    assert engagement.cache_hardware_serial(IP_ADDR, BENCH_HW, transport_id="") == ""
    assert engagement.cache_hardware_serial("USB001", BENCH_HW, transport_id="1") == ""
    assert engagement.cache_hardware_serial(IP_ADDR, "unknown", transport_id="1") == ""
    assert len(engagement._hw_serial_cache) == 0

    # Valid entry stored, then invalidated by sync(None, target=...) and sync(None).
    assert engagement.cache_hardware_serial(IP_ADDR, BENCH_HW, transport_id="7") == BENCH_HW
    assert engagement._cached_hardware_serial(IP_ADDR, transport_id="7") == BENCH_HW
    assert engagement._cached_hardware_serial(IP_ADDR, transport_id="") == ""
    engagement.sync_hardware_serial_cache(None, target=IP_ADDR)
    assert engagement._cached_hardware_serial(IP_ADDR, transport_id="7") == ""

    assert engagement.cache_hardware_serial(IP_ADDR, BENCH_HW, transport_id="7") == BENCH_HW
    engagement.sync_hardware_serial_cache(None)
    assert len(engagement._hw_serial_cache) == 0

    # read_transport_ids returns None on non-string block or client failure.
    ctrl = AndroidController()
    ctrl._client = MagicMock()
    bad_conn = MagicMock()
    bad_conn.__enter__.return_value = bad_conn
    bad_conn.read_string_block.return_value = MagicMock()  # non-str
    ctrl._client.make_connection.return_value = bad_conn
    assert ctrl.read_transport_ids() is None

    ctrl._client = None
    monkeypatch.setattr(ctrl, "_get_client", MagicMock(side_effect=OSError("no adb")))
    assert ctrl.read_transport_ids() is None

    # Listing probe exception inside resolve_device_decision still allows probe & refuses safely.
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    dec = engagement.resolve_device_decision(
        IP_ADDR,
        probe_transport_ids=MagicMock(side_effect=RuntimeError("listing boom")),
        probe_hw=lambda _s: PERSONAL_HW,
    )
    assert dec.allowed is False
    assert dec.error_category == "personal_device"


def test_contract_enforcing_decision_never_trusts_a_reused_transport_id(
    isolated_env: dict[str, Path],
) -> None:
    """[contract] after an adb server restart transport_ids start again at 1, so the
    same address and transport_id can belong to another device: an enforcing
    decision probes afresh instead of reusing the cached identity, while a
    display-only (no-probe) lookup may still use the cache."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    engagement.clear_hardware_serial_cache()
    listing = {IP_ADDR: "1"}
    first = engagement.resolve_device_decision(
        IP_ADDR, probe_hw=lambda _s: BENCH_HW, probe_transport_ids=lambda: dict(listing)
    )
    assert first.allowed is True

    probes: list[str] = []

    def _personal_probe(serial: str) -> str:
        probes.append(serial)
        return PERSONAL_HW

    # adb restarted; the personal device now holds the same address and transport_id 1
    second = engagement.resolve_device_decision(
        IP_ADDR, probe_hw=_personal_probe, probe_transport_ids=lambda: dict(listing)
    )
    assert probes == [IP_ADDR]
    assert second.allowed is False
    assert second.error_category == "personal_device"

    display = engagement.resolve_device_decision(
        IP_ADDR,
        probe_hw=_personal_probe,
        probe_transport_ids=lambda: dict(listing),
        allow_probe=False,
    )
    assert probes == [IP_ADDR]  # no extra probe: display path reads the cache
    assert display.allowed is False
