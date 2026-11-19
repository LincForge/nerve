"""Engagement mode and NERVE_ARTIFACTS_ROOT confinement helpers.

Single source of truth for resolving host-side artifact paths and scratch
temp locations. When ``NERVE_ARTIFACTS_ROOT`` or ``NERVE_ENGAGEMENT`` is set,
every customer-engagement artifact stays under one owner-only (0700 dirs,
0600 files) root, and misconfiguration fails closed rather than falling back
to ``~/.linc`` or the system temp directory.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Literal

import structlog

from linc_nerve.core.android_identity import (
    is_network_transport,
    mdns_hardware_serial,
    normalize_hardware_serial,
)
from linc_nerve.core.fleet_entries import FleetEntry
from linc_nerve.core.fleet_parse import parse_fleet_text

log = structlog.get_logger().bind(domain="device_control")

logger = structlog.get_logger().bind(domain="device_control")

ENGAGEMENT_ENV = "NERVE_ENGAGEMENT"
ARTIFACTS_ROOT_ENV = "NERVE_ARTIFACTS_ROOT"
ALLOW_PERSONAL_ENV = "NERVE_ALLOW_PERSONAL_SERIALS"
FLEET_FILENAME = "device-fleet.yaml"
_MAX_FLEET_BYTES = 1024 * 1024  # 1 MiB
_FLEET_READ_LIMIT = _MAX_FLEET_BYTES + 1  # one byte past the cap detects a file that grew
_FLEET_ENCODING = "utf-8"
_UTF8_BOM = "\ufeff"
ERROR_CATEGORY = "engagement_misconfigured"
PERSONAL_DEVICE_ERROR_CATEGORY = "personal_device"

ENGAGEMENT_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

_PRIVATE_FILE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
)


class EngagementMisconfiguredError(RuntimeError):
    """Raised when NERVE_ENGAGEMENT or NERVE_ARTIFACTS_ROOT is invalid."""

    error_category = ERROR_CATEGORY


class PersonalDeviceError(RuntimeError):
    """Raised when device-fleet policy refuses a personal device."""

    error_category = PERSONAL_DEVICE_ERROR_CATEGORY


def refusal_payload(exc: BaseException) -> dict[str, str]:
    """Build the standard MCP/tool refusal dict preserving ``exc.error_category``."""
    category = getattr(exc, "error_category", ERROR_CATEGORY)
    return {"error": str(exc), "error_category": str(category)}


def default_linc_home() -> Path:
    """Resolve the default LINC home directory (``$LINC_HOME`` or ``~/.linc``)."""
    env = os.getenv("LINC_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".linc"


def default_temp_dir() -> Path:
    """Return the unconfined system temp directory when no artifacts root is active."""
    return Path(tempfile.gettempdir())


def output_sweep_root(parent: str | None) -> str:
    """Directory swept for stale ``nerve-output-*`` dirs: *parent* if rooted, else system temp."""
    return parent if parent is not None else str(default_temp_dir())


def safe_serial(serial: str) -> str:
    """Sanitize a device serial for safe embedding in a single host filename."""
    cleaned = serial.replace("/", "_").replace("\\", "_").replace(":", "_").replace("\x00", "_")
    return "_" if cleaned in ("", ".", "..") else cleaned


def _default_linc_home() -> Path:
    return default_linc_home()


def _parse_engagement(env: Mapping[str, str]) -> str | None:
    """Return validated engagement id, None when off, or raise on invalid id."""
    raw = env.get(ENGAGEMENT_ENV)
    if raw is None or raw == "":
        return None
    if not ENGAGEMENT_ID_RE.fullmatch(raw):
        raise EngagementMisconfiguredError(
            f"{ENGAGEMENT_ENV} is invalid ({raw!r}); "
            "expected 1-64 characters matching [A-Za-z0-9][A-Za-z0-9._-]*"
        )
    return raw


def _check_root_syntax(raw_root: str) -> None:
    """Refuse control characters, relative paths and '..' segments."""
    if "\x00" in raw_root or "\n" in raw_root or "\r" in raw_root or raw_root != raw_root.strip():
        raise EngagementMisconfiguredError(
            f"{ARTIFACTS_ROOT_ENV} contains control characters or surrounding whitespace"
        )
    if not os.path.isabs(raw_root):
        raise EngagementMisconfiguredError(
            f"{ARTIFACTS_ROOT_ENV} must be an absolute path, got {raw_root!r}"
        )
    if ".." in PurePath(raw_root).parts:
        raise EngagementMisconfiguredError(
            f"{ARTIFACTS_ROOT_ENV} must not contain '..' segments: {raw_root!r}"
        )


def _check_root_canonical(raw_root: str, normalized: str) -> None:
    """Refuse a root with a symlink in ANY component (leaf or ancestor).

    ``lstat`` alone is not enough: a trailing ``/``, ``//`` or ``/.`` makes it
    follow a symlinked leaf, and it never sees a symlinked ancestor such as
    ``/Volumes/vault-link/acme``. Comparing the resolved path with the purely
    lexical normal form catches every spelling. This fails closed on OS-level
    ancestor links too (macOS ``/tmp`` -> ``/private/tmp``, ``/var`` ->
    ``/private/var``): spell the root by its resolved path.
    """
    resolved = os.path.realpath(normalized)
    if resolved != normalized:
        raise EngagementMisconfiguredError(
            f"{ARTIFACTS_ROOT_ENV} must be a canonical path with no symlink in any "
            f"component: {raw_root!r} resolves to {resolved!r}. Only if that resolved "
            "directory is the intended vault, set the variable to it directly"
        )


def _validate_root(raw_root: str) -> Path:
    """Validate NERVE_ARTIFACTS_ROOT strictly; return its normalized path.

    The root must exist as a writable directory whose path contains no
    symlink anywhere (see ``_check_root_canonical``).
    """
    _check_root_syntax(raw_root)
    normalized = os.path.normpath(raw_root)
    try:
        st = os.lstat(normalized)
    except OSError as exc:
        raise EngagementMisconfiguredError(
            f"{ARTIFACTS_ROOT_ENV} is missing or inaccessible ({raw_root!r}): {exc}"
        ) from exc
    if stat.S_ISLNK(st.st_mode):
        raise EngagementMisconfiguredError(
            f"{ARTIFACTS_ROOT_ENV} must not be a symlink: {raw_root!r} "
            f"(points to {os.path.realpath(normalized)!r})"
        )
    _check_root_canonical(raw_root, normalized)
    if not stat.S_ISDIR(st.st_mode):
        raise EngagementMisconfiguredError(
            f"{ARTIFACTS_ROOT_ENV} is not a directory: {raw_root!r}"
        )
    if not os.access(normalized, os.W_OK | os.X_OK) or (st.st_mode & 0o222) == 0:
        raise EngagementMisconfiguredError(f"{ARTIFACTS_ROOT_ENV} is not writable: {raw_root!r}")
    return Path(normalized)


def engagement_id(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the active, validated engagement id or None; raises if invalid."""
    env = os.environ if environ is None else environ
    return _parse_engagement(env)


def is_engagement_on(environ: Mapping[str, str] | None = None) -> bool:
    """Return True if NERVE_ENGAGEMENT is set (non-empty); raises if invalid."""
    return engagement_id(environ) is not None


def artifacts_root(environ: Mapping[str, str] | None = None) -> Path | None:
    """Validate and return NERVE_ARTIFACTS_ROOT, or None when inactive.

    Raises EngagementMisconfiguredError if NERVE_ENGAGEMENT is active (or
    invalid) with an unset or invalid root, or if NERVE_ARTIFACTS_ROOT itself
    is configured to a hostile/non-writable/symlink/non-directory path.
    """
    env = os.environ if environ is None else environ
    eng_id = _parse_engagement(env)
    raw_root = env.get(ARTIFACTS_ROOT_ENV)
    if raw_root is None or raw_root == "":
        if eng_id is not None:
            raise EngagementMisconfiguredError(
                f"{ENGAGEMENT_ENV}={eng_id!r} requires {ARTIFACTS_ROOT_ENV} to be set"
            )
        return None
    return _validate_root(raw_root)


def is_within_root(candidate: Path, root: Path) -> bool:
    """Return True iff *candidate* resolves (following symlinks and '..') under *root*."""
    root_real = Path(os.path.realpath(root))
    cand_real = Path(os.path.realpath(candidate))
    return cand_real == root_real or cand_real.is_relative_to(root_real)


def _check_subdir_safe(subdir: Path, root: Path) -> None:
    if subdir.is_symlink() or not is_within_root(subdir, root):
        raise EngagementMisconfiguredError(
            f"Artifacts subdirectory resolves outside root: {subdir}"
        )
    if subdir.exists() and not subdir.is_dir():
        raise EngagementMisconfiguredError(f"Artifacts path is not a directory: {subdir}")


def ensure_private_subdir(root: Path, name: str) -> Path:
    """Create ``root / name`` with mode 0700 after verifying no symlink escape."""
    subdir = root / name
    _check_subdir_safe(subdir, root)
    subdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_subdir_safe(subdir, root)
    subdir.chmod(0o700)
    return subdir


def ensure_private_dir(directory: Path, mode: int = 0o700) -> Path:
    """Create *directory* with mode *mode* (when newly created or under root)."""
    directory.mkdir(mode=mode, parents=True, exist_ok=True)
    if artifacts_root() is not None:
        directory.chmod(mode)
    return directory


def write_private_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """Write *data* to *path* with mode 0600, refusing symlinks and FIFOs."""
    fd = os.open(path, _PRIVATE_FILE_FLAGS, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"not a regular file: {path}")
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def write_private_text(path: str | os.PathLike[str], text: str) -> None:
    """Write UTF-8 *text* to *path* with mode 0600, refusing symlinks and FIFOs."""
    write_private_bytes(path, text.encode("utf-8"))


def recordings_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the recordings directory (and ensure 0700 under root)."""
    root = artifacts_root(environ)
    if root is not None:
        return ensure_private_subdir(root, "recordings")
    return _default_linc_home() / "nerve" / "recordings"


def output_parent(environ: Mapping[str, str] | None = None) -> str | None:
    """Return parent directory for ``nerve-output-*`` dirs, or None for system tmp."""
    root = artifacts_root(environ)
    if root is not None:
        return str(ensure_private_subdir(root, "output"))
    return None


def cleanup_output_root(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the output sweep/cleanup directory when root is set, else None."""
    root = artifacts_root(environ)
    if root is None:
        return None
    subdir = root / "output"
    _check_subdir_safe(subdir, root)
    return str(subdir)


def scratch_dir(environ: Mapping[str, str] | None = None) -> str | None:
    """Return private scratch directory ``<root>/tmp`` when root is set, else None."""
    root = artifacts_root(environ)
    if root is not None:
        return str(ensure_private_subdir(root, "tmp"))
    return None


def actionlogs_dir(
    environ: Mapping[str, str] | None = None,
    *,
    create: bool = False,
) -> Path:
    """Return the default actionlogs directory; create with 0700 under root if requested."""
    root = artifacts_root(environ)
    if root is not None:
        if create:
            return ensure_private_subdir(root, "actionlogs")
        subdir = root / "actionlogs"
        _check_subdir_safe(subdir, root)
        return subdir
    return _default_linc_home() / "nerve" / "actionlogs"


def mcp_call_log_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return path to ``mcp-calls.jsonl`` (under root when configured, else LINC_HOME)."""
    root = artifacts_root(environ)
    if root is not None:
        return root / "mcp-calls.jsonl"
    return _default_linc_home() / "mcp-calls.jsonl"


def thread_dump_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return path to ``nerve_threads.txt`` (under root when configured, else LINC_HOME)."""
    root = artifacts_root(environ)
    if root is not None:
        return root / "nerve_threads.txt"
    return _default_linc_home() / "nerve" / "nerve_threads.txt"


# ---------------------------------------------------------------------------
# Device fleet registry & fail-closed personal-device / engagement gating
# ---------------------------------------------------------------------------

_FLEET_CACHE_LOCK = threading.Lock()
_fleet_cache: dict[str, tuple[tuple[int, int, int, int, int, int], FleetRegistry]] = {}


@dataclass(frozen=True)
class FleetRegistry:
    """Cached parse of ``$LINC_HOME/device-fleet.yaml``."""

    state: Literal["loaded", "absent", "unreadable"]
    path: Path
    by_id: Mapping[str, tuple[FleetEntry, ...]]
    error: str | None = None
    has_personal: bool = False
    malformed_count: int = 0
    degraded: bool = False


@dataclass(frozen=True)
class DeviceDecision:
    """Policy verdict for a device identifier set."""

    allowed: bool
    reason: str | None = None
    error_category: str | None = None


def fleet_registry_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return ``$LINC_HOME/device-fleet.yaml`` resolved at call time."""
    if environ is not None:
        raw_home = environ.get("LINC_HOME", "").strip()
        if raw_home:
            return Path(os.path.expanduser(raw_home)) / FLEET_FILENAME
    return _default_linc_home() / FLEET_FILENAME


def _drop_fleet_cache(path: Path) -> None:
    with _FLEET_CACHE_LOCK:
        _fleet_cache.pop(str(path), None)


def _read_fleet_file(path: Path) -> FleetRegistry:
    """Read and parse *path*; never raises (an unreadable registry is a state)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_FLEET_READ_LIMIT)
        if len(raw) > _MAX_FLEET_BYTES:
            return FleetRegistry("unreadable", path, {}, f"exceeds {_MAX_FLEET_BYTES} bytes")
        text = raw.decode(_FLEET_ENCODING).lstrip(_UTF8_BOM)
    except (OSError, UnicodeDecodeError) as exc:
        return FleetRegistry("unreadable", path, {}, f"{type(exc).__name__}: {exc}")
    parsed = parse_fleet_text(text)
    if not parsed.readable:
        return FleetRegistry(
            "unreadable", path, {}, parsed.error, malformed_count=parsed.malformed_count
        )
    has_personal = any(e.personal is True for entries in parsed.by_id.values() for e in entries)
    return FleetRegistry(
        "loaded",
        path,
        parsed.by_id,
        error=parsed.error,
        has_personal=has_personal,
        malformed_count=parsed.malformed_count,
        degraded=parsed.degraded,
    )


def load_fleet_registry(environ: Mapping[str, str] | None = None) -> FleetRegistry:
    """Load ``device-fleet.yaml``, reloading when stat metadata changes."""
    path = fleet_registry_path(environ)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        _drop_fleet_cache(path)
        if os.path.islink(path):
            return FleetRegistry("unreadable", path, {}, "dangling symlink")
        return FleetRegistry("absent", path, {})
    except OSError as exc:
        _drop_fleet_cache(path)
        return FleetRegistry("unreadable", path, {}, str(exc))

    if not stat.S_ISREG(st.st_mode):
        _drop_fleet_cache(path)
        return FleetRegistry("unreadable", path, {}, "not a regular file")
    if st.st_size > _MAX_FLEET_BYTES:
        _drop_fleet_cache(path)
        return FleetRegistry("unreadable", path, {}, f"exceeds {_MAX_FLEET_BYTES} bytes")

    stamp = (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    key = str(path)
    with _FLEET_CACHE_LOCK:
        cached = _fleet_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    registry = _read_fleet_file(path)
    with _FLEET_CACHE_LOCK:
        _fleet_cache[key] = (stamp, registry)
    return registry


_HW_SERIAL_CACHE_TTL_S = 30.0
_FAILED_PROBE_CACHE_TTL_S = 5.0
_MAX_HW_SERIAL_CACHE = 256
_hw_serial_cache_lock = threading.Lock()
_hw_serial_cache: OrderedDict[tuple[str, str, str, str], tuple[float, str]] = OrderedDict()
_failed_probe_cache: OrderedDict[tuple[str, str, str, str, tuple[str, int]], float] = OrderedDict()


def _hw_cache_clock() -> float:
    """Monotonic clock for the hardware-serial resolution cache."""
    return time.monotonic()


def clear_hardware_serial_cache() -> None:
    """Drop cached ``ro.serialno`` lookups and failed-probe entries."""
    with _hw_serial_cache_lock:
        _hw_serial_cache.clear()
        _failed_probe_cache.clear()


def _hw_cache_scope(environ: Mapping[str, str] | None) -> tuple[str, str]:
    env = os.environ if environ is None else environ
    adb_port = env.get("ANDROID_ADB_SERVER_PORT", "")
    adb_sock = env.get("ADB_SERVER_SOCKET", "")
    return str(fleet_registry_path(environ)), f"{adb_port}|{adb_sock}"


def _hw_cache_key(
    transport: str,
    environ: Mapping[str, str] | None,
    transport_id: str = "",
) -> tuple[str, str, str, str]:
    fleet_key, adb_key = _hw_cache_scope(environ)
    return (transport, transport_id, fleet_key, adb_key)


def _probe_scope(
    probe_hw: Callable[[str], str] | None,
    controller: Any = None,
) -> tuple[str, int]:
    """Identify the probe source for brief failure-cache isolation."""
    if controller is not None:
        return ("ctrl", id(controller))
    if probe_hw is not None:
        return ("direct", 0)
    return ("default", 0)


def _drop_transport_entries_locked(
    transport: str,
    fleet_key: str,
    adb_key: str,
    *,
    keep_tid: str | None = None,
) -> None:
    stale = [
        k
        for k in _hw_serial_cache
        if k[0] == transport
        and k[2] == fleet_key
        and k[3] == adb_key
        and (keep_tid is None or k[1] != keep_tid)
    ]
    for k in stale:
        _hw_serial_cache.pop(k, None)


def _drop_failed_entries_locked(
    transport: str,
    fleet_key: str,
    adb_key: str,
    *,
    scope: tuple[str, int] | None = None,
) -> None:
    stale = [
        k
        for k in _failed_probe_cache
        if k[0] == transport
        and k[2] == fleet_key
        and k[3] == adb_key
        and (scope is None or k[4] == scope)
    ]
    for k in stale:
        _failed_probe_cache.pop(k, None)


def prune_hardware_serial_cache(
    active_transports: Iterable[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Drop cached entries for transports absent from *active_transports*."""
    active = {t.strip() for t in active_transports if isinstance(t, str) and t.strip()}
    fleet_key, adb_key = _hw_cache_scope(environ)
    with _hw_serial_cache_lock:
        stale = [
            k
            for k in _hw_serial_cache
            if k[2] == fleet_key and k[3] == adb_key and k[0] not in active
        ]
        for k in stale:
            _hw_serial_cache.pop(k, None)
        stale_failed = [
            k
            for k in _failed_probe_cache
            if k[2] == fleet_key and k[3] == adb_key and k[0] not in active
        ]
        for k in stale_failed:
            _failed_probe_cache.pop(k, None)


def sync_hardware_serial_cache(
    listing: Mapping[str, str] | None,
    *,
    target: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Reconcile cached entries against the current ``host:devices-l`` listing."""
    fleet_key, adb_key = _hw_cache_scope(environ)
    clean_target = target.strip() if isinstance(target, str) else ""
    with _hw_serial_cache_lock:
        if listing is None:
            if clean_target:
                _drop_transport_entries_locked(clean_target, fleet_key, adb_key)
                _drop_failed_entries_locked(clean_target, fleet_key, adb_key)
                return
            for k in [k for k in _hw_serial_cache if k[2] == fleet_key and k[3] == adb_key]:
                _hw_serial_cache.pop(k, None)
            for k in [k for k in _failed_probe_cache if k[2] == fleet_key and k[3] == adb_key]:
                _failed_probe_cache.pop(k, None)
            return
        stale = [
            k
            for k in _hw_serial_cache
            if k[2] == fleet_key
            and k[3] == adb_key
            and (not listing.get(k[0]) or listing.get(k[0]) != k[1])
        ]
        for k in stale:
            _hw_serial_cache.pop(k, None)
        stale_failed = [
            k
            for k in _failed_probe_cache
            if k[2] == fleet_key
            and k[3] == adb_key
            and k[1]
            and (not listing.get(k[0]) or listing.get(k[0]) != k[1])
        ]
        for k in stale_failed:
            _failed_probe_cache.pop(k, None)


def cache_hardware_serial(
    transport: str,
    hardware_serial: str,
    *,
    transport_id: str = "",
    environ: Mapping[str, str] | None = None,
) -> str:
    """Normalize and cache *hardware_serial* for *(transport, transport_id)*."""
    key_transport = transport.strip() if isinstance(transport, str) else ""
    tid = transport_id.strip() if isinstance(transport_id, str) else ""
    if not key_transport or not tid or not is_network_transport(transport):
        return ""
    norm = normalize_hardware_serial(hardware_serial)
    if not norm:
        return ""
    key = _hw_cache_key(key_transport, environ, tid)
    now = _hw_cache_clock()
    with _hw_serial_cache_lock:
        _drop_transport_entries_locked(key_transport, key[2], key[3], keep_tid=tid)
        _drop_failed_entries_locked(key_transport, key[2], key[3])
        _hw_serial_cache[key] = (now, norm)
        _hw_serial_cache.move_to_end(key)
        while len(_hw_serial_cache) > _MAX_HW_SERIAL_CACHE:
            _hw_serial_cache.popitem(last=False)
    return norm


def _cached_hardware_serial(
    transport: str,
    *,
    transport_id: str = "",
    environ: Mapping[str, str] | None = None,
    ttl_s: float = _HW_SERIAL_CACHE_TTL_S,
) -> str:
    tid = transport_id.strip() if isinstance(transport_id, str) else ""
    if not tid:
        return ""
    key = _hw_cache_key(transport.strip(), environ, tid)
    now = _hw_cache_clock()
    with _hw_serial_cache_lock:
        entry = _hw_serial_cache.get(key)
        if entry is None:
            return ""
        ts, hw = entry
        if now - ts > ttl_s:
            _hw_serial_cache.pop(key, None)
            return ""
        _hw_serial_cache.move_to_end(key)
        return hw


def _cache_probe_failure(
    transport: str,
    *,
    transport_id: str = "",
    environ: Mapping[str, str] | None = None,
    scope: tuple[str, int] = ("default", 0),
) -> None:
    """Record a failed identity probe briefly so unreachable calls fail fast."""
    key_transport = transport.strip() if isinstance(transport, str) else ""
    tid = transport_id.strip() if isinstance(transport_id, str) else ""
    if not key_transport or not is_network_transport(transport):
        return
    fleet_key, adb_key = _hw_cache_scope(environ)
    key = (key_transport, tid, fleet_key, adb_key, scope)
    now = _hw_cache_clock()
    with _hw_serial_cache_lock:
        _failed_probe_cache[key] = now
        _failed_probe_cache.move_to_end(key)
        while len(_failed_probe_cache) > _MAX_HW_SERIAL_CACHE:
            _failed_probe_cache.popitem(last=False)


def _has_cached_probe_failure(
    transport: str,
    *,
    transport_id: str = "",
    environ: Mapping[str, str] | None = None,
    scope: tuple[str, int] = ("default", 0),
    ttl_s: float = _FAILED_PROBE_CACHE_TTL_S,
) -> bool:
    """True when an unexpired failed probe was cached for *(transport, tid, scope)*."""
    key_transport = transport.strip() if isinstance(transport, str) else ""
    if not key_transport:
        return False
    tid = transport_id.strip() if isinstance(transport_id, str) else ""
    fleet_key, adb_key = _hw_cache_scope(environ)
    key = (key_transport, tid, fleet_key, adb_key, scope)
    now = _hw_cache_clock()
    with _hw_serial_cache_lock:
        ts = _failed_probe_cache.get(key)
        if ts is None:
            return False
        if now - ts > ttl_s:
            _failed_probe_cache.pop(key, None)
            return False
        _failed_probe_cache.move_to_end(key)
        return True


def _clear_probe_failure(
    transport: str,
    *,
    environ: Mapping[str, str] | None = None,
    scope: tuple[str, int] | None = None,
) -> None:
    key_transport = transport.strip() if isinstance(transport, str) else ""
    if not key_transport:
        return
    fleet_key, adb_key = _hw_cache_scope(environ)
    with _hw_serial_cache_lock:
        _drop_failed_entries_locked(key_transport, fleet_key, adb_key, scope=scope)


def _drop_positive_cache(
    transport: str,
    *,
    environ: Mapping[str, str] | None = None,
    mismatch_hw: str | None = None,
) -> None:
    """Drop positive cache entries for *transport* (all, or only those != *mismatch_hw*)."""
    key_transport = transport.strip() if isinstance(transport, str) else ""
    if not key_transport:
        return
    fleet_key, adb_key = _hw_cache_scope(environ)
    with _hw_serial_cache_lock:
        stale = [
            k
            for k, (_ts, hw) in _hw_serial_cache.items()
            if k[0] == key_transport
            and k[2] == fleet_key
            and k[3] == adb_key
            and (mismatch_hw is None or hw != mismatch_hw)
        ]
        for k in stale:
            _hw_serial_cache.pop(k, None)


def _append_unique(seen: list[str], item: str) -> None:
    if item and item not in seen:
        seen.append(item)


def _device_extra_transports(device: object) -> tuple[list[str], str]:
    if device is None:
        return [], ""
    transports: list[str] = []
    dev_serial = getattr(device, "serial", "")
    if isinstance(dev_serial, str):
        transports.append(dev_serial)
    aliases = getattr(device, "aliases", ())
    if isinstance(aliases, (list, tuple)):
        transports.extend(a for a in aliases if isinstance(a, str))
    raw_hw = getattr(device, "hardware_serial", "")
    hw = normalize_hardware_serial(raw_hw) if isinstance(raw_hw, str) and raw_hw.strip() else ""
    return transports, hw


def device_identifiers(serial: str = "", device: object = None) -> tuple[str, ...]:
    """Return unique trimmed identifiers for a device without any new device probe.

    Covers the requested serial, the device's transport serial, any hardware
    serial / aliases already attached by discovery, and the hardware serial
    embedded in an mDNS ``_adb._tcp`` or ``_adb-tls-connect._tcp`` name.
    """
    extra_transports, hw = _device_extra_transports(device)
    transports = [serial, *extra_transports]
    seen: list[str] = []
    for raw in transports:
        if not isinstance(raw, str):
            continue
        _append_unique(seen, raw.strip())
        _append_unique(seen, mdns_hardware_serial(raw))
    _append_unique(seen, hw)
    return tuple(seen)


def _allow_personal_set(env: Mapping[str, str]) -> set[str]:
    raw = env.get(ALLOW_PERSONAL_ENV, "")
    return {
        tok.strip().casefold()
        for tok in raw.split(",")
        if tok.strip() and not is_network_transport(tok)
    }


def _is_personal_allowlisted(
    personal_entries: Sequence[FleetEntry],
    resolved_hardware_serial: str,
    env: Mapping[str, str],
) -> bool:
    """True iff every matched personal entry matches by serial or resolved hardware serial."""
    allow_set = _allow_personal_set(env)
    if not allow_set:
        return False
    norm_hw = normalize_hardware_serial(resolved_hardware_serial).casefold()
    hw_allowed = bool(norm_hw and not is_network_transport(norm_hw) and norm_hw in allow_set)
    return all(
        hw_allowed
        or any(
            ident in allow_set for ident in entry.identifiers if not is_network_transport(ident)
        )
        for entry in personal_entries
    )


def _expand_identifiers(identifiers: Iterable[str]) -> tuple[list[str], list[str]]:
    raw_items: list[str] = []
    raw_ids: list[str] = []
    for item in identifiers:
        if not isinstance(item, str) or not item.strip():
            continue
        raw_items.append(item)
        _append_unique(raw_ids, item.strip())
        _append_unique(raw_ids, mdns_hardware_serial(item))
    return raw_items, raw_ids


def _has_resolved_hardware_identity(identifiers: Sequence[str]) -> bool:
    """True when *identifiers* carries a non-network ID or mDNS-embedded serial."""
    return any(
        (not is_network_transport(ident)) or bool(mdns_hardware_serial(ident))
        for ident in identifiers
    )


def _engagement_mode_decision(
    eng_id: str,
    reg: FleetRegistry,
    matched: list[FleetEntry],
    display: str,
) -> DeviceDecision:
    if reg.state == "absent":
        return DeviceDecision(
            False,
            f"Device '{display}' is refused in engagement '{eng_id}': "
            f"fleet registry is absent ({reg.path}).",
            ERROR_CATEGORY,
        )
    if reg.state == "unreadable":
        return DeviceDecision(
            False,
            f"Device '{display}' is refused in engagement '{eng_id}': "
            f"fleet registry is unreadable ({reg.path}).",
            ERROR_CATEGORY,
        )
    if not matched:
        return DeviceDecision(
            False,
            f"Device '{display}' is refused in engagement '{eng_id}': "
            f"not registered in {reg.path}.",
            ERROR_CATEGORY,
        )
    if all(e.personal is False for e in matched):
        return DeviceDecision(True)
    return DeviceDecision(
        False,
        f"Device '{display}' is refused in engagement '{eng_id}': "
        "fleet entry lacks a boolean 'personal' flag.",
        ERROR_CATEGORY,
    )


def _warn_untrusted_registry(reg: FleetRegistry, display: str) -> None:
    """Warn on EVERY decision while the registry is unreadable or degraded."""
    if reg.state == "unreadable":
        log.warning(
            "fleet_registry_unreadable", path=str(reg.path), error=reg.error, device=display
        )
    elif reg.degraded:
        log.warning(
            "fleet_registry_degraded",
            path=str(reg.path),
            error=reg.error,
            malformed_count=reg.malformed_count,
            device=display,
        )


def _untrusted_registry_decision(
    reg: FleetRegistry, raw_items: Sequence[str], display: str
) -> DeviceDecision:
    """Refuse network transports while the registry cannot be trusted; USB stays usable."""
    if not any(is_network_transport(item) for item in raw_items):
        return DeviceDecision(True)
    condition = "unreadable" if reg.state == "unreadable" else "degraded"
    return DeviceDecision(
        False,
        f"Device '{display}' identity could not be verified; "
        f"fleet registry is {condition} ({reg.path}), refusing network transport.",
        PERSONAL_DEVICE_ERROR_CATEGORY,
    )


def device_decision(
    identifiers: Iterable[str],
    *,
    environ: Mapping[str, str] | None = None,
    resolved_hardware_serial: str = "",
) -> DeviceDecision:
    """Evaluate personal-device and engagement-mode policy for *identifiers*."""
    env = os.environ if environ is None else environ
    try:
        eng_id = _parse_engagement(env)
    except EngagementMisconfiguredError as exc:
        return DeviceDecision(False, str(exc), ERROR_CATEGORY)

    raw_items, raw_ids = _expand_identifiers(identifiers)
    norm_ids = [s.casefold() for s in raw_ids]
    display = raw_ids[0] if raw_ids else "<unknown>"
    reg = load_fleet_registry(env)
    matched = [entry for ident in norm_ids for entry in reg.by_id.get(ident, ())]
    _warn_untrusted_registry(reg, display)

    personal_entries = [e for e in matched if e.personal is True]
    if reg.state == "loaded" and personal_entries:
        if eng_id is None and _is_personal_allowlisted(
            personal_entries, resolved_hardware_serial, env
        ):
            return DeviceDecision(True)
        return DeviceDecision(
            False,
            f"Device '{display}' is registered as a personal device in {reg.path} and is refused.",
            PERSONAL_DEVICE_ERROR_CATEGORY,
        )

    if eng_id is not None:
        if reg.degraded:  # a degraded registry refuses network transports in every mode
            refusal = _untrusted_registry_decision(reg, raw_items, display)
            if not refusal.allowed:
                return refusal
        return _engagement_mode_decision(eng_id, reg, matched, display)

    if reg.state == "unreadable" or reg.degraded:
        return _untrusted_registry_decision(reg, raw_items, display)

    if (
        reg.has_personal
        and any(is_network_transport(item) for item in raw_items)
        and not _has_resolved_hardware_identity(raw_items)
    ):
        return DeviceDecision(
            False,
            f"Device '{display}' identity could not be verified; "
            f"refusing unverified network transport while personal devices are "
            f"registered in {reg.path}.",
            PERSONAL_DEVICE_ERROR_CATEGORY,
        )

    return DeviceDecision(True)


def _is_clean_probe_target(serial: str) -> bool:
    return bool(serial) and serial == serial.strip() and serial.isprintable()


def probe_controller_hardware_serial(controller: Any, serial: str) -> str:
    """Probe *serial* via an AndroidController (or mock stand-in)."""
    reader = getattr(controller, "read_hardware_serial", None)
    if not callable(reader):
        return ""
    raw = reader(serial)
    return normalize_hardware_serial(raw) if isinstance(raw, str) else ""


def _fresh_android_controller() -> Any:
    from linc_nerve.core.android import AndroidController

    return AndroidController()


def _default_probe_hardware_serial(serial: str) -> str:
    """Resolve ``ro.serialno`` via a fresh AndroidController."""
    return probe_controller_hardware_serial(_fresh_android_controller(), serial)


def _normalize_transport_ids_mapping(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    return {
        k.strip(): v.strip()
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
    }


def _read_transport_ids_from_controller(controller: Any) -> dict[str, str] | None:
    """Read ``{serial: transport_id}`` from *controller*."""
    try:
        reader = getattr(controller, "read_transport_ids", None)
        if not callable(reader):
            return None
        raw = reader()
    except Exception as exc:
        logger.debug(
            "network_transport_listing_failed",
            error=str(exc) or type(exc).__name__,
        )
        return None
    return _normalize_transport_ids_mapping(raw)


def _default_read_transport_ids() -> dict[str, str] | None:
    """Read ``{serial: transport_id}`` from a fresh AndroidController."""
    try:
        ctrl = _fresh_android_controller()
    except Exception as exc:
        logger.debug(
            "network_transport_listing_failed",
            error=str(exc) or type(exc).__name__,
        )
        return None
    return _read_transport_ids_from_controller(ctrl)


def _read_target_transport_id(
    target: str,
    probe_transport_ids: Callable[[], Mapping[str, str] | None] | None,
    env: Mapping[str, str],
    *,
    controller: Any = None,
) -> str:
    """Fetch current adb ``transport_id`` for *target* and sync cache; ``""`` if unreadable."""
    try:
        if probe_transport_ids is not None:
            listing = probe_transport_ids()
        elif controller is not None:
            listing = _read_transport_ids_from_controller(controller)
        else:
            listing = _default_read_transport_ids()
    except Exception as exc:
        logger.debug(
            "network_transport_listing_failed",
            serial=target,
            error=str(exc) or type(exc).__name__,
        )
        listing = None
    clean_listing = _normalize_transport_ids_mapping(listing)
    if clean_listing is None:
        sync_hardware_serial_cache(None, target=target, environ=env)
        return ""
    sync_hardware_serial_cache(clean_listing, environ=env)
    return clean_listing.get(target.strip(), "")


def _select_probe_fn(
    probe_hw: Callable[[str], str] | None,
    controller: Any,
) -> Callable[[str], str]:
    if probe_hw is not None:
        return probe_hw
    if controller is not None:
        return lambda serial: probe_controller_hardware_serial(controller, serial)
    return _default_probe_hardware_serial


def _run_identity_probe(
    target: str,
    probe_fn: Callable[[str], str],
    tid: str,
    env: Mapping[str, str],
    scope: tuple[str, int],
) -> str:
    try:
        raw_hw = probe_fn(target)
    except Exception as exc:
        logger.warning(
            "network_identity_probe_failed",
            serial=target,
            error=str(exc) or type(exc).__name__,
        )
        _cache_probe_failure(target, transport_id=tid, environ=env, scope=scope)
        _drop_positive_cache(target, environ=env)
        return ""
    norm_hw = normalize_hardware_serial(raw_hw) if isinstance(raw_hw, str) else ""
    if not norm_hw:
        _cache_probe_failure(target, transport_id=tid, environ=env, scope=scope)
        _drop_positive_cache(target, environ=env)
        return ""
    _clear_probe_failure(target, environ=env, scope=scope)
    if tid:
        cache_hardware_serial(target, norm_hw, transport_id=tid, environ=env)
    else:
        _drop_positive_cache(target, environ=env, mismatch_hw=norm_hw)
    return norm_hw


def _resolve_network_hardware_serial(
    target: str,
    probe_hw: Callable[[str], str] | None,
    env: Mapping[str, str],
    *,
    allow_probe: bool,
    transport_id: str | None = None,
    probe_transport_ids: Callable[[], Mapping[str, str] | None] | None = None,
    use_cache: bool = True,
    controller: Any = None,
) -> str:
    if not _is_clean_probe_target(target):
        return ""
    scope = _probe_scope(probe_hw, controller)
    if isinstance(transport_id, str):
        tid = transport_id.strip()
    elif use_cache:
        tid = _read_target_transport_id(target, probe_transport_ids, env, controller=controller)
    else:
        tid = ""
    if tid and use_cache:
        cached = _cached_hardware_serial(target, transport_id=tid, environ=env)
        if cached:
            return cached
    if not allow_probe:
        return ""
    if _has_cached_probe_failure(target, transport_id=tid, environ=env, scope=scope):
        return ""
    return _run_identity_probe(target, _select_probe_fn(probe_hw, controller), tid, env, scope)


def _resolved_hw_from_device(target: str, device: object) -> str:
    raw_hw = getattr(device, "hardware_serial", "") if device is not None else ""
    if isinstance(raw_hw, str) and raw_hw.strip():
        return normalize_hardware_serial(raw_hw)
    if isinstance(target, str) and is_network_transport(target):
        return mdns_hardware_serial(target)
    return ""


def _needs_identity_probe(
    target: Any,
    idents: Sequence[str],
    eng_id: str | None,
    reg: FleetRegistry,
) -> bool:
    return (
        isinstance(target, str)
        and is_network_transport(target)
        and not _has_resolved_hardware_identity(idents)
        and (eng_id is not None or reg.has_personal)
        and not reg.degraded
    )


def needs_network_identity_probe(
    serial: str = "",
    device: object = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """True when policy requires probing ``ro.serialno`` for *serial*/*device*."""
    env = os.environ if environ is None else environ
    try:
        eng_id = _parse_engagement(env)
    except EngagementMisconfiguredError:
        return False
    idents = device_identifiers(serial, device)
    target = serial or (getattr(device, "serial", "") if device is not None else "")
    return _needs_identity_probe(target, idents, eng_id, load_fleet_registry(env))


def resolve_device_decision(
    serial: str = "",
    device: object = None,
    *,
    probe_hw: Callable[[str], str] | None = None,
    environ: Mapping[str, str] | None = None,
    allow_probe: bool = True,
    transport_id: str | None = None,
    probe_transport_ids: Callable[[], Mapping[str, str] | None] | None = None,
    fresh_identity: bool | None = None,
    controller: Any = None,
) -> DeviceDecision:
    """Evaluate policy for *serial*/*device*, resolving ``ro.serialno`` for unmapped
    network transports (``host:port`` or mDNS without embedded serial) when needed.

    ``fresh_identity`` (default: ``allow_probe``) makes the decision probe afresh instead
    of trusting the per-transport identity cache; only display paths should pass False.
    """
    env = os.environ if environ is None else environ
    try:
        eng_id = _parse_engagement(env)
    except EngagementMisconfiguredError as exc:
        return DeviceDecision(False, str(exc), ERROR_CATEGORY)

    idents = list(device_identifiers(serial, device))
    target = serial or (getattr(device, "serial", "") if device is not None else "")
    reg = load_fleet_registry(env)
    hw = ""
    if _needs_identity_probe(target, idents, eng_id, reg):
        # An enforcing decision (allow_probe) always reads ro.serialno afresh: adb
        # renumbers transport_ids from 1 after a server restart, so a cached identity
        # could belong to a different device on the same address and transport_id.
        # The cache only serves display paths that may not probe.
        hw = _resolve_network_hardware_serial(
            target,
            probe_hw,
            env,
            allow_probe=allow_probe,
            transport_id=transport_id,
            probe_transport_ids=probe_transport_ids,
            use_cache=not (allow_probe if fresh_identity is None else fresh_identity),
            controller=controller,
        )
        _append_unique(idents, hw)
    raw_items = (
        [target, *idents]
        if isinstance(target, str) and target.strip() and not _is_clean_probe_target(target)
        else idents
    )
    resolved_hw = hw or _resolved_hw_from_device(target, device)
    return device_decision(raw_items, environ=env, resolved_hardware_serial=resolved_hw)


def device_refusal(
    identifiers: Iterable[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return refusal message if *identifiers* are blocked, else ``None``."""
    return device_decision(identifiers, environ=environ).reason


def annotate_discovered_device(
    device: Any,
    *,
    environ: Mapping[str, str] | None = None,
    allow_probe: bool = True,
    probe_hw: Callable[[str], str] | None = None,
    controller: Any = None,
) -> dict[str, Any]:
    """Serialize *device*, adding ``usable``/``refusal`` only when refused."""
    entry = device.to_dict()
    reason = resolve_device_decision(
        device.serial,
        device,
        probe_hw=probe_hw,
        environ=environ,
        allow_probe=allow_probe,
        fresh_identity=False,
        controller=controller,
    ).reason
    if reason is not None:
        entry["usable"] = False
        entry["refusal"] = reason
    return entry


def annotate_discovered_devices(
    devices: Iterable[Any],
    *,
    failed_serials: object = None,
    environ: Mapping[str, str] | None = None,
    probe_hw: Callable[[str], str] | None = None,
    controller: Any = None,
) -> list[dict[str, Any]]:
    """Serialize and annotate *devices*, skipping redundant probes for *failed_serials*."""
    skip = failed_serials if isinstance(failed_serials, (set, frozenset)) else ()
    return [
        annotate_discovered_device(
            d,
            environ=environ,
            allow_probe=(d.serial not in skip),
            probe_hw=probe_hw,
            controller=controller,
        )
        for d in devices
    ]
