"""HTTP router for nerve daemon — maps /tools/* to nerve MCP tool functions.

Routes:
- GET /health   — sub-50ms liveness probe (no I/O, no controller imports)
- GET /devices  — device-fleet enumeration with a short-TTL cache
- POST /tools/* — JSON tool calls
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import typing
from collections.abc import Callable
from typing import Any

import structlog

from linc_nerve.core import adb_health
from linc_nerve.core.emulator_console import is_allowed_sensor_type
from linc_nerve.core.observability import log_mcp_call

log = structlog.get_logger().bind(domain="http_router")

_DAEMON_START_TIME = time.monotonic()

# --- Adversarial-input hardening (linc-nerve#128, repaired per #129 round 0)
#
# `route_request` is the daemon's primary HTTP-reachable dispatch point
# (the log-stream routes reach a device without going through it — see
# `_run_pre_dispatch_guards` below); it is reachable from every session host
# on the tailnet (issue #128 context). The checks below run BEFORE a tool is
# dispatched, so a hostile/malformed request never reaches device I/O —
# they reject before any tool code or device I/O runs, instead of letting
# the tool's own best-effort validation (which returns a soft
# `{"error": ...}` at HTTP 200, per the codebase's existing tool convention)
# mask the failure as a "successful" response.
#
# NOTE (finding 8 correction): "before a tool is dispatched" is accurate;
# "at the transport level" was NOT — the request body is already fully read
# and JSON-parsed by sse_server._handle_json_tool_request before
# `route_request` ever runs. The genuinely cheap, Content-Length-based
# oversized-body rejection lives in sse_server (ahead of the body read); the
# `_reject_oversized` guard below is a second, JSON-shape-based check that
# still runs post-parse, kept as defense-in-depth for any other caller of
# `route_request` (e.g. direct test callers) that doesn't go through the
# sse_server pre-check.

# Generous budget for JSON tool params — screenshots/binaries flow through
# the daemon as file paths, not inline blobs, so legitimate bodies are small.
_MAX_BODY_BYTES = 262_144  # 256 KiB

# `../`, `..\`, or an embedded NUL byte (classic path-truncation attack).
_TRAVERSAL_PATTERN = re.compile(r"\.\.[/\\]|\x00")

# Fields whose VALUE is a filesystem path or device identifier — the only
# fields the traversal check inspects (finding 7 fix). The original guard
# blanket-scanned every string value anywhere in the body, which
# false-positived on ordinary user-facing text (e.g.
# `nerve_type(text="../docs/readme")` — a UI test literally typing a
# relative path) and would have broken linc-crucible's
# `nerve_client.install()` payload shape (`{"device":.., "path": path}`) had
# it ever carried a relative path. Scoped by key name at ANY nesting depth —
# see `_contains_traversal` below — not just the top level, so the existing
# `extras.path` regression guard still holds.
_TRAVERSAL_PARAM_NAMES = frozenset(
    {"device", "path", "apk_path", "remote_path", "local_path", "app_path", "serial"}
)

# `package` field validation (finding 3, round 0; ALLOWLIST since round 1
# finding 2). Deliberately scoped to `package` (plus intent-extras KEYS, see
# `_contains_bad_extras_key`): `core/` now validates every identifier it
# interpolates (`AndroidController.kill()`/`launch()` run
# `_validate_shell_identifier`/`_validate_activity` with `fullmatch`, and
# shlex-quote each token — 2026-10-02 security review H1), so this router
# check is the outer layer of two, not the only one. A field like `uri`
# legitimately contains `&` in multi-param deep links (guarding it here would
# repeat the finding-7 false-positive mistake); core shell-quotes it.
# `package` is the one field this PR can safely close at the router
# chokepoint. NOTE: round 0 justified that with "Android package
# identifiers are always `[a-zA-Z0-9_.]+`, so there is no legitimate value
# this guard can collide with" — that claim is FALSE and round 2 disproved
# it twice (see the round-2 paragraph below): `package` also carries iOS
# bundle IDs, which legitimately contain `-`, and linc-crucible sends
# `package=""` on every BVT navigate step. The shipped grammar at
# `_ROUTER_IDENTIFIER_RE` reflects those collisions, not the original claim.
# (Historical: the activity/uri class this comment once deferred to a
# follow-up is closed in core — activity is allowlisted, uri is quoted.)
#
# round-0 repair used a DENYLIST (`_SHELL_METACHAR_PATTERN =
# re.compile(r"[;&|`$()\n\r]")`) that missed `>`, `<`, `*`, quotes, and
# space — `nerve_kill(package="com.x > /sdcard/pwn")` contains no denylisted
# character at all and reached the tool at 200 (linc-nerve#128/129 round 1
# finding 2). Denylists enumerate known-bad characters; an allowlist
# enumerates the one known-good shape and rejects everything else, which is
# the correct direction for a field with a fixed, well-known grammar.
#
# round-1 repair's allowlist OVER-CORRECTED (round 2 finding 1): it rejected
# `package=""` outright (with no empty-value skip, unlike `_contains_bad_
# filter`), which 400'd linc-crucible's `NerveClient.navigate()` payload
# (`{"device":..., "package":"", "uri":...}` — every BVT navigate step sends
# this shape, `core/bvt/step_handlers.py:445`, `core/bvt/nerve_adapter.py:
# 380`) and rejected any hyphenated iOS bundle ID (`com.my-app.Runner`) even
# though `package` also carries iOS bundle IDs (`app_tools.py:56,101`),
# whose ARGV-only path (`core/ios_apps_logs.py:171-185,238-251`) never
# touches a shell and legitimately allows hyphens. Fixed by (1) skipping
# empty values, matching `_contains_bad_filter`'s own guard, and (2) adding
# `-` to the allowlist — safe because a hyphen is not a shell metacharacter
# and the leading-char requirement (`^[A-Za-z]`) still blocks flag
# injection (`-n com.evil.package`). The strict Android-only grammar (no
# hyphen — real Android package identifiers never contain one) is enforced
# where it actually matters, downstream, by `core/android_apps.py`'s
# `_validate_shell_identifier`/`_SAFE_IDENTIFIER_RE` for the calls that use
# it (kill, launch, uninstall, clear_app_data, grant_permission, and every
# intent-extras key). Kept as a separate constant here rather than
# imported: `core.android_apps` and `core.android` import each other (a
# mixin split, `android.py:117` does `from .android_apps import
# _AppsMixin`), and that pair only resolves cleanly when `core.android` is
# the FIRST of the two imported — importing `core.android_apps` directly
# (as this module would if it were the first thing in a process to touch
# either) raises `ImportError: cannot import name '_AppsMixin' from
# partially initialized module` (reproduced).
#
# DO NOT sync the two patterns (round-cap finding
# `carry-over-3-package-guard-comment-still-self-contradictory`; an earlier
# revision of this comment ended "Keep both patterns in sync by hand if
# either changes", which would re-introduce the round-2 regression on the
# next edit). They are DELIBERATELY divergent and serve different jobs:
#
#   router  `_ROUTER_IDENTIFIER_RE`      `\A[A-Za-z][A-Za-z0-9_.-]*\Z`  permits `-`
#   core    `android_apps._SAFE_IDENTIFIER_RE`  `\A[A-Za-z][A-Za-z0-9_.]*\Z`  forbids `-`
#
# The router guards a field that carries BOTH Android package names and iOS
# bundle IDs, and must not reject the latter; `core/android_apps.py` guards
# only Android identifiers on their way into a shell, where the stricter
# grammar is correct. The router constant is named differently from the core
# one precisely so the two never read as copies of each other.
#
# Both router allowlists are anchored `\A...\Z` AND applied with `fullmatch`
# (2026-10-02 security review H1): `^...$` + `re.match` accepts a trailing
# `\n`, because `$` matches just before a final newline.
_ROUTER_IDENTIFIER_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_.-]*\Z")
_SHELL_GUARDED_PARAM_NAMES = frozenset({"package"})

# Log-filter field validation (linc-nerve#128/129 round 1 finding 3):
# `filter`/`filter_str` used to be in NEITHER `_TRAVERSAL_PARAM_NAMES` nor
# `_SHELL_GUARDED_PARAM_NAMES`, so `_reject_via_stream_guards` inspected it
# with no guard at all — it flowed straight through to
# `IOSLogController.get_log_stream_cmd()` / `AndroidUI.get_log_stream_cmd()`
# and on to `create_subprocess_exec`'s argv unvalidated. A leading `-`
# (e.g. `filter=-c`) risks argument injection into the downstream `log`/
# `adb logcat`/`xcrun simctl` invocation the same way an unvalidated
# leading-dash `package` would. Allowlist: must start with an
# alphanumeric/underscore, then any run of word chars, dots, or hyphens —
# permits real bundle IDs / logcat tags (`com.app.bundle`, `MyTag-1`) while
# rejecting a leading flag-shaped `-foo`. `filter` is the query-string key
# `sse_server` uses when building the guard-chain probe body for `/stream`;
# `filter_str` is the POST JSON body key `nerve_stream_logs` accepts.
#
# `tag` added (round 2 finding 2): `nerve_logcat(device, lines, tag)`
# (mcp/diagnostic_tools.py) was in NONE of this module's guard sets, so a
# hostile `tag` reached `core/android_ui.py`'s `logcat()`, which
# STRING-INTERPOLATES it into `self._shell(serial, f"logcat -d -s {tag}")`
# — a plain string handed to on-device `sh`, a strictly worse sink than the
# argv-injection case the `filter`/`filter_str` guard already closed. Same
# allowlist grammar applies: logcat tags are `[A-Za-z0-9_][\w.-]*`.
_FILTER_ALLOWLIST_RE = re.compile(r"\A[A-Za-z0-9_][\w.\-]*\Z")
_FILTER_PARAM_NAMES = frozenset({"filter", "filter_str", "tag"})

# Tools that must stay callable regardless of who currently holds a device's
# reservation. Two distinct reasons, both about ownership rather than name:
#
#   1. Reservation management (`reserve`/`release`/`pool`/`discover`) — the
#      reservation state IS the subject of the call; guarding it would make the
#      state unobservable and un-negotiable.
#
#   2. Teardown of a resource the CALLER already owns (`record_stop`,
#      `stream_stop`) — round-cap finding
#      `teardown-leak-still-open-reservation-exempt-set-is-unchanged`. Without
#      this, the guard introduces a resource leak of its own: session A starts a
#      recording/stream on device X, session B then reserves X, and A's stop is
#      rejected 409 *before dispatch*. `nerve_record_stop` is the only way to
#      flush the MP4 trailer and pull the artifact (`mcp/recording_tools.py`),
#      and `nerve_stream_stop` the only way to close the scrcpy device socket,
#      the host TCP forward and the frame-decoder thread
#      (`mcp/screenshot_tools.py`) — so a blocked stop leaks all of them until
#      the daemon restarts. Neither tool calls `_core._check_guard` internally
#      (`grep -c _check_guard` is 0 for `recording_tools.py`; `screenshot_tools`
#      guards stream_START only), so there is no second line of defence.
#
# Deliberately NOT exempt, though they also read as "stop": `nerve_emulator_stop`
# and `nerve_kill` destroy state belonging to whoever holds the device — that is
# the case the guard exists for. Extend this set by ownership, never by name
# shape; `test_route_still_guards_destructive_tools_under_a_foreign_reservation`
# pins that distinction.
#
# Owner fence (audit F5): a POST body for a NON-exempt tool may carry an
# optional "owner" string, fenced against the lease holder by
# `DeviceManager.guard(device, owner=...)`. `_reservation_conflict` pops it
# inside the guard chain — before #170's typed-param coercion and before
# `_dispatch_tool` splats the body — so it never reaches a tool with no such
# parameter. Exempt tools return before the pop and keep "owner"
# (nerve_reserve/nerve_release take it themselves).
_RESERVATION_EXEMPT_TOOLS = frozenset(
    {
        "nerve_reserve",
        "nerve_release",
        "nerve_pool",
        "nerve_discover",
        "nerve_record_stop",
        "nerve_stream_stop",
    }
)


def _payload_too_large(body: dict[str, Any]) -> bool:
    """True if the JSON-serialized body exceeds the size budget."""
    try:
        return len(json.dumps(body)) > _MAX_BODY_BYTES
    except (TypeError, ValueError):
        # Unserializable bodies aren't a size problem — let normal dispatch
        # (and its TypeError handling) surface the real error.
        return False


def _contains_traversal(body: dict[str, Any]) -> bool:
    """True if a path-shaped field (`_TRAVERSAL_PARAM_NAMES`), at any
    nesting depth, contains a path-traversal sequence or NUL byte.

    A string is only checked when it's reached via a guarded key — plain
    text fields (e.g. `text`) are never scanned, regardless of what they
    contain, so this can't false-positive the way a blanket string scan did.
    """

    def scan(value: Any, guarded: bool) -> bool:
        if isinstance(value, str):
            return guarded and bool(_TRAVERSAL_PATTERN.search(value))
        if isinstance(value, dict):
            return any(scan(v, k in _TRAVERSAL_PARAM_NAMES) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return any(scan(v, guarded) for v in value)
        return False

    return scan(body, False)


def _contains_shell_metacharacters(body: dict[str, Any]) -> bool:
    """True if a shell-guarded field (`_SHELL_GUARDED_PARAM_NAMES`) is not a
    safe identifier. Top-level only — the only field currently guarded
    (`package`) is always a top-level tool parameter.

    Allowlist-based (linc-nerve#128/129 round 1 finding 2) — see the
    module-level comment on `_SHELL_GUARDED_PARAM_NAMES` for why this
    replaced a denylist, and on `_ROUTER_IDENTIFIER_RE` for why the grammar is
    duplicated here rather than imported from `core.android_apps`.

    Empty values are skipped (round 2 finding 1) — `package=""` is a
    supported shape (an "any/no specific package" launch), matching
    `_contains_bad_filter`'s own `value and ...` guard.
    """
    for name in _SHELL_GUARDED_PARAM_NAMES:
        value = body.get(name)
        if isinstance(value, str) and value and not _ROUTER_IDENTIFIER_RE.fullmatch(value):
            return True
    return False


def _contains_bad_filter(body: dict[str, Any]) -> bool:
    """True if a log-filter field (`_FILTER_PARAM_NAMES`) is present and
    doesn't match the safe filter grammar (linc-nerve#128/129 round 1
    finding 3; `tag` added round 2 finding 2). Top-level only, same
    rationale as `_contains_shell_metacharacters` — all currently-guarded
    filter keys are always top-level tool parameters, never nested."""
    for name in _FILTER_PARAM_NAMES:
        value = body.get(name)
        if isinstance(value, str) and value and not _FILTER_ALLOWLIST_RE.fullmatch(value):
            return True
    return False


def _contains_bad_extras_key(body: dict[str, Any]) -> bool:
    """True if a top-level `extras` dict has a key that isn't a safe identifier.

    2026-10-02 security review H1: intent-extras KEYS are interpolated into
    the device-side `am start` string (Android) or a simctl launch arg (iOS),
    and this guard never looked at them — `{"extras": {"K\\n": "reboot"}}`
    reached `nerve_launch` at 200. Same grammar as `package`
    (`_ROUTER_IDENTIFIER_RE`: leading letter, `-` allowed for iOS-style
    launch-arg names); core re-validates with the stricter Android grammar.
    """
    extras = body.get("extras")
    if not isinstance(extras, dict):
        return False
    return not all(_ROUTER_IDENTIFIER_RE.fullmatch(str(key)) for key in extras)


def _pop_owner(body: dict[str, Any]) -> str:
    """Remove the router-level "owner" fence field from `body`; a non-string
    value reads as unfenced ("")."""
    owner = body.pop("owner", "")
    return owner if isinstance(owner, str) else ""


def _reservation_conflict(tool_name: str, body: dict[str, Any]) -> str | None:
    """Return a conflict error message if `body["device"]` is reserved by a
    different nerve session, or by this session for a different "owner",
    else None. Pops "owner" from `body` for non-exempt tools.

    CORRECTION (finding 8): `DeviceManager.guard()` is NOT read-only. When
    the reservation it finds belongs to a session whose owning PID is dead,
    `guard()` auto-reaps it — it issues a real `DELETE FROM reservations`
    against the daemon's live reservations DB before returning "no
    conflict" (see `device_manager.py`'s `guard()` docstring/implementation).
    This check wires an existing read-AND-sometimes-write path
    (`DeviceManager.guard`, already used internally by individual device
    tools) into the router's own pre-dispatch gate so a blocked call is
    rejected before dispatch instead of silently succeeding with a soft
    error body — but callers of this module must not describe `guard()`
    itself as read-only.
    """
    if tool_name in _RESERVATION_EXEMPT_TOOLS:
        return None
    owner = _pop_owner(body)
    device = body.get("device")
    if not isinstance(device, str) or not device:
        return None

    from linc_nerve.mcp import _core

    if _core._device_mgr is None:
        return None
    return _core._device_mgr.guard(device, owner=owner)


def _resolve_git_sha(repo_dir: str | None = None) -> str:
    """Resolve the git SHA of the served code.

    Returns the short HEAD SHA of the repo containing this module, or the
    string ``"unknown"`` if git metadata is unavailable (git missing, not a
    repo, or any subprocess error). Never raises — a liveness probe must not
    fail because of a missing `.git` directory.

    Args:
        repo_dir: Directory to resolve the SHA against. Defaults to the
            directory of this module so the SHA reflects the *served* code's
            repository, not the daemon's working directory.
    """
    if repo_dir is None:
        repo_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        sha = subprocess.check_output(
            ["git", "-C", repo_dir, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
        return sha or "unknown"
    except Exception:
        # git missing, not a repo, timeout, OSError, non-zero exit — degrade.
        return "unknown"


# Resolved once at process start (like _DAEMON_START_TIME) so /health stays a
# no-I/O sub-50ms liveness probe — never shell out per request. Lets an external
# monitor detect a daemon serving stale code (issue #58).
_SERVED_GIT_SHA = _resolve_git_sha()


def _resolve_package_version(dist_name: str = "linc-nerve") -> str:
    """Installed version of the served package, or ``"unknown"``.

    Never raises — a liveness probe must not fail because package metadata is
    missing (e.g. running from a raw source checkout without an install).
    """
    try:
        import importlib.metadata

        return importlib.metadata.version(dist_name)
    except Exception:
        return "unknown"


# Resolved once at process start — the value a long-lived daemon serves is the
# version of the code it actually imported, which is exactly what the
# post-merge kickstart step asserts against (issue #79: a stale daemon keeps
# serving pre-merge code until `launchctl kickstart`).
_SERVED_PACKAGE_VERSION = _resolve_package_version()

# Per-process device-enumeration cache. /devices is dispatched on the daemon's
# GLOBAL execution lane (sse_server routes every no-serial request through the
# global lane's single worker; only device-serial-targeted /tools calls get
# their own per-device lanes — issue #85), so concurrent /devices requests
# still serialize through that one worker — no thread-stampede possible. A
# cache miss runs a synchronous .discover() on each controller (~5.5s on the
# 54-device farm) and blocks the global lane's worker (not the event loop —
# /health is served inline on the loop and stays responsive).
_DEVICES_CACHE: tuple[float, dict[str, Any]] | None = None
_DEVICES_CACHE_TTL = 5.0  # seconds — matches typical monitor poll cadence


def _build_health() -> dict[str, Any]:
    """Liveness + the last cached reading of the ADB transport.

    Still NO I/O: the `adb` block is whatever the daemon's transport watchdog
    last published to `core.adb_health` (see `_run_adb_watchdog`), so this stays
    a sub-50ms inline probe. It must never call adb itself — that call is
    precisely the one that hangs.

    `ok` is no longer a constant. It goes False when the watchdog reports a
    wedged/unresponsive transport, when nerve's device set has drifted from
    adb's, or when the watchdog stops reporting at all (it is then blocked in
    the same hung adb call as every other client). Before this, `/health`
    stayed green through a 28-hour bench outage — linc-nerve#131.

    The HTTP status stays 200 in every state: `_probe_existing_daemon` decides
    port ownership from the status line alone, so a non-200 would send a second
    daemon into a bind fight with a live one. Read `ok`, not the status code.
    """
    ok, adb_block = adb_health.transport_health(
        adb_health.get_transport_status(),
        now=time.time(),
        monitor_uptime_seconds=adb_health.watchdog_uptime_seconds(),
    )
    return {
        "ok": ok,
        "version": "1.0",
        "uptime_seconds": round(time.monotonic() - _DAEMON_START_TIME, 1),
        "git_sha": _SERVED_GIT_SHA,
        "package_version": _SERVED_PACKAGE_VERSION,
        "adb": adb_block,
    }


def _describe_error(exc: BaseException) -> str:
    """Human-readable text for a swallowed discovery failure.

    Falls back to the class name: a bare ``TimeoutError()`` stringifies to the
    empty string, which would put an empty value in ``platform_errors`` and lose
    the very distinction this field exists to make.
    """
    return str(exc) or type(exc).__name__


def _build_devices() -> dict[str, Any]:
    """Device-fleet enumeration with a 5s per-process TTL cache."""
    global _DEVICES_CACHE
    now = time.monotonic()
    if _DEVICES_CACHE is not None:
        ts, cached = _DEVICES_CACHE
        if now - ts < _DEVICES_CACHE_TTL:
            return {**cached, "cache_age_seconds": round(now - ts, 2)}

    from linc_nerve.core.android import AndroidController
    from linc_nerve.core.ios import IOSController
    from linc_nerve.mcp._core import _ensure_initialized

    _ensure_initialized()
    devices: list[dict[str, Any]] = []
    platform_counts: dict[str, int] = {}
    platform_errors: dict[str, str] = {}
    for ctrl_cls, platform in [(AndroidController, "android"), (IOSController, "ios")]:
        try:
            ctrl = ctrl_cls()
            devs = ctrl.discover()
            platform_counts[platform] = len(devs)
            for d in devs:
                # Use the canonical NerveDevice serializer so this stays in
                # sync with the MCP discovery path. A hand-built dict here
                # previously dropped `state`, silently disabling crucible's
                # online filter over HTTP (improvement d07eef8f).
                devices.append(d.to_dict())
        except Exception as exc:
            # A swallowed discovery failure used to be indistinguishable from a
            # genuinely empty fleet: during the #131 outage `android: 0` was
            # reported for 28 hours by a controller that could not reach adb at
            # all. Name the failure so callers can assert on it.
            platform_counts[platform] = 0
            platform_errors[platform] = _describe_error(exc)

    body = {
        "devices": devices,
        "device_count": len(devices),
        "platform_counts": platform_counts,
        "platform_errors": platform_errors,
        "ok": not platform_errors,
        "ttl_seconds": _DEVICES_CACHE_TTL,
        "cache_age_seconds": 0.0,
    }
    # Store the END timestamp, not the call-start `now`. If enumeration
    # itself takes longer than TTL (51 iOS simulators on this fleet ≈ 6s),
    # the cache would otherwise appear "expired" the moment it's stored
    # and every call would be a miss. See e2e regression on inbox 9abb5f9d.
    _DEVICES_CACHE = (time.monotonic(), body)
    return body


def _strip_inline_sentinel(result: Any) -> Any:
    """Drop the inline-image bytes sentinel from a tool result dict.

    The FastMCP stdio wrapper pops this sentinel and re-emits it as an
    ImageContent block; this JSON path must strip it or json.dumps raises
    TypeError (bug 01c1763d). Imported lazily — the module header promises
    /health stays import-light.
    """
    if isinstance(result, dict):
        from linc_nerve.mcp._core import _INLINE_IMAGE_KEY

        result.pop(_INLINE_IMAGE_KEY, None)
    return result


def _reject_oversized(tool_name: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """413 rejection if the body exceeds the size budget, else None."""
    if not _payload_too_large(body):
        return None
    log.warning("tool_call_rejected_oversized_payload", tool=tool_name)
    return {
        "status": 413,
        "body": {"error": f"Request body exceeds {_MAX_BODY_BYTES} byte budget"},
    }


def _reject_bad_path(tool_name: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """400 rejection if a path-shaped field contains a traversal/NUL probe, else None."""
    if not _contains_traversal(body):
        return None
    log.warning("tool_call_rejected_path_traversal", tool=tool_name)
    return {
        "status": 400,
        "body": {"error": "Request body contains a disallowed path sequence"},
    }


def _reject_shell_metacharacters(tool_name: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """400 rejection if a shell-guarded field isn't a safe identifier, else None."""
    if not _contains_shell_metacharacters(body):
        return None
    log.warning("tool_call_rejected_shell_metacharacter", tool=tool_name)
    return {
        "status": 400,
        "body": {"error": "Request body contains a disallowed shell metacharacter"},
    }


def _reject_bad_extras_key(tool_name: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """400 rejection if an intent-extras key isn't a safe identifier, else None."""
    if not _contains_bad_extras_key(body):
        return None
    log.warning("tool_call_rejected_bad_extras_key", tool=tool_name)
    return {
        "status": 400,
        "body": {"error": "Request body contains a disallowed intent-extras key"},
    }


def _reject_bad_filter(tool_name: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """400 rejection if a log-filter field doesn't match the safe grammar, else None."""
    if not _contains_bad_filter(body):
        return None
    log.warning("tool_call_rejected_bad_filter", tool=tool_name)
    return {
        "status": 400,
        "body": {"error": "Request body contains a disallowed filter value"},
    }


def _reject_bad_sensor_type(tool_name: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """400 rejection if `sensor_type` is present but not an allowlisted emulator
    sensor name, else None (security review M1 — the value is written to the
    authenticated emulator console, where a line break runs a second verb)."""
    if "sensor_type" not in body or is_allowed_sensor_type(body["sensor_type"]):
        return None
    log.warning("tool_call_rejected_bad_sensor_type", tool=tool_name)
    return {
        "status": 400,
        "body": {"error": "Request body contains a disallowed sensor_type value"},
    }


def _check_reservation_conflict(tool_name: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """409 rejection if the target device is reserved by another session."""
    conflict = _reservation_conflict(tool_name, body)
    if not conflict:
        return None
    log.warning("tool_call_rejected_reservation_conflict", tool=tool_name)
    return {"status": 409, "body": {"error": conflict}}


# Ordered pre-dispatch guards (linc-nerve#128) — reject hostile/malformed
# requests before any tool code or device I/O runs. Shared by both HTTP
# entry points that reach a tool/device: POST /tools/* (via route_request)
# AND GET /stream + GET /tools/nerve_logcat_stream (via sse_server calling
# `_run_pre_dispatch_guards` directly — finding 4, those routes used to
# bypass every guard here).
_PRE_DISPATCH_GUARDS = (
    _reject_oversized,
    _reject_bad_path,
    _reject_shell_metacharacters,
    _reject_bad_extras_key,
    _reject_bad_filter,
    _reject_bad_sensor_type,
    _check_reservation_conflict,
)


def _run_pre_dispatch_guards(tool_name: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """Run each guard in order; return the first rejection, else None.

    Public-ish (imported directly by `linc_nerve.mcp.sse_server` for the
    `/stream` routes, which don't go through `route_request`'s registry
    dispatch at all — see the module-level comment above).
    """
    for guard in _PRE_DISPATCH_GUARDS:
        rejection = guard(tool_name, body)
        if rejection is not None:
            return rejection
    return None


# Exception type -> HTTP status for the dispatch error mapper below. Order
# matters: TypeError is checked first (bad call signature), then the
# client-input family, then anything else falls through to 500.
def _error_status_and_body(tool_name: str, exc: Exception) -> tuple[int, dict[str, Any]]:
    """Map a dispatch-time exception to an HTTP status + JSON body."""
    if isinstance(exc, TypeError):
        return 400, {"error": f"Invalid parameters: {exc}"}
    if isinstance(exc, (ValueError, KeyError, LookupError)):
        # Client-input errors (bad/unknown identifiers, e.g. an unknown
        # device serial) — reject 400, don't mask as a server crash.
        return 400, {"error": str(exc)}
    log.error("tool_call_error", tool=tool_name, error=str(exc))
    return 500, {"error": str(exc)}


# Typed-parameter coercion (audit 2026-09-30 F1, spec linc-nerve-df3 / bead
# linc-nerve-df3.1). The stdio path gets pydantic validation from FastMCP for
# free; :8111 forwards the raw JSON body into `registry[tool](**body)`, so a
# string like "1 MARKER" in an `x: int` param used to reach `input tap` on the
# device shell. Coerce every int/bool/float-annotated param from its JSON form
# and refuse anything else with a 400 — str and every other annotation (dicts,
# unions, Any) pass through untouched.
_INT_STRING_RE = re.compile(r"-?[0-9]+")
_DECIMAL_STRING_RE = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_BOOL_STRINGS = {"true": True, "false": False}
_INVALID = object()  # sentinel: value is not coercible to the annotated type

# The registry is static, so resolve each tool's hints once.
_TYPE_HINT_CACHE: dict[Callable[..., Any], dict[str, Any]] = {}

_JSON_TYPE_NAMES: dict[type, str] = {
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
    list: "array",
    dict: "object",
    type(None): "null",
}


def _type_hints_for(fn: Callable[..., Any]) -> dict[str, Any]:
    """`typing.get_type_hints(fn)`, cached; unresolvable hints → no coercion."""
    if fn not in _TYPE_HINT_CACHE:
        try:
            _TYPE_HINT_CACHE[fn] = typing.get_type_hints(fn)
        except Exception:  # a bad forward ref must not 500 the call
            _TYPE_HINT_CACHE[fn] = {}
    return _TYPE_HINT_CACHE[fn]


def _is_real_number(value: Any) -> bool:
    """int/float but not bool (bool is an int subclass in Python, not in JSON)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _coerce_int(value: Any) -> Any:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and _INT_STRING_RE.fullmatch(value):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)  # pydantic lax-mode parity: 540.0 → 540, 1.5 → rejected
    return _INVALID


def _coerce_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    return _BOOL_STRINGS.get(value, _INVALID) if isinstance(value, str) else _INVALID


def _coerce_float(value: Any) -> Any:
    if _is_real_number(value):
        return value
    if isinstance(value, str) and _DECIMAL_STRING_RE.fullmatch(value):
        return float(value)
    return _INVALID


_COERCERS: dict[Any, Callable[[Any], Any]] = {
    int: _coerce_int,
    bool: _coerce_bool,
    float: _coerce_float,
}


def _json_type_name(value: Any) -> str:
    return _JSON_TYPE_NAMES.get(type(value), type(value).__name__)


def _coerce_typed_params(fn: Callable[..., Any], body: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce int/bool/float-annotated params of `fn` in `body`, in place.

    Returns None when every typed param is (now) the annotated type, else a
    400 rejection naming the FIRST bad field.
    """
    hints = _type_hints_for(fn)
    for name, value in body.items():
        coerce = _COERCERS.get(hints.get(name))
        coerced = value if coerce is None else coerce(value)
        if coerced is _INVALID:
            expected = hints[name].__name__
            error = f"Invalid parameters: {name} must be {expected}, got {_json_type_name(value)}"
            return {"status": 400, "body": {"error": error}}
        body[name] = coerced
    return None


def _reject_bad_param_types(
    tool_name: str, body: dict[str, Any], fn: Callable[..., Any], t0: float
) -> dict[str, Any] | None:
    """Run `_coerce_typed_params`; on rejection log it like the TypeError path."""
    rejection = _coerce_typed_params(fn, body)
    if rejection is not None:
        error = rejection["body"]["error"]
        log.warning("tool_call_rejected_bad_type", tool=tool_name, error=error)
        duration_ms = (time.monotonic() - t0) * 1000
        log_mcp_call(
            "linc-nerve", tool_name, _rejected_log_params(body), duration_ms, False, error
        )
    return rejection


def _rejected_log_params(body: dict[str, Any]) -> dict[str, Any]:
    """Params to usage-log for a type-rejected call: a ``secret`` flag that is not a
    bool ("yes", 1 — which MCP would honour as true) is logged as True, so
    NERVE_USAGE_LOG=full still redacts the text the caller meant to keep secret.
    """
    if "secret" in body and not isinstance(body["secret"], bool):
        # "secret" is the boolean redaction flag, not a credential (bandit B105).
        return {**body, "secret": True}  # nosec B105
    return body


def _dispatch_tool(
    tool_name: str,
    body: dict[str, Any],
    registry: dict[str, Callable[..., Any]],
) -> dict[str, Any]:
    """Call the registered tool and translate the outcome into a response.

    Logs the call (success or failure) and maps any raised exception to an
    HTTP status/body pair via `_error_status_and_body`.
    """
    t0 = time.monotonic()
    rejection = _reject_bad_param_types(tool_name, body, registry[tool_name], t0)
    if rejection is not None:
        return rejection
    try:
        result = _strip_inline_sentinel(registry[tool_name](**body))
        duration_ms = (time.monotonic() - t0) * 1000
        log_mcp_call("linc-nerve", tool_name, body, duration_ms, True)
        return {"status": 200, "body": {"result": result}}
    except Exception as e:
        duration_ms = (time.monotonic() - t0) * 1000
        log_mcp_call("linc-nerve", tool_name, body, duration_ms, False, str(e))
        status, error_body = _error_status_and_body(tool_name, e)
        return {"status": status, "body": error_body}


def route_request(
    method: str,
    path: str,
    body: dict[str, Any],
    registry: dict[str, Callable[..., Any]],
) -> dict[str, Any]:
    """Route an HTTP request to the appropriate handler.

    Args:
        method: HTTP method (GET, POST)
        path: URL path (e.g., /tools/nerve_screenshot, /health, /devices)
        body: Parsed JSON body (empty dict for GET)
        registry: Map of tool_name → callable

    Returns:
        Dict with "status" (int) and "body" (dict) for JSON serialization.
    """
    if path == "/health" and method == "GET":
        return {"status": 200, "body": _build_health()}

    if path == "/devices" and method == "GET":
        return {"status": 200, "body": _build_devices()}

    if path.startswith("/tools/") and method == "POST":
        tool_name = path[len("/tools/") :]
        if tool_name not in registry:
            return {
                "status": 404,
                "body": {"error": f"Unknown tool: {tool_name}"},
            }

        rejection = _run_pre_dispatch_guards(tool_name, body)
        if rejection is not None:
            return rejection

        return _dispatch_tool(tool_name, body, registry)

    return {"status": 404, "body": {"error": f"Not found: {method} {path}"}}
