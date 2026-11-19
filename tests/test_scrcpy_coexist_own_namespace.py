"""Tests for nerve's own scrcpy-server device path and hex scid namespace (linc-nerve-cj7w)."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from linc_nerve.core import ios as _ios_preload  # noqa: F401
from linc_nerve.core import scrcpy as scrcpy_mod
from linc_nerve.core.adb_path import ADB_PATH
from linc_nerve.core.scrcpy import SCRCPY_VERSION, ScrcpyStream

EXPECTED_DEVICE_PATH = f"/data/local/tmp/nerve-scrcpy-server-v{SCRCPY_VERSION}.jar"
UPSTREAM_DEVICE_PATH = "/data/local/tmp/scrcpy-server.jar"


def _vendored_bytes_and_sha256() -> tuple[int, str]:
    server_path = scrcpy_mod._SERVER_DIR / scrcpy_mod._SERVER_FILENAME
    raw = server_path.read_bytes()
    return len(raw), hashlib.sha256(raw).hexdigest()


class TestEnsureServerPushedOwnNamespace:
    """[test] _ensure_server_pushed uses nerve's versioned jar path and verifies sha256."""

    @pytest.mark.parametrize(
        "wc_stdout",
        ["", "0\n"],
        ids=["missing_remote_file", "different_remote_size"],
    )
    def test_ensure_server_pushed_uses_versioned_nerve_device_path(
        self, monkeypatch: pytest.MonkeyPatch, wc_stdout: str
    ) -> None:
        local_size, _local_sha = _vendored_bytes_and_sha256()
        recorded_cmds: list[list[str]] = []
        recorded_kwargs: list[dict[str, object]] = []
        log_calls: list[tuple[str, dict[str, object]]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded_cmds.append(list(cmd))
            recorded_kwargs.append(dict(kwargs))
            joined = " ".join(cmd)
            if "wc -c" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=wc_stdout, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            scrcpy_mod.logger,
            "info",
            lambda event, **kw: log_calls.append((event, kw)),
        )

        stream = ScrcpyStream("R3CFAKE00001", port=27183)
        stream._ensure_server_pushed()

        assert scrcpy_mod._DEVICE_PATH == EXPECTED_DEVICE_PATH
        assert recorded_cmds, "expected adb subprocess calls"
        for argv in recorded_cmds:
            for token in argv:
                assert UPSTREAM_DEVICE_PATH not in token, (
                    f"must never pass upstream path {UPSTREAM_DEVICE_PATH!r}, got {argv!r}"
                )

        push_calls = [
            (argv, kw)
            for argv, kw in zip(recorded_cmds, recorded_kwargs, strict=True)
            if "push" in argv
        ]
        assert len(push_calls) == 1
        push_argv, push_kw = push_calls[0]
        assert push_argv == [
            ADB_PATH,
            "-s",
            "R3CFAKE00001",
            "push",
            str(scrcpy_mod._SERVER_DIR / scrcpy_mod._SERVER_FILENAME),
            EXPECTED_DEVICE_PATH,
        ]
        assert push_kw == {"capture_output": True, "check": True, "timeout": 30}
        assert log_calls == [
            (
                "scrcpy_server_pushed",
                {
                    "serial": "R3CFAKE00001",
                    "size": local_size,
                    "reason": "size_mismatch",
                },
            )
        ]

    def test_ensure_server_pushed_skips_push_only_when_size_and_sha256_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        local_size, local_sha = _vendored_bytes_and_sha256()
        recorded_cmds: list[list[str]] = []
        recorded_kwargs: list[dict[str, object]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded_cmds.append(list(cmd))
            recorded_kwargs.append(dict(kwargs))
            joined = " ".join(cmd)
            if "wc -c" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{local_size}\n", stderr="")
            if "sha256sum" in joined:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"{local_sha}  {EXPECTED_DEVICE_PATH}\n", stderr=""
                )
            raise AssertionError(f"unexpected adb command: {cmd!r}")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)

        stream = ScrcpyStream("R3CFAKE00001", port=27183)
        stream._ensure_server_pushed()

        assert len(recorded_cmds) == 2
        assert recorded_cmds[0] == [
            ADB_PATH,
            "-s",
            "R3CFAKE00001",
            "shell",
            f"wc -c < {EXPECTED_DEVICE_PATH}",
        ]
        assert recorded_kwargs[0] == {"capture_output": True, "text": True, "timeout": 5}
        assert recorded_cmds[1] == [
            ADB_PATH,
            "-s",
            "R3CFAKE00001",
            "shell",
            f"sha256sum {EXPECTED_DEVICE_PATH}",
        ]
        assert recorded_kwargs[1] == {"capture_output": True, "text": True, "timeout": 5}

    def test_ensure_server_pushed_repushes_when_same_size_wrong_sha256(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        local_size, local_sha = _vendored_bytes_and_sha256()
        wrong_sha = ("0" if local_sha[0] != "0" else "f") * 64
        recorded_cmds: list[list[str]] = []
        log_calls: list[tuple[str, dict[str, object]]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded_cmds.append(list(cmd))
            joined = " ".join(cmd)
            if "wc -c" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{local_size}\n", stderr="")
            if "sha256sum" in joined:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"{wrong_sha}  {EXPECTED_DEVICE_PATH}\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            scrcpy_mod.logger,
            "info",
            lambda event, **kw: log_calls.append((event, kw)),
        )

        stream = ScrcpyStream("R3CFAKE00001", port=27183)
        stream._ensure_server_pushed()

        push_cmds = [argv for argv in recorded_cmds if "push" in argv]
        assert len(push_cmds) == 1
        assert push_cmds[0][-1] == EXPECTED_DEVICE_PATH
        assert log_calls == [
            (
                "scrcpy_server_pushed",
                {
                    "serial": "R3CFAKE00001",
                    "size": local_size,
                    "reason": "sha256_mismatch",
                },
            )
        ]

    @pytest.mark.parametrize(
        "sha_stdout",
        [
            "",
            "sha256sum: not found",
            "a" * 63,
            "g" * 64,
        ],
        ids=["empty", "not_found", "hex_63_chars", "non_hex_64_chars"],
    )
    def test_ensure_server_pushed_forces_push_on_invalid_sha_output(
        self, monkeypatch: pytest.MonkeyPatch, sha_stdout: str
    ) -> None:
        local_size, _local_sha = _vendored_bytes_and_sha256()
        recorded_cmds: list[list[str]] = []
        log_calls: list[tuple[str, dict[str, object]]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded_cmds.append(list(cmd))
            joined = " ".join(cmd)
            if "wc -c" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{local_size}\n", stderr="")
            if "sha256sum" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=sha_stdout, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            scrcpy_mod.logger,
            "info",
            lambda event, **kw: log_calls.append((event, kw)),
        )

        stream = ScrcpyStream("R3CFAKE00001", port=27183)
        stream._ensure_server_pushed()

        push_cmds = [argv for argv in recorded_cmds if "push" in argv]
        assert len(push_cmds) == 1
        assert push_cmds[0][-1] == EXPECTED_DEVICE_PATH
        assert log_calls == [
            (
                "scrcpy_server_pushed",
                {
                    "serial": "R3CFAKE00001",
                    "size": local_size,
                    "reason": "sha256_unreadable",
                },
            )
        ]

    def test_ensure_server_pushed_forces_push_when_sha256_command_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        local_size, _local_sha = _vendored_bytes_and_sha256()
        recorded_cmds: list[list[str]] = []
        log_calls: list[tuple[str, dict[str, object]]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded_cmds.append(list(cmd))
            joined = " ".join(cmd)
            if "wc -c" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{local_size}\n", stderr="")
            if "sha256sum" in joined:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            scrcpy_mod.logger,
            "info",
            lambda event, **kw: log_calls.append((event, kw)),
        )

        stream = ScrcpyStream("R3CFAKE00001", port=27183)
        stream._ensure_server_pushed()

        push_cmds = [argv for argv in recorded_cmds if "push" in argv]
        assert len(push_cmds) == 1
        assert log_calls == [
            (
                "scrcpy_server_pushed",
                {
                    "serial": "R3CFAKE00001",
                    "size": local_size,
                    "reason": "sha256_unreadable",
                },
            )
        ]

    def test_vendored_sha256_computed_once_per_process(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake_jar = tmp_path / f"scrcpy-server-v{SCRCPY_VERSION}"
        payload = b"nerve-vendored-scrcpy-jar-bytes"
        fake_jar.write_bytes(payload)
        expected_sha = hashlib.sha256(payload).hexdigest()

        read_count = 0
        real_read_bytes = Path.read_bytes

        def counting_read_bytes(self_path: Path) -> bytes:
            nonlocal read_count
            if self_path == fake_jar:
                read_count += 1
            return real_read_bytes(self_path)

        monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
        monkeypatch.setattr(scrcpy_mod, "_SERVER_DIR", tmp_path)
        monkeypatch.setattr(
            scrcpy_mod.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    f"{len(payload)}\n"
                    if "wc -c" in " ".join(cmd)
                    else f"{expected_sha}  {EXPECTED_DEVICE_PATH}\n"
                ),
                stderr="",
            ),
        )

        stream = ScrcpyStream("R3CFAKE00001", port=27183)
        stream._ensure_server_pushed()
        stream._ensure_server_pushed()
        assert read_count == 1


class TestStartServerNerveScidContract:
    """[contract] nerve_scid and scid_socket_name drive both adb forward and Popen scid=."""

    @pytest.mark.parametrize(
        ("port", "expected_hex"),
        [
            (27183, "6e656a2f"),
            (27199, "6e656a3f"),
            (65535, "6e65ffff"),
        ],
        ids=["port_27183", "port_27199", "port_65535"],
    )
    def test_start_server_forward_and_popen_scid_match(
        self, monkeypatch: pytest.MonkeyPatch, port: int, expected_hex: str
    ) -> None:
        from linc_nerve.core import scrcpy_stream_server
        from linc_nerve.core.scrcpy import nerve_scid, scid_socket_name

        assert "nerve_scid" in scrcpy_mod.__all__
        assert "scid_socket_name" in scrcpy_mod.__all__
        assert scrcpy_stream_server.nerve_scid is nerve_scid
        assert scrcpy_stream_server.scid_socket_name is scid_socket_name

        run_calls: list[tuple[list[str], dict[str, object]]] = []
        popen_calls: list[tuple[list[str], dict[str, object]]] = []
        fake_proc = MagicMock()

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            run_calls.append((list(cmd), dict(kwargs)))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        def fake_popen(cmd: list[str], **kwargs: object) -> MagicMock:
            popen_calls.append((list(cmd), dict(kwargs)))
            return fake_proc

        monkeypatch.setattr(scrcpy_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(scrcpy_mod.subprocess, "Popen", fake_popen)

        stream = ScrcpyStream("R3CFAKE00001", port=port)
        stream._start_server()

        scid = nerve_scid(port)
        assert 0 < scid < 2**31
        assert (scid >> 16) == 0x6E65
        assert (scid & 0xFFFF) == port
        assert scid == int(expected_hex, 16)
        assert nerve_scid(0x10000 | port) == scid

        expected_remote = f"localabstract:scrcpy_{expected_hex}"
        assert scid_socket_name(scid) == expected_remote

        assert len(run_calls) == 1
        forward_argv, forward_kw = run_calls[0]
        assert forward_argv == [
            ADB_PATH,
            "-s",
            "R3CFAKE00001",
            "forward",
            f"tcp:{port}",
            expected_remote,
        ]
        assert forward_kw == {"capture_output": True, "check": True, "timeout": 5}
        forward_remote = forward_argv[5]
        assert scid_socket_name(scid) == forward_remote

        assert len(popen_calls) == 1
        popen_argv, popen_kw = popen_calls[0]
        assert popen_kw == {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        }
        assert f"CLASSPATH={EXPECTED_DEVICE_PATH}" in popen_argv
        assert f"CLASSPATH={UPSTREAM_DEVICE_PATH}" not in popen_argv

        scid_args = [arg for arg in popen_argv if arg.startswith("scid=")]
        assert scid_args == [f"scid={expected_hex}"]
        scid_str = scid_args[0].removeprefix("scid=")
        assert len(scid_str) == 8
        parsed_scid = int(scid_str, 16)
        assert parsed_scid == scid
        assert parsed_scid < 2**31
        assert (parsed_scid >> 16) == 0x6E65
        assert scid_socket_name(parsed_scid) == forward_remote
