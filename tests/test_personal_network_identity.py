"""Contract and hostile-input tests for R4 personal-device network identity
(bead linc-nerve-eh10).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from click.testing import CliRunner

from linc_nerve.cli import main as cli_main
from linc_nerve.cli.main import main as cli_group
from linc_nerve.core import engagement
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.ios import IOSController
from linc_nerve.daemon import http_router
from linc_nerve.mcp import server as mcp_mod
from linc_nerve.mcp.app_tools import _nerve_kill_sync, _nerve_launch_sync
from linc_nerve.mcp.discovery_tools import _nerve_discover_sync
from linc_nerve.mcp.input_tools import _nerve_key_sync
from linc_nerve.mcp.reservation_tools import _nerve_reserve_sync
from linc_nerve.mcp.sse_server import _get_tool_registry

pytestmark = pytest.mark.usefixtures("restore_signal_handlers")

PERSONAL_HW = "FAKEPERSONAL1"
CLEARED_HW = "FAKELAB001"
IP_TRANSPORT = "10.9.9.9:5555"
MDNS_NON_TLS = f"adb-{PERSONAL_HW}-AbCd._adb._tcp"
MDNS_TLS = f"adb-{PERSONAL_HW}-AbCd._adb-tls-connect._tcp"
USB_LAB = "LABUSB001"
PKG = "com.example.target"


class _FakeAdbDevice:
    """Minimal adbutils device stand-in recording every shell command."""

    def __init__(self, serial: str, shell_fn: Any) -> None:
        self.serial = serial
        self._shell_fn = shell_fn

    def shell(self, cmd: str, timeout: int = 10) -> str:
        return self._shell_fn(self.serial, cmd, timeout)


class _FakeAdbConnection:
    def __init__(self, payload: str, long_payload: str = "") -> None:
        self._payload = payload
        self._long_payload = long_payload or payload
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
        return self._long_payload if self._cmd == "host:devices-l" else self._payload


class _RefusingClient:
    """Force CLI HttpDaemonTransport onto LocalTransport in-process fallback."""

    def __enter__(self) -> _RefusingClient:
        return self

    def __exit__(self, *a: object) -> bool:
        return False

    def post(self, url: str, json: object = None, timeout: object = None) -> object:
        raise httpx.ConnectError("daemon down in test")


def _make_fake_android(
    serial_to_hw: dict[str, str | Exception],
    *,
    device_types: dict[str, str] | None = None,
) -> tuple[AndroidController, list[tuple[str, str]]]:
    """Build a real AndroidController backed by a fake adb shell recorder."""
    recorded_shells: list[tuple[str, str]] = []
    device_types = device_types or {}

    def _shell(serial: str, cmd: str, timeout: int = 10) -> str:
        recorded_shells.append((serial, cmd))
        if cmd == "getprop ro.serialno":
            val = serial_to_hw.get(serial, "")
            if isinstance(val, Exception):
                raise val
            if callable(val):
                return val(serial, cmd, timeout)
            return val
        if cmd == "getprop ro.product.model":
            return "Fake Device"
        if cmd == "getprop ro.build.version.release":
            return "15"
        if cmd == "getprop ro.build.characteristics":
            return device_types.get(serial, "default")
        return ""

    payload = "".join(f"{s}\tdevice\n" for s in serial_to_hw)
    long_payload = "".join(
        f"{s}          device transport_id:{idx}\n" for idx, s in enumerate(serial_to_hw, start=1)
    )
    fake_devs = {s: _FakeAdbDevice(s, _shell) for s in serial_to_hw}

    def _device_lookup(serial: str) -> _FakeAdbDevice:
        if serial not in fake_devs:
            return _FakeAdbDevice(serial, _shell)
        return fake_devs[serial]

    mock_client = MagicMock()
    mock_client.make_connection.side_effect = lambda: _FakeAdbConnection(payload, long_payload)
    mock_client.device.side_effect = _device_lookup

    ctrl = AndroidController()
    ctrl._scrcpy_available = False
    ctrl._client = mock_client
    return ctrl, recorded_shells


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


def _wire(
    tmp_path: Path,
    serial_to_hw: dict[str, str | Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AndroidController, list[tuple[str, str]], DeviceManager]:
    ctrl, shells = _make_fake_android(serial_to_hw)
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    mgr = DeviceManager(db_path=tmp_path / "devices.db", controller=ctrl)
    mcp_mod._android = ctrl
    mcp_mod._ios = mock_ios
    mcp_mod._device_mgr = mgr
    monkeypatch.setattr(cli_main.TRANSPORT, "_client_factory", _RefusingClient)
    return ctrl, shells, mgr


def _write_fleet(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _am_or_input_commands(shells: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [
        (s, c)
        for s, c in shells
        if c.startswith("am ") or c.startswith("input ") or " am " in c or " input " in c
    ]


# ---------------------------------------------------------------------------
# [contract] Personal device refused across ip:port and _adb._tcp on every path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("transport", [IP_TRANSPORT, MDNS_NON_TLS])
def test_contract_personal_network_transports_refused_across_all_paths(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    """[contract] fleet registers FAKEPERSONAL1 with personal: true; fake adb shell
    reports ro.serialno=FAKEPERSONAL1 for 10.9.9.9:5555 and adb-FAKEPERSONAL1-AbCd._adb._tcp.
    kill, key, launch and reserve on each transport return personal_device refusal across
    MCP tools, HTTP daemon, CLI in-process, and DeviceManager.reserve, and the fake shell
    receives NO am/input command."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    _ctrl, shells, mgr = _wire(
        isolated_env["base"],
        {IP_TRANSPORT: PERSONAL_HW, MDNS_NON_TLS: PERSONAL_HW},
        monkeypatch,
    )

    # 1. MCP tool sync calls
    res_kill = _nerve_kill_sync(transport, PKG)
    res_key = _nerve_key_sync(transport, "HOME")
    res_launch = _nerve_launch_sync(transport, PKG)
    res_reserve = _nerve_reserve_sync(transport, purpose="test")
    for res in (res_kill, res_key, res_launch, res_reserve):
        assert res.get("error_category") == "personal_device", res
        assert "personal" in res["error"].lower(), res

    # 2. Direct DeviceManager.reserve
    direct_reserve = mgr.reserve(transport, purpose="direct")
    assert isinstance(direct_reserve, str)
    assert "personal" in direct_reserve.lower()
    assert mgr.pool() == []

    # 3. HTTP daemon routes
    registry = _get_tool_registry()
    for tool_name, body in [
        ("nerve_kill", {"device": transport, "package": PKG}),
        ("nerve_key", {"device": transport, "key": "HOME"}),
        ("nerve_launch", {"device": transport, "package": PKG}),
        ("nerve_reserve", {"device": transport, "purpose": "http"}),
    ]:
        resp = http_router.route_request("POST", f"/tools/{tool_name}", body, registry)
        assert resp["status"] == 200
        assert resp["body"]["result"].get("error_category") == "personal_device", resp

    # 4. CLI in-process (LocalTransport)
    runner = CliRunner()
    for argv in [
        ["kill", transport, PKG],
        ["key", "--device", transport, "--key", "HOME"],
        ["launch", transport, PKG],
    ]:
        cli_res = runner.invoke(cli_group, argv)
        assert cli_res.exit_code == 1, cli_res.stderr
        assert cli_res.stdout == ""
        env_err = json.loads(cli_res.stderr)["error"]
        assert env_err["details"]["error_category"] == "personal_device"

    # Zero am/input commands ever reached the fake shell.
    assert _am_or_input_commands(shells) == []
    if transport == MDNS_NON_TLS:
        assert [c for _s, c in shells if "ro.serialno" in c] == []
    assert mgr.pool() == []


# ---------------------------------------------------------------------------
# [contract] Unresolved network transport fails closed iff a personal entry exists
# ---------------------------------------------------------------------------


def test_contract_probe_failure_refused_with_personal_and_allowed_without_personal(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] A network transport whose probe fails is refused while a personal
    entry exists, and allowed when the registry has no personal entries."""
    # Case A: Registry has a personal: true entry (for a DIFFERENT device).
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    _ctrl, shells, mgr = _wire(
        isolated_env["base"],
        {IP_TRANSPORT: OSError("adb transport offline"), USB_LAB: ""},
        monkeypatch,
    )

    for res in (
        _nerve_kill_sync(IP_TRANSPORT, PKG),
        _nerve_key_sync(IP_TRANSPORT, "HOME"),
        _nerve_launch_sync(IP_TRANSPORT, PKG),
        _nerve_reserve_sync(IP_TRANSPORT),
    ):
        assert res.get("error_category") == "personal_device", res
        assert "identity could not be verified" in res["error"].lower(), res

    direct = mgr.reserve(IP_TRANSPORT)
    assert isinstance(direct, str)
    assert "identity could not be verified" in direct.lower()
    assert mgr.pool() == []
    assert _am_or_input_commands(shells) == []

    # USB serial keeps today's behavior even while personal entry exists.
    assert _nerve_kill_sync(USB_LAB, PKG) == {"success": True, "action": "kill", "package": PKG}

    # Case B: Registry has NO personal: true entries -> allowed even when probe fails.
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {CLEARED_HW}\n    personal: false\n",
    )
    assert _nerve_kill_sync(IP_TRANSPORT, PKG) == {
        "success": True,
        "action": "kill",
        "package": PKG,
    }
    assert _nerve_key_sync(IP_TRANSPORT, "HOME")["success"] is True
    assert _nerve_launch_sync(IP_TRANSPORT, PKG)["success"] is True
    assert _nerve_reserve_sync(IP_TRANSPORT)["success"] is True


# ---------------------------------------------------------------------------
# Hostile cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_output",
    [
        f"  {PERSONAL_HW}\r\n",
        f"\t{PERSONAL_HW} \r\n",
        f"{PERSONAL_HW}\n",
    ],
    ids=["spaces_crlf", "tab_crlf", "newline"],
)
def test_hostile_probe_output_whitespace_crlf_refuses_personal(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    raw_output: str,
) -> None:
    """Hostile ro.serialno output with surrounding whitespace/CRLF still matches personal."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    _ctrl, shells, _mgr = _wire(
        isolated_env["base"],
        {IP_TRANSPORT: raw_output},
        monkeypatch,
    )

    res = _nerve_kill_sync(IP_TRANSPORT, PKG)
    assert res.get("error_category") == "personal_device"
    assert "registered as a personal device" in res["error"]
    assert _am_or_input_commands(shells) == []


@pytest.mark.parametrize(
    "placeholder",
    ["unknown", "UNKNOWN\r\n", "0123456789ABCDEF", "0000000000000000", "", "   \r\n"],
    ids=["unknown", "unknown_crlf", "0123456789ABCDEF", "all_zeros", "empty", "blank_crlf"],
)
def test_hostile_placeholder_serial_fails_closed_when_personal_exists(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    placeholder: str,
) -> None:
    """Hostile placeholder/empty ro.serialno outputs fail closed as personal_device."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    _ctrl, shells, mgr = _wire(
        isolated_env["base"],
        {IP_TRANSPORT: placeholder},
        monkeypatch,
    )

    res = _nerve_kill_sync(IP_TRANSPORT, PKG)
    assert res.get("error_category") == "personal_device"
    assert "identity could not be verified" in res["error"].lower()
    assert _nerve_reserve_sync(IP_TRANSPORT).get("error_category") == "personal_device"
    assert mgr.pool() == []
    assert _am_or_input_commands(shells) == []


def test_hostile_hanging_probe_times_out_and_fails_closed(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ro.serialno probe that hangs past the hard timeout times out and fails closed."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )

    def _hanging_shell(_serial: str, _cmd: str, _timeout: int = 10) -> str:
        time.sleep(5)
        return CLEARED_HW

    ctrl, shells, _mgr = _wire(
        isolated_env["base"],
        {IP_TRANSPORT: _hanging_shell},
        monkeypatch,
    )
    real_hard_timeout = ctrl._hard_timeout

    def _fast_hard_timeout(fn: Any, *, timeout: int, label: str, **kw: Any) -> Any:
        return real_hard_timeout(fn, timeout=0.05, label=label, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(ctrl, "_hard_timeout", _fast_hard_timeout)

    t0 = time.monotonic()
    res = _nerve_kill_sync(IP_TRANSPORT, PKG)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0
    assert res.get("error_category") == "personal_device"
    assert "identity could not be verified" in res["error"].lower()
    assert _am_or_input_commands(shells) == []


@pytest.mark.parametrize(
    "hostile_ip",
    [f"{IP_TRANSPORT}\n", f"{IP_TRANSPORT}\r\n", f"{IP_TRANSPORT}\n;id"],
    ids=["trailing_lf", "trailing_crlf", "lf_injection"],
)
def test_hostile_ip_port_trailing_newline_refused_across_paths(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    hostile_ip: str,
) -> None:
    """An ip:port with a trailing newline cannot bypass network-transport verification."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    _ctrl, shells, mgr = _wire(
        isolated_env["base"],
        {IP_TRANSPORT: CLEARED_HW},
        monkeypatch,
    )

    for res in (
        _nerve_kill_sync(hostile_ip, PKG),
        _nerve_key_sync(hostile_ip, "HOME"),
        _nerve_launch_sync(hostile_ip, PKG),
        _nerve_reserve_sync(hostile_ip),
    ):
        assert res.get("error_category") == "personal_device", res

    direct = mgr.reserve(hostile_ip)
    assert isinstance(direct, str)
    assert "personal" in direct.lower() or "identity could not be verified" in direct.lower()
    assert mgr.pool() == []
    assert _am_or_input_commands(shells) == []


# ---------------------------------------------------------------------------
# Opt-in & caching & discovery reuse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("transport", [IP_TRANSPORT, MDNS_NON_TLS])
def test_allow_personal_serials_opt_in_works_for_resolved_hardware_serial(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    """NERVE_ALLOW_PERSONAL_SERIALS=FAKEPERSONAL1 allows the resolved transport when
    engagement is OFF, and is ignored when engagement is ON."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    _ctrl, _shells, _mgr = _wire(
        isolated_env["base"],
        {IP_TRANSPORT: PERSONAL_HW, MDNS_NON_TLS: PERSONAL_HW},
        monkeypatch,
    )

    monkeypatch.setenv("NERVE_ALLOW_PERSONAL_SERIALS", PERSONAL_HW)
    assert _nerve_kill_sync(transport, PKG)["success"] is True
    assert _nerve_reserve_sync(transport)["success"] is True
    runner = CliRunner()
    assert runner.invoke(cli_group, ["kill", transport, PKG]).exit_code == 0

    monkeypatch.setenv("NERVE_ENGAGEMENT", "eng-1")
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(isolated_env["vault"]))
    assert _nerve_kill_sync(transport, PKG).get("error_category") == "personal_device"
    assert _nerve_reserve_sync(transport).get("error_category") == "personal_device"
    cli_eng = runner.invoke(cli_group, ["kill", transport, PKG])
    assert cli_eng.exit_code == 1
    assert json.loads(cli_eng.stderr)["error"]["details"]["error_category"] == "personal_device"


def test_discovery_caches_identity_but_enforcing_calls_probe_afresh(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery probes ro.serialno once and caches it for display, but every enforcing
    call (kill, key, reserve) reads it afresh, so an address that now belongs to a
    personal device is refused at once, without waiting for the cache TTL."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    current_hw = {"val": CLEARED_HW}
    probe_count = {"n": 0}

    def _dynamic_shell(_serial: str, _cmd: str, _timeout: int = 10) -> str:
        probe_count["n"] += 1
        return current_hw["val"]

    _ctrl, _shells, _mgr = _wire(
        isolated_env["base"],
        {IP_TRANSPORT: _dynamic_shell},
        monkeypatch,
    )

    now = {"t": 1000.0}
    monkeypatch.setattr(engagement, "_hw_cache_clock", lambda: now["t"])

    # nerve_discover probes ro.serialno once (during discover()) and annotates from the
    # cache; kill, key and reserve each probe afresh (pre- and post-action checks).
    discovered = _nerve_discover_sync()
    assert discovered["count"] == 1
    assert "usable" not in discovered["devices"][0]
    assert probe_count["n"] == 1

    assert _nerve_kill_sync(IP_TRANSPORT, PKG)["success"] is True
    assert _nerve_key_sync(IP_TRANSPORT, "HOME")["success"] is True
    assert _nerve_reserve_sync(IP_TRANSPORT)["success"] is True
    assert probe_count["n"] == 7

    # The address is reassigned to FAKEPERSONAL1 well inside the cache TTL: still refused.
    now["t"] += 1.0
    current_hw["val"] = PERSONAL_HW
    res = _nerve_kill_sync(IP_TRANSPORT, PKG)
    assert res.get("error_category") == "personal_device"
    assert probe_count["n"] == 8


def test_discovery_grouped_device_attributes_reused_without_probing(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When discovery already attached hardware_serial / aliases on a device object,
    policy checks reuse that identity without issuing another ro.serialno probe."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )

    class _GroupedDevice:
        def __init__(
            self,
            serial: str,
            hardware_serial: str,
            aliases: tuple[str, ...] = (),
        ) -> None:
            self.serial = serial
            self.platform = Platform.ANDROID
            self.device_type = DeviceType.WATCH
            self.model = "Watch"
            self.os_version = "14"
            self.state = "online"
            self.hardware_serial = hardware_serial
            self.aliases = aliases

        def to_dict(self) -> dict[str, Any]:
            return {"serial": self.serial, "platform": "android", "device_type": "watch"}

    probes: list[str] = []
    monkeypatch.setattr(
        engagement, "_default_probe_hardware_serial", lambda s: probes.append(s) or ""
    )

    personal_grouped = _GroupedDevice(IP_TRANSPORT, PERSONAL_HW, (MDNS_NON_TLS,))
    annotated = engagement.annotate_discovered_device(personal_grouped)
    assert annotated["usable"] is False
    assert "personal" in annotated["refusal"].lower()

    cleared_grouped = _GroupedDevice("10.9.9.10:5555", CLEARED_HW)
    annotated_ok = engagement.annotate_discovered_device(cleared_grouped)
    assert "usable" not in annotated_ok
    assert probes == []


@pytest.mark.parametrize(
    "transport",
    [
        "watch.local:5555",
        "[::1]:5555",
        "adb-unknown-AbCd._adb._tcp",
        "adb-0123456789ABCDEF-AbCd._adb-tls-connect._tcp",
        "adb-FAKEPERSONAL1-AbCd._adb-tls-pairing._tcp",
    ],
)
def test_other_unmapped_network_transports_probe_and_fail_closed(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    """Hostname host:port, IPv6 [::1]:port, and mDNS with missing/placeholder serial
    all probe ro.serialno and fail closed when personal devices exist."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    _ctrl, shells, _mgr = _wire(
        isolated_env["base"],
        {transport: OSError("offline")},
        monkeypatch,
    )

    res = _nerve_kill_sync(transport, PKG)
    assert res.get("error_category") == "personal_device"
    assert "identity could not be verified" in res["error"].lower()
    assert _am_or_input_commands(shells) == []


def test_standalone_device_manager_reserve_and_http_devices_and_doctor(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone DeviceManager.reserve (even with _core._android=None), HTTP /devices,
    and nerve doctor all enforce resolved hardware serial policy."""
    import adbutils

    from linc_nerve.cli import doctor as doctor_mod
    from linc_nerve.core import android as android_mod
    from linc_nerve.core import ios as ios_mod

    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    ctrl, shells = _make_fake_android({IP_TRANSPORT: PERSONAL_HW, MDNS_NON_TLS: PERSONAL_HW})
    monkeypatch.setattr(adbutils, "AdbClient", lambda **_kw: ctrl._client)

    # Standalone DeviceManager.reserve with _core._android = None
    assert mcp_mod._android is None
    mgr = DeviceManager(db_path=isolated_env["base"] / "standalone.db")
    try:
        refusal = mgr.reserve(IP_TRANSPORT, purpose="standalone")
        assert isinstance(refusal, str)
        assert "personal" in refusal.lower()
        assert mgr.pool() == []
        assert mgr.reserve(MDNS_NON_TLS, purpose="standalone-mdns")
        assert mgr.pool() == []
    finally:
        mgr.close()

    # HTTP GET /devices annotates both IP and non-TLS mDNS transports
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    monkeypatch.setattr(android_mod, "AndroidController", lambda: ctrl)
    monkeypatch.setattr(ios_mod, "IOSController", lambda: mock_ios)
    http_router._DEVICES_CACHE = None
    devices_resp = http_router.route_request("GET", "/devices", {}, {})
    assert devices_resp["status"] == 200
    by_serial = {d["serial"]: d for d in devices_resp["body"]["devices"]}
    assert by_serial[IP_TRANSPORT]["usable"] is False
    assert by_serial[MDNS_NON_TLS]["usable"] is False

    # nerve doctor counts both refused network transports
    adb_out = f"List of devices attached\n{IP_TRANSPORT}\tdevice\n{MDNS_NON_TLS}\tdevice\n"
    _state, _path, _err, _eng_id, _eng_err, refused_count = doctor_mod._probe_device_fleet(
        adb_out, None
    )
    assert refused_count == 2
    assert _am_or_input_commands(shells) == []


def test_discover_failure_records_failed_serial_and_skips_double_probe(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AndroidController.discover() fails while reading properties for an unmapped
    network transport, it records the transport in _last_discover_failed_serials so
    _nerve_discover_sync() refuses closed without issuing a redundant second probe."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    probe_calls: list[str] = []

    def _flaky_shell(serial: str, cmd: str, _timeout: int = 10) -> str:
        if cmd == "getprop ro.serialno":
            probe_calls.append(serial)
        raise OSError("device disconnected mid-discover")

    ctrl, _shells, _mgr = _wire(
        isolated_env["base"],
        {IP_TRANSPORT: _flaky_shell},
        monkeypatch,
    )
    monkeypatch.setattr(
        engagement,
        "_default_probe_hardware_serial",
        lambda s: probe_calls.append(f"default:{s}") or "",
    )

    discovered = _nerve_discover_sync()
    assert IP_TRANSPORT in ctrl._last_discover_failed_serials
    assert discovered["count"] == 1
    assert discovered["devices"][0]["usable"] is False
    assert "identity could not be verified" in discovered["devices"][0]["refusal"].lower()
    # One probe inside discover(), zero second probe from annotate_discovered_device.
    assert probe_calls == [IP_TRANSPORT]

    # Case 2: Early exception on getprop ro.product.model still marks unmapped IP transport failed.
    probe_calls.clear()
    orig_device = ctrl._client.device.side_effect

    class _EarlyFailDevice:
        def __init__(self, serial: str) -> None:
            self.serial = serial

        def shell(self, cmd: str, timeout: int = 10) -> str:
            raise OSError(f"boom on {cmd}")

    monkeypatch.setattr(ctrl._client, "device", lambda serial: _EarlyFailDevice(serial))
    discovered_early = _nerve_discover_sync()
    assert IP_TRANSPORT in ctrl._last_discover_failed_serials
    assert discovered_early["devices"][0]["usable"] is False
    assert probe_calls == []
    assert orig_device is not None


def test_hardware_serial_cache_evicts_oldest_entries_beyond_bound(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache bounds memory to _MAX_HW_SERIAL_CACHE entries using LRU eviction."""
    _write_fleet(
        isolated_env["fleet"],
        f"devices:\n  - serial: {PERSONAL_HW}\n    personal: true\n",
    )
    engagement.clear_hardware_serial_cache()
    monkeypatch.setattr(engagement, "_hw_cache_clock", lambda: 100.0)

    limit = engagement._MAX_HW_SERIAL_CACHE
    for i in range(limit + 5):
        engagement.cache_hardware_serial(
            f"10.0.0.{i}:5555", f"SERIAL{i:04d}", transport_id=str(i + 1)
        )

    assert len(engagement._hw_serial_cache) == limit
    first_key = engagement._hw_cache_key("10.0.0.0:5555", None, transport_id="1")
    last_key = engagement._hw_cache_key(
        f"10.0.0.{limit + 4}:5555", None, transport_id=str(limit + 5)
    )
    assert first_key not in engagement._hw_serial_cache
    assert last_key in engagement._hw_serial_cache
    assert (
        engagement.resolve_device_decision(
            f"10.0.0.{limit + 4}:5555",
            transport_id=str(limit + 5),
            probe_hw=lambda _s: pytest.fail("should hit cache instead of probing"),
            allow_probe=False,
        ).reason
        is None
    )
