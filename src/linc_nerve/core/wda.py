"""WebDriverAgent integration for iOS device interaction.

WDA is an XCTest runner that exposes an HTTP API for touch input and
UI hierarchy on iOS simulators and physical devices. This module manages
WDA process lifecycle and provides a thin HTTP client for tap/swipe/elements.

Requires: WDA_PROJECT_DIR env var pointing to a WebDriverAgent checkout.
Build artifacts cached in ~/.linc/wda/build/.

Physical devices require:
- WDA_TEAM_ID env var (Apple Developer Team ID for code signing)
- Device must be registered in the developer portal
"""

from __future__ import annotations

import os
import plistlib
import re
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import structlog

from .procs import pid_alive

logger = structlog.get_logger().bind(domain="device_control")

_WDA_BUILD_DIR = Path("~/.linc/wda/build").expanduser()
_WDA_HEALTH_TIMEOUT = 30.0
_WDA_REQUEST_TIMEOUT = 10.0

_SERVER_URL_RE = re.compile(r"ServerURLHere->http://([^:]+):(\d+)<-ServerURLHere")

# Matches the device identifier out of a WDA runner's `-destination` arg —
# both the simulator shape (`platform=iOS Simulator,id=<udid>`) and the
# physical-device shape (`platform=iOS,id=<udid>`), per WDASession's
# destination string (wda_session.py). `<udid>` is the same value used as
# the serial key in `_port_allocations` (#142 round 1 — orphan/live
# discrimination).
_DESTINATION_UDID_RE = re.compile(r"platform=iOS(?:\s+Simulator)?,id=([^\s,]+)")

_WDA_DEFAULT_PORT = 8100
_WDA_PORT_MIN = 8100
_WDA_PORT_MAX = 8199  # 100 ports, well beyond practical concurrent simulators

# MJPEG server port pool — distinct from the USE_PORT pool (8100-8199).
# 9100 is the JetDirect printing port; the 100-slot pool caps simultaneous
# simulators at 100 (same ceiling as USE_PORT). Fine for current fleet sizes.
_MJPEG_PORT_MIN = 9100
_MJPEG_PORT_MAX = 9199

# MJPEG runtime tuning (pushed via /appium/settings after session create).
# framerate: WDA default is 10fps (the trap); 60 is FBMjpegServer MAX_FPS and
#   keeps parity with the T13 spike's ~4ms staleness / 15.8ms p50. (CPU cost on
#   many-simulator CI is the tradeoff — acceptable; streams stop on teardown.)
# scaling=100: do NOT downscale at the WDA layer — IOSController owns payload
#   sizing via _downscale_if_needed, matching Android. Avoids an iOS-vs-Android
#   verification-fidelity regression.
# quality=80: near-visually-lossless JPEG (the only lossy step before PNG re-encode).
_MJPEG_FRAMERATE = 60
_MJPEG_QUALITY = 80
_MJPEG_SCALING = 100

# UI-source tree parsing split into wda_source.py (#53). Re-exported so
# ``from linc_nerve.core.wda import parse_wda_source`` keeps working. The port
# pools below stay in this module because the test suite reaches into and rebinds
# their module-level state (``wda._next_port``, ``wda._port_allocations`` ...).
# build_wda + _find_xctestrun split into wda_build.py (#53). Re-exported so
# ``patch("linc_nerve.core.wda.build_wda")`` / ``..._find_xctestrun`` and the
# import path keep working; the build helpers reach this module's patched
# globals (subprocess / time / Path / _find_xctestrun) via the parent module.
from .wda_build import _find_xctestrun, build_wda  # noqa: E402

# WDASession lives in wda_session.py (split in #53 to stay under the hot-file
# LOC ceiling) and is re-exported here so its public import path
# ``from linc_nerve.core.wda import WDASession`` and the warning-sweep / crap
# tests keep working. It references this module's patched globals via the
# parent module, so build_wda / _prepare_xctestrun / subprocess / time patches
# on ``linc_nerve.core.wda`` still take effect.
from .wda_session import WDASession  # noqa: E402
from .wda_source import parse_wda_source  # noqa: E402

# Public surface re-exported from the split sibling modules (#53). Declared so
# linters treat the re-imports as intentional rather than unused. ``time`` /
# ``subprocess`` / ``Path`` are listed because the split sibling modules and the
# test suite reach them through this module (``linc_nerve.core.wda.time`` etc.)
# as patch targets — keep them importable here even where this module no longer
# references them directly.
__all__ = [
    "_MJPEG_FRAMERATE",
    "_MJPEG_PORT_MAX",
    "_MJPEG_PORT_MIN",
    "_MJPEG_QUALITY",
    "_MJPEG_SCALING",
    "Path",
    "WDAManager",
    "WDASession",
    "_allocate_mjpeg_port",
    "_allocate_port",
    "_enumerate_wda_runners",
    "_extract_wda_destination_udid",
    "_find_xctestrun",
    "_parent_gone",
    "_parse_pgrep_long_output",
    "_parse_ps_pid_ppid",
    "_prepare_xctestrun",
    "_release_mjpeg_port",
    "_release_port",
    "_reset_mjpeg_port_state",
    "_reset_port_state",
    "_runner_parents",
    "_select_orphan_pids",
    "_terminate_pids",
    "_wda_process_pattern",
    "build_wda",
    "parse_wda_source",
    "subprocess",
    "time",
]


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


def _wda_process_pattern() -> str:
    """Pattern matching ONLY the REAL WDA xcodebuild RUNNER cmdline.

    The process xcodebuild actually spawns for a WDA runner looks like::

        xcodebuild test-without-building -xctestrun
            ~/.linc/wda/build/Build/Products/tmpXXXX.xctestrun
            -destination platform=iOS Simulator,id=<udid>

    There is no literal "WebDriverAgent" anywhere in that cmdline — the
    ``.xctestrun`` file is a randomly-named tmp file, so the old
    ``xcodebuild.*WebDriverAgent`` pattern never matched a real runner
    (#136).

    Round-0 fix regression (#136 repair round 0): matching on ``xcodebuild``
    plus the WDA build directory alone is NOT sufficient — the BUILD step
    that PRODUCES the xctestrun (e.g. ``xcodebuild build-for-testing
    -derivedDataPath <build dir> -scheme WebDriverAgentRunner``) also
    references the build directory, via ``-derivedDataPath``, and matched
    identically. Since ``_cleanup_orphans`` runs ``pkill -f`` on every
    ``WDAManager()`` construction, that let a live in-flight WDA *build*
    (not just an orphaned runner) get SIGTERMed, and could just as easily
    kill a live *runner* belonging to a concurrent nerve process sharing the
    same host-global ``_WDA_BUILD_DIR``.

    The fix requires BOTH the literal ``test-without-building`` subcommand
    AND the build directory to appear (in that order) — that subcommand is
    the one token a build-time invocation never carries, so it is the
    discriminator between "runner" and "build".

    ``_cleanup_orphans`` and ``_sweep_dead_ports`` MUST call this SAME
    function rather than each hard-coding their own pattern — otherwise the
    sweep's "no survivors" verdict can silently disagree with what cleanup
    actually targeted (the second half of #136: a false
    ``wda_ports_swept reclaimed=all`` while a live WDA process still held
    its ports).
    """
    return f"xcodebuild.*test-without-building.*{re.escape(str(_WDA_BUILD_DIR))}"


def _extract_wda_destination_udid(cmdline: str) -> str | None:
    """Parse the device identifier out of a WDA runner cmdline's
    ``-destination`` argument.

    Matches both the simulator shape (``platform=iOS Simulator,id=<udid>``)
    and the physical-device shape (``platform=iOS,id=<udid>``) — see
    ``WDASession`` (wda_session.py), which builds this same value from
    ``self.serial`` for both. That ``serial`` is also the exact key
    ``_port_allocations`` uses, so a match here can be checked directly
    against the currently-allocated device set (#142 round 1).

    Returns ``None`` if no destination udid is present in the cmdline.
    """
    match = _DESTINATION_UDID_RE.search(cmdline)
    return match.group(1) if match else None


def _parse_pgrep_long_output(stdout: str) -> list[tuple[int, str]]:
    """Parse ``pgrep -lf`` output into ``(pid, full argv)`` pairs.

    On macOS/BSD, ``-l`` combined with ``-f`` prints ``<pid> <full argument
    list>`` per match. Lines without a numeric pid are skipped.
    """
    candidates: list[tuple[int, str]] = []
    for line in stdout.splitlines():
        pid_str, _, cmdline = line.strip().partition(" ")
        if pid_str.isdigit():
            candidates.append((int(pid_str), cmdline))
    return candidates


def _enumerate_wda_runners() -> list[tuple[int, str]]:
    """List ``(pid, cmdline)`` for every process matching
    ``_wda_process_pattern()``, the same detector ``_sweep_dead_ports`` uses.

    Uses ``pgrep -lf``, NOT ``pgrep -af``. On macOS (the only platform a WDA
    runner exists on), ``-a`` means "include process ancestors" and prints
    bare PIDs with no cmdline, so a udid can never be parsed. Every live
    sibling then looked like an orphan and was SIGTERMed (#142 (b)). Returns
    ``[]`` when pgrep is unavailable, times out, or finds nothing.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-lf", _wda_process_pattern()],
            capture_output=True,
            timeout=5,
            text=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    return _parse_pgrep_long_output(result.stdout)


def _parse_ps_pid_ppid(stdout: str) -> dict[int, int]:
    """Parse ``ps -o pid=,ppid=`` output into a ``{pid: ppid}`` map.

    Each well-formed line is exactly two whitespace-separated integers
    (``ps`` right-pads the columns). Anything else is skipped.
    """
    parents: dict[int, int] = {}
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            parents[int(fields[0])] = int(fields[1])
    return parents


def _runner_parents(pids: list[int]) -> dict[int, int]:
    """Look up the parent pid of each WDA runner in ONE ``ps`` call.

    Returns ``{}`` (and logs ``wda_orphan_scan_skipped``) when there is
    nothing to look up, or ``ps`` fails, hangs or is missing. An empty map
    makes ``_select_orphan_pids`` select nothing: fail closed.
    """
    if not pids:
        logger.debug("wda_orphan_scan_skipped", reason="no_candidates")
        return {}
    try:
        result = subprocess.run(  # nosec B603 B607 — fixed argv, shell=False
            ["ps", "-o", "pid=,ppid=", "-p", ",".join(str(pid) for pid in pids)],
            capture_output=True,
            timeout=5,
            text=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("wda_orphan_scan_skipped", reason=type(exc).__name__)
        return {}
    if result.returncode != 0:
        logger.warning("wda_orphan_scan_skipped", reason="ps_failed", rc=result.returncode)
        return {}
    return _parse_ps_pid_ppid(result.stdout)


def _parent_gone(ppid: int) -> bool:
    """True when a runner's spawning process no longer exists.

    A runner whose nerve parent died is re-parented to launchd (pid 1), so
    ``ppid <= 1`` means gone; otherwise the shared ``pid_alive`` predicate
    decides (EPERM counts as alive).
    """
    return ppid <= 1 or not pid_alive(ppid)


def _udid_unallocated(cmdline: str, allocated_udids: set[str]) -> bool:
    """True when the runner's destination udid parses AND this process has
    not allocated it. An unparseable cmdline is never unallocated."""
    udid = _extract_wda_destination_udid(cmdline)
    return udid is not None and udid not in allocated_udids


def _select_orphan_pids(
    candidates: list[tuple[int, str]],
    allocated_udids: set[str],
    parents: dict[int, int],
) -> list[int]:
    """Return only the pids that are safe to reap.

    ``allocated_udids`` is THIS process's ``_port_allocations``, so the udid
    check only separates this process's own live runners from the rest. A
    runner with a foreign udid may still be live: it can belong to the
    daemon or a sibling bellows on the same host (audit F3). So a candidate
    is an orphan only if ALL of these hold:

    * its ``-destination`` udid parses and is not allocated here, and
    * its pid is in ``parents`` (from ``_runner_parents``), and
    * its parent is gone (``_parent_gone``: ppid 1 or a dead pid).

    Every check fails closed: an unparseable cmdline or a pid with no known
    parent is never selected.
    """
    return [
        pid
        for pid, cmdline in candidates
        if _udid_unallocated(cmdline, allocated_udids)
        and pid in parents
        and _parent_gone(parents[pid])
    ]


def _terminate_pids(pids: list[int]) -> bool:
    """SIGTERM each pid; return True if at least one signal was delivered.
    Pids that have already exited are skipped."""
    killed_any = False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed_any = True
        except ProcessLookupError:
            pass  # already gone
    return killed_any


class WDAManager:
    """Manages WDA sessions across simulators and physical devices."""

    def __init__(self) -> None:
        self._sessions: dict[str, WDASession] = {}
        self._cleanup_orphans()
        self._sweep_dead_ports()

    @staticmethod
    def _cleanup_orphans() -> None:
        """Kill orphaned WDA xcodebuild runner processes from previous
        sessions.

        Enumerates candidates (``_enumerate_wda_runners``, not a blind
        ``pkill -f``), looks up each one's parent pid in one ``ps`` call
        (``_runner_parents``), and lets ``_select_orphan_pids`` decide. The
        udid check against ``_port_allocations`` only spares THIS process's
        own runners; a fresh ``IOSController`` in one process has an empty
        allocation map and cannot see the daemon's or a sibling bellows'
        allocations. Their runners are spared because their parent is still
        alive. Only a runner whose parent is gone (re-parented to launchd, or
        a dead ppid) is reaped (audit F3). A blind ``pkill -f`` cannot tell
        any of these apart: once ``_wda_process_pattern()`` started matching
        real runners (#136), it matched LIVE runners too, and killing one let
        ``_sweep_dead_ports`` see "no survivors" and wipe that session's port
        allocations (#142).
        """
        candidates = _enumerate_wda_runners()
        parents = _runner_parents([pid for pid, _ in candidates])
        orphan_pids = _select_orphan_pids(candidates, set(_port_allocations), parents)
        if _terminate_pids(orphan_pids):
            logger.info("wda_orphans_cleaned")

    @staticmethod
    def _sweep_dead_ports() -> None:
        """Reclaim ports after orphan cleanup.

        If _cleanup_orphans() successfully killed all WDA processes,
        reset port state so ports are fully recycled. If any survive,
        leave state alone (defensive). Uses the SAME detector
        (`_wda_process_pattern`) as `_cleanup_orphans` so this verdict can
        never disagree with what cleanup actually targeted.
        """
        try:
            result = subprocess.run(
                ["pgrep", "-f", _wda_process_pattern()],
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                # No surviving WDA processes — safe to reset
                _reset_port_state()
                logger.debug("wda_ports_swept", reclaimed="all")
            else:
                logger.warning("wda_sweep_skipped", reason="surviving_processes")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    def get_or_start(self, serial: str, *, is_physical: bool = False) -> WDASession:
        """Get an existing healthy session or start a new one."""
        session = self._sessions.get(serial)
        if session and session.is_running and session._is_healthy():
            return session

        # Clean up dead session if any
        if session:
            session.stop()

        port = _allocate_port(serial)
        session = WDASession(serial, port, is_physical=is_physical)
        session.start()
        self._sessions[serial] = session
        return session

    def stop(self, serial: str) -> None:
        """Stop a WDA session for a specific simulator."""
        session = self._sessions.pop(serial, None)
        if session:
            session.stop()
            _release_port(serial)

    def stop_all(self) -> None:
        """Stop all WDA sessions."""
        for serial, session in self._sessions.items():
            session.stop()
            _release_port(serial)
        self._sessions.clear()


# ---------------------------------------------------------------------------
# Port pools (kept here: the test suite reaches into / rebinds module state)
# ---------------------------------------------------------------------------


_port_lock = threading.Lock()
_port_allocations: dict[str, int] = {}
_next_port: int = _WDA_DEFAULT_PORT
_free_ports: set[int] = set()


def _allocate_port(serial: str) -> int:
    """Allocate a unique WDA port for this device serial.

    Sequential allocation starting at 8100. First device gets 8100
    (backwards-compatible), second gets 8101, etc. Released ports are
    recycled before incrementing.
    """
    with _port_lock:
        if serial in _port_allocations:
            return _port_allocations[serial]
        if _free_ports:
            port = min(_free_ports)
            _free_ports.discard(port)
        else:
            global _next_port
            if _next_port > _WDA_PORT_MAX:
                raise RuntimeError(
                    f"WDA port pool exhausted ({_WDA_PORT_MIN}-{_WDA_PORT_MAX}). "
                    f"Release unused sessions with nerve_release."
                )
            port = _next_port
            _next_port += 1
        _port_allocations[serial] = port
        return port


def _release_port(serial: str) -> None:
    """Release a port allocation when a session stops."""
    with _port_lock:
        port = _port_allocations.pop(serial, None)
        if port is not None:
            _free_ports.add(port)


def _reset_port_state() -> None:
    """Reset all port allocation state. For tests only."""
    global _next_port
    with _port_lock:
        _port_allocations.clear()
        _free_ports.clear()
        _next_port = _WDA_DEFAULT_PORT


_mjpeg_port_lock = threading.Lock()
_mjpeg_port_allocations: dict[str, int] = {}
_next_mjpeg_port: int = _MJPEG_PORT_MIN
_free_mjpeg_ports: set[int] = set()


def _allocate_mjpeg_port(serial: str) -> int:
    """Allocate a unique WDA MJPEG server port for this device serial."""
    with _mjpeg_port_lock:
        if serial in _mjpeg_port_allocations:
            return _mjpeg_port_allocations[serial]
        if _free_mjpeg_ports:
            port = min(_free_mjpeg_ports)
            _free_mjpeg_ports.discard(port)
        else:
            global _next_mjpeg_port
            if _next_mjpeg_port > _MJPEG_PORT_MAX:
                raise RuntimeError(
                    f"MJPEG port pool exhausted ({_MJPEG_PORT_MIN}-{_MJPEG_PORT_MAX}). "
                    f"Release unused sessions with nerve_release."
                )
            port = _next_mjpeg_port
            _next_mjpeg_port += 1
        _mjpeg_port_allocations[serial] = port
        return port


def _release_mjpeg_port(serial: str) -> None:
    """Release an MJPEG port allocation when a session stops."""
    with _mjpeg_port_lock:
        port = _mjpeg_port_allocations.pop(serial, None)
        if port is not None:
            _free_mjpeg_ports.add(port)


def _reset_mjpeg_port_state() -> None:
    """Reset MJPEG port allocation state. For tests only."""
    global _next_mjpeg_port
    with _mjpeg_port_lock:
        _mjpeg_port_allocations.clear()
        _free_mjpeg_ports.clear()
        _next_mjpeg_port = _MJPEG_PORT_MIN


def _prepare_xctestrun(xctestrun: Path, port: int, mjpeg_port: int | None = None) -> Path:
    """Copy xctestrun plist with USE_PORT (and optional MJPEG_SERVER_PORT) injected.

    Returns path to the modified temp plist. Caller must delete it when done.
    """
    with open(xctestrun, "rb") as f:
        plist = plistlib.load(f)

    # Find the WDA runner target key (name varies by Xcode version)
    for key in plist:
        if "WebDriverAgentRunner" in key and isinstance(plist[key], dict):
            env = plist[key].setdefault("EnvironmentVariables", {})
            env["USE_PORT"] = str(port)
            if mjpeg_port is not None:
                env["MJPEG_SERVER_PORT"] = str(mjpeg_port)
            break

    # Write to temp file in same directory (same filesystem, easy to debug)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".xctestrun",
        dir=xctestrun.parent,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            plistlib.dump(plist, f)
    except Exception:
        os.unlink(tmp_path)
        raise

    return Path(tmp_path)
