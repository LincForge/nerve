"""Contract tests for linc-nerve-6c33: failed-probe cache, lock-free probe,
doctor bounded probe budget, network transport forms, explicit probe wiring,
and first-tab split in parse_adb_transport_ids.
"""

from __future__ import annotations

import inspect
import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from click.testing import CliRunner

from linc_nerve.cli import doctor as doctor_mod
from linc_nerve.cli import main as cli_main
from linc_nerve.cli.main import main as cli_group
from linc_nerve.core import adb_health, android_identity, engagement
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.ios import IOSController
from linc_nerve.daemon import http_router
from linc_nerve.mcp import _core
from linc_nerve.mcp import server as mcp_mod
from linc_nerve.mcp.input_tools import _nerve_key_sync

pytestmark = pytest.mark.usefixtures("restore_signal_handlers")

PERSONAL_HW = "FAKEPERSONAL1"
BENCH_HW = "BENCHFAKE01"
IP_ADDR = "10.9.9.9:5555"


class _FakeAdbDevice:
    def __init__(self, serial: str, shell_fn: Any) -> None:
        self.serial = serial
        self._shell_fn = shell_fn

    def shell(self, cmd: str, timeout: int = 10) -> str:
        return self._shell_fn(self.serial, cmd, timeout)


class _FakeAdbConnection:
    def __init__(self, devices_payload: str, devices_l_payload: str = "") -> None:
        self._devices_payload = devices_payload
        self._devices_l_payload = devices_l_payload or devices_payload
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
        return self._devices_l_payload if self._cmd == "host:devices-l" else self._devices_payload


class _RefusingClient:
    def __enter__(self) -> _RefusingClient:
        return self

    def __exit__(self, *a: object) -> bool:
        return False

    def post(self, url: str, json: object = None, timeout: object = None) -> object:
        raise httpx.ConnectError("daemon down in test")


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


def _wire_fake_android(
    tmp_path: Path,
    serial_to_hw: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AndroidController, list[tuple[str, str]], DeviceManager]:
    recorded_shells: list[tuple[str, str]] = []

    def _shell(serial: str, cmd: str, timeout: int = 10) -> str:
        recorded_shells.append((serial, cmd))
        if cmd == "getprop ro.serialno":
            val = serial_to_hw.get(serial, "")
            if isinstance(val, BaseException):
                raise val
            if callable(val):
                return val(serial, cmd, timeout)
            return val
        if cmd == "getprop ro.product.model":
            return "Fake Device"
        if cmd == "getprop ro.build.version.release":
            return "15"
        if cmd == "getprop ro.build.characteristics":
            return "default"
        return ""

    payload = "".join(f"{s}\tdevice\n" for s in serial_to_hw)
    long_payload = "".join(
        f"{s}          device transport_id:{idx}\n" for idx, s in enumerate(serial_to_hw, start=1)
    )
    mock_client = MagicMock()
    mock_client.make_connection.side_effect = lambda: _FakeAdbConnection(payload, long_payload)
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
    monkeypatch.setattr(cli_main.TRANSPORT, "_client_factory", _RefusingClient)
    return ctrl, recorded_shells, mgr


# ---------------------------------------------------------------------------
# [contract] Acceptance 1: hanging probe failure cached across 5 calls
# ---------------------------------------------------------------------------


def test_contract_hanging_probe_five_consecutive_calls_take_under_two_timeouts(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] with a personal entry registered and a hanging probe, 5 consecutive
    calls on the same unreachable address take < 2x one probe timeout in total and
    all are refused."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    probe_timeout = 0.25
    probe_calls = {"n": 0}

    def _hanging_shell(_serial: str, _cmd: str, _timeout: int = 10) -> str:
        probe_calls["n"] += 1
        time.sleep(probe_timeout * 4)
        return BENCH_HW

    ctrl, shells, _mgr = _wire_fake_android(
        isolated_env["base"],
        {IP_ADDR: _hanging_shell},
        monkeypatch,
    )
    real_hard_timeout = ctrl._hard_timeout

    def _bounded_hard_timeout(fn: Any, *, timeout: int, label: str, **kw: Any) -> Any:
        return real_hard_timeout(fn, timeout=probe_timeout, label=label, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(ctrl, "_hard_timeout", _bounded_hard_timeout)

    clock = {"t": 1000.0}
    monkeypatch.setattr(engagement, "_hw_cache_clock", lambda: clock["t"])

    t0 = time.monotonic()
    results = [_nerve_key_sync(IP_ADDR, "HOME") for _ in range(5)]
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0 * probe_timeout, (
        f"5 calls took {elapsed:.3f}s (limit {2 * probe_timeout}s)"
    )
    assert probe_calls["n"] == 1
    for res in results:
        assert res.get("error_category") == "personal_device", res
        assert "identity could not be verified" in res["error"].lower(), res
    assert [c for _s, c in shells if c.startswith("input ")] == []

    # After the brief failure-cache TTL expires, the enforcing path probes afresh.
    clock["t"] += engagement._FAILED_PROBE_CACHE_TTL_S + 1.0
    res_after_ttl = _nerve_key_sync(IP_ADDR, "HOME")
    assert res_after_ttl.get("error_category") == "personal_device"
    assert probe_calls["n"] == 2


def test_contract_failure_cache_stays_fail_closed_and_success_never_skips_enforcing_probe(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] Cached probe failure refuses (never allows), and a cached success
    never lets an enforcing decision skip a fresh ro.serialno probe."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    clock = {"t": 2000.0}
    monkeypatch.setattr(engagement, "_hw_cache_clock", lambda: clock["t"])

    calls = {"n": 0}

    def _sleep_then_fail(_serial: str) -> str:
        calls["n"] += 1
        time.sleep(0.2)
        return ""

    t0 = time.monotonic()
    decisions = [
        engagement.resolve_device_decision(IP_ADDR, probe_hw=_sleep_then_fail) for _ in range(5)
    ]
    elapsed = time.monotonic() - t0

    assert elapsed < 0.4
    assert calls["n"] == 1
    assert all(d.allowed is False and d.error_category == "personal_device" for d in decisions)

    # Different transport_id explicitly supplied invalidates the failure entry.
    tid_calls = {"n": 0}

    def _tid_probe(_serial: str) -> str:
        tid_calls["n"] += 1
        return ""

    engagement.clear_hardware_serial_cache()
    engagement.resolve_device_decision(IP_ADDR, transport_id="7", probe_hw=_tid_probe)
    engagement.resolve_device_decision(IP_ADDR, transport_id="7", probe_hw=_tid_probe)
    assert tid_calls["n"] == 1
    engagement.resolve_device_decision(IP_ADDR, transport_id="8", probe_hw=_tid_probe)
    assert tid_calls["n"] == 2

    # Cached SUCCESS (display cache) must NEVER skip fresh probe on enforcing paths.
    engagement.clear_hardware_serial_cache()
    engagement.cache_hardware_serial(IP_ADDR, BENCH_HW, transport_id="7")
    listing_calls = {"n": 0}

    def _listing() -> dict[str, str]:
        listing_calls["n"] += 1
        return {IP_ADDR: "7"}

    enforcing = engagement.resolve_device_decision(
        IP_ADDR,
        probe_hw=lambda _s: PERSONAL_HW,
        probe_transport_ids=_listing,
    )
    assert enforcing.allowed is False
    assert enforcing.error_category == "personal_device"
    # Desk note (2): enforcing path does not call host:devices-l.
    assert listing_calls["n"] == 0


# ---------------------------------------------------------------------------
# [contract] Acceptance 2: tcp:/udp:, bare IPv4/IPv6, upper-case & .local. mDNS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transport",
    [
        "tcp:10.9.9.9:5555",
        "10.9.9.9",
        f"ADB-{PERSONAL_HW}-AB._ADB._TCP",
        f"adb-{PERSONAL_HW}-ab._adb._tcp.local.",
    ],
    ids=["tcp_ip_port", "bare_ipv4", "uppercase_mdns", "dot_local_mdns"],
)
def test_contract_acceptance_network_transport_forms_refused_for_personal_serial(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    """[contract] tcp:10.9.9.9:5555, 10.9.9.9, ADB-FAKEPERSONAL1-AB._ADB._TCP and
    adb-FAKEPERSONAL1-ab._adb._tcp.local. are classified as network transports and
    refused for the registered personal serial."""
    assert android_identity.is_network_transport(transport) is True

    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )

    # 1. Direct policy decision (even with no probe / unreachable probe) refuses.
    dec = engagement.resolve_device_decision(transport, probe_hw=lambda _s: PERSONAL_HW)
    assert dec.allowed is False, (transport, dec)
    assert dec.error_category == "personal_device"

    # 2. Allowlist ignores the network transport string itself.
    monkeypatch.setenv("NERVE_ALLOW_PERSONAL_SERIALS", transport)
    dec_allow_attempt = engagement.resolve_device_decision(
        transport, probe_hw=lambda _s: PERSONAL_HW
    )
    assert dec_allow_attempt.allowed is False
    assert dec_allow_attempt.error_category == "personal_device"
    monkeypatch.delenv("NERVE_ALLOW_PERSONAL_SERIALS", raising=False)

    # 3. Full MCP, HTTP daemon, and CLI paths refuse and send zero input commands.
    _ctrl, shells, mgr = _wire_fake_android(
        isolated_env["base"],
        {transport: PERSONAL_HW},
        monkeypatch,
    )
    res_mcp = _nerve_key_sync(transport, "HOME")
    assert res_mcp.get("error_category") == "personal_device", res_mcp

    from linc_nerve.mcp.sse_server import _get_tool_registry

    resp_http = http_router.route_request(
        "POST",
        "/tools/nerve_key",
        {"device": transport, "key": "HOME"},
        _get_tool_registry(),
    )
    assert resp_http["status"] == 200
    assert resp_http["body"]["result"].get("error_category") == "personal_device"

    cli_res = CliRunner().invoke(cli_group, ["key", "--device", transport, "--key", "HOME"])
    assert cli_res.exit_code == 1
    assert json.loads(cli_res.stderr)["error"]["details"]["error_category"] == "personal_device"
    assert mgr.pool() == []
    assert [c for _s, c in shells if c.startswith("input ")] == []


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("tcp:10.9.9.9:5555", True),
        ("TCP:10.9.9.9", True),
        ("udp:10.9.9.9:5555", True),
        ("UDP:watch.local:5555", True),
        ("10.9.9.9", True),
        ("::1", True),
        ("[::1]", True),
        ("2001:db8::1", True),
        (f"ADB-{PERSONAL_HW}-AB._ADB._TCP", True),
        (f"adb-{PERSONAL_HW}-ab._adb._tcp.local.", True),
        (f"adb-{PERSONAL_HW}-ab._adb-tls-connect._tcp.local.", True),
        ("adb-unknown-ab._adb._tcp.local.", True),
        ("123.5", False),
        ("012345", False),
        ("LABUSB001", False),
        ("emulator-5554", False),
        ("00008110-0011223344556677", False),
    ],
)
def test_is_network_transport_variants(target: str, expected: bool) -> None:
    assert android_identity.is_network_transport(target) is expected


def test_mdns_hardware_serial_handles_uppercase_and_dot_local() -> None:
    assert android_identity.mdns_hardware_serial(f"ADB-{PERSONAL_HW}-AB._ADB._TCP") == PERSONAL_HW
    assert (
        android_identity.mdns_hardware_serial(f"adb-{PERSONAL_HW}-ab._adb._tcp.local.")
        == PERSONAL_HW
    )
    assert (
        android_identity.mdns_hardware_serial(f"ADB-{PERSONAL_HW}-AB._ADB-TLS-CONNECT._TCP.LOCAL.")
        == PERSONAL_HW
    )


# ---------------------------------------------------------------------------
# [contract] Requirement (2): identity probe does not queue on device lock
# ---------------------------------------------------------------------------


def test_contract_read_hardware_serial_does_not_queue_behind_device_lock() -> None:
    """[contract] AndroidController.read_hardware_serial does not wait behind a long
    operation holding _device_lock(serial)."""
    ctrl = AndroidController()
    mock_device = _FakeAdbDevice(
        IP_ADDR, lambda _s, cmd, _t: PERSONAL_HW if cmd == "getprop ro.serialno" else ""
    )
    mock_client = MagicMock()
    mock_client.device.return_value = mock_device
    ctrl._client = mock_client

    lock = ctrl._device_lock(IP_ADDR)
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def _hold_lock() -> None:
        with lock:
            lock_acquired.set()
            release_lock.wait(timeout=5.0)

    holder = threading.Thread(target=_hold_lock, daemon=True)
    holder.start()
    assert lock_acquired.wait(timeout=2.0)
    try:
        t0 = time.monotonic()
        hw = ctrl.read_hardware_serial(IP_ADDR)
        elapsed = time.monotonic() - t0
        assert hw == PERSONAL_HW
        assert elapsed < 0.5, f"read_hardware_serial blocked on device lock ({elapsed:.3f}s)"
    finally:
        release_lock.set()
        holder.join(timeout=2.0)


# ---------------------------------------------------------------------------
# [contract] Requirement (3): doctor probes network serials with bounded budget
# ---------------------------------------------------------------------------


def test_contract_doctor_probes_network_serials_in_bounded_budget(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] cli/doctor.py probes multiple network serials with a bounded total
    budget (parallel / capped), not 1 full timeout each in series."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    serials = [f"10.9.9.{i}:5555" for i in range(1, 6)]
    adb_out = "List of devices attached\n" + "".join(f"{s}\tdevice\n" for s in serials)

    per_probe_sleep = 0.35

    def _slow_decision(serial: str = "", *_a: object, **_k: object) -> engagement.DeviceDecision:
        time.sleep(per_probe_sleep)
        return engagement.DeviceDecision(
            False, "refused", engagement.PERSONAL_DEVICE_ERROR_CATEGORY
        )

    monkeypatch.setattr(engagement, "resolve_device_decision", _slow_decision)

    t0 = time.monotonic()
    _state, _path, _err, _eng_id, _eng_err, refused = doctor_mod._probe_device_fleet(adb_out, None)
    elapsed = time.monotonic() - t0

    assert refused == 5
    assert elapsed < per_probe_sleep * 2.5, f"doctor ran in series ({elapsed:.3f}s)"

    # Also test budget cap when probes hang past the total budget: returns promptly, fails closed.
    monkeypatch.setattr(doctor_mod, "_FLEET_PROBE_BUDGET_S", 0.2)

    def _hung_decision(serial: str = "", *_a: object, **_k: object) -> engagement.DeviceDecision:
        time.sleep(0.5)
        return engagement.DeviceDecision(True)

    monkeypatch.setattr(engagement, "resolve_device_decision", _hung_decision)
    t1 = time.monotonic()
    _s2, _p2, _e2, _ei2, _ee2, refused_timeout = doctor_mod._probe_device_fleet(adb_out, None)
    elapsed_timeout = time.monotonic() - t1
    assert elapsed_timeout < 0.8
    assert refused_timeout == 5


# ---------------------------------------------------------------------------
# [contract] Requirement (5) & Desk Note (4): explicit probe & NerveDevice & tab split
# ---------------------------------------------------------------------------


def test_contract_no_hidden_sys_modules_coupling_and_device_model(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] engagement.py does not inspect sys.modules[...]._android, mcp/_core.py
    does not call private engagement._probe_via_controller, and NerveDevice carries
    hardware_serial + aliases attributes."""
    assert "sys.modules" not in inspect.getsource(engagement)
    assert "_probe_via_controller" not in inspect.getsource(_core)

    sentinel_ctrl = MagicMock(spec=AndroidController)
    sentinel_ctrl.read_hardware_serial.return_value = BENCH_HW
    mcp_mod._android = sentinel_ctrl

    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    # Direct resolve_device_decision with explicit controller uses it; without it, does
    # NOT reach into mcp_mod._android.
    import adbutils

    patched_client = MagicMock()
    patched_client.device.side_effect = OSError("no adb in unit test")
    monkeypatch.setattr(adbutils, "AdbClient", lambda **_kw: patched_client)

    dec_no_ctrl = engagement.resolve_device_decision(IP_ADDR)
    assert dec_no_ctrl.allowed is False
    sentinel_ctrl.read_hardware_serial.assert_not_called()

    dec_with_ctrl = engagement.resolve_device_decision(IP_ADDR, controller=sentinel_ctrl)
    assert dec_with_ctrl.allowed is True
    sentinel_ctrl.read_hardware_serial.assert_called_once_with(IP_ADDR)

    dev = NerveDevice(serial="ABC", platform=Platform.ANDROID, device_type=DeviceType.PHONE)
    assert dev.hardware_serial == ""
    assert dev.aliases == []
    assert set(dev.to_dict()) == {
        "serial",
        "platform",
        "device_type",
        "model",
        "os_version",
        "state",
    }


def test_parse_adb_transport_ids_splits_on_first_tab_only() -> None:
    """Desk note (4): tab-delimited host:devices-l lines split on the first tab only."""
    sample = (
        "List of devices attached\n"
        "my device\tdevice\tproduct:p\tmodel:m\ttransport_id:7\n"
        "10.9.9.9:5555\tdevice product:x transport_id:8\n"
        "offline device\toffline\ttransport_id:9\n"
        "bad\tnot_a_state\tdevice\ttransport_id:10\n"
        "onlyserial\t\n"
        "USB001                 device usb:1-1 product:p model:m device:d transport_id:2\n"
    )
    assert adb_health.parse_adb_transport_ids(sample) == {
        "my device": "7",
        "10.9.9.9:5555": "8",
        "USB001": "2",
    }


def test_enforcing_change_or_failure_invalidates_stale_display_cache_and_core_mgr_wiring(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforcing calls drop stale cached BENCH_HW on change/failure, and _core._init_singletons
    wires _device_mgr to the active _android controller without hidden sys.modules lookups."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    engagement.cache_hardware_serial(IP_ADDR, BENCH_HW, transport_id="7")

    # Enforcing call sees PERSONAL_HW -> refuses and drops stale BENCH_HW display cache.
    dec = engagement.resolve_device_decision(IP_ADDR, probe_hw=lambda _s: PERSONAL_HW)
    assert dec.allowed is False
    display_after = engagement.resolve_device_decision(
        IP_ADDR, allow_probe=False, probe_transport_ids=lambda: {IP_ADDR: "7"}
    )
    assert display_after.allowed is False

    # Also test _core._init_singletons wiring of _device_mgr with a fake _android.
    ctrl = MagicMock(spec=AndroidController)
    ctrl.read_hardware_serial.return_value = PERSONAL_HW
    mcp_mod._android = ctrl
    mcp_mod._ios = MagicMock(spec=IOSController)
    mcp_mod._state_mgr = MagicMock()
    mcp_mod._metrics = MagicMock()
    mcp_mod._emulator = MagicMock()
    mcp_mod._device_mgr = None
    _core._ensure_initialized()
    assert mcp_mod._device_mgr is not None
    # Point _device_mgr at an isolated temp DB path by instantiating via _init_singletons?
    # Verify _device_mgr has controller_getter bound to _sync_hw_cache_controller:
    assert mcp_mod._device_mgr._controller_getter is _core._sync_hw_cache_controller
    mcp_mod._device_mgr.close()
    mcp_mod._device_mgr = None

    # Exercise doctor bounded helper with raising worker + worker overflow past budget.
    def _raise_on_even(s: str) -> engagement.DeviceDecision:
        if s.endswith("2:5555"):
            raise RuntimeError("boom")
        time.sleep(0.4)
        return engagement.DeviceDecision(True)

    refused = doctor_mod._probe_network_serials_bounded(
        ["10.9.9.1:5555", "10.9.9.2:5555", "10.9.9.3:5555", "10.9.9.4:5555"],
        _raise_on_even,
        budget_s=0.1,
        max_workers=2,
    )
    assert refused == 4
    assert doctor_mod._probe_network_serials_bounded([], _raise_on_even) == 0
