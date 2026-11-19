"""Wave 4 file-hardening regressions (linc-nerve-lgkt).

1. ``frames._save_png`` opens ``O_WRONLY | O_CREAT | O_NOFOLLOW | O_NONBLOCK``
   (never ``O_TRUNC``), checks ``fstat(fd)`` is a single-link regular file
   (``S_ISREG`` and ``st_nlink == 1``), then ``ftruncate(fd, 0)`` and writes.
2. ``action_log._require_regular`` closes ``fd`` on any exception from
   ``os.fstat`` before re-raising.
3. ``pyproject.toml`` declares ``pillow>=13.0.0`` in ``[project].dependencies``.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import threading
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
from packaging.requirements import Requirement
from packaging.version import Version
from PIL import Image

import linc_nerve.core.ios  # noqa: F401
from linc_nerve.core import action_log, frames

REPO = Path(__file__).resolve().parents[1]


def _sample_image() -> Image.Image:
    return Image.new("RGB", (8, 6), (20, 40, 60))


def test_save_png_refuses_planted_hard_link_and_preserves_victim(tmp_path: Path) -> None:
    """[contract] A planted hard link (st_nlink > 1) is refused without truncating victim."""
    victim = tmp_path / "victim.txt"
    payload = b"important victim contents that must not be overwritten\n"
    victim.write_bytes(payload)
    planted = tmp_path / "frame_1000.png"
    os.link(victim, planted)

    opened_fds: list[tuple[int, int, int]] = []
    closed_fds: list[int] = []
    ftruncate_calls: list[tuple[int, int]] = []
    real_open = os.open
    real_close = os.close
    real_ftruncate = os.ftruncate

    def tracking_open(path: Path | str, flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)
        opened_fds.append((fd, flags, mode))
        return fd

    def tracking_close(fd: int) -> None:
        closed_fds.append(fd)
        real_close(fd)

    def tracking_ftruncate(fd: int, length: int) -> None:
        ftruncate_calls.append((fd, length))
        real_ftruncate(fd, length)

    with (
        patch.object(os, "open", side_effect=tracking_open),
        patch.object(os, "close", side_effect=tracking_close),
        patch.object(os, "ftruncate", side_effect=tracking_ftruncate),
        pytest.raises(OSError) as exc_info,
    ):
        frames._save_png(_sample_image(), planted)

    assert str(exc_info.value) == f"not a single-link regular file: {planted}"
    assert victim.read_bytes() == payload
    assert planted.read_bytes() == payload
    assert frames._PNG_FLAGS & os.O_TRUNC == 0
    assert frames._PNG_FLAGS & os.O_NOFOLLOW != 0
    assert len(opened_fds) == 1
    assert opened_fds[0][1] == frames._PNG_FLAGS
    assert opened_fds[0][2] == 0o644
    assert closed_fds == [opened_fds[0][0]]
    assert ftruncate_calls == []

    # Once the second link is removed (st_nlink == 1), ftruncate(fd, 0) runs and writes a PNG.
    victim.unlink()
    with patch.object(os, "ftruncate", side_effect=tracking_ftruncate):
        frames._save_png(_sample_image(), planted)
    assert len(ftruncate_calls) == 1
    assert ftruncate_calls[0][1] == 0


def test_save_png_refuses_fifo_without_blocking(tmp_path: Path) -> None:
    """[contract] A FIFO named frame_2000.png is refused without blocking."""
    fifo = tmp_path / "frame_2000.png"
    os.mkfifo(fifo)
    img = _sample_image()
    outcome: dict[str, BaseException | None] = {"exc": None}

    def worker() -> None:
        try:
            frames._save_png(img, fifo)
        except BaseException as exc:
            outcome["exc"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=2.0)
    if thread.is_alive():
        reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            thread.join(timeout=2.0)
        finally:
            os.close(reader)
        pytest.fail("_save_png blocked opening a FIFO named frame_2000.png")

    assert isinstance(outcome["exc"], OSError)
    assert str(fifo) in str(outcome["exc"])

    # Also verify a FIFO that already has a reader open is refused by the fstat S_ISREG check.
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(OSError) as exc_info:
            frames._save_png(img, fifo)
        assert str(exc_info.value) == f"not a single-link regular file: {fifo}"
        assert os.read(reader, 64) == b""
    finally:
        os.close(reader)


def test_save_png_overwrites_existing_single_link_file(tmp_path: Path) -> None:
    """[pin] An existing single-link frame_3000.png is overwritten with a valid PNG."""
    target = tmp_path / "frame_3000.png"
    stale = b"stale-bytes-" * 500
    target.write_bytes(stale)

    frames._save_png(_sample_image(), target)

    raw = target.read_bytes()
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    assert raw.endswith(b"IEND\xaeB`\x82")
    assert len(raw) < len(stale)
    with Image.open(target) as written:
        written.load()
        assert written.size == (8, 6)


def test_save_png_closes_fd_when_fstat_raises(tmp_path: Path) -> None:
    """If os.fstat raises inside _save_png, the opened fd is closed and the error propagates."""
    dest = tmp_path / "frame_4000.png"
    opened_fds: list[int] = []
    closed_fds: list[int] = []
    real_open = os.open
    real_close = os.close

    def tracking_open(path: Path | str, flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)
        opened_fds.append(fd)
        return fd

    def tracking_close(fd: int) -> None:
        closed_fds.append(fd)
        real_close(fd)

    try:
        with (
            patch.object(os, "open", side_effect=tracking_open),
            patch.object(os, "close", side_effect=tracking_close),
            patch.object(os, "fstat", side_effect=OSError(errno.EIO, "fstat failed")),
            pytest.raises(OSError, match="fstat failed"),
        ):
            frames._save_png(_sample_image(), dest)
        assert opened_fds and closed_fds == opened_fds
    finally:
        for fd in set(opened_fds) - set(closed_fds):
            with pytest.raises(OSError):
                real_close(fd)


def test_open_append_closes_fd_when_fstat_raises(tmp_path: Path) -> None:
    """[contract] Patching os.fstat to raise OSError inside _open_append closes the fd."""
    log_path = tmp_path / "session.jsonl"
    opened_fds: list[int] = []
    closed_fds: list[int] = []
    real_open = os.open
    real_close = os.close

    def tracking_open(path: Path | str, flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)
        opened_fds.append(fd)
        return fd

    def tracking_close(fd: int) -> None:
        closed_fds.append(fd)
        real_close(fd)

    try:
        with (
            patch.object(os, "open", side_effect=tracking_open),
            patch.object(os, "close", side_effect=tracking_close),
            patch.object(os, "fstat", side_effect=OSError(errno.EIO, "simulated fstat error")),
            pytest.raises(OSError, match="simulated fstat error"),
        ):
            action_log._open_append(log_path)

        assert len(opened_fds) == 1
        assert closed_fds == opened_fds
    finally:
        for fd in set(opened_fds) - set(closed_fds):
            with contextlib.suppress(OSError):
                real_close(fd)

    # Normal regular file succeeds and FIFO with reader raises exact message and closes fd.
    with action_log._open_append(log_path) as handle:
        action_log._require_regular(handle.fileno(), log_path)
        handle.write("{}\n")
    fifo_log = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo_log)
    reader = os.open(fifo_log, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(OSError) as exc_info:
            action_log._open_append(fifo_log)
        assert str(exc_info.value) == f"not a regular file: {fifo_log}"
    finally:
        os.close(reader)


def test_pyproject_pillow_minimum_is_at_least_13() -> None:
    """[test] pyproject.toml requires pillow >= 13.0.0."""
    pyproject_path = REPO / "pyproject.toml"
    if not pyproject_path.is_file():
        pyproject_path = REPO.parent / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    deps = [Requirement(d) for d in data["project"]["dependencies"]]
    pillow_reqs = [r for r in deps if r.name.lower() == "pillow"]
    assert len(pillow_reqs) == 1
    req = pillow_reqs[0]
    lower_bounds = [
        Version(spec.version) for spec in req.specifier if spec.operator in (">=", "==")
    ]
    assert lower_bounds, f"No lower bound on pillow requirement: {req}"
    assert min(lower_bounds) >= Version("13.0.0"), (
        f"Expected pillow minimum >= 13.0.0, got {req}"
    )
    assert not req.specifier.contains(Version("12.99.0"))
    assert req.specifier.contains(Version("13.0.0"))
    assert stat.S_ISREG(pyproject_path.stat().st_mode)
