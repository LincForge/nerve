"""Which adb server nerve talks to — ANDROID_ADB_SERVER_PORT / ADB_SERVER_SOCKET (7aaba3c3).

nerve used to hard-code ``127.0.0.1:5037`` for its persistent adbutils client
and ``adb -L tcp:5037`` for the wedge watchdog's pgrep, so it always saw (and
could only ever recover) the host's default adb server — every USB device on
the machine. Exporting adb's own variables now points the whole of nerve at a
different server (an emulator-only host or container), which is the
isolation knob SECURITY.md names.

No real adb server is ever started here: the end-to-end test speaks the adb
host protocol from a ``socketserver`` fake on an ephemeral loopback port, and
adbutils' own ``adb start-server`` fallback is stubbed to fail loudly.
"""

from __future__ import annotations

import ast
import pathlib
import socketserver
import threading
import types
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest

from linc_nerve.core import adb_server
from linc_nerve.core.adb_server import AdbServerAddress, adb_server_address
from linc_nerve.core.android import AndroidController

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "linc_nerve"


@pytest.fixture(autouse=True)
def _no_adb_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from an environment with neither variable exported."""
    monkeypatch.delenv("ANDROID_ADB_SERVER_PORT", raising=False)
    monkeypatch.delenv("ADB_SERVER_SOCKET", raising=False)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def test_unset_env_is_the_default_server() -> None:
    """Pin: with nothing exported nerve still talks to 127.0.0.1:5037."""
    assert adb_server_address() == AdbServerAddress("127.0.0.1", 5037, "tcp:5037")


def test_android_adb_server_port_moves_the_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5099")
    assert adb_server_address() == AdbServerAddress("127.0.0.1", 5099, "tcp:5099")


def test_adb_server_socket_wins_over_the_port_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5099")
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:localhost:5098")
    assert adb_server_address() == AdbServerAddress("localhost", 5098, "tcp:localhost:5098")


def test_adb_server_socket_port_only_form(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:5098")
    assert adb_server_address() == AdbServerAddress("127.0.0.1", 5098, "tcp:5098")


@pytest.mark.parametrize("spec", ["tcp:[::1]:5098", "tcp:::1:5098"])
def test_adb_server_socket_accepts_ipv6_loopback(
    monkeypatch: pytest.MonkeyPatch, spec: str
) -> None:
    monkeypatch.setenv("ADB_SERVER_SOCKET", spec)
    assert adb_server_address() == AdbServerAddress("::1", 5098, spec)


def test_blank_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """``export ANDROID_ADB_SERVER_PORT=`` is the shell idiom for "clear it"."""
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "  ")
    monkeypatch.setenv("ADB_SERVER_SOCKET", "")
    assert adb_server_address().port == 5037


def test_resolver_reads_the_environment_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    assert adb_server_address().port == 5037
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5099")
    assert adb_server_address().port == 5099


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("ANDROID_ADB_SERVER_PORT", "abc"),
        ("ANDROID_ADB_SERVER_PORT", "70000"),
        ("ANDROID_ADB_SERVER_PORT", "0"),
        ("ANDROID_ADB_SERVER_PORT", "-1"),
        ("ADB_SERVER_SOCKET", "abc"),
        ("ADB_SERVER_SOCKET", "tcp:70000"),
        ("ADB_SERVER_SOCKET", "tcp:abc"),
        ("ADB_SERVER_SOCKET", "tcp:10.0.0.5:5037"),
        ("ADB_SERVER_SOCKET", "tcp:example.com:5037"),
        ("ADB_SERVER_SOCKET", "localabstract:adb"),
    ],
)
def test_malformed_or_non_loopback_values_name_the_variable(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(ValueError, match=var):
        adb_server_address()


def test_a_bad_socket_spec_is_reported_even_when_the_port_variable_is_fine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADB_SERVER_SOCKET wins, so a broken one must not silently fall through."""
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5099")
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:10.0.0.5:5037")
    with pytest.raises(ValueError, match="ADB_SERVER_SOCKET"):
        adb_server_address()


# ---------------------------------------------------------------------------
# Watchdog pgrep pattern — derived from the same resolver
# ---------------------------------------------------------------------------


def test_pgrep_pattern_targets_the_configured_server_only() -> None:
    pattern = AdbServerAddress("127.0.0.1", 5099, "tcp:5099").pgrep_pattern
    assert pattern.startswith("adb -L tcp:5099")
    assert "5037" not in pattern


def test_pgrep_pattern_is_anchored_so_a_port_prefix_cannot_match_another_server() -> None:
    """``tcp:503`` must not also match the default server's ``tcp:5037``."""
    import re

    pattern = AdbServerAddress("127.0.0.1", 503, "tcp:503").pgrep_pattern
    assert re.search(pattern, "adb -L tcp:503 fork-server server --reply-fd 4")
    assert not re.search(pattern, "adb -L tcp:5037 fork-server server --reply-fd 4")


def test_pgrep_pattern_escapes_regex_metacharacters_in_the_spec() -> None:
    import re

    pattern = AdbServerAddress("::1", 5098, "tcp:[::1]:5098").pgrep_pattern
    assert re.search(pattern, "adb -L tcp:[::1]:5098 fork-server server")
    assert not re.search(pattern, "adb -L tcp:1:5098 fork-server server")


# ---------------------------------------------------------------------------
# adb subprocesses inherit the environment — nerve never pins -P/-L 5037
# ---------------------------------------------------------------------------


def _code_constants(path: pathlib.Path) -> Iterator[str]:
    """Every str/int literal in *path* except docstrings (prose may name 5037)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        literal = isinstance(node, ast.Constant) and id(node) not in docstrings
        if literal and type(node.value) in (str, int):
            yield str(node.value)


def test_no_nerve_code_pins_the_default_adb_server() -> None:
    """``adb`` honours both variables natively, so argv must never override them.

    An explicit ``-P 5037`` / ``-L tcp:5037`` on any adb subprocess would send
    that call back to the host's default server behind the operator's back.
    The only place the default may be spelled is the resolver itself.
    """
    resolver = pathlib.Path(adb_server.__file__).resolve()
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.resolve() == resolver:
            continue
        for value in _code_constants(path):
            if value in {"-P", "-L"} or "5037" in value:
                offenders.append(f"{path.relative_to(SRC)}: {value[:60]!r}")
    assert offenders == []


# ---------------------------------------------------------------------------
# End to end — a fake adb server on an ephemeral loopback port
# ---------------------------------------------------------------------------


class _FakeAdbServer(socketserver.ThreadingTCPServer):
    """Speaks just enough of the adb host protocol: ``host:version``/``host:devices``."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FakeAdbHandler)
        self.requests_seen: list[str] = []
        self.lock = threading.Lock()


class _FakeAdbHandler(socketserver.BaseRequestHandler):
    _REPLIES: ClassVar[dict[str, str]] = {"host:version": "0029", "host:devices": ""}

    def handle(self) -> None:
        server: Any = self.server
        header = self._read(4)
        command = self._read(int(header, 16)).decode()
        with server.lock:
            server.requests_seen.append(command)
        if command in self._REPLIES:
            payload = self._REPLIES[command].encode()
            self.request.sendall(b"OKAY" + f"{len(payload):04x}".encode() + payload)
        else:
            reason = b"unsupported by fake"
            self.request.sendall(b"FAIL" + f"{len(reason):04x}".encode() + reason)

    def _read(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf


@pytest.fixture
def fake_adb_server() -> Iterator[_FakeAdbServer]:
    server = _FakeAdbServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="fake-adb")
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def no_real_adb_start(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """adbutils runs ``adb start-server`` when it cannot connect — forbid it."""
    import adbutils._adb

    attempts: list[Any] = []

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        attempts.append(args)
        raise AssertionError("test tried to start a real adb server")

    monkeypatch.setattr(adbutils._adb, "subprocess", types.SimpleNamespace(run=_forbidden))
    return attempts


def test_discovery_talks_to_the_server_named_by_android_adb_server_port(
    monkeypatch: pytest.MonkeyPatch,
    fake_adb_server: _FakeAdbServer,
    no_real_adb_start: list[Any],
) -> None:
    port = fake_adb_server.server_address[1]
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", str(port))

    ctrl = AndroidController()
    devices = ctrl.discover()

    assert devices == []
    assert "host:devices" in fake_adb_server.requests_seen
    assert ctrl._get_client().port == port
    assert no_real_adb_start == []


def test_fake_adb_server_answers_host_version(
    monkeypatch: pytest.MonkeyPatch,
    fake_adb_server: _FakeAdbServer,
    no_real_adb_start: list[Any],
) -> None:
    """The fake is a faithful enough adb: the real adbutils client can read its version."""
    port = fake_adb_server.server_address[1]
    monkeypatch.setenv("ADB_SERVER_SOCKET", f"tcp:{port}")

    assert AndroidController()._get_client().server_version() == 41
    assert fake_adb_server.requests_seen == ["host:version"]
    assert no_real_adb_start == []
