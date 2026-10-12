"""Process existence and liveness predicates."""

from __future__ import annotations

import os


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
    except OSError:
        return False
