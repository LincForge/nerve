"""Automated stdio MCP round trip through ``nerve bellows`` (molecule linc-nerve mcp-2x, bead
stdio-roundtrip-pin).

Spawns ``nerve bellows`` as a subprocess over real OS pipes and drives newline-delimited
JSON-RPC: initialize, notifications/initialized, tools/list, five sequential tools/call of the
device-free global tool ``nerve_session_stats``, malformed and hostile lines, an unknown argument,
then stdin EOF. This is the wire-level oracle for the mcp 1.x -> 2.x port.

Isolation: HOME, LINC_HOME and TMPDIR live under ``tmp_path``; NERVE_HTTP_PORT is a free port so
bellows' HTTP side never meets the live daemon on :8111; PATH holds only the interpreter's bin
dir, so no adb / xcrun is reachable. Nothing here touches a device or the network.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import queue
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import Any

import pytest

# Import _core first: importing linc_nerve.core.ios_discovery first (conftest's autouse fixture)
# trips a pre-existing ios <-> ios_discovery import cycle when this file runs on its own.
from linc_nerve.mcp import _core
from linc_nerve.mcp.sse_server import _TOOL_MODULES

_CALL_DEADLINE_S = 10.0
_START_DEADLINE_S = 30.0
_EXIT_DEADLINE_S = 10.0
_PROTOCOL_VERSION = "2025-06-18"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _registry_names() -> set[str]:
    """Registered tool names, without importing ``linc_nerve.mcp.server`` (signal handlers)."""
    for module_path in _TOOL_MODULES:
        importlib.import_module(module_path)
    return set(_core.TOOL_REGISTRY)


class _Bellows:
    """A ``nerve bellows`` subprocess with a deadline-bounded JSON-RPC reader."""

    def __init__(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        linc_home = tmp_path / "linc_home"
        tmpdir = tmp_path / "tmp"
        for d in (home, linc_home, tmpdir):
            d.mkdir(parents=True, exist_ok=True)
        env = {
            "HOME": str(home),
            "LINC_HOME": str(linc_home),
            "TMPDIR": str(tmpdir),
            "NERVE_HTTP_PORT": str(_free_port()),
            "PATH": str(Path(sys.executable).parent),
            "PYTHONUNBUFFERED": "1",
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "from linc_nerve.cli.main import main; main()", "bellows"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(tmp_path),
        )
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._stderr: list[bytes] = []
        self._pumps = [
            threading.Thread(target=self._pump_stdout, daemon=True),
            threading.Thread(target=self._pump_stderr, daemon=True),
        ]
        for pump in self._pumps:
            pump.start()

    def _pump_stdout(self) -> None:
        stdout = self.proc.stdout
        if stdout is None:
            raise RuntimeError("bellows stdout is not a pipe")
        for line in stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _pump_stderr(self) -> None:
        stderr = self.proc.stderr
        if stderr is None:
            raise RuntimeError("bellows stderr is not a pipe")
        self._stderr.extend(stderr)

    def stderr_tail(self) -> str:
        return b"".join(self._stderr).decode("utf-8", errors="replace")[-4000:]

    def send_raw(self, payload: bytes) -> None:
        stdin = self.proc.stdin
        if stdin is None:
            raise RuntimeError("bellows stdin is not a pipe")
        stdin.write(payload)
        stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw((json.dumps(msg) + "\n").encode())

    def request(
        self,
        req_id: int,
        method: str,
        params: dict[str, Any] | None = None,
        deadline_s: float = _CALL_DEADLINE_S,
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw((json.dumps(msg) + "\n").encode())
        deadline = time.monotonic() + deadline_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pytest.fail(
                    f"no reply to {method} id={req_id} within {deadline_s}s\n{self.stderr_tail()}"
                )
            try:
                line = self._lines.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if line is None:
                pytest.fail(
                    f"bellows closed stdout before replying to {method} id={req_id}\n"
                    f"{self.stderr_tail()}"
                )
            reply = json.loads(line)
            if reply.get("id") == req_id:
                return reply
            # Notifications (no id) and stale replies are skipped.

    def initialize(self) -> dict[str, Any]:
        reply = self.request(
            1,
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "nerve-roundtrip-test", "version": "0"},
            },
            deadline_s=_START_DEADLINE_S,
        )
        self.notify("notifications/initialized")
        return reply

    def close_stdin_and_wait(self, deadline_s: float = _EXIT_DEADLINE_S) -> int | None:
        stdin = self.proc.stdin
        if stdin is not None:
            stdin.close()
        try:
            return self.proc.wait(timeout=deadline_s)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        """Kill the process if still alive, then release every pipe (filterwarnings=error)."""
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=_EXIT_DEADLINE_S)
        for pump in self._pumps:
            pump.join(timeout=_EXIT_DEADLINE_S)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if pipe is not None and not pipe.closed:
                with contextlib.suppress(BrokenPipeError):
                    pipe.close()


@pytest.fixture
def bellows(tmp_path: Path) -> Iterator[_Bellows]:
    b = _Bellows(tmp_path)
    try:
        yield b
    finally:
        b.kill()


def _tool_names(reply: dict[str, Any]) -> set[str]:
    return {tool["name"] for tool in reply["result"]["tools"]}


def _first_text(reply: dict[str, Any]) -> str:
    content = reply["result"]["content"]
    return next(block["text"] for block in content if block.get("type") == "text")


def test_stdio_roundtrip_session(bellows: _Bellows) -> None:
    """[test] initialize, tools/list, 5 sequential calls, malformed line, unknown arg, EOF exit."""
    init = bellows.initialize()
    assert init["result"]["serverInfo"]["name"] == "linc-nerve"
    # mcp 2.x: MCPServer(version=) reports nerve's own version (1.x reported the SDK's).
    assert init["result"]["serverInfo"]["version"] == _dist_version("linc-nerve")

    listed = bellows.request(2, "tools/list")
    assert _tool_names(listed) == _registry_names()

    for i in range(5):
        t0 = time.monotonic()
        reply = bellows.request(
            10 + i, "tools/call", {"name": "nerve_session_stats", "arguments": {}}
        )
        assert time.monotonic() - t0 < _CALL_DEADLINE_S
        assert reply["result"]["isError"] is False, reply
        assert "count" in json.loads(_first_text(reply))

    bellows.send_raw(b"{not json\n")
    survived = bellows.request(20, "tools/list")
    assert _tool_names(survived) == _registry_names()

    unknown = bellows.request(
        21, "tools/call", {"name": "nerve_session_stats", "arguments": {"unknown_arg_xyz": 1}}
    )
    assert unknown["result"]["isError"] is True, unknown
    assert "unknown_arg_xyz" in _first_text(unknown)

    rc = bellows.close_stdin_and_wait()
    assert rc is not None, f"bellows did not exit within {_EXIT_DEADLINE_S}s of stdin EOF"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"\n", id="empty-line"),
        pytest.param(b"[]\n", id="json-array"),
        pytest.param(b"\xff\xfe\xfa{\n", id="invalid-utf8"),
        pytest.param(b"x" * (1 << 20) + b"\n", id="1mb-line"),
    ],
)
def test_contract_hostile_line_keeps_server_answering(bellows: _Bellows, payload: bytes) -> None:
    """[contract] a hostile stdio line never kills the reader: the next tools/list is answered."""
    bellows.initialize()
    bellows.send_raw(payload)
    reply = bellows.request(30, "tools/list")
    assert _tool_names(reply) == _registry_names()
