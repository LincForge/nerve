"""Device-state tools — Doze, App-Standby, battery-saver, screen power control.

These are the adversarial-condition controls a device-truth benchmark (linc-bench)
needs to drive: force/exit Doze, set App-Standby buckets, toggle battery-saver, and
control the screen. Every action maps to a *fixed, parameterized* shell string — the
only injected values are ``package`` (regex-validated) and ``bucket`` (enum-validated).
There is deliberately no free-form command surface.

Kept separate from ``input_tools`` because these are device *state* mutations, not
input events, and because callers (benchmarks, crucible BVTs) want to verify the
transition actually took via the read-back actions (``*_status``/``*_get``/
``whitelist_check``) rather than fire-and-forget.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from linc_nerve.mcp import _core
from linc_nerve.mcp._core import device_tool

# App-Standby bucket name -> integer value (UsageStatsManager.STANDBY_BUCKET_*).
_STANDBY_BUCKETS: dict[str, int] = {
    "active": 10,
    "working_set": 20,
    "frequent": 30,
    "rare": 40,
    "restricted": 45,
}
_STANDBY_BY_VALUE: dict[int, str] = {v: k for k, v in _STANDBY_BUCKETS.items()}

# Anchored ``\A...\Z`` + ``fullmatch`` (security review 2026-10-02 H1): ``^...$``
# with ``re.match`` accepts a trailing newline.
_PACKAGE_RE = re.compile(r"\A[A-Za-z0-9._]+\Z")

# Actions that require a validated package.
_PACKAGE_ACTIONS = frozenset({"standby_set", "standby_get", "whitelist_remove", "whitelist_check"})

# Static command table (no injected values).
_STATIC_COMMANDS: dict[str, str] = {
    "doze_force": "dumpsys deviceidle force-idle",
    "doze_unforce": "dumpsys deviceidle unforce",
    "doze_status": "dumpsys deviceidle get deep",
    "battery_unplug": "dumpsys battery unplug",
    "battery_reset": "dumpsys battery reset",
    "power_save_on": "settings put global low_power 1",
    "power_save_off": "settings put global low_power 0",
    "power_save_status": "settings get global low_power",
    "screen_on": "input keyevent 224",  # KEYCODE_WAKEUP
    "screen_off": "input keyevent 223",  # KEYCODE_SLEEP
    "screen_status": "dumpsys power | grep 'mWakefulness='",
}

# Ordered composite: leave the device clean (undo everything, restore screen).
_RESET_STEPS: tuple[str, ...] = (
    "battery_reset",
    "doze_unforce",
    "power_save_off",
    "screen_on",
)

_ALL_ACTIONS = frozenset(_STATIC_COMMANDS) | _PACKAGE_ACTIONS | {"reset"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_package(package: str) -> str | None:
    """Return an error string if ``package`` is not a safe Android package id."""
    if not package or not _PACKAGE_RE.fullmatch(package):
        return f"Invalid package '{package}': must match [A-Za-z0-9._]+"
    return None


def _validate_bucket(bucket: str) -> str | None:
    """Return an error string if ``bucket`` is not a known App-Standby bucket."""
    if bucket not in _STANDBY_BUCKETS:
        return f"Invalid bucket '{bucket}': must be one of {sorted(_STANDBY_BUCKETS)}"
    return None


# ---------------------------------------------------------------------------
# Command mapping
# ---------------------------------------------------------------------------


def _command_for(action: str, package: str, bucket: str) -> str:
    """Map an action to its fixed shell command. Raises ValueError on unknown action.

    Callers MUST validate ``package``/``bucket`` before calling this for
    package/bucket actions; interpolation here assumes already-validated input.
    """
    if action in _STATIC_COMMANDS:
        return _STATIC_COMMANDS[action]
    if action == "standby_set":
        return f"am set-standby-bucket {shlex.quote(package)} {shlex.quote(bucket)}"
    if action == "standby_get":
        return f"am get-standby-bucket {shlex.quote(package)}"
    if action == "whitelist_remove":
        return f"dumpsys deviceidle whitelist {shlex.quote('-' + package)}"
    if action == "whitelist_check":
        return "dumpsys deviceidle whitelist"
    raise ValueError(f"Unknown device_state action: {action}")


# ---------------------------------------------------------------------------
# Parsers — each returns (state, parsed_ok); a miss is never coerced to a default
# ---------------------------------------------------------------------------

_DOZE_STATES = ("active", "idle_maintenance", "idle_pending", "sensing", "locating", "idle")


def _parse_doze_status(raw: str) -> tuple[str | None, bool]:
    """Parse `dumpsys deviceidle get deep` output (a single state token)."""
    token = raw.strip().lower()
    # Longest-match first so IDLE_MAINTENANCE doesn't match "idle".
    for state in _DOZE_STATES:
        if token == state:
            return state, True
    return None, False


def _parse_power_save(raw: str) -> tuple[bool | None, bool]:
    """Parse `settings get global low_power` — '1' on, '0'/'null' off."""
    token = raw.strip().lower()
    if token == "1":  # nosec B105 — "1" is the low_power settings value, not a password
        return True, True
    if token in ("0", "null"):
        return False, True
    return None, False


def _parse_standby(raw: str) -> tuple[str | None, bool]:
    """Parse `am get-standby-bucket` — an integer bucket value."""
    token = raw.strip()
    try:
        value = int(token)
    except (ValueError, TypeError):
        return None, False
    name = _STANDBY_BY_VALUE.get(value)
    if name is None:
        return None, False
    return name, True


def _parse_whitelist(raw: str, package: str) -> tuple[bool | None, bool]:
    """Parse `dumpsys deviceidle whitelist` — is ``package`` present?"""
    lines = {line.strip() for line in raw.splitlines() if line.strip()}
    return (package in lines), True


# Which parser (if any) interprets each action's output.
_PARSERS = {
    "doze_status": ("_parse_doze_status", "state"),
    "power_save_status": ("_parse_power_save", "power_save"),
    "standby_get": ("_parse_standby", "bucket"),
}


# ---------------------------------------------------------------------------
# Sync dispatcher
# ---------------------------------------------------------------------------


@device_tool
def _nerve_device_state_sync(
    device: str,
    action: str,
    package: str = "",
    bucket: str = "",
) -> dict[str, Any]:
    """Control Android device power/idle/standby state for adversarial testing.

    USE FOR: doze (doze_force + status), App-Standby buckets, battery-saver,
        the Doze whitelist, battery_unplug, screen power, composite reset.
    NOT FOR: hardware keys (nerve_key); typing (nerve_type); process/crash
        state (nerve_inspect); iOS (Android only).
    TIP: follow a mutation with its read-back — emulators report charging,
        so doze_force no-ops without battery_unplug.

    Args:
        device: Device serial. Android only.
        action: doze_force/doze_unforce/doze_status, battery_unplug/
            battery_reset, whitelist_remove/whitelist_check, standby_set/get,
            power_save_on/off/status, screen_on/off/status, reset.
        package: Package id (standby_* and whitelist_*).
        bucket: standby_set bucket: active..restricted.

    Returns:
        Dict with success/action/device (+ state/parsed_ok for read-backs).
    """
    if action not in _ALL_ACTIONS:
        return {
            "error": f"Unknown action '{action}'. Valid: {sorted(_ALL_ACTIONS)}",
            "validation_error": True,
        }

    # Validate injected values BEFORE resolving/touching the device.
    if action in _PACKAGE_ACTIONS:
        err = _validate_package(package)
        if err:
            return {"error": err, "validation_error": True}
    if action == "standby_set":
        err = _validate_bucket(bucket)
        if err:
            return {"error": err, "validation_error": True}

    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}
    if platform != "android":
        return {"error": "nerve_device_state supports Android devices only"}

    guard = _core._check_guard(device)
    if guard:
        return guard

    ctrl = _core._get_controller(platform)

    if action == "reset":
        return _run_reset(ctrl, device)

    cmd = _command_for(action, package, bucket)
    try:
        out = ctrl._shell(device, cmd, timeout=20)
    except Exception as exc:
        return {"error": f"device_state {action} failed: {exc}"}

    result: dict[str, Any] = {"success": True, "action": action, "device": device}

    if action in _PARSERS:
        parser_name, key = _PARSERS[action]
        parsed, ok = globals()[parser_name](out)
        result[key] = parsed
        result["state"] = parsed  # uniform "state" alias for any read-back
        result["parsed_ok"] = ok
    elif action == "whitelist_check":
        present, ok = _parse_whitelist(out, package)
        result["whitelisted"] = present
        result["state"] = present
        result["parsed_ok"] = ok
    else:
        result["raw"] = out.strip()[:200]

    return result


def _run_reset(ctrl: Any, device: str) -> dict[str, Any]:
    """Composite: undo battery/doze/power-save and restore the screen."""
    ran: list[str] = []
    errors: list[str] = []
    for step in _RESET_STEPS:
        cmd = _STATIC_COMMANDS[step]
        try:
            ctrl._shell(device, cmd, timeout=20)
            ran.append(step)
        except Exception as exc:  # keep going — best-effort cleanup
            errors.append(f"{step}: {exc}")
    result: dict[str, Any] = {
        "success": not errors,
        "action": "reset",
        "device": device,
        "steps": ran,
    }
    if errors:
        result["errors"] = errors
    return result
