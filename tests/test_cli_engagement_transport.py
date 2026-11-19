"""Contract and unit tests for CLI in-process routing under NERVE_ENGAGEMENT.

Covers bead linc-nerve-wp3e (R4 finding 3): when ``NERVE_ENGAGEMENT`` is set
in the CLI environment, tool calls execute in-process under that environment's
engagement policy instead of talking to ``:8111``, ``daemon.port``, or
``LINC_NERVE_DAEMON``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from linc_cli_kit.errors import parse_error_envelope

from linc_nerve.cli import main as cli_main
from linc_nerve.cli.main import main
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.ios import IOSController
from linc_nerve.mcp import _core
from linc_nerve.mcp import server as mcp_mod

pytestmark = pytest.mark.usefixtures("restore_signal_handlers")


class _RecordingDaemon:
    """Loopback HTTP fake daemon that records every incoming request."""

    def __init__(self, response_body: dict[str, Any] | None = None) -> None:
        self.requests: list[tuple[str, str, bytes]] = []
        self._lock = threading.Lock()
        self._response_body = response_body or {
            "result": {"success": True, "action": "key", "key": "HOME"}
        }
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with outer._lock:
                    outer.requests.append(("GET", self.path, b""))
                payload = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                with outer._lock:
                    outer.requests.append(("POST", self.path, body))
                payload = json.dumps(outer._response_body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _FakeAdbShellServer:
    """Loopback fake adb server listing UNREGISTERED-SERIAL and recording shell commands."""

    def __init__(self, serials: tuple[str, ...] = ("UNREGISTERED-SERIAL",)) -> None:
        self._serials = serials
        self.commands: list[str] = []
        self._lock = threading.Lock()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(2.0)
            while True:
                try:
                    header = conn.recv(4)
                except OSError:
                    return
                if len(header) < 4:
                    return
                try:
                    length = int(header.decode("ascii"), 16)
                except ValueError:
                    return
                cmd_bytes = b""
                while len(cmd_bytes) < length:
                    chunk = conn.recv(length - len(cmd_bytes))
                    if not chunk:
                        return
                    cmd_bytes += chunk
                cmd = cmd_bytes.decode("utf-8", errors="replace")
                with self._lock:
                    self.commands.append(cmd)
                if cmd == "host:devices":
                    body = "".join(f"{s}\tdevice\n" for s in self._serials).encode("utf-8")
                    conn.sendall(b"OKAY" + f"{len(body):04x}".encode("ascii") + body)
                elif cmd.startswith("host:transport:"):
                    conn.sendall(b"OKAY")
                elif cmd.startswith("shell"):
                    conn.sendall(b"OKAY")
                    return
                else:
                    msg = b"unsupported"
                    conn.sendall(b"FAIL" + f"{len(msg):04x}".encode("ascii") + msg)
                    return

    def close(self) -> None:
        self._srv.close()


@pytest.fixture
def recording_daemon() -> Iterator[_RecordingDaemon]:
    daemon = _RecordingDaemon()
    try:
        yield daemon
    finally:
        daemon.close()


@pytest.fixture
def fake_adb_server() -> Iterator[_FakeAdbShellServer]:
    srv = _FakeAdbShellServer()
    try:
        yield srv
    finally:
        srv.close()


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _setup_subprocess_env(
    tmp_path: Path,
    *,
    daemon_url: str | None = None,
    engagement_id: str | None = None,
    artifacts_root: Path | None = None,
    fleet_yaml: str | None = None,
    adb_port: int | None = None,
) -> tuple[dict[str, str], Path]:
    root = Path(os.path.realpath(tmp_path))
    home = root / "home"
    linc_home = root / "linc_home"
    sys_tmp = root / "tmp"
    fake_bin = root / "bin"
    adb_log = root / "fake-adb.log"
    for d in (home, linc_home, sys_tmp, fake_bin):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)

    _write_exe(
        fake_bin / "adb",
        f'#!/usr/bin/env bash\necho "$*" >> "{adb_log}"\n'
        'if [[ "${1:-}" == "devices" ]]; then\n'
        "  printf 'List of devices attached\\nUNREGISTERED-SERIAL\\tdevice\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    _write_exe(
        fake_bin / "xcrun",
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args and args[0] == 'simctl':\n"
        "    print(json.dumps({'devices': {}}))\n"
        "elif '--json-output' in args:\n"
        "    out = args[args.index('--json-output') + 1]\n"
        "    open(out, 'w').write(json.dumps({'result': {'devices': []}}))\n"
        "sys.exit(0)\n",
    )

    if fleet_yaml is not None:
        (linc_home / "device-fleet.yaml").write_text(fleet_yaml, encoding="utf-8")

    env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "HOME": str(home),
        "LINC_HOME": str(linc_home),
        "TMPDIR": str(sys_tmp),
        "NERVE_USAGE_LOG": "off",
    }
    if daemon_url is not None:
        env["LINC_NERVE_DAEMON"] = daemon_url
    if engagement_id is not None:
        env["NERVE_ENGAGEMENT"] = engagement_id
    if artifacts_root is not None:
        env["NERVE_ARTIFACTS_ROOT"] = str(artifacts_root)
    if adb_port is not None:
        env["ANDROID_ADB_SERVER_PORT"] = str(adb_port)
    return env, adb_log


def _run_nerve_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "import sys; from linc_nerve.cli.main import main; main()", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _last_stderr_json(stderr: str) -> dict[str, Any]:
    doc = parse_error_envelope(stderr)
    assert doc is not None, f"Expected JSON error envelope on stderr, got: {stderr!r}"
    return doc


# ---------------------------------------------------------------------------
# [contract] Acceptance test
# ---------------------------------------------------------------------------


def test_contract_engagement_bypasses_daemon_and_refuses_unregistered_device(
    tmp_path: Path,
    recording_daemon: _RecordingDaemon,
    fake_adb_server: _FakeAdbShellServer,
) -> None:
    """[contract] With NERVE_ENGAGEMENT=acme-1, LINC_NERVE_DAEMON -> fake daemon,
    and a fake adb shell, `nerve key --device UNREGISTERED-SERIAL --key HOME`
    exits non-zero with the engagement refusal and the fake daemon receives no request.
    Without NERVE_ENGAGEMENT the daemon path is unchanged.
    """
    vault = Path(os.path.realpath(tmp_path)) / "vault"
    vault.mkdir(mode=0o700)
    fleet_yaml = "devices:\n  - serial: FAKESERIAL01\n    personal: false\n"

    env_eng, adb_log = _setup_subprocess_env(
        tmp_path / "eng",
        daemon_url=recording_daemon.url,
        engagement_id="acme-1",
        artifacts_root=vault,
        fleet_yaml=fleet_yaml,
        adb_port=fake_adb_server.port,
    )
    res_eng = _run_nerve_cli(["key", "--device", "UNREGISTERED-SERIAL", "--key", "HOME"], env_eng)

    assert res_eng.returncode != 0, (
        f"Expected non-zero exit under NERVE_ENGAGEMENT=acme-1; "
        f"stdout={res_eng.stdout!r} stderr={res_eng.stderr!r}"
    )
    assert res_eng.stdout == ""
    envelope = _last_stderr_json(res_eng.stderr)
    err = envelope["error"]
    assert err["details"].get("error_category") == "engagement_misconfigured"
    assert "acme-1" in err["message"]
    assert "UNREGISTERED-SERIAL" in err["message"]
    assert recording_daemon.requests == [], (
        f"Fake daemon must receive zero requests under NERVE_ENGAGEMENT, "
        f"got {recording_daemon.requests!r}"
    )
    assert not any("keyevent" in cmd for cmd in fake_adb_server.commands)
    adb_lines = adb_log.read_text().splitlines() if adb_log.exists() else []
    assert not any(
        "UNREGISTERED-SERIAL" in ln or "keyevent" in ln or "shell" in ln for ln in adb_lines
    ), f"Unexpected adb invocation for UNREGISTERED-SERIAL: {adb_lines!r}"

    # Without NERVE_ENGAGEMENT, the daemon path is unchanged.
    env_plain, _ = _setup_subprocess_env(
        tmp_path / "plain",
        daemon_url=recording_daemon.url,
        engagement_id=None,
        adb_port=fake_adb_server.port,
    )
    res_plain = _run_nerve_cli(
        ["key", "--device", "UNREGISTERED-SERIAL", "--key", "HOME"], env_plain
    )
    assert res_plain.returncode == 0, res_plain.stderr
    assert json.loads(res_plain.stdout) == {"success": True, "action": "key", "key": "HOME"}
    assert len(recording_daemon.requests) == 1
    method, path, body = recording_daemon.requests[0]
    assert method == "POST"
    assert path == "/tools/nerve_key"
    assert json.loads(body.decode("utf-8"))["device"] == "UNREGISTERED-SERIAL"


# ---------------------------------------------------------------------------
# Additional isolation, misconfiguration, port-file, and daemon-only coverage
# ---------------------------------------------------------------------------


def test_engagement_bypasses_port_file_daemon_too(
    tmp_path: Path,
    recording_daemon: _RecordingDaemon,
    fake_adb_server: _FakeAdbShellServer,
) -> None:
    """When NERVE_ENGAGEMENT is set without LINC_NERVE_DAEMON, a daemon.port file is ignored."""
    vault = Path(os.path.realpath(tmp_path)) / "vault"
    vault.mkdir(mode=0o700)
    env, _ = _setup_subprocess_env(
        tmp_path / "portfile",
        daemon_url=None,
        engagement_id="acme-1",
        artifacts_root=vault,
        fleet_yaml="devices:\n  - serial: FAKESERIAL01\n    personal: false\n",
        adb_port=fake_adb_server.port,
    )
    port_file = Path(env["LINC_HOME"]) / "nerve" / "daemon.port"
    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(str(recording_daemon.port), encoding="utf-8")

    res = _run_nerve_cli(["key", "--device", "UNREGISTERED-SERIAL", "--key", "HOME"], env)
    assert res.returncode != 0
    assert recording_daemon.requests == []
    envelope = _last_stderr_json(res.stderr)
    assert envelope["error"]["details"].get("error_category") == "engagement_misconfigured"


@pytest.mark.parametrize(
    ("eng_val", "with_vault"),
    [
        ("acme-1", False),
        ("bad id!", True),
        ("../escape", True),
        ("   ", True),
        ("acme-1\n", True),
    ],
    ids=["missing_root", "invalid_chars", "traversal", "whitespace", "trailing_newline"],
)
def test_misconfigured_engagement_fails_with_existing_error_and_zero_daemon_requests(
    tmp_path: Path,
    recording_daemon: _RecordingDaemon,
    eng_val: str,
    with_vault: bool,
) -> None:
    """Misconfigured NERVE_ENGAGEMENT / missing root fails with engagement_misconfigured,
    never touches the daemon, and never creates devices.db."""
    vault: Path | None = None
    if with_vault:
        vault = Path(os.path.realpath(tmp_path)) / "vault"
        vault.mkdir(mode=0o700)
    env, _ = _setup_subprocess_env(
        tmp_path / "misconfig",
        daemon_url=recording_daemon.url,
        engagement_id=eng_val,
        artifacts_root=vault,
    )

    res = _run_nerve_cli(["key", "--device", "UNREGISTERED-SERIAL", "--key", "HOME"], env)
    assert res.returncode == 1
    assert res.stdout == ""
    assert recording_daemon.requests == []
    envelope = _last_stderr_json(res.stderr)
    assert envelope["error"]["details"].get("error_category") == "engagement_misconfigured"
    assert not (Path(env["LINC_HOME"]) / ".local" / "share" / "linc-nerve" / "devices.db").exists()


def test_empty_engagement_env_uses_daemon_path(
    tmp_path: Path,
    recording_daemon: _RecordingDaemon,
) -> None:
    """NERVE_ENGAGEMENT='' means OFF, matching engagement.py semantics."""
    env, _ = _setup_subprocess_env(
        tmp_path / "empty_eng",
        daemon_url=recording_daemon.url,
        engagement_id="",
    )
    res = _run_nerve_cli(["key", "--device", "UNREGISTERED-SERIAL", "--key", "HOME"], env)
    assert res.returncode == 0, res.stderr
    assert len(recording_daemon.requests) == 1


def test_daemon_only_tool_under_engagement_never_contacts_daemon(
    tmp_path: Path,
    recording_daemon: _RecordingDaemon,
) -> None:
    """Daemon-only tools fail closed without hitting the shared daemon under NERVE_ENGAGEMENT."""
    # Misconfigured engagement -> engagement_misconfigured even for daemon-only tools.
    env_bad, _ = _setup_subprocess_env(
        tmp_path / "bad_daemon_only",
        daemon_url=recording_daemon.url,
        engagement_id="acme-1",
        artifacts_root=None,
    )
    res_bad = _run_nerve_cli(["record-start", "--device", "FAKESERIAL01"], env_bad)
    assert res_bad.returncode == 1
    assert recording_daemon.requests == []
    assert (
        _last_stderr_json(res_bad.stderr)["error"]["details"].get("error_category")
        == "engagement_misconfigured"
    )

    # Valid engagement + vault -> DAEMON_UNAVAILABLE (daemon-only tools need an engagement daemon).
    vault = Path(os.path.realpath(tmp_path)) / "vault"
    vault.mkdir(mode=0o700)
    env_ok, _ = _setup_subprocess_env(
        tmp_path / "ok_daemon_only",
        daemon_url=recording_daemon.url,
        engagement_id="acme-1",
        artifacts_root=vault,
    )
    res_ok = _run_nerve_cli(["record-start", "--device", "FAKESERIAL01"], env_ok)
    assert res_ok.returncode == 1
    assert recording_daemon.requests == []
    assert _last_stderr_json(res_ok.stderr)["error"]["code"] == "DAEMON_UNAVAILABLE"


@pytest.fixture
def inprocess_cli_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path, MagicMock]]:
    """Isolate LINC_HOME, vault, and fake controllers for in-process CliRunner tests."""
    root = Path(os.path.realpath(tmp_path))
    linc_home = root / "linc_home"
    vault = root / "vault"
    linc_home.mkdir(mode=0o700)
    vault.mkdir(mode=0o700)
    (linc_home / "device-fleet.yaml").write_text(
        "devices:\n  - serial: FAKESERIAL01\n    personal: false\n", encoding="utf-8"
    )
    monkeypatch.setenv("LINC_HOME", str(linc_home))

    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = [
        NerveDevice(
            serial="FAKESERIAL01",
            platform=Platform.ANDROID,
            device_type=DeviceType.PHONE,
            model="FakePhone",
        )
    ]
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []

    mock_mgr = MagicMock(spec=DeviceManager)
    mock_mgr.guard.return_value = None

    saved = (
        mcp_mod._android,
        mcp_mod._ios,
        mcp_mod._device_mgr,
        _core._cached_devices,
        _core._cache_timestamp,
    )
    try:
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios
        mcp_mod._device_mgr = mock_mgr
        _core._cached_devices = []
        _core._cache_timestamp = 0.0
        monkeypatch.setattr(cli_main, "_ensure_initialized", lambda: (mock_android, mock_ios))
        yield linc_home, vault, mock_android
    finally:
        (
            mcp_mod._android,
            mcp_mod._ios,
            mcp_mod._device_mgr,
            _core._cached_devices,
            _core._cache_timestamp,
        ) = saved


def test_registered_non_personal_device_executes_in_process_under_engagement(
    inprocess_cli_env: tuple[Path, Path, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    recording_daemon: _RecordingDaemon,
) -> None:
    """A registered personal: false device runs in-process under NERVE_ENGAGEMENT."""
    _linc_home, vault, mock_android = inprocess_cli_env
    monkeypatch.setenv("NERVE_ENGAGEMENT", "acme-1")
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(vault))
    monkeypatch.setenv("LINC_NERVE_DAEMON", recording_daemon.url)

    res = CliRunner().invoke(main, ["key", "--device", "FAKESERIAL01", "--key", "HOME"])
    assert res.exit_code == 0, res.stderr
    assert json.loads(res.stdout.strip().splitlines()[-1]) == {
        "success": True,
        "action": "key",
        "key": "HOME",
    }
    mock_android.press_key.assert_called_once_with("FAKESERIAL01", "HOME")
    assert recording_daemon.requests == []


def test_inprocess_engagement_branches_cover_misconfig_daemon_only_and_refusal(
    inprocess_cli_env: tuple[Path, Path, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    recording_daemon: _RecordingDaemon,
) -> None:
    """In-process branch coverage for misconfig, daemon-only, overrides, and refusal."""
    _linc_home, vault, _mock_android = inprocess_cli_env
    monkeypatch.setenv("LINC_NERVE_DAEMON", recording_daemon.url)

    # 1. Misconfigured engagement (missing root) -> fails closed before fallback/daemon.
    monkeypatch.setenv("NERVE_ENGAGEMENT", "acme-1")
    monkeypatch.delenv("NERVE_ARTIFACTS_ROOT", raising=False)
    res_bad = CliRunner().invoke(main, ["key", "--device", "UNREGISTERED-SERIAL", "--key", "HOME"])
    assert res_bad.exit_code == 1
    assert (
        _last_stderr_json(res_bad.stderr)["error"]["details"].get("error_category")
        == "engagement_misconfigured"
    )

    # 2. Valid root + daemon-only tool -> DAEMON_UNAVAILABLE, no daemon request.
    monkeypatch.setenv("NERVE_ARTIFACTS_ROOT", str(vault))
    res_daemon_only = CliRunner().invoke(main, ["record-start", "--device", "FAKESERIAL01"])
    assert res_daemon_only.exit_code == 1
    assert _last_stderr_json(res_daemon_only.stderr)["error"]["code"] == "DAEMON_UNAVAILABLE"

    # 3. Valid root + unregistered device on generated and hand-written override commands.
    res_unreg = CliRunner().invoke(
        main, ["key", "--device", "UNREGISTERED-SERIAL", "--key", "HOME"]
    )
    assert res_unreg.exit_code == 1
    assert (
        _last_stderr_json(res_unreg.stderr)["error"]["details"].get("error_category")
        == "engagement_misconfigured"
    )

    res_override = CliRunner().invoke(main, ["tap", "UNREGISTERED-SERIAL", "10", "20"])
    assert res_override.exit_code == 1
    assert (
        _last_stderr_json(res_override.stderr)["error"]["details"].get("error_category")
        == "engagement_misconfigured"
    )
    assert recording_daemon.requests == []


def test_contract_call_in_process_under_engagement_refuses_first_then_daemon_only_then_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] Under NERVE_ENGAGEMENT the CLI helper (1) returns the engagement refusal
    for THIS tool before touching the local transport (so a misconfiguration never
    initialises device state), (2) refuses daemon-only tools with an exact
    DAEMON_UNAVAILABLE error, and (3) otherwise runs the tool through the local transport."""
    from types import SimpleNamespace

    from linc_cli_kit import ToolError

    local_calls: list[tuple[str, dict[str, Any]]] = []

    class _Local:
        def call(self, spec: Any, payload: dict[str, Any]) -> dict[str, Any]:
            local_calls.append((spec.name, payload))
            return {"ran": spec.name}

    transport = SimpleNamespace(
        tool="nerve", daemon_only=frozenset({"nerve_record_start"}), fallback=_Local()
    )
    refusal = {"error": "misconfigured", "error_category": "engagement_misconfigured"}
    monkeypatch.setattr(
        cli_main._core,
        "_engagement_refusal",
        lambda name: refusal if name == "nerve_key" else None,
    )

    key = SimpleNamespace(name="nerve_key")
    assert cli_main._call_in_process_under_engagement(transport, key, {"k": 1}) == refusal
    assert local_calls == []

    with pytest.raises(ToolError) as exc:
        cli_main._call_in_process_under_engagement(
            transport, SimpleNamespace(name="nerve_record_start"), {}
        )
    err = exc.value
    assert err.code == "DAEMON_UNAVAILABLE"
    assert err.category == "unavailable"
    assert err.message == (
        "nerve daemon cannot be used for daemon-only tool 'nerve_record_start' "
        "while NERVE_ENGAGEMENT is active"
    )
    assert err.hint == "use a single-shot CLI command or an MCP session under NERVE_ENGAGEMENT"
    assert local_calls == []

    tap = SimpleNamespace(name="nerve_tap")
    assert cli_main._call_in_process_under_engagement(transport, tap, {"x": 1}) == {
        "ran": "nerve_tap"
    }
    assert local_calls == [("nerve_tap", {"x": 1})]

    no_local = SimpleNamespace(tool="nerve", daemon_only=frozenset(), fallback=None)
    with pytest.raises(RuntimeError, match=r"^local transport is not initialised$"):
        cli_main._call_in_process_under_engagement(no_local, tap, {})
