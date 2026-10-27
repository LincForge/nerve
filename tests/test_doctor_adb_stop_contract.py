"""[contract] `nerve doctor` only stops an adb server its own probe started.

`adb kill-server` drops every attached device, so doctor may run it only when
both facts hold: the pre-probe connect was positively refused (no server was
there) AND the `adb devices` stderr says *this* call started the daemon. Every
other outcome — connect timeout, an unexpected exception, an invalid
``ADB_SERVER_SOCKET`` — must read as "a server may already be running, leave it
alone". Everything here is stubbed: no real adb, subprocess, or socket.
"""

from __future__ import annotations

import errno
import subprocess as sp

import pytest

from linc_nerve.cli import doctor as doctor_mod
from linc_nerve.cli.doctor import AdbProbe, classify_connect_error, should_stop
from linc_nerve.core.adb_server import AdbServerAddress

_STARTED = "* daemon not running; starting now at tcp:5037\n* daemon started successfully\n"
_ADDR = AdbServerAddress("127.0.0.1", 5037, "tcp:5037")


@pytest.fixture
def stubbed(monkeypatch) -> list[list[str]]:
    """Stub every impure probe except the adb path under test; record adb argv."""
    monkeypatch.setattr(doctor_mod, "ADB_PATH", "/sdk/adb")
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda _p: "/sdk/adb")
    monkeypatch.setattr(doctor_mod, "_probe_daemon_health", lambda _port: None)
    monkeypatch.setattr(doctor_mod, "_probe_xcode_clt", lambda: None)
    return []


def _fake_run(calls: list[list[str]], stderr: str):
    def run(cmd: list[str], **_kw: object) -> sp.CompletedProcess[str]:
        calls.append(list(cmd))
        return sp.CompletedProcess(cmd, 0, stdout="List of devices attached\n", stderr=stderr)

    return run


def _kills(calls: list[list[str]]) -> int:
    return sum(1 for c in calls if c[1:] == ["kill-server"])


# ---------------------------------------------------------------------------
# The four required end-to-end cases (gather_probes with injected seams)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec", ["10.0.0.5:5037", "tcp:evil.example:5037", "garbage", "tcp:99999", "tcp:-1", "tcp:"]
)
def test_invalid_adb_server_socket_does_not_kill(stubbed, monkeypatch, spec) -> None:
    """[contract] An ADB_SERVER_SOCKET nerve rejects never leads to kill-server."""
    monkeypatch.setenv("ADB_SERVER_SOCKET", spec)
    monkeypatch.setattr(doctor_mod.subprocess, "run", _fake_run(stubbed, _STARTED))
    monkeypatch.setattr(
        doctor_mod.socket,
        "create_connection",
        lambda *_a, **_k: (_ for _ in ()).throw(ConnectionRefusedError()),
    )
    probes = doctor_mod.gather_probes()
    assert _kills(stubbed) == 0
    assert probes.adb_server_listening is True


def test_connect_timeout_does_not_kill(stubbed, monkeypatch) -> None:
    """[contract] A wedged server that misses the connect timeout is left alone."""

    def timeout(*_a: object, **_k: object) -> None:
        raise TimeoutError("timed out")

    monkeypatch.delenv("ADB_SERVER_SOCKET", raising=False)
    monkeypatch.setattr(doctor_mod.socket, "create_connection", timeout)
    monkeypatch.setattr(doctor_mod.subprocess, "run", _fake_run(stubbed, _STARTED))
    doctor_mod.gather_probes()
    assert _kills(stubbed) == 0


@pytest.mark.parametrize("stderr", ["", "error: something else\n", None])
def test_refused_without_daemon_started_does_not_kill(stubbed, monkeypatch, stderr) -> None:
    """[contract] Refused, but adb devices did not start a daemon (race: someone else did)."""
    monkeypatch.setattr(doctor_mod.subprocess, "run", _fake_run(stubbed, stderr))
    doctor_mod.gather_probes(listening_probe=lambda _a: AdbProbe.REFUSED)
    assert _kills(stubbed) == 0


def test_refused_and_daemon_started_kills_once(stubbed, monkeypatch) -> None:
    """[contract] Refused + 'daemon started successfully' -> exactly one kill-server."""
    monkeypatch.setattr(doctor_mod.subprocess, "run", _fake_run(stubbed, _STARTED))
    probes = doctor_mod.gather_probes(listening_probe=lambda _a: AdbProbe.REFUSED)
    assert stubbed == [["/sdk/adb", "devices"], ["/sdk/adb", "kill-server"]]
    assert _kills(stubbed) == 1
    assert probes.adb_server_listening is False


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("probe", "stderr", "expected"),
    [
        (AdbProbe.REFUSED, _STARTED, True),
        (AdbProbe.REFUSED, "*  DAEMON   Started\tSuccessfully *", True),
        (AdbProbe.REFUSED, "", False),
        (AdbProbe.REFUSED, None, False),
        (AdbProbe.REFUSED, "daemon started", False),
        (AdbProbe.LISTENING, _STARTED, False),
        (AdbProbe.UNKNOWN, _STARTED, False),
        (AdbProbe.UNKNOWN, None, False),
    ],
)
def test_should_stop_truth_table(probe, stderr, expected) -> None:
    """[contract] should_stop requires REFUSED AND the daemon-started phrase."""
    assert should_stop(probe, stderr) is expected


@pytest.mark.parametrize("hostile", [True, False, 0, 1, "refused", object()])
def test_should_stop_rejects_non_enum_probe(hostile) -> None:
    """[contract] A non-AdbProbe value (e.g. a legacy bool) never authorises a kill."""
    assert should_stop(hostile, _STARTED) is False  # type: ignore[arg-type]


def test_should_stop_rejects_non_str_stderr() -> None:
    """[contract] Bytes / non-str stderr is not parsed as consent to kill."""
    assert should_stop(AdbProbe.REFUSED, b"daemon started successfully") is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ConnectionRefusedError(), AdbProbe.REFUSED),
        (OSError(errno.ECONNREFUSED, "refused"), AdbProbe.REFUSED),
        (TimeoutError(), AdbProbe.UNKNOWN),
        (OSError(errno.ETIMEDOUT, "timed out"), AdbProbe.UNKNOWN),
        (OSError(errno.EHOSTUNREACH, "unreachable"), AdbProbe.UNKNOWN),
        (OSError("no errno"), AdbProbe.UNKNOWN),
        (ValueError("bad"), AdbProbe.UNKNOWN),
    ],
)
def test_classify_connect_error(exc, expected) -> None:
    """[contract] Only a positive ECONNREFUSED classifies as REFUSED."""
    assert classify_connect_error(exc) is expected


def test_adb_server_probe_maps_connect_outcomes(monkeypatch) -> None:
    """[contract] The socket probe: connect -> LISTENING, refused -> REFUSED, else UNKNOWN."""

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    seen: list[tuple[object, object]] = []

    def ok(target: object, timeout: object = None) -> _Conn:
        seen.append((target, timeout))
        return _Conn()

    monkeypatch.setattr(doctor_mod.socket, "create_connection", ok)
    assert doctor_mod._adb_server_probe(_ADDR) is AdbProbe.LISTENING
    assert seen == [(("127.0.0.1", 5037), 0.5)]

    def refused(*_a: object, **_k: object) -> None:
        raise ConnectionRefusedError()

    monkeypatch.setattr(doctor_mod.socket, "create_connection", refused)
    assert doctor_mod._adb_server_probe(_ADDR) is AdbProbe.REFUSED

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(doctor_mod.socket, "create_connection", boom)
    assert doctor_mod._adb_server_probe(_ADDR) is AdbProbe.UNKNOWN


@pytest.mark.parametrize("bad", [True, False, None, "REFUSED"])
def test_probe_adb_listening_coerces_non_enum_to_unknown(monkeypatch, bad) -> None:
    """[contract] An injected probe returning a non-AdbProbe value reads as UNKNOWN."""
    monkeypatch.delenv("ADB_SERVER_SOCKET", raising=False)
    assert doctor_mod._probe_adb_listening(lambda _a: bad) is AdbProbe.UNKNOWN


def test_probe_adb_listening_exception_is_unknown(monkeypatch) -> None:
    """[contract] A raising probe reads as UNKNOWN (never REFUSED)."""

    def boom(_a: object) -> AdbProbe:
        raise OSError(errno.ECONNREFUSED, "refused")

    monkeypatch.delenv("ADB_SERVER_SOCKET", raising=False)
    assert doctor_mod._probe_adb_listening(boom) is AdbProbe.UNKNOWN


def test_devices_stderr_captured_on_nonzero_exit(monkeypatch) -> None:
    """[contract] A probe-started daemon is still stopped when adb devices exits non-zero."""
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kw: object) -> sp.CompletedProcess[str]:
        calls.append(list(cmd))
        return sp.CompletedProcess(cmd, 1, stdout="", stderr=_STARTED)

    monkeypatch.setattr(doctor_mod.subprocess, "run", run)
    listening, out = doctor_mod._run_adb_probe("/sdk/adb", True, lambda _a: AdbProbe.REFUSED)
    assert out is None
    assert calls == [["/sdk/adb", "devices"], ["/sdk/adb", "kill-server"]]
    assert listening is False
