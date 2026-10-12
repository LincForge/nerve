"""Which adb server nerve talks to — the single source of truth (7aaba3c3).

nerve honours adb's own variables, read at call time:

1. ``ADB_SERVER_SOCKET`` — ``tcp:<port>`` or ``tcp:<host>:<port>`` (adb's syntax);
2. ``ANDROID_ADB_SERVER_PORT`` — a bare port;
3. otherwise the default server, ``127.0.0.1:5037``.

Both the persistent adbutils client (``android_reliability._get_client``) and
the wedge watchdog's ``pgrep`` (``adb_health.find_adb_server_pid``) resolve
through here, so recovery only ever targets the server nerve is configured
for. ``adb`` subprocesses inherit the environment and adb honours the same two
variables natively, so nerve never passes ``-P`` / ``-L`` itself.

This is the isolation knob: point nerve at an adb server that sees only the
devices you intend (an emulator-only host or container) by exporting its port.
The daemon's threat model is loopback, so only loopback hosts are accepted.
A malformed value raises ``ValueError`` naming the variable — it never falls
through to the default server, which would silently reach the host's devices.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

SOCKET_ENV = "ADB_SERVER_SOCKET"
PORT_ENV = "ANDROID_ADB_SERVER_PORT"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5037

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_PORT_RE = re.compile(r"[0-9]{1,5}")


@dataclass(frozen=True)
class AdbServerAddress:
    """Where the adb server listens, plus the socket spec it was started with."""

    host: str
    port: int
    spec: str

    @property
    def pgrep_pattern(self) -> str:
        """``pgrep -f`` ERE matching this server's process (``adb -L <spec> fork-server …``).

        Anchored on the following space / end so ``tcp:503`` can never match
        the default server's ``tcp:5037``.
        """
        return f"adb -L {re.escape(self.spec)}( |$)"


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _parse_port(raw: str, var: str, value: str) -> int:
    port = int(raw) if _PORT_RE.fullmatch(raw) else 0
    if not 1 <= port <= 65535:
        raise ValueError(f"{var}={value!r}: port must be an integer in 1-65535")
    return port


def _parse_socket_spec(value: str) -> AdbServerAddress:
    if not value.startswith("tcp:"):
        raise ValueError(f"{SOCKET_ENV}={value!r}: expected tcp:<port> or tcp:<host>:<port>")
    host, _, raw_port = value[len("tcp:") :].rpartition(":")
    host = host.removeprefix("[").removesuffix("]").lower() or DEFAULT_HOST
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"{SOCKET_ENV}={value!r}: only loopback hosts are supported "
            f"({', '.join(sorted(LOOPBACK_HOSTS))})"
        )
    return AdbServerAddress(host, _parse_port(raw_port, SOCKET_ENV, value), value)


def adb_server_address() -> AdbServerAddress:
    """Resolve the adb server from the environment *now* (never cached)."""
    socket_spec = _env(SOCKET_ENV)
    if socket_spec:
        return _parse_socket_spec(socket_spec)
    raw_port = _env(PORT_ENV)
    if raw_port:
        port = _parse_port(raw_port, PORT_ENV, raw_port)
        return AdbServerAddress(DEFAULT_HOST, port, f"tcp:{port}")
    return AdbServerAddress(DEFAULT_HOST, DEFAULT_PORT, f"tcp:{DEFAULT_PORT}")
