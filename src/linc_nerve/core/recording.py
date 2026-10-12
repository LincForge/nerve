"""Screen recording verification utilities.

Small fail-closed predicates shared by the platform controllers, so the
controller ``start_recording`` / ``stop_recording`` methods stay branch-free
(audit F7).
"""

from __future__ import annotations

import contextlib
import signal
import subprocess
from pathlib import Path

import av

from .frames import open_video


def assert_recording_nonempty(path: Path, stderr: str) -> None:
    """Raise if the recorded artifact is 0 bytes or missing."""
    size = path.stat().st_size if path.exists() else 0
    if size == 0:
        detail = f" stderr: {stderr.strip()}" if stderr.strip() else ""
        msg = f"Recording produced a 0-byte artifact at {path}.{detail}"
        raise RuntimeError(msg)


def _stderr_detail(stderr: str) -> str:
    return f" stderr: {stderr.strip()}" if stderr.strip() else ""


def _has_video_frame(path: Path) -> bool:
    """True when the MP4 at *path* decodes at least one video frame.

    Stops at the first frame, so a long recording costs one decode.
    """
    with open_video(path) as container:
        if not container.streams.video:
            return False
        return next(container.decode(container.streams.video[0]), None) is not None


def assert_recording_playable(path: Path, stderr: str) -> None:
    """Raise if the MP4 at *path* has no decodable video frame.

    Wear OS ``adb shell screenrecord`` exits 0 but writes an ~8 KB MP4 with no
    frames, which the 0-byte guard lets through (reactor 25be2624). Opened with
    the MP4 demuxer pinned (``frames.open_video``); any FFmpeg error counts as
    unplayable rather than escaping as an ``av`` exception.
    """
    try:
        playable = _has_video_frame(path)
    except av.FFmpegError:
        playable = False
    if not playable:
        size = path.stat().st_size
        msg = (
            f"Recording at {path} has no decodable video frames ({size} bytes). "
            "On Wear OS, adb screenrecord exits 0 but writes an empty MP4; "
            f"record the watch with scrcpy instead.{_stderr_detail(stderr)}"
        )
        raise RuntimeError(msg)


def exited_stderr(process: subprocess.Popen) -> str:
    """Return an exited child's stderr as text; ``""`` while it still runs.

    Reading a live child's pipe would block, so only drain once ``poll()``
    reports an exit code.
    """
    if process.poll() is None or process.stderr is None:
        return ""
    raw = process.stderr.read()
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)


def raise_if_exited(process: subprocess.Popen, label: str) -> None:
    """Raise if a child that should still be running has already exited."""
    rc = process.poll()
    if rc is not None:
        raise RuntimeError(f"{label} exited {rc}: {exited_stderr(process)}")


def pop_recording(
    processes: dict[str, subprocess.Popen],
    paths: dict[str, str],
    serial: str,
) -> tuple[subprocess.Popen, str]:
    """Remove and return ``(process, path)`` for ``serial``; raise if none."""
    process = processes.pop(serial, None)
    path = paths.pop(serial, "")
    if process is None:
        raise ValueError(f"No recording in progress for {serial}")
    return process, path


def interrupt_and_reap(
    process: subprocess.Popen,
    grace: float = 10,
    kill_grace: float = 5,
) -> None:
    """SIGINT a recorder so it finalizes the file, then reap it (bounded).

    Falls back to SIGKILL after ``grace``; the post-kill wait is bounded too
    so a wedged child can't pin the daemon's single tool worker indefinitely.
    """
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=kill_grace)
