"""L3 + Info regressions: no fixed /tmp path at import, private temp files.

Security review 2026-10-02 (linc-nerve 0.1.0 pre-release), finding L3:
``mcp/server.py`` opened ``/tmp/nerve_threads.txt`` (symlink-followable) every
time the MCP server was imported. Info items: ``_devicectl_pid`` leaked its
``NamedTemporaryFile(delete=False)`` files, and the screenshot output dir was a
shared, predictable ``<tmp>/nerve-output`` parent created at import.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import pytest

_PROBE = textwrap.dedent(
    """
    import json, os, sys

    events = []

    def _hook(event, args):
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            mode, flags = args[1], args[2] or 0
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                mode is None and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
            )
            if writing:
                events.append(os.fsdecode(args[0]))
        elif event == "os.mkdir":
            events.append(os.fsdecode(args[0]))

    sys.addaudithook(_hook)
    import linc_nerve.mcp.server  # noqa: F401
    sys.stdout.write(json.dumps([e for e in events if "__pycache__" not in e]))
    """
)


def test_importing_server_creates_nothing_in_tmp(tmp_path: Path) -> None:
    """A fresh interpreter importing the MCP server writes nothing to any temp dir."""
    private_tmp = tmp_path / "tmpdir"
    private_tmp.mkdir()
    env = {**os.environ, "TMPDIR": str(private_tmp), "LINC_HOME": str(tmp_path / "home")}
    env.pop("NERVE_THREAD_DUMP", None)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=True,
    )
    written = json.loads(proc.stdout)
    temp_roots = ("/tmp", "/private/tmp", str(private_tmp))
    offenders = [p for p in written if p.startswith(temp_roots)]
    assert offenders == []
    assert list(private_tmp.iterdir()) == []


def test_thread_dump_defaults_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    from linc_nerve.mcp import server

    monkeypatch.delenv("NERVE_THREAD_DUMP", raising=False)
    assert server._thread_dump_file() is sys.stderr


def test_thread_dump_file_is_private_under_linc_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from linc_nerve.mcp import server

    monkeypatch.setenv("NERVE_THREAD_DUMP", "1")
    monkeypatch.setenv("LINC_HOME", str(tmp_path))
    f = server._thread_dump_file()
    try:
        path = tmp_path / "nerve" / "nerve_threads.txt"
        assert f.writable()
        assert path.exists()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        f.close()


def test_thread_dump_refuses_planted_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from linc_nerve.mcp import server

    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    dump_dir = tmp_path / "nerve"
    dump_dir.mkdir()
    (dump_dir / "nerve_threads.txt").symlink_to(victim)
    monkeypatch.setenv("NERVE_THREAD_DUMP", "1")
    monkeypatch.setenv("LINC_HOME", str(tmp_path))
    with pytest.raises(OSError):
        server._thread_dump_file()
    assert victim.read_text() == "keep me"


def test_output_dir_is_private_mkdtemp_created_lazily(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from linc_nerve.mcp import _core

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(_core, "_OUTPUT_DIR", None)
    first = _core._output_dir()
    assert first == _core._output_dir()
    assert Path(first).parent == tmp_path
    assert Path(first).name.startswith("nerve-output-")
    assert stat.S_IMODE(os.stat(first).st_mode) == 0o700
    assert _core._next_output_path("emu-1", "screenshot").startswith(first + os.sep)


class _RecordingDevicectl:
    """Recording fake for ``subprocess.run`` that writes devicectl JSON output."""

    def __init__(self, payloads: dict[str, Any], fail: bool = False) -> None:
        self.payloads = payloads
        self.fail = fail
        self.output_paths: list[str] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        out = cmd[cmd.index("--json-output") + 1]
        self.output_paths.append(out)
        if self.fail:
            raise OSError("xcrun not found")
        with open(out, "w") as f:
            json.dump(self.payloads[cmd[4]], f)
        return subprocess.CompletedProcess(cmd, 0)


@pytest.mark.parametrize("fail", [False, True])
def test_devicectl_temp_files_removed_after_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail: bool
) -> None:
    from linc_nerve.mcp import diagnostic_tools

    url = "file:///private/var/containers/Bundle/App/ABC/iosApp.app/"
    fake = _RecordingDevicectl(
        {
            "apps": {"result": {"apps": [{"bundleIdentifier": "com.x", "url": url}]}},
            "processes": {
                "result": {
                    "runningProcesses": [{"executable": url + "iosApp", "processIdentifier": 42}]
                }
            },
        },
        fail=fail,
    )
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", fake)

    pid = diagnostic_tools._devicectl_pid("00008110-0000000000000001", "com.x")

    assert pid == (None if fail else 42)
    assert fake.output_paths
    assert all(not os.path.exists(p) for p in fake.output_paths)
    assert list(tmp_path.iterdir()) == []
