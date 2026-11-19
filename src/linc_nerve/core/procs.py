"""Process existence and liveness predicates."""

from __future__ import annotations

import os
import subprocess


def pid_alive(pid: int) -> bool:
    """Check whether a process exists and is alive.

    A pid <= 0 returns False without signalling (never signal process groups).
    Sending signal 0 tests for process existence.
    ProcessLookupError (ESRCH) means the process does not exist -> False.
    PermissionError (EPERM) means the process exists under another UID -> True.
    Any other OSError -> False.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):  # OverflowError: pid beyond the platform's pid_t
        return False


def _process_uid(pid: int) -> int | None:
    """Return the UID owning *pid* when readable via /proc or ps, else None."""
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        pass
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "uid="],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        return int(out) if out else None
    except Exception:
        return None


def pid_alive_same_uid(pid: int) -> bool:
    """Check whether *pid* is alive and owned by the caller's UID.

    For non-root callers, ``os.kill(pid, 0)`` rejects foreign-UID PIDs with
    ``PermissionError``. When running as root, verify process UID matches
    ``os.getuid()`` when readable.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):  # OverflowError: pid beyond the platform's pid_t
        return False
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        uid = _process_uid(pid)
        if uid is not None and uid != os.getuid():
            return False
    return True
