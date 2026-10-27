"""nerve-output retention: per-dir file cap, owner lock, stale sweep, cleanup (linc-nerve-si3y).

Each process writes tool output to its own private ``<tmp>/nerve-output-<random>/`` directory
and holds an exclusive ``flock`` on ``<dir>/.nerve-output.lock`` for its whole lifetime. The
kernel drops that lock when the process exits, so "lock held" is an exact liveness test that
survives an idle daemon and needs no pid bookkeeping (the pid in the file is informational).

Liveness fails SAFE: if the lock state cannot be determined (the lock path is a symlink or a
directory, ``open``/``flock`` errors), the directory is treated as live and never removed. A
directory with no lock file at all (written by an older nerve, or in the instant between
``mkdtemp`` and taking the lock) falls back to the age rules.

Pure stdlib — no click — so ``mcp/_core`` can import it without pulling in the CLI.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO

import structlog

log = structlog.get_logger().bind(domain="output_retention")

OUTPUT_DIR_PREFIX = "nerve-output-"
LOCK_NAME = ".nerve-output.lock"
CLEANUP_ACTIVE_WINDOW_S = 60.0
DEFAULT_OUTPUT_KEEP = 200
DEFAULT_OUTPUT_MAX_AGE_S = 86400
_OUTPUT_FILE_RE = re.compile(r"\A.+_(\d{4,})_[A-Za-z][A-Za-z0-9_]*\.[A-Za-z0-9]+\Z")
# Never follow a symlinked lock path, never block on a FIFO planted there, never leak the fd.
_NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def positive_int_env(name: str, default: int) -> int:
    """Parse a positive int from env *name*, falling back to *default* on 0/invalid."""
    try:
        val = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return val if val > 0 else default


def output_keep() -> int:
    return positive_int_env("NERVE_OUTPUT_KEEP", DEFAULT_OUTPUT_KEEP)


def output_max_age_s() -> int:
    return positive_int_env("NERVE_OUTPUT_MAX_AGE_S", DEFAULT_OUTPUT_MAX_AGE_S)


def sweep_enabled(environ: Mapping[str, str]) -> bool:
    """Whether the first-output stale sweep runs.

    ``NERVE_OUTPUT_SWEEP=0`` disables it, ``=1`` forces it. Under pytest it is off by default
    so a test run can never delete the real temp root's ``nerve-output-*`` directories.
    """
    flag = environ.get("NERVE_OUTPUT_SWEEP", "")
    if flag in ("0", "1"):
        return flag == "1"
    return "PYTEST_CURRENT_TEST" not in environ


# ---------------------------------------------------------------------------
# Per-dir retention cap
# ---------------------------------------------------------------------------


def matching_output_files(dir_path: str | os.PathLike[str]) -> list[tuple[int, int, str, str]]:
    """Return sorted ``(counter, mtime_ns, name, path)`` for regular nerve output files."""
    items: list[tuple[int, int, str, str]] = []
    with contextlib.suppress(OSError), os.scandir(dir_path) as it:
        for entry in it:
            match = _OUTPUT_FILE_RE.fullmatch(entry.name)
            if match is None:
                continue
            with contextlib.suppress(OSError):
                if entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    items.append((int(match.group(1)), st.st_mtime_ns, entry.name, entry.path))
    items.sort()
    return items


def prune_output_dir(dir: str | os.PathLike[str], keep: int = DEFAULT_OUTPUT_KEEP) -> list[str]:
    """Delete oldest regular nerve output files beyond ``keep - 1`` in *dir*."""
    limit = keep if keep > 0 else DEFAULT_OUTPUT_KEEP
    files = matching_output_files(dir)
    excess = max(0, len(files) - (limit - 1))
    removed: list[str] = []
    for _, _, _, path in files[:excess]:
        with contextlib.suppress(OSError):
            os.unlink(path)
            removed.append(path)
    return removed


# ---------------------------------------------------------------------------
# Owner lock (liveness)
# ---------------------------------------------------------------------------


def acquire_dir_lock(dir_path: str) -> BinaryIO | None:
    """Take this process's exclusive owner lock on *dir_path*; None if it cannot be taken.

    The returned handle must stay open for the life of the process (closing it releases the
    lock). Failure never raises: the dir still works, it is just unprotected.
    """
    try:
        fd = os.open(os.path.join(dir_path, LOCK_NAME), os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)
    except OSError:
        return None
    handle = os.fdopen(fd, "r+b", buffering=0)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.truncate(0)
        handle.write(f"{os.getpid()}\n".encode())
    except OSError:
        handle.close()
        return None
    return handle


def _lock_is_held(fd: int) -> bool:
    """True unless *fd* is a regular file a non-blocking shared flock succeeds on.

    Anything else (not a regular file, any ``fstat``/``flock`` error) is unknown -> held.
    """
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return True
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    return False


def dir_is_live(dir_path: str) -> bool:
    """True if a live process holds *dir_path*'s owner lock, or its state is unknown."""
    try:
        fd = os.open(os.path.join(dir_path, LOCK_NAME), os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        return _lock_is_held(fd)
    finally:
        os.close(fd)


def create_locked_output_dir(tmp_root: str | None = None) -> tuple[str, BinaryIO | None]:
    """Create a fresh private ``nerve-output-*`` dir and take its owner lock."""
    path = tempfile.mkdtemp(prefix=OUTPUT_DIR_PREFIX, dir=tmp_root)
    return path, acquire_dir_lock(path)


# ---------------------------------------------------------------------------
# Directory scanning
# ---------------------------------------------------------------------------


def is_owned_output_dir(entry_path: str, uid: int) -> os.stat_result | None:
    """Return ``lstat`` for *entry_path* if it is a real directory owned by *uid*, else None.

    Uses ``os.lstat`` so symlinks are never followed (`stat.S_ISDIR` is False on symlinks).
    """
    try:
        st = os.lstat(entry_path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
        return None
    return st


def newest_entry_mtime(dir_path: str, dir_st: os.stat_result) -> float:
    """Return the newest mtime of *dir_path* and its direct entries (never following symlinks)."""
    newest = dir_st.st_mtime
    with contextlib.suppress(OSError), os.scandir(dir_path) as it:
        for child in it:
            with contextlib.suppress(OSError):
                newest = max(newest, child.stat(follow_symlinks=False).st_mtime)
    return newest


def dir_regular_bytes(dir_path: str) -> int:
    """Sum ``st_size`` of non-symlink regular files directly inside *dir_path*."""
    total = 0
    with contextlib.suppress(OSError), os.scandir(dir_path) as it:
        for child in it:
            with contextlib.suppress(OSError):
                st = child.stat(follow_symlinks=False)
                if stat.S_ISREG(st.st_mode):
                    total += st.st_size
    return total


def candidate_output_dirs(root: Path) -> list[str]:
    """Return sorted full paths of ``nerve-output-*`` entries directly under *root*."""
    paths: list[str] = []
    with contextlib.suppress(OSError), os.scandir(root) as it:
        for entry in it:
            if entry.name.startswith(OUTPUT_DIR_PREFIX):
                paths.append(os.path.join(str(root), entry.name))
    paths.sort()
    return paths


def _is_own_dir(entry_path: str, own_dir: str | None) -> bool:
    return own_dir is not None and os.path.abspath(entry_path) == os.path.abspath(own_dir)


def _removable_age(entry_path: str, uid: int, now: float) -> float | None:
    """Age of an owned, non-symlink, unlocked dir; None if it must be kept regardless of age."""
    st = is_owned_output_dir(entry_path, uid)
    if st is None or dir_is_live(entry_path):
        return None
    return now - newest_entry_mtime(entry_path, st)


# ---------------------------------------------------------------------------
# Stale sweep (first output) and on-demand cleanup (`nerve cleanup`)
# ---------------------------------------------------------------------------


def sweep_stale_output_dirs(
    tmp_root: str | os.PathLike[str],
    own_dir: str | None,
    max_age_s: float,
    now: float,
) -> list[str]:
    """Remove owned, non-symlink, unlocked ``nerve-output-*`` dirs older than *max_age_s*."""
    uid = os.getuid()
    removed: list[str] = []
    errors: list[str] = []
    for entry_path in candidate_output_dirs(Path(tmp_root)):
        if _is_own_dir(entry_path, own_dir):
            continue
        try:
            age = _removable_age(entry_path, uid, now)
            if age is not None and age > max_age_s:
                shutil.rmtree(entry_path)
                removed.append(entry_path)
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        log.warning("stale_output_dir_sweep_error", error=errors[0], count=len(errors))
    return removed


def cleanup_output_dirs(
    tmp_root: str | os.PathLike[str] | None = None,
    *,
    remove_all: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Remove current-uid, non-symlink ``nerve-output-*`` dirs under *tmp_root*.

    A dir whose owner process is alive (lock held, or lock state unknown) is always skipped.
    Dirs modified within the last 60 s are also skipped unless *remove_all* is True.
    """
    root = Path(tmp_root if tmp_root is not None else tempfile.gettempdir())
    current_time = time.time() if now is None else float(now)
    window = -1.0 if remove_all else CLEANUP_ACTIVE_WINDOW_S
    uid = os.getuid()
    removed: list[str] = []
    skipped: list[str] = []
    bytes_freed = 0
    for entry_path in candidate_output_dirs(root):
        age = _removable_age(entry_path, uid, current_time)
        if age is None or age < window:
            skipped.append(entry_path)
            continue
        dir_bytes = dir_regular_bytes(entry_path)
        try:
            shutil.rmtree(entry_path)
        except OSError:
            skipped.append(entry_path)
            continue
        removed.append(entry_path)
        bytes_freed += dir_bytes
    return {"removed": removed, "skipped": skipped, "bytes_freed": bytes_freed}
