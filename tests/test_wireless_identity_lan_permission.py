"""[contract] Wireless adb Local Network denial classification and doctor remedy (linc-nerve-3p1v).

When the daemon replaces a wedged adb server from a launchd job on macOS, the
restarted adb server can lack Local Network access: ``adb connect <ip>:<port>``
exits 0 while printing ``failed to connect to '...': No route to host`` (or
``protocol fault`` / ``Host is down``), even though nerve's own process can open
a direct TCP connection to ``(ip, port)``. This suite pins:
1. ``classify_adb_connect(output, tcp_reachable)`` across the full output x
   reachability matrix.
2. ``_reconnect`` / ``recover_adb_server`` TCP probing, ``adb_reconnect_classified``
   logging, and ``local_network_denied`` tracking on ``/health`` (and clearing
   on a subsequent ``ok`` reconnect).
3. ``nerve doctor`` reporting ``adb_local_network`` ``fail`` with the GUI
   terminal restart remedy when ``/health`` lists denied serials, and reporting
   nothing new when empty.
"""

from __future__ import annotations

import subprocess as sp
from typing import Any

import pytest
from click.testing import CliRunner

import linc_nerve.core.ios  # noqa: F401 — preload ios before conftest ios_discovery fixture
from linc_nerve.cli import doctor as doctor_mod
from linc_nerve.cli.doctor import ProbeReport, doctor, overall_ok, run_checks
from linc_nerve.core import adb_health
from linc_nerve.core.adb_health import AdbTransportStatus
from linc_nerve.daemon import http_router

_WATCH_IP_SERIAL = "192.0.2.41:5555"
_WATCH_MDNS_SERIAL = "adb-FAKESERIAL01-AbCdEf._adb-tls-connect._tcp"
_USB_SERIAL = "FAKESERIAL01"


@pytest.fixture(autouse=True)
def _clean_adb_health_registry() -> Any:
    adb_health.reset_transport_status()
    yield
    adb_health.reset_transport_status()


def _status(**overrides: Any) -> AdbTransportStatus:
    base: dict[str, Any] = dict(
        responsive=True,
        wedged=False,
        device_count=1,
        probe_seconds=0.05,
        server_pid=4242,
        server_cpu_percent=0.2,
        detail="1 device(s)",
        checked_at=1_000.0,
    )
    base.update(overrides)
    return AdbTransportStatus(**base)


def _green_probes(**overrides: object) -> ProbeReport:
    base: dict[str, Any] = dict(
        adb_path="/opt/sdk/platform-tools/adb",
        adb_is_executable=True,
        adb_devices_output="List of devices attached\nFAKESERIAL01\tdevice\n",
        scrcpy_server_exists=True,
        scrcpy_server_path="/pkg/vendor/scrcpy-server-v3.3.4",
        wda_project_dir="/opt/wda/src",
        wda_checkout_exists=True,
        wda_team_id="ABCDE12345",
        is_macos=True,
        xcode_clt_path="/Library/Developer/CommandLineTools",
        daemon_health={
            "ok": True,
            "package_version": "0.1.0",
            "adb": {"local_network_denied": []},
        },
        installed_version="0.1.0",
        daemon_port=8111,
        linc_home_exists=True,
        missing_linc_dirs=(),
    )
    base.update(overrides)
    return ProbeReport(**base)


# ---------------------------------------------------------------------------
# 1. Pure classifier: classify_adb_connect(output, tcp_reachable)
# ---------------------------------------------------------------------------

_CLASSIFY_CASES: list[tuple[str, str, bool | None, str]] = [
    # "connected to ..." -> ok regardless of tcp_reachable
    ("connected_tcp_true", "connected to 192.0.2.41:5555", True, "ok"),
    ("connected_tcp_false", "connected to 192.0.2.41:5555", False, "ok"),
    ("connected_tcp_none", "connected to 192.0.2.41:5555", None, "ok"),
    # "already connected to ..." -> ok regardless of tcp_reachable
    ("already_to_tcp_true", "already connected to 192.0.2.41:5555", True, "ok"),
    ("already_to_tcp_false", "already connected to 192.0.2.41:5555", False, "ok"),
    ("already_to_tcp_none", "already connected to 192.0.2.41:5555", None, "ok"),
    # "already connected" without "connected to" -> ok
    ("already_bare_tcp_none", "already connected", None, "ok"),
    # "No route to host" x tcp_reachable
    (
        "no_route_tcp_true",
        "failed to connect to '192.0.2.41:5555': No route to host",
        True,
        "local_network_denied",
    ),
    (
        "no_route_tcp_false",
        "failed to connect to '192.0.2.41:5555': No route to host",
        False,
        "unreachable",
    ),
    (
        "no_route_tcp_none",
        "failed to connect to '192.0.2.41:5555': No route to host",
        None,
        "unknown",
    ),
    # "protocol fault" x tcp_reachable
    (
        "proto_fault_tcp_true",
        "error: protocol fault (couldn't read status message): Undefined error: 0",
        True,
        "local_network_denied",
    ),
    (
        "proto_fault_tcp_false",
        "error: protocol fault (couldn't read status message): Undefined error: 0",
        False,
        "unreachable",
    ),
    (
        "proto_fault_tcp_none",
        "error: protocol fault (couldn't read status message): Undefined error: 0",
        None,
        "unknown",
    ),
    # "Host is down" x tcp_reachable
    (
        "host_down_tcp_true",
        "failed to connect to '192.0.2.41:5555': Host is down",
        True,
        "local_network_denied",
    ),
    (
        "host_down_tcp_false",
        "failed to connect to '192.0.2.41:5555': Host is down",
        False,
        "unreachable",
    ),
    (
        "host_down_tcp_none",
        "failed to connect to '192.0.2.41:5555': Host is down",
        None,
        "unknown",
    ),
    # "Connection refused" -> refused regardless of tcp_reachable
    (
        "refused_tcp_true",
        "failed to connect to '192.0.2.41:5555': Connection refused",
        True,
        "refused",
    ),
    (
        "refused_tcp_false",
        "failed to connect to '192.0.2.41:5555': Connection refused",
        False,
        "refused",
    ),
    (
        "refused_tcp_none",
        "failed to connect to '192.0.2.41:5555': Connection refused",
        None,
        "refused",
    ),
    # Empty output -> unknown
    ("empty_tcp_true", "", True, "unknown"),
    ("empty_tcp_false", "", False, "unknown"),
    ("empty_tcp_none", "", None, "unknown"),
    # Garbage with NUL byte -> unknown
    ("garbage_tcp_true", "garbage\x00\n", True, "unknown"),
    ("garbage_tcp_false", "garbage\x00\n", False, "unknown"),
    ("garbage_tcp_none", "garbage\x00\n", None, "unknown"),
]


@pytest.mark.parametrize(
    ("output", "tcp_reachable", "expected"),
    [(out, tcp, exp) for (_cid, out, tcp, exp) in _CLASSIFY_CASES],
    ids=[cid for (cid, _out, _tcp, _exp) in _CLASSIFY_CASES],
)
def test_classify_adb_connect_table(
    output: str, tcp_reachable: bool | None, expected: str
) -> None:
    """[contract] Parametrized table over adb outputs x tcp_reachable pins every category."""
    assert adb_health.classify_adb_connect(output, tcp_reachable) == expected


def test_classify_adb_connect_non_string_returns_unknown() -> None:
    """[contract] Non-string output never raises and classifies as unknown."""
    assert adb_health.classify_adb_connect(None, True) == "unknown"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. _reconnect, recover_adb_server, TCP probe, and /health local_network_denied
# ---------------------------------------------------------------------------


class _FakeSocket:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.closed = True


def test_recover_adb_server_records_and_clears_local_network_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] No-route output with exit code 0 and TCP True is classified
    local_network_denied, surfaced on /health, and cleared on a later 'connected to'."""
    no_route_out = f"failed to connect to '{_WATCH_IP_SERIAL}': No route to host\n"
    outputs = iter(
        [
            # 1st recover_adb_server: start-server, then connect -> No route (exit 0!)
            sp.CompletedProcess(["adb", "start-server"], 0, stdout="", stderr=""),
            sp.CompletedProcess(
                ["adb", "connect", _WATCH_IP_SERIAL], 0, stdout=no_route_out, stderr=""
            ),
            # 2nd recover_adb_server: start-server, then connect -> connected to
            sp.CompletedProcess(["adb", "start-server"], 0, stdout="", stderr=""),
            sp.CompletedProcess(
                ["adb", "connect", _WATCH_IP_SERIAL],
                0,
                stdout=f"connected to {_WATCH_IP_SERIAL}\n",
                stderr="",
            ),
        ]
    )
    tcp_calls: list[tuple[tuple[str, int], float | None]] = []
    fake_sock = _FakeSocket()

    def fake_run(argv: list[str], **_kwargs: Any) -> sp.CompletedProcess[str]:
        return next(outputs)

    def fake_create_connection(
        address: tuple[str, int], timeout: float | None = None
    ) -> _FakeSocket:
        tcp_calls.append((address, timeout))
        return fake_sock

    logged: list[dict[str, Any]] = []
    monkeypatch.setattr(adb_health.subprocess, "run", fake_run)
    monkeypatch.setattr(adb_health.socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(
        adb_health.log,
        "info",
        lambda event, **kw: logged.append({"event": event, **kw}),
    )

    assert adb_health.recover_adb_server(None, reconnect=[_WATCH_IP_SERIAL]) is True
    assert tcp_calls == [(("192.0.2.41", 5555), 2.0)]
    assert fake_sock.closed is True
    assert any(
        entry.get("event") == "adb_reconnect_classified"
        and entry.get("serial") == _WATCH_IP_SERIAL
        and entry.get("category") == "local_network_denied"
        and entry.get("tcp_reachable") is True
        and entry.get("returncode") == 0
        for entry in logged
    )

    # Rendered /health adb block in all states includes local_network_denied
    health_body = http_router._build_health()
    assert health_body["adb"]["local_network_denied"] == [_WATCH_IP_SERIAL]
    for status, uptime in [
        (None, None),
        (None, 5.0),
        (None, 600.0),
        (_status(), 600.0),
    ]:
        _ok, block = adb_health.transport_health(
            status, now=1_000.0, monitor_uptime_seconds=uptime
        )
        assert block["local_network_denied"] == [_WATCH_IP_SERIAL]

    # A later "connected to" reconnect clears local_network_denied without probing TCP
    assert adb_health.recover_adb_server(None, reconnect=[_WATCH_IP_SERIAL]) is True
    assert len(tcp_calls) == 1, "must not probe TCP when adb connect succeeded"
    assert http_router._build_health()["adb"]["local_network_denied"] == []
    _ok, block_after = adb_health.transport_health(
        _status(), now=1_000.0, monitor_uptime_seconds=600.0
    )
    assert block_after["local_network_denied"] == []


def test_reconnect_oserror_yields_unreachable_and_unexpected_exception_yields_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] create_connection raising OSError yields 'unreachable'; raising an
    unexpected exception yields 'unknown' (never 'ok') and never raises."""
    no_route_stderr = f"failed to connect to '{_WATCH_IP_SERIAL}': No route to host\n"
    monkeypatch.setattr(
        adb_health.subprocess,
        "run",
        lambda argv, **_kw: sp.CompletedProcess(argv, 0, stdout="", stderr=no_route_stderr),
    )

    # 1. Seed local_network_denied, then verify OSError yields "unreachable" without clearing
    monkeypatch.setattr(adb_health.socket, "create_connection", lambda *_a, **_k: _FakeSocket())
    assert adb_health._reconnect(_WATCH_IP_SERIAL) == "local_network_denied"
    assert http_router._build_health()["adb"]["local_network_denied"] == [_WATCH_IP_SERIAL]

    def raise_oserror(*_a: Any, **_k: Any) -> Any:
        raise OSError("network unreachable")

    logged: list[dict[str, Any]] = []
    monkeypatch.setattr(adb_health.socket, "create_connection", raise_oserror)
    monkeypatch.setattr(
        adb_health.log,
        "info",
        lambda event, **kw: logged.append({"event": event, **kw}),
    )

    assert adb_health.recover_adb_server(None, reconnect=[_WATCH_IP_SERIAL]) is True
    assert logged[-1]["event"] == "adb_reconnect_classified"
    assert logged[-1]["category"] == "unreachable"
    assert logged[-1]["tcp_reachable"] is False
    # Still listed under local_network_denied because reconnect was not "ok"
    assert http_router._build_health()["adb"]["local_network_denied"] == [_WATCH_IP_SERIAL]

    # 2. Unexpected exception in create_connection -> "unknown", logged, never raises
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        adb_health.log,
        "warning",
        lambda event, **kw: warnings.append({"event": event, **kw}),
    )

    def raise_unexpected(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("socket layer exploded")

    monkeypatch.setattr(adb_health.socket, "create_connection", raise_unexpected)
    assert adb_health.recover_adb_server(None, reconnect=[_WATCH_IP_SERIAL]) is True
    assert logged[-1]["category"] == "unknown"
    assert logged[-1]["category"] != "ok"
    assert logged[-1]["tcp_reachable"] is None
    assert any(
        w.get("event") == "adb_tcp_probe_error" and "socket layer exploded" in w.get("error", "")
        for w in warnings
    )


def test_reconnect_classifier_exception_is_caught_and_logged_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] If classify_adb_connect raises, _reconnect logs and yields 'unknown'."""
    monkeypatch.setattr(
        adb_health.subprocess,
        "run",
        lambda argv, **_kw: sp.CompletedProcess(
            argv, 0, stdout=f"connected to {_WATCH_IP_SERIAL}\n", stderr=""
        ),
    )

    def broken_classifier(_out: str, _tcp: bool | None) -> str:
        raise ValueError("classifier boom")

    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(adb_health, "classify_adb_connect", broken_classifier)
    monkeypatch.setattr(
        adb_health.log,
        "warning",
        lambda event, **kw: warnings.append({"event": event, **kw}),
    )

    assert adb_health.recover_adb_server(None, reconnect=[_WATCH_IP_SERIAL]) is True
    assert adb_health._reconnect(_WATCH_IP_SERIAL) == "unknown"
    assert any(w.get("event") == "adb_reconnect_classify_error" for w in warnings)


def test_tcp_probe_runs_only_for_network_serials(monkeypatch: pytest.MonkeyPatch) -> None:
    """[contract] USB serials and mDNS service names never trigger connect or TCP probe."""
    monkeypatch.setattr(
        adb_health.socket,
        "create_connection",
        lambda *_a, **_k: pytest.fail("TCP probe must not run for non-network serials"),
    )
    assert adb_health._probe_tcp_reachable(_USB_SERIAL) is None
    assert adb_health._probe_tcp_reachable(_WATCH_MDNS_SERIAL) is None
    assert adb_health._reconnect(_USB_SERIAL) is None
    assert adb_health._reconnect(_WATCH_MDNS_SERIAL) is None


def test_reconnect_subprocess_exception_returns_unknown_and_sorted_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] _reconnect returns 'unknown' when _run raises, and /health sorts denied."""
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        adb_health.log,
        "warning",
        lambda event, **kw: warnings.append({"event": event, **kw}),
    )

    def raise_timeout(_argv: list[str], **_kw: Any) -> Any:
        raise sp.TimeoutExpired(["adb", "connect", _WATCH_IP_SERIAL], 10.0)

    monkeypatch.setattr(adb_health.subprocess, "run", raise_timeout)
    assert adb_health._reconnect(_WATCH_IP_SERIAL) == "unknown"
    assert any(
        w.get("event") == "adb_reconnect_failed" and w.get("serial") == _WATCH_IP_SERIAL
        for w in warnings
    )

    # Record two denied serials out of order and verify sorted rendering on /health
    second_serial = "192.0.2.42:5555"
    monkeypatch.setattr(
        adb_health.subprocess,
        "run",
        lambda argv, **_kw: sp.CompletedProcess(
            argv, 0, stdout="failed to connect: No route to host\n", stderr=""
        ),
    )
    monkeypatch.setattr(adb_health.socket, "create_connection", lambda *_a, **_k: _FakeSocket())
    assert adb_health._reconnect(second_serial) == "local_network_denied"
    assert adb_health._reconnect(_WATCH_IP_SERIAL) == "local_network_denied"
    assert http_router._build_health()["adb"]["local_network_denied"] == [
        _WATCH_IP_SERIAL,
        second_serial,
    ]


# ---------------------------------------------------------------------------
# 3. nerve doctor: adb_local_network check on daemon /health
# ---------------------------------------------------------------------------


def test_doctor_reports_adb_local_network_fail_with_remedy_on_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] doctor with a faked daemon /health carrying local_network_denied
    reports adb_local_network 'fail' with the macOS GUI terminal remedy text."""
    second_serial = "192.0.2.42:5555"
    health = {
        "ok": False,
        "package_version": "0.1.0",
        "adb": {"local_network_denied": [_WATCH_IP_SERIAL, second_serial]},
    }
    direct = doctor_mod.check_adb_local_network(health)
    assert direct is not None
    assert direct.detail == f"adb local_network_denied for: {_WATCH_IP_SERIAL}, {second_serial}"
    assert "macOS Local Network access" in direct.hint

    probes = _green_probes(is_macos=True, daemon_health=health)
    results = run_checks(probes)
    lan = next((r for r in results if r.name == "adb_local_network"), None)
    assert lan is not None
    assert lan.status == "fail"
    assert lan.detail == f"adb local_network_denied for: {_WATCH_IP_SERIAL}, {second_serial}"
    assert lan.hint == (
        "the adb server was started without macOS Local Network access "
        "(e.g. by a launchd job); restart it from a GUI terminal: "
        "adb kill-server && adb start-server"
    )
    assert overall_ok(results) is False

    monkeypatch.setattr(doctor_mod, "gather_probes", lambda **_kw: probes)
    cli_res = CliRunner().invoke(doctor, [])
    assert cli_res.exit_code == 1
    assert "adb_local_network" in cli_res.output
    assert _WATCH_IP_SERIAL in cli_res.output
    assert "adb kill-server && adb start-server" in cli_res.output


def test_doctor_reports_adb_local_network_non_darwin_hint_omits_macos() -> None:
    """[test] Non-darwin: same classification, hint omits 'macOS'."""
    probes = _green_probes(
        is_macos=False,
        daemon_health={
            "ok": False,
            "package_version": "0.1.0",
            "adb": {"local_network_denied": [_WATCH_IP_SERIAL]},
        },
    )
    results = run_checks(probes)
    lan = next((r for r in results if r.name == "adb_local_network"), None)
    assert lan is not None
    assert lan.status == "fail"
    assert _WATCH_IP_SERIAL in lan.detail
    assert "macOS" not in lan.hint
    assert lan.hint == (
        "the adb server was started without Local Network access "
        "(e.g. by a launchd job); restart it from a GUI terminal: "
        "adb kill-server && adb start-server"
    )


def test_doctor_with_no_local_network_denied_reports_nothing_new_pin() -> None:
    """[test] With no local_network_denied in /health, doctor reports nothing new (pin)."""
    for health in (
        {"ok": True, "package_version": "0.1.0", "adb": {"local_network_denied": []}},
        {"ok": True, "package_version": "0.1.0"},
        None,
    ):
        results = run_checks(_green_probes(daemon_health=health))
        names = [r.name for r in results]
        assert "adb_local_network" not in names
        assert names == [
            "adb",
            "device-auth",
            "scrcpy-server",
            "wda-project",
            "wda-team-id",
            "xcode-clt",
            "daemon",
            "linc-layout",
        ]


def _mark_serial_denied_via_reconnect(monkeypatch: pytest.MonkeyPatch, serial: str) -> None:
    monkeypatch.setattr(
        adb_health.subprocess,
        "run",
        lambda argv, **_kw: sp.CompletedProcess(
            argv,
            0,
            stdout=f"failed to connect to '{serial}': No route to host\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(adb_health.socket, "create_connection", lambda *_a, **_k: _FakeSocket())
    assert adb_health._reconnect(serial) == "local_network_denied"


def test_record_transport_status_responsive_clears_online_serial_from_local_network_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] mark serial 192.0.2.50:5555 denied via a faked _reconnect,
    then record a responsive status whose adb_serials contains it: the /health
    transport block lists local_network_denied == []."""
    serial = "192.0.2.50:5555"
    _mark_serial_denied_via_reconnect(monkeypatch, serial)
    assert http_router._build_health()["adb"]["local_network_denied"] == [serial]

    adb_health.record_transport_status(_status(responsive=True, adb_serials=(serial,)))

    _ok, block = adb_health.transport_health(
        adb_health.get_transport_status(), now=1_000.0, monitor_uptime_seconds=600.0
    )
    assert block["local_network_denied"] == []
    assert http_router._build_health()["adb"]["local_network_denied"] == []


def test_record_transport_status_unresponsive_does_not_clear_local_network_denied_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] an unresponsive status (responsive False) does not clear the set (pin)."""
    serial = "192.0.2.50:5555"
    _mark_serial_denied_via_reconnect(monkeypatch, serial)

    adb_health.record_transport_status(
        _status(responsive=False, device_count=None, adb_serials=(serial,))
    )

    _ok, block = adb_health.transport_health(
        adb_health.get_transport_status(), now=1_000.0, monitor_uptime_seconds=600.0
    )
    assert block["local_network_denied"] == [serial]
    assert http_router._build_health()["adb"]["local_network_denied"] == [serial]


def test_doctor_fed_cleared_block_reports_no_local_network_denied_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] nerve doctor check fed the cleared block reports no local_network_denied finding."""
    serial = "192.0.2.50:5555"
    _mark_serial_denied_via_reconnect(monkeypatch, serial)
    assert doctor_mod.check_adb_local_network(http_router._build_health()) is not None

    adb_health.record_transport_status(_status(responsive=True, adb_serials=(serial,)))
    cleared_health = http_router._build_health()

    assert cleared_health["adb"]["local_network_denied"] == []
    assert doctor_mod.check_adb_local_network(cleared_health) is None
    results = run_checks(
        _green_probes(
            daemon_health=cleared_health,
            installed_version=cleared_health["package_version"],
        )
    )
    assert all(r.name != "adb_local_network" for r in results)
    assert overall_ok(results) is True
