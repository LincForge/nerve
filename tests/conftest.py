"""Shared test fixtures for linc-nerve."""

import os
import pathlib
import subprocess
import sys
import tempfile
import time
from typing import Any

import pytest

# Make repo-root-level packages (e.g. scripts/) importable from tests.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Process-wide LINC_HOME isolation, set at import time (before any fixture,
# event loop, or worker thread) and deliberately NEVER restored.
#
# Tools logged via ``log_mcp_call`` (``observability._default_log_path``)
# resolve their path from LINC_HOME. The daemon offloads tool execution to
# worker threads, and a timed-out call abandons its worker
# (``test_tool_timeout``): that orphaned thread finishes its sleep and logs
# ~2s later — potentially during interpreter shutdown, *after* every fixture
# (even session-scoped) has torn down and restored LINC_HOME. No fixture-scoped
# override can win that race, so without a never-restored process-level value
# the abandoned worker writes ``nerve_fake_block`` into the real
# ``~/.linc/mcp-calls.jsonl`` (the leak imp b643ab53 targets; crucible's #77
# fix needed only the function-scoped half because it has no off-loop workers).
# A fixed dir under the system temp avoids per-run litter — we only require it
# not be the real ~/.linc; orphaned-worker records are never read back.
os.environ["LINC_HOME"] = str(pathlib.Path(tempfile.gettempdir()) / "linc-nerve-test-home")


@pytest.fixture(autouse=True)
def _isolate_linc_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Redirect LINC_HOME to a per-test tmp dir so each test gets a clean log.

    Any tool exercised through ``@_observed_tool`` (or the daemon's
    ``log_mcp_call``) writes to ``$LINC_HOME/mcp-calls.jsonl``. Without
    isolation, in-process fixture tools (broken_dummy_tool, nerve_bad) leaked
    records into the production log — the same class of leak crucible fixed in
    #77 (imp 01606251 / b643ab53). This restores to the import-time temp
    baseline (not real ~/.linc) on teardown, so even off-loop late writers stay
    out of prod. Tests that assert specific LINC_HOME behaviour set their own
    value locally (later setenv wins).
    """
    monkeypatch.setenv("LINC_HOME", str(tmp_path / "linc_home"))


@pytest.fixture(autouse=True)
def _default_xcode_first_launch_ready():
    """Default pre-existing unit tests to a warm "Xcode first launch ready" cache
    so Linux CI and single-command subprocess fakes in test_ios / test_crap_critical_gaps
    exercise their downstream xcrun logic; gate tests call
    _reset_xcode_first_launch_state() to exercise the cold probe.
    """
    from linc_nerve.core import ios_discovery

    ios_discovery._first_launch_cache = (time.monotonic(), True)
    ios_discovery._first_launch_last_state = True
    try:
        yield
    finally:
        ios_discovery._reset_xcode_first_launch_state()


@pytest.fixture(autouse=True)
def _guard_no_real_pkill_or_adb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that spawns real ``pkill`` or ``adb`` without faking subprocess."""
    from linc_nerve.core.adb_path import ADB_PATH

    real_popen = subprocess.Popen

    def _forbid_pkill_or_adb(cmd: object) -> None:
        if isinstance(cmd, (str, os.PathLike)):
            tokens = str(cmd).split()
            prog = tokens[0] if tokens else ""
        elif isinstance(cmd, (list, tuple)) and cmd:
            prog = str(cmd[0])
        else:
            return
        name = pathlib.Path(prog).name
        if name in {"pkill", "adb"} or prog == ADB_PATH:
            raise AssertionError(
                f"Unfaked subprocess call to {name or prog!r} is forbidden in tests: {cmd!r}"
            )

    class _GuardedPopen(real_popen):  # type: ignore[misc,valid-type]
        """``Popen`` subclass (keeps ``Popen[bytes]`` + isinstance); ``run`` uses it too."""

        def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
            _forbid_pkill_or_adb(args)
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)


@pytest.fixture
def restore_signal_handlers():
    """Snapshot/restore SIGINT+SIGTERM around an in-process CLI invocation.

    linc-cli-kit's ``install_globals`` hardens streams on every group callback, which
    installs its own SIGINT/SIGTERM handlers. In a real ``nerve`` process that is the
    point; inside pytest it would clobber the handlers ``linc_nerve.mcp.server``
    registers at import (tests/test_mcp.py::TestShutdownCleanup asserts on them).
    """
    import signal

    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            if handler is not None:
                signal.signal(sig, handler)
