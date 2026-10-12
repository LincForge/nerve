"""ADB transport health — busy-spin detection, watchdog, pid-kill recovery (#131 recurrence).

Regression suite for the failure mode that took the mini's bench dark twice:
the adb server enters an ``fdevent_context_poll::Loop()`` busy-spin at ~100%
CPU, every adb client blocks forever, ``nerve_discover`` reports
``android: 0``, and the daemon's ``/health`` keeps answering ``ok: true``
because it was a pure liveness probe that touched nothing.

Everything here is a pure function over injected primitives or a monkeypatched
subprocess — no adb, no devices, no network.
"""

from __future__ import annotations

from typing import Any

import pytest

from linc_nerve.core import adb_health
from linc_nerve.core.adb_health import AdbTransportStatus

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """Every test starts and ends with an empty transport-status registry."""
    adb_health.reset_transport_status()
    yield
    adb_health.reset_transport_status()


def _status(**overrides: Any) -> AdbTransportStatus:
    base: dict[str, Any] = dict(
        responsive=True,
        wedged=False,
        device_count=3,
        probe_seconds=0.12,
        server_pid=4242,
        server_cpu_percent=0.4,
        detail="3 device(s)",
        checked_at=1_000.0,
    )
    base.update(overrides)
    return AdbTransportStatus(**base)


_WEDGED = dict(
    responsive=False,
    wedged=True,
    device_count=None,
    probe_seconds=8.0,
    server_cpu_percent=99.7,
    detail="adb devices timed out after 8.0s; server pid 4242 at 99.7% CPU",
)


# ---------------------------------------------------------------------------
# Pure: parsing `adb devices`
# ---------------------------------------------------------------------------


def test_count_ready_devices_counts_only_the_device_state() -> None:
    out = (
        "List of devices attached\n"
        "R3CFAKE00001\tdevice\n"
        "1A2B3C\tunauthorized\n"
        "emulator-5554\tdevice\n"
        "192.0.2.10:5555\toffline\n"
        "\n"
    )
    assert adb_health.count_ready_devices(out) == 2


def test_count_ready_devices_on_empty_fleet() -> None:
    assert adb_health.count_ready_devices("List of devices attached\n\n") == 0


def test_count_ready_devices_ignores_daemon_startup_chatter() -> None:
    out = (
        "* daemon not running; starting now at tcp:5037\n"
        "* daemon started successfully\n"
        "List of devices attached\n"
        "R3CFAKE00001\tdevice\n"
    )
    assert adb_health.count_ready_devices(out) == 1


def test_parse_adb_devices_keeps_a_spaced_mdns_serial_intact() -> None:
    """An mDNS wifi-adb name can carry a collision suffix with a space
    (`adb-RFAT629KQ9A-x (2)._adb-tls-connect._tcp`) — adb separates serial
    and state with a TAB, so splitting on ANY whitespace mangles it."""
    out = (
        "List of devices attached\n"
        "R3CFAKE00001\tdevice\n"
        "adb-RFAT629KQ9A-x (2)._adb-tls-connect._tcp\tdevice\n"
        "\n"
    )
    assert adb_health.parse_adb_devices(out) == (
        "R3CFAKE00001",
        "adb-RFAT629KQ9A-x (2)._adb-tls-connect._tcp",
    )


def test_parse_adb_devices_excludes_a_spaced_mdns_serial_when_offline() -> None:
    out = (
        "List of devices attached\n"
        "R3CFAKE00001\tdevice\n"
        "adb-RFAT629KQ9A-x (2)._adb-tls-connect._tcp\toffline\n"
    )
    assert adb_health.parse_adb_devices(out) == ("R3CFAKE00001",)
    assert adb_health.count_ready_devices(out) == 1


def test_parse_adb_devices_pin_tab_less_line_still_parses() -> None:
    """A hand-written space-separated fixture (no tab) keeps working via the
    `rsplit(None, 1)` fallback."""
    out = "List of devices attached\nR3C device\n"
    assert adb_health.parse_adb_devices(out) == ("R3C",)


def test_split_adb_device_line_keeps_the_spaced_serial_and_state() -> None:
    line = "adb-RFAT629KQ9A-x (2)._adb-tls-connect._tcp\tdevice"
    assert adb_health.split_adb_device_line(line) == (
        "adb-RFAT629KQ9A-x (2)._adb-tls-connect._tcp",
        "device",
    )


def test_split_adb_device_line_falls_back_to_whitespace_when_no_tab() -> None:
    assert adb_health.split_adb_device_line("R3C device") == ("R3C", "device")


def test_split_adb_device_line_returns_none_for_a_single_token() -> None:
    assert adb_health.split_adb_device_line("R3C") is None


def test_probe_transport_records_the_full_spaced_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    out = (
        "List of devices attached\n"
        "R3CFAKE00001\tdevice\n"
        "adb-RFAT629KQ9A-x (2)._adb-tls-connect._tcp\tdevice\n"
    )
    monkeypatch.setattr(adb_health, "_run", lambda argv, timeout: _FakeCompleted(out))
    monkeypatch.setattr(adb_health, "find_adb_server_pid", lambda: 4242)
    monkeypatch.setattr(adb_health, "process_cpu_percent", lambda pid: 0.3)

    status = adb_health.probe_transport()

    assert "adb-RFAT629KQ9A-x (2)._adb-tls-connect._tcp" in status.adb_serials
    assert status.device_count == 2


# ---------------------------------------------------------------------------
# Pure: busy-spin detection
# ---------------------------------------------------------------------------


def test_is_busy_spin_true_when_unresponsive_and_cpu_pegged() -> None:
    """The exact 2026-09-03 signature: adb devices hangs, server at 99.7% CPU."""
    assert adb_health.is_busy_spin(responsive=False, server_cpu_percent=99.7) is True


def test_is_busy_spin_false_when_responsive_even_at_high_cpu() -> None:
    """A busy but answering server is doing work, not spinning — never kill it."""
    assert adb_health.is_busy_spin(responsive=True, server_cpu_percent=99.7) is False


def test_is_busy_spin_false_when_unresponsive_but_idle_cpu() -> None:
    """Server down / not started is a different (non-destructive) failure."""
    assert adb_health.is_busy_spin(responsive=False, server_cpu_percent=0.0) is False


def test_is_busy_spin_false_when_cpu_unknown() -> None:
    """No pid / no ps reading is not evidence — fail safe, do not kill."""
    assert adb_health.is_busy_spin(responsive=False, server_cpu_percent=None) is False


def test_is_busy_spin_honours_a_custom_threshold() -> None:
    assert adb_health.is_busy_spin(responsive=False, server_cpu_percent=50.0) is False
    assert (
        adb_health.is_busy_spin(responsive=False, server_cpu_percent=50.0, threshold=40.0) is True
    )


# ---------------------------------------------------------------------------
# Pure: is_network_serial — which serials `adb connect` can re-establish
# ---------------------------------------------------------------------------


def test_is_network_serial_true_for_an_ip_port_serial() -> None:
    """The Galaxy Watch7 charging-puck lane — the whole point of this bead."""
    assert adb_health.is_network_serial("192.0.2.10:35593") is True


def test_is_network_serial_false_for_a_usb_serial() -> None:
    assert adb_health.is_network_serial("R3CFAKE00001") is False


def test_is_network_serial_false_for_an_mdns_service_name() -> None:
    """mDNS `_adb-tls-connect` names are not reconnectable via `adb connect`."""
    assert adb_health.is_network_serial("adb-RFAT629KQ9A-abc._adb-tls-connect._tcp") is False


# ---------------------------------------------------------------------------
# Pure: transport_health — the view /health renders
# ---------------------------------------------------------------------------


def test_transport_health_unmonitored_when_no_watchdog_runs_here() -> None:
    """`nerve bellows` / library users run no watchdog — never fail their health
    on a transport nobody is watching."""
    ok, block = adb_health.transport_health(None, now=1_000.0, monitor_uptime_seconds=None)
    assert ok is True
    assert block["state"] == "unmonitored"


def test_transport_health_pending_during_startup_grace() -> None:
    ok, block = adb_health.transport_health(None, now=1_000.0, monitor_uptime_seconds=5.0)
    assert ok is True
    assert block["state"] == "pending"


def test_transport_health_stale_when_no_probe_landed_after_grace() -> None:
    """A watchdog that never reports is itself the wedge signal: the tick is
    blocked in the same adb call every other client is blocked in."""
    ok, block = adb_health.transport_health(None, now=1_000.0, monitor_uptime_seconds=600.0)
    assert ok is False
    assert block["state"] == "stale"


def test_transport_health_stale_when_last_probe_is_too_old() -> None:
    status = _status(checked_at=1_000.0)
    ok, block = adb_health.transport_health(
        status, now=1_000.0 + 900.0, monitor_uptime_seconds=5_000.0
    )
    assert ok is False
    assert block["state"] == "stale"
    assert block["age_seconds"] == pytest.approx(900.0)


def test_transport_health_ok_when_transport_answers() -> None:
    ok, block = adb_health.transport_health(_status(), now=1_000.0, monitor_uptime_seconds=600.0)
    assert ok is True
    assert block["state"] == "ok"
    assert block["device_count"] == 3


def test_transport_health_not_ok_when_wedged() -> None:
    ok, block = adb_health.transport_health(
        _status(**_WEDGED), now=1_000.0, monitor_uptime_seconds=600.0
    )
    assert ok is False
    assert block["state"] == "wedged"
    assert block["server_cpu_percent"] == pytest.approx(99.7)


def test_transport_health_not_ok_when_unresponsive_without_spin() -> None:
    ok, block = adb_health.transport_health(
        _status(responsive=False, wedged=False, device_count=None, server_cpu_percent=0.0),
        now=1_000.0,
        monitor_uptime_seconds=600.0,
    )
    assert ok is False
    assert block["state"] == "unresponsive"


def test_transport_health_reports_zero_devices_as_ok_but_visible() -> None:
    """Zero devices is a real fleet state, not a transport failure — but the
    count must be on the wire so callers can assert on it (the whole point:
    assert on device count, not on the boolean)."""
    ok, block = adb_health.transport_health(
        _status(device_count=0, detail="0 device(s)"),
        now=1_000.0,
        monitor_uptime_seconds=600.0,
    )
    assert ok is True
    assert block["device_count"] == 0


def test_transport_health_surfaces_a_completed_recovery() -> None:
    ok, block = adb_health.transport_health(
        _status(recovered=True), now=1_000.0, monitor_uptime_seconds=600.0
    )
    assert ok is True
    assert block["recovered"] is True


def test_transport_health_lost_after_recovery_is_always_present_and_empty_by_default() -> None:
    ok, block = adb_health.transport_health(_status(), now=1_000.0, monitor_uptime_seconds=600.0)
    assert ok is True
    assert block["lost_after_recovery"] == []


def test_transport_health_unmonitored_still_carries_lost_after_recovery() -> None:
    ok, block = adb_health.transport_health(None, now=1_000.0, monitor_uptime_seconds=None)
    assert ok is True
    assert block["lost_after_recovery"] == []


def test_transport_health_not_ok_when_recovery_shrank_the_device_set() -> None:
    """A wedge recovery that came back with fewer devices is NOT ok — the
    exact failure this bead closes: nerve used to bless this as `ok`."""
    ok, block = adb_health.transport_health(
        _status(recovered=True, lost_after_recovery=("192.0.2.10:35593",)),
        now=1_000.0,
        monitor_uptime_seconds=600.0,
    )
    assert ok is False
    assert block["state"] == "recovery-shrank"
    assert block["lost_after_recovery"] == ["192.0.2.10:35593"]
    assert "192.0.2.10:35593" in block["detail"]


def test_transport_health_checks_recovery_shrank_before_nerve_stale() -> None:
    """Both conditions can be simultaneously true after a wedge — the
    recovery-shrank signal must win so the lost device is not buried under a
    generic 'nerve serials are stale' reading."""
    ok, block = adb_health.transport_health(
        _status(
            recovered=True,
            lost_after_recovery=("192.0.2.10:35593",),
            nerve_stale=True,
            nerve_stale_detail="nerve serials are stale",
        ),
        now=1_000.0,
        monitor_uptime_seconds=600.0,
    )
    assert ok is False
    assert block["state"] == "recovery-shrank"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_round_trips_the_last_status() -> None:
    assert adb_health.get_transport_status() is None
    s = _status()
    adb_health.record_transport_status(s)
    assert adb_health.get_transport_status() is s


def test_watchdog_is_not_registered_by_default() -> None:
    assert adb_health.watchdog_uptime_seconds(now=1_000.0) is None


def test_mark_watchdog_started_makes_the_process_monitored() -> None:
    adb_health.mark_watchdog_started(now=1_000.0)
    assert adb_health.watchdog_uptime_seconds(now=1_060.0) == pytest.approx(60.0)


def test_reset_transport_status_clears_registration() -> None:
    adb_health.mark_watchdog_started(now=1_000.0)
    adb_health.record_transport_status(_status())
    adb_health.reset_transport_status()
    assert adb_health.get_transport_status() is None
    assert adb_health.watchdog_uptime_seconds(now=1_000.0) is None


def test_reset_transport_status_clears_last_good_serials() -> None:
    """A fresh process (or a reset one) has no memory of a prior device set,
    so a wedge/recovery sequence right after reset reports no loss — there is
    nothing to have lost."""
    adb_health.record_transport_status(
        _status(responsive=True, adb_serials=("R3C", "192.0.2.10:35593"))
    )
    adb_health.reset_transport_status()

    probes = iter([_status(**_WEDGED), _status(adb_serials=("R3C",))])
    status = adb_health.watchdog_tick(
        probe=lambda: next(probes),
        recover=lambda pid, reconnect=(): True,
    )
    assert status.lost_after_recovery == ()


# ---------------------------------------------------------------------------
# Probe (subprocess-level, monkeypatched)
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_probe_transport_reports_responsive_with_device_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        adb_health,
        "_run",
        lambda argv, timeout: _FakeCompleted("List of devices attached\nR3C\tdevice\n"),
    )
    monkeypatch.setattr(adb_health, "find_adb_server_pid", lambda: 4242)
    monkeypatch.setattr(adb_health, "process_cpu_percent", lambda pid: 0.3)

    status = adb_health.probe_transport()

    assert status.responsive is True
    assert status.wedged is False
    assert status.device_count == 1
    assert status.server_pid == 4242


def test_probe_transport_flags_the_busy_spin_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """`adb devices` times out while the server burns CPU → wedged."""
    import subprocess

    def _boom(argv: list[str], timeout: float) -> Any:
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(adb_health, "_run", _boom)
    monkeypatch.setattr(adb_health, "find_adb_server_pid", lambda: 4242)
    monkeypatch.setattr(adb_health, "process_cpu_percent", lambda pid: 99.7)

    status = adb_health.probe_transport()

    assert status.responsive is False
    assert status.wedged is True
    assert status.device_count is None
    assert "99.7" in status.detail


def test_probe_transport_timeout_without_spin_is_unresponsive_not_wedged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def _boom(argv: list[str], timeout: float) -> Any:
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(adb_health, "_run", _boom)
    monkeypatch.setattr(adb_health, "find_adb_server_pid", lambda: None)
    monkeypatch.setattr(adb_health, "process_cpu_percent", lambda pid: None)

    status = adb_health.probe_transport()

    assert status.responsive is False
    assert status.wedged is False


def test_probe_transport_never_raises_on_a_missing_adb_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(argv: list[str], timeout: float) -> Any:
        raise FileNotFoundError("adb")

    monkeypatch.setattr(adb_health, "_run", _boom)
    monkeypatch.setattr(adb_health, "find_adb_server_pid", lambda: None)
    monkeypatch.setattr(adb_health, "process_cpu_percent", lambda pid: None)

    status = adb_health.probe_transport()
    assert status.responsive is False
    assert "adb" in status.detail


def test_probe_transport_nonzero_exit_is_unresponsive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adb_health, "_run", lambda argv, timeout: _FakeCompleted("", returncode=1))
    monkeypatch.setattr(adb_health, "find_adb_server_pid", lambda: None)
    monkeypatch.setattr(adb_health, "process_cpu_percent", lambda pid: None)

    assert adb_health.probe_transport().responsive is False


def test_process_cpu_percent_parses_ps_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adb_health, "_run", lambda argv, timeout: _FakeCompleted(" 99.7\n"))
    assert adb_health.process_cpu_percent(4242) == pytest.approx(99.7)


def test_process_cpu_percent_returns_none_when_ps_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adb_health, "_run", lambda argv, timeout: _FakeCompleted("", returncode=1))
    assert adb_health.process_cpu_percent(4242) is None


def test_process_cpu_percent_returns_none_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adb_health, "_run", lambda argv, timeout: _FakeCompleted("n/a\n"))
    assert adb_health.process_cpu_percent(4242) is None


def test_find_adb_server_pid_parses_pgrep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adb_health, "_run", lambda argv, timeout: _FakeCompleted("75160\n"))
    assert adb_health.find_adb_server_pid() == 75160


def test_find_adb_server_pid_targets_the_configured_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery must only ever find the server nerve talks to, never the default (7aaba3c3)."""
    monkeypatch.delenv("ADB_SERVER_SOCKET", raising=False)
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5099")
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], timeout: float) -> Any:
        calls.append(argv)
        return _FakeCompleted("75160\n")

    monkeypatch.setattr(adb_health, "_run", _fake_run)
    assert adb_health.find_adb_server_pid() == 75160
    assert len(calls) == 1
    assert calls[0][:2] == ["pgrep", "-f"]
    assert "tcp:5099" in calls[0][2]
    assert "tcp:5037" not in calls[0][2]


def test_find_adb_server_pid_follows_adb_server_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5099")
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:localhost:5098")
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], timeout: float) -> Any:
        calls.append(argv)
        return _FakeCompleted("81\n")

    monkeypatch.setattr(adb_health, "_run", _fake_run)
    assert adb_health.find_adb_server_pid() == 81
    assert "adb -L tcp:localhost:5098" in calls[0][2]


def test_find_adb_server_pid_never_raises_on_a_malformed_server_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probes never raise; the client path reports the bad variable loudly instead."""
    monkeypatch.delenv("ADB_SERVER_SOCKET", raising=False)
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "abc")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        adb_health, "_run", lambda argv, timeout: calls.append(argv) or _FakeCompleted("1\n")
    )
    assert adb_health.find_adb_server_pid() is None
    assert calls == []


def test_find_adb_server_pid_takes_the_first_of_several(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adb_health, "_run", lambda argv, timeout: _FakeCompleted("75160\n81\n"))
    assert adb_health.find_adb_server_pid() == 75160


def test_find_adb_server_pid_returns_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adb_health, "_run", lambda argv, timeout: _FakeCompleted("", returncode=1))
    assert adb_health.find_adb_server_pid() is None


def test_find_adb_server_pid_returns_none_when_pgrep_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(argv: list[str], timeout: float) -> Any:
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(adb_health, "_run", _boom)
    assert adb_health.find_adb_server_pid() is None


# ---------------------------------------------------------------------------
# Recovery — NEVER `adb kill-server`
# ---------------------------------------------------------------------------


def test_recover_adb_server_kills_by_pid_and_never_calls_kill_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`adb kill-server` talks to the SAME wedged socket and hangs too — the
    only recovery that works is signalling the server pid directly."""
    signals: list[tuple[int, int]] = []
    argvs: list[list[str]] = []

    monkeypatch.setattr(adb_health.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(adb_health, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        adb_health,
        "_run",
        lambda argv, timeout: argvs.append(argv) or _FakeCompleted("* daemon started\n"),
    )

    assert adb_health.recover_adb_server(4242) is True

    import signal as _signal

    assert (4242, _signal.SIGTERM) in signals
    flat = [" ".join(a) for a in argvs]
    assert any("start-server" in c for c in flat)
    assert not any("kill-server" in c for c in flat), "kill-server hangs on a wedged server"


def test_recover_adb_server_escalates_to_sigkill_when_sigterm_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spinning adb ignores SIGTERM while stuck in its poll loop."""
    signals: list[int] = []

    monkeypatch.setattr(adb_health.os, "kill", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(adb_health, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(adb_health, "_sleep", lambda s: None)
    monkeypatch.setattr(adb_health, "_run", lambda argv, timeout: _FakeCompleted(""))

    adb_health.recover_adb_server(4242)

    import signal as _signal

    assert _signal.SIGKILL in signals


def test_recover_adb_server_without_a_pid_just_starts_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argvs: list[list[str]] = []
    monkeypatch.setattr(
        adb_health.os,
        "kill",
        lambda pid, sig: pytest.fail("must not signal when there is no pid"),
    )
    monkeypatch.setattr(
        adb_health, "_run", lambda argv, timeout: argvs.append(argv) or _FakeCompleted("")
    )
    assert adb_health.recover_adb_server(None) is True
    assert any("start-server" in " ".join(a) for a in argvs)


def test_recover_adb_server_returns_false_when_start_server_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adb_health.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(adb_health, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(adb_health, "_run", lambda argv, timeout: _FakeCompleted("", returncode=1))
    assert adb_health.recover_adb_server(4242) is False


def test_recover_adb_server_returns_false_when_start_server_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def _boom(argv: list[str], timeout: float) -> Any:
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(adb_health.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(adb_health, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(adb_health, "_run", _boom)
    assert adb_health.recover_adb_server(4242) is False


def test_recover_adb_server_survives_a_dead_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    def _kill(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(adb_health.os, "kill", _kill)
    monkeypatch.setattr(adb_health, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(adb_health, "_run", lambda argv, timeout: _FakeCompleted(""))
    assert adb_health.recover_adb_server(4242) is True


# ---------------------------------------------------------------------------
# Recovery — reconnecting wifi-adb serials after the server comes back
# ---------------------------------------------------------------------------


def test_recover_adb_server_reconnects_network_serials_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After `start-server`, issue exactly one `adb connect` for the ip:port
    serial — never for the USB serial, which isn't reconnectable this way."""
    argvs: list[list[str]] = []
    monkeypatch.setattr(adb_health.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(adb_health, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        adb_health,
        "_run",
        lambda argv, timeout: argvs.append(argv) or _FakeCompleted(""),
    )

    result = adb_health.recover_adb_server(4242, reconnect=("192.0.2.10:35593", "R3CFAKE00001"))

    assert result is True
    assert argvs[0] == [adb_health.ADB_PATH, "start-server"]
    connect_calls = [a for a in argvs if len(a) > 1 and a[1] == "connect"]
    assert connect_calls == [[adb_health.ADB_PATH, "connect", "192.0.2.10:35593"]]


def test_recover_adb_server_survives_a_reconnect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung `adb connect` must not sink the whole recovery — the server is
    already back up by the time reconnect runs."""
    import subprocess

    def _run(argv: list[str], timeout: float) -> Any:
        if len(argv) > 1 and argv[1] == "connect":
            raise subprocess.TimeoutExpired(argv, timeout)
        return _FakeCompleted("")

    monkeypatch.setattr(adb_health.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(adb_health, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(adb_health, "_run", _run)

    assert adb_health.recover_adb_server(4242, reconnect=("192.0.2.10:35593",)) is True


def test_recover_adb_server_reconnect_defaults_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing callers that pass no `reconnect` keep working unchanged."""
    argvs: list[list[str]] = []
    monkeypatch.setattr(adb_health.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(adb_health, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        adb_health, "_run", lambda argv, timeout: argvs.append(argv) or _FakeCompleted("")
    )
    assert adb_health.recover_adb_server(4242) is True
    assert all(len(a) < 2 or a[1] != "connect" for a in argvs)


# ---------------------------------------------------------------------------
# Watchdog tick — probe, record, self-heal
# ---------------------------------------------------------------------------


def test_watchdog_tick_records_a_healthy_probe_and_does_not_recover() -> None:
    calls: list[int | None] = []
    status = adb_health.watchdog_tick(
        probe=lambda: _status(),
        recover=lambda pid, reconnect=(): calls.append(pid) or True,
    )
    assert status.wedged is False
    assert calls == []
    assert adb_health.get_transport_status() is status


def test_watchdog_tick_self_heals_a_wedged_transport() -> None:
    """The whole point: detect the spin and repair it without a human."""
    recovered_pids: list[int | None] = []
    probes = iter([_status(**_WEDGED), _status(detail="3 device(s) after recovery")])

    status = adb_health.watchdog_tick(
        probe=lambda: next(probes),
        recover=lambda pid, reconnect=(): recovered_pids.append(pid) or True,
    )

    assert recovered_pids == [4242]
    assert status.recovered is True
    assert status.responsive is True, "re-probed after the repair"
    recorded = adb_health.get_transport_status()
    assert recorded is not None and recorded.recovered is True


def test_watchdog_tick_resets_the_persistent_adb_client_after_recovery() -> None:
    """The daemon holds a persistent adbutils socket to the OLD server process.
    Without this reset the bench stays dark until someone bounces the daemon —
    exactly what the 2026-09-03 manual repair had to do."""
    resets: list[bool] = []
    probes = iter([_status(**_WEDGED), _status()])

    adb_health.watchdog_tick(
        probe=lambda: next(probes),
        recover=lambda pid, reconnect=(): True,
        on_recover=lambda: resets.append(True),
    )
    assert resets == [True]


def test_watchdog_tick_does_not_reset_the_client_when_healthy() -> None:
    resets: list[bool] = []
    adb_health.watchdog_tick(
        probe=lambda: _status(),
        recover=lambda pid, reconnect=(): True,
        on_recover=lambda: resets.append(True),
    )
    assert resets == []


def test_watchdog_tick_survives_an_on_recover_callback_that_raises() -> None:
    def _boom() -> None:
        raise RuntimeError("client reset failed")

    probes = iter([_status(**_WEDGED), _status()])
    status = adb_health.watchdog_tick(
        probe=lambda: next(probes), recover=lambda pid, reconnect=(): True, on_recover=_boom
    )
    assert status.recovered is True


def test_watchdog_tick_respects_the_recovery_cooldown() -> None:
    """Never enter a kill/start loop against a server that keeps re-wedging."""
    attempts: list[int | None] = []
    probes = iter([_status(**_WEDGED), _status(), _status(**_WEDGED)])
    clock = iter([1_000.0, 1_010.0])

    kwargs: dict[str, Any] = dict(
        probe=lambda: next(probes),
        recover=lambda pid, reconnect=(): attempts.append(pid) or True,
        now=lambda: next(clock),
    )
    adb_health.watchdog_tick(**kwargs)
    second = adb_health.watchdog_tick(**kwargs)

    assert attempts == [4242], "second wedge is inside the cooldown window"
    assert second.wedged is True
    assert second.recovered is False


def test_watchdog_tick_recovers_again_after_the_cooldown_expires() -> None:
    attempts: list[int | None] = []
    probes = iter([_status(**_WEDGED), _status(), _status(**_WEDGED), _status()])
    clock = iter([1_000.0, 9_999.0])

    kwargs: dict[str, Any] = dict(
        probe=lambda: next(probes),
        recover=lambda pid, reconnect=(): attempts.append(pid) or True,
        now=lambda: next(clock),
    )
    adb_health.watchdog_tick(**kwargs)
    adb_health.watchdog_tick(**kwargs)

    assert attempts == [4242, 4242]


def test_watchdog_tick_keeps_the_wedged_status_when_recovery_fails() -> None:
    status = adb_health.watchdog_tick(
        probe=lambda: _status(**_WEDGED),
        recover=lambda pid, reconnect=(): False,
    )
    assert status.wedged is True
    assert status.recovered is False


def test_watchdog_tick_never_raises_when_the_probe_explodes() -> None:
    """A watchdog that can die is a watchdog that stops watching."""

    def _boom() -> AdbTransportStatus:
        raise RuntimeError("ps went missing")

    status = adb_health.watchdog_tick(probe=_boom, recover=lambda pid, reconnect=(): True)
    assert status.responsive is False
    assert "ps went missing" in status.detail
    assert adb_health.get_transport_status() is status


def test_watchdog_tick_never_raises_when_recovery_explodes() -> None:
    def _boom(pid: int | None, reconnect: Any = ()) -> bool:
        raise OSError("no permission")

    status = adb_health.watchdog_tick(probe=lambda: _status(**_WEDGED), recover=_boom)
    assert status.wedged is True
    assert status.recovered is False


# ---------------------------------------------------------------------------
# Watchdog tick — reconnect wifi-adb serials and report a post-recovery shrink
# ---------------------------------------------------------------------------

_WATCH_SERIAL = "192.0.2.10:35593"
_FIVE_SERIALS = ("R3C1", "R3C2", "R3C3", "R3C4", _WATCH_SERIAL)
_FOUR_USB_SERIALS = ("R3C1", "R3C2", "R3C3", "R3C4")


def test_watchdog_tick_passes_last_good_network_serials_to_recover() -> None:
    """`_handle_wedge` reconnects exactly the wifi-adb serials from the last
    healthy reading — never a USB serial."""
    reconnect_kwargs: list[tuple[str, ...]] = []

    def _fake_recover(pid: int | None, reconnect: tuple[str, ...] = ()) -> bool:
        reconnect_kwargs.append(tuple(reconnect))
        return True

    # First tick: healthy, publishes the 5-serial "last good" set.
    adb_health.watchdog_tick(probe=lambda: _status(adb_serials=_FIVE_SERIALS))

    # Second tick: wedged, then a clean re-probe with all 5 serials back.
    probes = iter([_status(**_WEDGED), _status(adb_serials=_FIVE_SERIALS)])
    adb_health.watchdog_tick(probe=lambda: next(probes), recover=_fake_recover)

    assert reconnect_kwargs == [(_WATCH_SERIAL,)]


def test_watchdog_tick_reports_lost_after_recovery_when_a_serial_does_not_come_back() -> None:
    """The bug this bead closes: recovery drops the wifi-adb lane and the
    watchdog used to bless the shrunk fleet as `ok`."""

    def _fake_recover(pid: int | None, reconnect: tuple[str, ...] = ()) -> bool:
        return True

    adb_health.watchdog_tick(probe=lambda: _status(adb_serials=_FIVE_SERIALS))

    probes = iter([_status(**_WEDGED), _status(adb_serials=_FOUR_USB_SERIALS)])
    status = adb_health.watchdog_tick(probe=lambda: next(probes), recover=_fake_recover)

    assert status.recovered is True
    assert status.lost_after_recovery == (_WATCH_SERIAL,)

    ok, block = adb_health.transport_health(status, now=1_010.0, monitor_uptime_seconds=600.0)
    assert ok is False
    assert block["state"] == "recovery-shrank"
    assert _WATCH_SERIAL in block["detail"]


def test_watchdog_tick_reports_no_loss_when_the_reprobe_sees_every_serial() -> None:
    def _fake_recover(pid: int | None, reconnect: tuple[str, ...] = ()) -> bool:
        return True

    adb_health.watchdog_tick(probe=lambda: _status(adb_serials=_FIVE_SERIALS))

    probes = iter([_status(**_WEDGED), _status(adb_serials=_FIVE_SERIALS)])
    status = adb_health.watchdog_tick(probe=lambda: next(probes), recover=_fake_recover)

    assert status.lost_after_recovery == ()

    ok, block = adb_health.transport_health(status, now=1_010.0, monitor_uptime_seconds=600.0)
    assert ok is True
    assert block["state"] == "ok"


# ---------------------------------------------------------------------------
# Nerve device-set freshness & watchdog drift reconciliation (AC 1-4)
# ---------------------------------------------------------------------------


def test_watchdog_tick_records_nerve_stale_when_serials_older_than_180s() -> None:
    """AC 1: after record_nerve_android_serials({'A'}, now=T-181)
    and mark_watchdog_started(now=T-600),
    watchdog_tick(probe=<responsive stub, adb_serials ('A',)>, now=lambda: T) records a status
    for which transport_health(status, now=T, monitor_uptime_seconds=600) returns ok False with
    state 'nerve-stale'."""
    T = 10_000.0
    adb_health.record_nerve_android_serials({"A"}, now=T - 181.0)
    adb_health.mark_watchdog_started(now=T - 600.0)

    status = adb_health.watchdog_tick(
        probe=lambda: _status(checked_at=T, adb_serials=("A",)),
        now=lambda: T,
    )

    recorded = adb_health.get_transport_status()
    assert recorded is status

    ok, block = adb_health.transport_health(
        status,
        now=T,
        monitor_uptime_seconds=600.0,
    )
    assert ok is False
    assert block["state"] == "nerve-stale"


def test_watchdog_tick_unpublished_serials_respects_startup_grace() -> None:
    """AC 2: with nerve serials never published (reset_transport_status() and no record call),
    a watchdog_tick at uptime 600s yields transport_health state 'nerve-stale', ok False;
    with watchdog uptime below STARTUP_GRACE_SECONDS it is NOT reported 'nerve-stale'."""
    T = 10_000.0

    # At uptime 600s: nerve-stale, ok False
    adb_health.reset_transport_status()
    adb_health.mark_watchdog_started(now=T - 600.0)
    status_600 = adb_health.watchdog_tick(
        probe=lambda: _status(checked_at=T, adb_serials=("A",)),
        now=lambda: T,
    )
    ok_600, block_600 = adb_health.transport_health(
        status_600,
        now=T,
        monitor_uptime_seconds=600.0,
    )
    assert ok_600 is False
    assert block_600["state"] == "nerve-stale"

    # At uptime below STARTUP_GRACE_SECONDS (e.g. 10s): NOT reported 'nerve-stale', ok True
    adb_health.reset_transport_status()
    adb_health.mark_watchdog_started(now=T - 10.0)
    status_10 = adb_health.watchdog_tick(
        probe=lambda: _status(checked_at=T, adb_serials=("A",)),
        now=lambda: T,
    )
    ok_10, block_10 = adb_health.transport_health(
        status_10,
        now=T,
        monitor_uptime_seconds=10.0,
    )
    assert ok_10 is True
    assert block_10["state"] != "nerve-stale"


def test_watchdog_tick_injected_clock_threaded_into_serial_age_check() -> None:
    """AC 3: the injected clock is threaded into the serial-age check.
    record_nerve_android_serials({'A'}, now=1000.0),
    then watchdog_tick(probe=<adb_serials ('A','B')>, now=lambda: 1060.0)
    yields status.drift == ('B',)."""
    adb_health.record_nerve_android_serials({"A"}, now=1000.0)

    status = adb_health.watchdog_tick(
        probe=lambda: _status(adb_serials=("A", "B")),
        now=lambda: 1060.0,
    )

    assert status.drift == ("B",)


def test_transport_health_nerve_stale_healthy_case_unchanged() -> None:
    """AC 4: (healthy case unchanged) one test:
    serials published 30s before the tick and matching adb give
    transport_health ok True with state 'ok'; WHILE the identical setup with serials
    published 181s before the tick gives ok False with state 'nerve-stale'."""
    T = 10_000.0

    # Healthy case: 30s before the tick -> ok True, state 'ok'
    adb_health.reset_transport_status()
    adb_health.mark_watchdog_started(now=T - 600.0)
    adb_health.record_nerve_android_serials({"A"}, now=T - 30.0)
    status_healthy = adb_health.watchdog_tick(
        probe=lambda: _status(checked_at=T, adb_serials=("A",)),
        now=lambda: T,
    )
    ok_healthy, block_healthy = adb_health.transport_health(
        status_healthy,
        now=T,
        monitor_uptime_seconds=600.0,
    )
    assert ok_healthy is True
    assert block_healthy["state"] == "ok"

    # Stale case: 181s before the tick -> ok False, state 'nerve-stale'
    adb_health.reset_transport_status()
    adb_health.mark_watchdog_started(now=T - 600.0)
    adb_health.record_nerve_android_serials({"A"}, now=T - 181.0)
    status_stale = adb_health.watchdog_tick(
        probe=lambda: _status(checked_at=T, adb_serials=("A",)),
        now=lambda: T,
    )
    ok_stale, block_stale = adb_health.transport_health(
        status_stale,
        now=T,
        monitor_uptime_seconds=600.0,
    )
    assert ok_stale is False
    assert block_stale["state"] == "nerve-stale"
