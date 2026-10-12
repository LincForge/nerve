"""ADB transport health — busy-spin detection, self-heal, and device-set reconciliation.

Why this module exists
----------------------
The daemon's ``/health`` used to be a pure liveness probe: it returned
``ok: true`` as long as the process could serve HTTP, touching nothing
downstream. On 2026-08-22→09-01 and again on 2026-09-03 the mini's adb server
entered an ``fdevent_context_poll::Loop()`` busy-spin at ~100% CPU. Every adb
client blocked forever, ``nerve_discover`` reported ``android: 0``, the nightly
device bench lost 6 Android slots across 5 apps — and ``/health`` stayed green
for 28 hours. **A health check that cannot fail is not a health check.**

Design
------
- **The probe never uses the wedged path.** It shells out to ``adb devices``
  with a hard ``subprocess`` timeout rather than reusing the daemon's
  persistent ``adbutils`` socket, which is exactly what hangs. A timed-out
  probe leaves a CLOSE_WAIT socket behind; that is harmless and expected.
- **Detection is a pure function.** ``adb devices`` not answering *and* the
  server process burning CPU is the busy-spin signature. Unresponsive with an
  idle (or unknown) CPU reading is a different, non-destructive failure — we
  never kill on it.
- **Recovery signals the pid, never ``adb kill-server``.** ``kill-server`` is
  itself an adb client: against a spinning server it hangs on the same socket.
  This is why the pre-existing ``_restart_adb_server`` could not repair the one
  outage it was written for.
- **Recovery resets the persistent client.** After the server is replaced, the
  daemon's cached ``adbutils`` client still points at the dead process. The
  2026-09-03 manual repair needed a daemon bounce for exactly this reason; the
  ``on_recover`` callback removes that step.
- **/health reads a cached reading, never probes inline.** ``/health`` stays a
  sub-50ms no-I/O endpoint (issue 9abb5f9d) and stays HTTP 200 in every state —
  ``_probe_existing_daemon`` reads only the status line, so a non-200 would make
  a second daemon fight for the port. The truth is in the ``ok`` field.
- **Only a process that actually watches can go red.** ``nerve bellows`` and
  library consumers run no watchdog; they report ``unmonitored`` and stay ok.
- **Drift is a failure too.** Nerve missing a device that ``adb devices`` shows
  is the same stale-state family as the wedge, so the watchdog compares the two
  serial sets and resets the client when they disagree for more than a grace
  window. The health signal is "nerve's device set matches adb's", not merely
  "adb answered".

See ``LINC.md § Operational Notes`` and ``LincForge/linc-nerve#131``.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess  # nosec B404 — fixed argv, no shell, hard timeouts
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

import structlog

from linc_nerve.core.adb_path import ADB_PATH
from linc_nerve.core.adb_server import adb_server_address
from linc_nerve.core.procs import pid_alive

log = structlog.get_logger().bind(domain="adb_health")

#: %CPU at or above which an *unresponsive* adb server counts as busy-spinning.
#: The observed spin sits at 99.7%; a healthy idle server sits at ~0%.
BUSY_SPIN_CPU_PERCENT = 80.0

#: Hard ceiling on the ``adb devices`` probe. The wedge makes it hang forever,
#: so this is the whole detection latency.
PROBE_TIMEOUT_SECONDS = 8.0

#: Watchdog cadence.
WATCHDOG_INTERVAL_SECONDS = 30.0

#: How long a monitored process may report nothing before /health goes red. A
#: watchdog blocked in the same hung adb call every other client is blocked in
#: stops reporting — silence is itself the signal. Must comfortably exceed
#: WATCHDOG_INTERVAL_SECONDS + PROBE_TIMEOUT_SECONDS.
STALE_AFTER_SECONDS = 150.0

#: Grace after the watchdog starts, before "no reading yet" counts as stale.
STARTUP_GRACE_SECONDS = 90.0

#: Minimum gap between two adb-server kills. Prevents a kill/start loop against
#: a server that re-wedges immediately.
RECOVERY_COOLDOWN_SECONDS = 300.0

#: How long nerve's device set may disagree with adb's before it counts as
#: drift. Probe and poll sample at different instants, so brief disagreement
#: around a connect/disconnect is normal.
DRIFT_GRACE_SECONDS = 90.0

#: Minimum gap between two client resets triggered by drift.
DRIFT_COOLDOWN_SECONDS = 120.0

#: A nerve device-set reading older than this is not evidence of drift.
NERVE_SERIALS_STALE_AFTER_SECONDS = 180.0


@dataclass(frozen=True)
class AdbTransportStatus:
    """One reading of the adb transport.

    ``device_count`` is ``None`` when the probe never got an answer — a caller
    must be able to tell "no devices" from "could not ask", the distinction
    that made a 28-hour outage look like an empty bench.
    """

    responsive: bool
    wedged: bool
    device_count: int | None
    probe_seconds: float
    server_pid: int | None
    server_cpu_percent: float | None
    detail: str
    checked_at: float
    recovered: bool = False
    adb_serials: tuple[str, ...] = ()
    drift: tuple[str, ...] = ()
    drift_seconds: float = 0.0
    nerve_stale: bool = False
    nerve_stale_detail: str | None = None
    nerve_serials_age: float | None = None
    lost_after_recovery: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Process-local registry — written by the watchdog, read by /health
# ---------------------------------------------------------------------------

_LAST_STATUS: AdbTransportStatus | None = None
_MONITOR_STARTED_AT: float | None = None
_LAST_RECOVERY_AT: float | None = None
_NERVE_SERIALS: tuple[frozenset[str], float] | None = None
_DRIFT_SINCE: float | None = None
_LAST_DRIFT_RESET_AT: float | None = None
#: Serials from the last *responsive* probe — what a wedge recovery tries to
#: reconnect, and what a post-recovery reprobe is diffed against to catch a
#: device-set shrink (killing the server drops every wifi-adb transport).
_LAST_GOOD_SERIALS: tuple[str, ...] = ()


def record_transport_status(status: AdbTransportStatus) -> None:
    """Publish the latest reading for ``/health`` to render.

    Also refreshes ``_LAST_GOOD_SERIALS`` whenever the transport answered, so
    a later wedge recovery knows what to reconnect and what "shrank" means.
    """
    global _LAST_STATUS, _LAST_GOOD_SERIALS
    _LAST_STATUS = status
    if status.responsive:
        _LAST_GOOD_SERIALS = status.adb_serials


def get_transport_status() -> AdbTransportStatus | None:
    """The latest reading, or ``None`` if none has landed in this process."""
    return _LAST_STATUS


def mark_watchdog_started(now: float | None = None) -> None:
    """Declare that this process runs a transport watchdog.

    Only a process that declares this can fail ``/health`` on transport state —
    ``nerve bellows`` and library consumers must not go red on a transport
    nobody is sampling.
    """
    global _MONITOR_STARTED_AT
    _MONITOR_STARTED_AT = time.time() if now is None else now


def watchdog_uptime_seconds(now: float | None = None) -> float | None:
    """Seconds since the watchdog registered, or ``None`` if unmonitored."""
    if _MONITOR_STARTED_AT is None:
        return None
    return (time.time() if now is None else now) - _MONITOR_STARTED_AT


def record_nerve_android_serials(serials: Any, now: float | None = None) -> None:
    """Publish the Android serials nerve's own discovery last returned.

    The watchdog compares this against what ``adb devices`` reports; a device
    adb can see but nerve cannot is stale-client drift.
    """
    global _NERVE_SERIALS
    _NERVE_SERIALS = (frozenset(serials), time.time() if now is None else now)


def get_nerve_android_serials(now: float | None = None) -> frozenset[str] | None:
    """The last nerve device set, or ``None`` if absent or too old to trust."""
    if _NERVE_SERIALS is None:
        return None
    serials, at = _NERVE_SERIALS
    stamp = time.time() if now is None else now
    if stamp - at > NERVE_SERIALS_STALE_AFTER_SECONDS:
        return None
    return serials


def get_nerve_serials_age(now: float | None = None) -> float | None:
    """Seconds since nerve serials were published, or None if never published."""
    if _NERVE_SERIALS is None:
        return None
    stamp = time.time() if now is None else now
    return stamp - _NERVE_SERIALS[1]


def reset_transport_status() -> None:
    """Clear all registry state (tests, and daemon restart)."""
    global _LAST_STATUS, _MONITOR_STARTED_AT, _LAST_RECOVERY_AT
    global _NERVE_SERIALS, _DRIFT_SINCE, _LAST_DRIFT_RESET_AT, _LAST_GOOD_SERIALS
    _LAST_STATUS = None
    _MONITOR_STARTED_AT = None
    _LAST_RECOVERY_AT = None
    _NERVE_SERIALS = None
    _DRIFT_SINCE = None
    _LAST_DRIFT_RESET_AT = None
    _LAST_GOOD_SERIALS = ()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def split_adb_device_line(line: str) -> tuple[str, str] | None:
    """Split one non-header ``adb devices`` line into ``(serial, state)``.

    adb separates the two with a TAB, and an mDNS wifi-adb serial can itself
    contain a space (``adb-RFAT629KQ9A-x (2)._adb-tls-connect._tcp``), so a
    plain whitespace split mangles it. Splitting on the LAST tab keeps the
    whole serial intact regardless of internal spaces. Hand-written fixtures
    that use plain spaces (no tab) fall back to splitting on the last run of
    whitespace instead, so those keep parsing unchanged. Returns ``None`` for
    a line with no separator to split on (e.g. a bare serial).
    """
    if "\t" in line:
        serial, _sep, state = line.rpartition("\t")
        return serial.strip(), state.strip()
    parts = line.rsplit(None, 1)
    if len(parts) != 2:
        return None
    return parts[0].strip(), parts[1].strip()


def parse_adb_devices(output: str) -> tuple[str, ...]:
    """Serials in the ``device`` state from ``adb devices`` output.

    Everything else — the header, ``* daemon started`` chatter, and devices in
    ``offline``/``unauthorized`` — is excluded: those are attached but unusable,
    and counting them would let a dead fleet read as healthy.
    """
    serials: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith(("*", "List of devices")):
            continue
        split = split_adb_device_line(line)
        if split is not None and split[1] == "device":
            serials.append(split[0])
    return tuple(serials)


def count_ready_devices(output: str) -> int:
    """Number of usable devices in ``adb devices`` output."""
    return len(parse_adb_devices(output))


def is_busy_spin(
    *,
    responsive: bool,
    server_cpu_percent: float | None,
    threshold: float = BUSY_SPIN_CPU_PERCENT,
) -> bool:
    """True for the busy-spin signature: hung transport + pegged server CPU.

    Deliberately conservative — every ``False`` here means "do not kill the adb
    server". A responsive server is doing work, not spinning. An unknown CPU
    reading is absence of evidence, not evidence.
    """
    if responsive or server_cpu_percent is None:
        return False
    return server_cpu_percent >= threshold


def serial_drift(adb_serials: Any, nerve_serials: Any) -> tuple[str, ...]:
    """Serials adb can see that nerve cannot — the stale-client signature."""
    return tuple(sorted(frozenset(adb_serials) - frozenset(nerve_serials)))


#: Matches a wifi-adb ``ip:port`` serial, e.g. ``192.0.2.10:35593``.
_NETWORK_SERIAL_RE = re.compile(r"\A(?:\d{1,3}\.){3}\d{1,3}:\d+\Z")


def is_network_serial(serial: str) -> bool:
    """True for a wifi-adb ``<ipv4>:<port>`` serial.

    A USB serial (``R3CFAKE00001``) is False. An mDNS ``_adb-tls-connect``
    service name (``adb-RFAT629KQ9A-abc._adb-tls-connect._tcp``) is also
    False — ``adb connect`` takes an explicit host:port, not a service name,
    so that lane is not reconnectable this way and must be skipped.
    """
    return bool(_NETWORK_SERIAL_RE.fullmatch(serial))  # H1: no trailing-\n bypass


def transport_health(
    status: AdbTransportStatus | None,
    *,
    now: float,
    monitor_uptime_seconds: float | None,
    grace_seconds: float = STARTUP_GRACE_SECONDS,
    stale_after: float = STALE_AFTER_SECONDS,
    drift_grace_seconds: float = DRIFT_GRACE_SECONDS,
) -> tuple[bool, dict[str, Any]]:
    """Render a reading as ``(ok, adb_block)`` for ``/health``.

    ``monitor_uptime_seconds`` is ``None`` when no watchdog runs in this
    process — the caller passes ``watchdog_uptime_seconds()`` straight through,
    so "is anyone watching" and "for how long" stay one value, not two.

    States: ``unmonitored`` (nobody is watching here) · ``pending`` (watchdog
    just started) · ``stale`` (no fresh reading — the watchdog is itself stuck)
    · ``wedged`` (busy-spin) · ``unresponsive`` · ``recovery-shrank`` (a wedge
    recovery came back with fewer devices than before) · ``drift`` (nerve is
    missing a device adb can see) · ``ok``. Only ``unmonitored``, ``pending``
    and ``ok`` are healthy. ``lost_after_recovery`` is always present in the
    rendered block (empty list when nothing was lost) so a caller can assert
    on the value without first checking the state.
    """
    if monitor_uptime_seconds is None:
        return True, {
            "state": "unmonitored",
            "detail": "no transport watchdog in this process",
            "lost_after_recovery": [],
        }

    if status is None:
        if monitor_uptime_seconds < grace_seconds:
            return True, {
                "state": "pending",
                "detail": "awaiting first transport probe",
                "lost_after_recovery": [],
            }
        return False, {
            "state": "stale",
            "detail": (
                f"no transport probe in {monitor_uptime_seconds:.0f}s of watchdog uptime — "
                "the watchdog is blocked in the same adb call every client is blocked in"
            ),
            "lost_after_recovery": [],
        }

    age = now - status.checked_at
    block: dict[str, Any] = {
        "state": "ok",
        "detail": status.detail,
        "age_seconds": round(age, 1),
        "device_count": status.device_count,
        "responsive": status.responsive,
        "server_pid": status.server_pid,
        "server_cpu_percent": status.server_cpu_percent,
        "probe_seconds": round(status.probe_seconds, 2),
        "recovered": status.recovered,
        "lost_after_recovery": list(status.lost_after_recovery),
    }
    if status.drift:
        block["missing_from_nerve"] = list(status.drift)

    if age > stale_after:
        block["state"] = "stale"
        block["detail"] = f"last transport probe was {age:.0f}s ago"
        return False, block
    if status.wedged:
        block["state"] = "wedged"
        return False, block
    if not status.responsive:
        block["state"] = "unresponsive"
        return False, block
    if status.lost_after_recovery:
        block["state"] = "recovery-shrank"
        block["detail"] = (
            f"adb recovery lost {len(status.lost_after_recovery)} device(s): "
            f"{', '.join(status.lost_after_recovery)}"
        )
        return False, block
    if status.nerve_stale:
        if (
            status.nerve_serials_age is None
            and monitor_uptime_seconds is not None
            and monitor_uptime_seconds < grace_seconds
        ):
            pass
        else:
            block["state"] = "nerve-stale"
            block["detail"] = status.nerve_stale_detail or "nerve serials are stale"
            return False, block
    if status.drift and status.drift_seconds >= drift_grace_seconds:
        block["state"] = "drift"
        block["detail"] = (
            f"adb sees {len(status.drift)} device(s) nerve does not: {', '.join(status.drift)}"
        )
        return False, block
    return True, block


# ---------------------------------------------------------------------------
# Impure probes — every one is argv-only, hard-timed-out, and never raises
# ---------------------------------------------------------------------------


def _run(argv: list[str], timeout: float) -> Any:
    """One guarded subprocess call. Patched wholesale in tests."""
    return subprocess.run(  # nosec B603 — fixed argv from constants, shell=False
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _pid_alive(pid: int) -> bool:
    return pid_alive(pid)


def find_adb_server_pid() -> int | None:
    """Pid of the adb *server* (not a client), or ``None``.

    Only the server nerve is configured for (``adb_server_address()``) — never
    the default one when ANDROID_ADB_SERVER_PORT / ADB_SERVER_SOCKET point
    elsewhere. A malformed variable reads as "no server" here (probes never
    raise); the client path reports it loudly.
    """
    try:
        proc = _run(["pgrep", "-f", adb_server_address().pgrep_pattern], timeout=5.0)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.split():
        try:
            return int(line)
        except ValueError:
            continue
    return None


def process_cpu_percent(pid: int) -> float | None:
    """``ps`` %CPU for *pid*, or ``None`` if unreadable."""
    try:
        proc = _run(["ps", "-o", "%cpu=", "-p", str(pid)], timeout=5.0)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def adb_server_config_error() -> str | None:
    """Why the configured adb server must not be used (a refused / malformed
    ADB_SERVER_SOCKET / ANDROID_ADB_SERVER_PORT), or ``None`` when it is fine.

    ``adb`` subprocesses honour those variables natively, so a value the
    client path refuses (a non-loopback host) must also stop every ``adb``
    subprocess here — or the watchdog would contact the refused server.
    """
    try:
        adb_server_address()
    except ValueError as exc:
        return str(exc)
    return None


def _adb_devices_reading(adb_path: str, timeout: float) -> tuple[tuple[str, ...] | None, str]:
    """(serials, "") from one bounded ``adb devices``, or (None, why it failed)."""
    refused = adb_server_config_error()
    if refused is not None:
        return None, refused
    try:
        proc = _run([adb_path, "devices"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"adb devices timed out after {timeout:.1f}s"
    except FileNotFoundError:
        return None, f"adb binary not found at {adb_path}"
    except Exception as exc:  # pragma: no cover — defensive
        return None, f"adb devices failed: {exc}"
    if proc.returncode != 0:
        return None, f"adb devices exited {proc.returncode}"
    return parse_adb_devices(proc.stdout), ""


def probe_transport(
    *, adb_path: str = ADB_PATH, timeout: float = PROBE_TIMEOUT_SECONDS
) -> AdbTransportStatus:
    """One bounded ``adb devices`` reading. Never raises.

    Runs adb as a subprocess rather than through the daemon's persistent
    ``adbutils`` socket, so a hung transport costs *timeout* seconds instead of
    forever — and the probe is not itself a victim of the wedge.
    """
    t0 = time.monotonic()
    serials, failure = _adb_devices_reading(adb_path, timeout)
    responsive = serials is not None
    device_count = None if serials is None else len(serials)
    serials = serials or ()

    elapsed = time.monotonic() - t0
    pid = find_adb_server_pid()
    cpu = process_cpu_percent(pid) if pid is not None else None
    wedged = is_busy_spin(responsive=responsive, server_cpu_percent=cpu)

    if responsive:
        detail = f"{device_count} device(s)"
    else:
        detail = failure or "adb devices did not answer"
        if pid is not None and cpu is not None:
            detail += f"; server pid {pid} at {cpu:.1f}% CPU"

    return AdbTransportStatus(
        responsive=responsive,
        wedged=wedged,
        device_count=device_count,
        probe_seconds=elapsed,
        server_pid=pid,
        server_cpu_percent=cpu,
        detail=detail,
        checked_at=time.time(),
        adb_serials=serials,
    )


def recover_adb_server(pid: int | None, *, reconnect: Iterable[str] = ()) -> bool:
    """Replace a wedged adb server. Returns True if a server is up afterwards.

    Signals *pid* directly and escalates to SIGKILL — a server spinning in its
    poll loop ignores SIGTERM. **Never** runs ``adb kill-server``: that is an
    adb client and blocks on the very socket that is wedged.

    Killing the server drops every wifi-adb transport, so once the server is
    back up this re-issues ``adb connect`` for each *reconnect* serial that is
    an ip:port wifi-adb serial (``is_network_serial``) — a USB serial or an
    mDNS ``_adb-tls-connect`` name is skipped, neither is reconnectable this
    way. A reconnect failure is logged and never raised: the caller's
    post-recovery reprobe, not this call, is the source of truth for what
    actually came back.

    A refused ADB_SERVER_SOCKET / ANDROID_ADB_SERVER_PORT (a non-loopback
    host, a bad port) returns False before any signal or ``adb`` subprocess.
    """
    refused = adb_server_config_error()
    if refused is not None:
        log.error("adb_recovery_refused", error=refused)
        return False
    if pid is not None:
        _signal_server(pid)
    if not _start_server():
        return False
    log.warning("adb_server_recovered", pid=pid)
    for serial in reconnect:
        _reconnect(serial)
    return True


def _signal_server(pid: int) -> None:
    """SIGTERM, then SIGKILL, the adb server *pid* until it is gone."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            log.info("adb_server_signal_skipped", pid=pid, signal=int(sig), error=str(exc))
            return
        _sleep(1.0)
        if not _pid_alive(pid):
            return
    log.error("adb_server_kill_failed", pid=pid)  # pragma: no cover — SIGKILL lands or raises


def _start_server() -> bool:
    """``adb start-server``; True when it exited 0."""
    try:
        proc = _run([ADB_PATH, "start-server"], timeout=20.0)
    except Exception as exc:
        log.error("adb_start_server_failed", error=str(exc))
        return False
    if proc.returncode != 0:
        log.error("adb_start_server_nonzero", returncode=proc.returncode)
        return False
    return True


def _reconnect(serial: str) -> None:
    """``adb connect`` one wifi-adb *serial*; failures are logged, never raised."""
    if not is_network_serial(serial):
        return
    try:
        conn = _run([ADB_PATH, "connect", serial], timeout=10.0)
    except Exception as exc:
        log.warning("adb_reconnect_failed", serial=serial, error=str(exc))
        return
    if conn.returncode == 0:
        log.info("adb_reconnect_ok", serial=serial)
    else:
        log.warning("adb_reconnect_failed", serial=serial, returncode=conn.returncode)


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


def watchdog_tick(
    *,
    probe: Callable[[], AdbTransportStatus] = probe_transport,
    recover: Callable[[int | None], bool] = recover_adb_server,
    on_recover: Callable[[], None] | None = None,
    on_drift: Callable[[], None] | None = None,
    now: Callable[[], float] = time.time,
) -> AdbTransportStatus:
    """Probe, publish, and self-heal. Never raises.

    A watchdog that can die is a watchdog that stops watching, so every failure
    here — including a failure of the repair itself — becomes a recorded
    reading rather than an exception.
    """
    global _LAST_RECOVERY_AT, _DRIFT_SINCE, _LAST_DRIFT_RESET_AT

    try:
        status = probe()
    except Exception as exc:
        status = AdbTransportStatus(
            responsive=False,
            wedged=False,
            device_count=None,
            probe_seconds=0.0,
            server_pid=None,
            server_cpu_percent=None,
            detail=f"transport probe raised: {exc}",
            checked_at=now(),
        )
        record_transport_status(status)
        return status

    if status.wedged:
        status = _handle_wedge(
            status, probe=probe, recover=recover, on_recover=on_recover, now=now
        )
    elif status.responsive:
        status = _handle_drift(status, on_drift=on_drift, now=now)

    record_transport_status(status)
    return status


def _handle_wedge(
    status: AdbTransportStatus,
    *,
    probe: Callable[[], AdbTransportStatus],
    recover: Callable[[int | None], bool],
    on_recover: Callable[[], None] | None,
    now: Callable[[], float],
) -> AdbTransportStatus:
    global _LAST_RECOVERY_AT

    stamp = now()
    if _LAST_RECOVERY_AT is not None and stamp - _LAST_RECOVERY_AT < RECOVERY_COOLDOWN_SECONDS:
        log.error(
            "adb_transport_wedged_in_cooldown",
            detail=status.detail,
            since_last_recovery_s=round(stamp - _LAST_RECOVERY_AT, 1),
        )
        return status

    log.error("adb_transport_wedged", detail=status.detail, pid=status.server_pid)
    _LAST_RECOVERY_AT = stamp
    reconnect = tuple(serial for serial in _LAST_GOOD_SERIALS if is_network_serial(serial))
    try:
        healed = recover(status.server_pid, reconnect=reconnect)
    except Exception as exc:
        log.error("adb_recovery_failed", error=str(exc))
        return status
    if not healed:
        return status

    _run_callback(on_recover, "adb_client_reset_failed")

    try:
        after = probe()
    except Exception as exc:
        log.error("adb_reprobe_failed", error=str(exc))
        return replace(status, recovered=True)

    lost = tuple(sorted(set(_LAST_GOOD_SERIALS) - set(after.adb_serials)))
    if lost:
        log.error("adb_recovery_shrank_device_set", lost=list(lost))
    return replace(after, recovered=True, lost_after_recovery=lost)


def _handle_drift(
    status: AdbTransportStatus,
    *,
    on_drift: Callable[[], None] | None,
    now: Callable[[], float],
) -> AdbTransportStatus:
    """Compare adb's device set to nerve's and reset the client on persistent drift."""
    global _DRIFT_SINCE, _LAST_DRIFT_RESET_AT

    stamp = now()
    nerve_serials = get_nerve_android_serials(now=stamp)
    age = get_nerve_serials_age(now=stamp)
    uptime = watchdog_uptime_seconds(now=stamp)

    if nerve_serials is None:
        _DRIFT_SINCE = None
        if _NERVE_SERIALS is None:
            if uptime is not None and uptime >= STARTUP_GRACE_SECONDS:
                return replace(
                    status,
                    nerve_stale=True,
                    nerve_stale_detail="nerve serials were never published",
                    nerve_serials_age=None,
                )
            return status
        return replace(
            status,
            nerve_stale=True,
            nerve_stale_detail=f"last nerve serials reading was {age:.0f}s ago",
            nerve_serials_age=age,
        )

    drift = serial_drift(status.adb_serials, nerve_serials)
    if not drift:
        _DRIFT_SINCE = None
        return replace(
            status,
            nerve_stale=False,
            nerve_stale_detail=None,
            nerve_serials_age=age,
            drift=(),
            drift_seconds=0.0,
        )

    if _DRIFT_SINCE is None:
        _DRIFT_SINCE = stamp
    drift_seconds = stamp - _DRIFT_SINCE
    status = replace(
        status,
        nerve_stale=False,
        nerve_stale_detail=None,
        nerve_serials_age=age,
        drift=drift,
        drift_seconds=drift_seconds,
    )

    if drift_seconds < DRIFT_GRACE_SECONDS:
        return status
    if _LAST_DRIFT_RESET_AT is not None and stamp - _LAST_DRIFT_RESET_AT < DRIFT_COOLDOWN_SECONDS:
        return status

    log.error("adb_device_set_drift", missing_from_nerve=list(drift))
    _LAST_DRIFT_RESET_AT = stamp
    _run_callback(on_drift, "adb_client_reset_failed")
    return status


def _run_callback(cb: Callable[[], None] | None, error_event: str) -> None:
    if cb is None:
        return
    try:
        cb()
    except Exception as exc:
        log.error(error_event, error=str(exc))
