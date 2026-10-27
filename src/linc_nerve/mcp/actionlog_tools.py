"""Action-log tools — start/stop a per-device JSONL log of every device-tool call,
and replay one.

The session registry is ``linc_nerve.core.action_log.REGISTRY``; the logging
itself happens in ``_core.device_tool``'s sync wrapper, so start/stop only
open and close sessions (and are never logged themselves). nerve_replay runs
a log through ``core/replay.py`` and dispatches each step through the
registered (wrapped) device tools, so wrapper hooks apply to replayed steps.
"""

from __future__ import annotations

import importlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from linc_nerve.core import action_log, observability, replay
from linc_nerve.mcp import _core
from linc_nerve.mcp._core import device_tool

# Process environment seam for nerve_replay's secrets_env (tests patch this).
_environ: Mapping[str, str] = os.environ


def _validation_error(message: str) -> dict[str, Any]:
    return {"error": message, "validation_error": True}


def _parse_meta(meta_json: str) -> dict[str, Any] | None:
    """A JSON object from *meta_json*, or None when it is not one."""
    try:
        meta = json.loads(meta_json or "{}")
    except ValueError:
        return None
    return meta if isinstance(meta, dict) else None


@device_tool
def _nerve_actionlog_start_sync(
    device: str,
    path: str,
    target_package: str = "",
    meta_json: str = "{}",
    allow_packages: str = "",
    deny_words: str = "",
) -> dict[str, Any]:
    """Start an action log — a JSONL record of every nerve tool call on a device.

    Opens ``path`` (append mode, parents created, new files 0600) and writes a
    header line. Until nerve_actionlog_stop, every device-tool call on this
    serial appends one line: tool, args, seq, t_session_s, t_rec_s (seconds
    into the active nerve_record_start recording, or null), duration_ms,
    ok/error, ``readonly`` for perception tools, and for nerve_tap /
    nerve_long_press the ``target`` element under the point.

    USE FOR: recording what an agent did during an exploratory or bug-repro
        run, lining actions up with a screen recording, replay/minimisation
        input, an audit trail of taps, swipes, typing and keys.
    NOT FOR: video capture (nerve_record_start), device logs (nerve_logcat),
        usage analytics across all calls (nerve_usage_stats).
    TIP: start the log, then nerve_record_start, so t_rec_s is set on every
        action. Type passwords with nerve_type secret=true — the log stores
        only their length. One log per device; the session lives in the nerve
        daemon process and is lost if the daemon restarts.

    GUARD: with target_package set the session is guarded. nerve_tap,
        nerve_long_press, nerve_swipe, nerve_type and nerve_key refuse while
        the foreground app is not the target, an allow_packages entry or a
        system permission/installer dialog (nerve_key BACK/HOME always run).
        Taps on an element labelled with a deny word (buy, subscribe, send,
        delete account, ...) are refused, as are a swipe shorter than 48 px
        or one that ends inside (or within 48 px of) the clickable element
        holding its start (a click), a longer swipe whose both ends lie on a
        non-scrollable deny-labelled control (slide to buy), and ENTER /
        DPAD_CENTER / SPACE (any spelling, incl. numeric codes and gamepad
        buttons that fall back to them) on a focused deny-labelled element;
        nerve_type text with a newline, or a space outside a focused text
        field, counts as that key. An activation key with nothing focused is
        refused. nerve_launch, nerve_uninstall,
        nerve_clear_app_data and nerve_grant_permission refuse other
        packages. A refusal returns {"error", "guard", "detail"}, is logged as
        a "guard" line and counted in nerve_actionlog_stop's guard_refusals.
        An unreadable foreground, or an unreadable, empty or truncated
        element tree, is refused (fail closed). The guard is a safety rail
        for agents, NOT a security boundary: nerve_actionlog_stop and
        nerve_install are unguarded, iOS skips the foreground check, a second
        serial for the same device (USB vs wireless) is not guarded,
        resource ids are not matched, UI without an accessibility tree
        (WebView, Flutter without semantics) has no labels to check, ENTER
        in a chat composer fires its IME send action, and lookalike
        characters (a Cyrillic B in "Buy", U+2800 / NBSP inside a word)
        evade the label match. A swipe-to-confirm control whose label lacks
        a deny word ("Swipe up to submit"), and a swipe that leaves the
        control, are not caught.

    Args:
        device: Device serial.
        path: Host path of the JSONL file to append to (must end in .jsonl,
            under $LINC_HOME/nerve/actionlogs or a NERVE_ACTIONLOG_ROOTS
            directory; a relative path is taken under the first root).
        target_package: App under test; recorded in the header meta. Setting
            it turns the guard on.
        meta_json: Extra header metadata as a JSON object string.
        allow_packages: Comma list of extra packages a guarded session may
            act in (e.g. a login provider).
        deny_words: Comma list of tap labels to refuse; replaces the default
            list when non-empty (call again with the same path and
            target_package to re-arm in place).

    Returns:
        Dict with success/serial/path/started_at (plus guarded/
        allowed_packages when guarded), or {"error": ...}.
    """
    roots = action_log.actionlog_roots(_environ)
    resolved = action_log.resolve_log_path(path, roots)
    if isinstance(resolved, str):
        return _validation_error(resolved)
    default_root = observability.linc_home() / "nerve" / "actionlogs"
    if default_root in roots and not default_root.exists():
        default_root.mkdir(parents=True, exist_ok=True)
        default_root.chmod(0o700)
    meta = _parse_meta(meta_json)
    if meta is None:
        return _validation_error("meta_json must be a JSON object string")
    if target_package:
        meta["target_package"] = target_package
    guard = action_log.build_guard(target_package, allow_packages, deny_words)
    try:
        info = action_log.REGISTRY.start(device, resolved, meta, guard)
    except action_log.ActionLogError as exc:
        return {"error": str(exc), "hint": action_log.ALREADY_ACTIVE_HINT}
    except OSError as exc:
        return {"error": f"Cannot open action log: {exc}"}
    return {"success": True, "action": "actionlog_start", **info, **_guard_info(guard)}


def _guard_info(guard: action_log.GuardConfig | None) -> dict[str, Any]:
    """Extra start-result keys for a guarded session (none when unguarded)."""
    if guard is None:
        return {}
    return {
        "guarded": True,
        "target_package": guard.target_package,
        "allowed_packages": sorted(guard.allowed_packages),
        "deny_words": list(guard.deny_words),
    }


@device_tool
def _nerve_actionlog_stop_sync(device: str) -> dict[str, Any]:
    """Stop the action log on a device and report the file and entry count.

    USE FOR: closing the JSONL log opened by nerve_actionlog_start, getting
        the log's path and how many actions it recorded.
    NOT FOR: stopping a screen recording (nerve_record_stop) or a mirror
        stream (nerve_stream_stop).
    TIP: every line is flushed as it is written, so the file is complete
        even without a stop. Errors if no action log is active on the device.

    Args:
        device: Device serial.

    Returns:
        Dict with success/serial/path/entries, or {"error": ...}.
    """
    try:
        info = action_log.REGISTRY.stop(device)
    except action_log.ActionLogError as exc:
        return {"error": str(exc)}
    return {"success": True, "action": "actionlog_stop", **info}


# ---------------------------------------------------------------------------
# Replay (linc-nerve-3me)
# ---------------------------------------------------------------------------


def _registered_device_tool(tool: str) -> Any:
    """The registered (wrapped) sync function of device tool *tool*, or None."""
    info = _core.TOOL_REGISTRY.get(tool)
    if info is None or info["kind"] != "device":
        return None
    return getattr(importlib.import_module(info["module"]), f"_{tool}_sync", None)


def _tool_dispatch(device: str) -> replay.Dispatch:
    """Replay dispatch through the registered device tools (wrapper hooks apply)."""

    def dispatch(tool: str, args: dict[str, Any]) -> Any:
        fn = _registered_device_tool(tool) if tool in replay.REPLAYABLE_TOOLS else None
        if fn is None:
            return {"error": f"{tool} is not replayable"}
        return fn(device, **args)

    return dispatch


# O_NONBLOCK so opening a FIFO named *.jsonl returns at once (then fails the
# regular-file check) instead of blocking the tool worker forever.
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW


def _too_big(limit: int) -> str:
    return f"action log is larger than {limit} bytes (the replay limit)"


def _read_regular(fd: int, path: Path, limit: int) -> bytes | str:
    """Up to *limit* + 1 bytes of the open regular file *fd*, or a message."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        return f"Cannot read action log: not a regular file: {path}"
    if info.st_size > limit:
        return _too_big(limit)
    with os.fdopen(fd, "rb", closefd=False) as handle:
        return handle.read(limit + 1)


def _read_log_bytes(path: Path) -> bytes | str:
    """The bytes of a regular-file log (at most replay.MAX_LOG_BYTES), or a message."""
    limit = replay.MAX_LOG_BYTES
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except FileNotFoundError:
        return f"action log not found: {path}"
    except OSError as exc:
        return f"Cannot read action log: {exc}"
    try:
        data = _read_regular(fd, path, limit)
    except OSError as exc:
        return f"Cannot read action log: {exc}"
    finally:
        os.close(fd)
    return _too_big(limit) if len(data) > limit else data


def _parse_lines(text: str) -> list[tuple[int, Any]] | str:
    """(line number, parsed JSON) for each non-blank line, or a message."""
    entries: list[tuple[int, Any]] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            entries.append((number, json.loads(line)))
        except (ValueError, RecursionError):
            return f"action log line {number} is not JSON"
    return entries


def _load_log(path: Path | str) -> list[tuple[int, Any]] | str:
    """Numbered, parsed JSONL lines of an action log, or a validation message."""
    resolved = action_log.resolve_log_path(str(path), action_log.actionlog_roots(_environ))
    if isinstance(resolved, str):
        return resolved
    data = _read_log_bytes(resolved)
    if isinstance(data, str):
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "action log is not UTF-8 text"
    return _parse_lines(text)


def _plan(log_path: str, secrets_env: str) -> tuple[list[replay.Step], dict[str, str]] | str:
    """Planned steps + resolved secrets, or a validation message."""
    lines = _load_log(log_path)
    if isinstance(lines, str):
        return lines
    try:
        secrets = replay.parse_secrets_env(secrets_env, _environ)
        steps = replay.plan_lines(lines)
    except ValueError as exc:
        return str(exc)
    if len(steps) > replay.MAX_STEPS:
        return f"action log has {len(steps)} replayable steps; the limit is {replay.MAX_STEPS}"
    return steps, secrets


def _replay_inputs(
    log_path: str, step_timeout_s: float, secrets_env: str
) -> tuple[list[replay.Step], dict[str, str]] | dict[str, Any]:
    """Planned steps + resolved secrets, or a validation-error dict."""
    if not replay.valid_step_timeout(step_timeout_s):
        limit = f"{replay.MAX_STEP_TIMEOUT_S:g}"
        return _validation_error(f"step_timeout_s must be a finite number > 0 and <= {limit}")
    planned = _plan(log_path, secrets_env)
    if isinstance(planned, str):
        return _validation_error(planned)
    return planned


@device_tool
def _nerve_replay_sync(
    device: str,
    log_path: str,
    step_timeout_s: float = 10.0,
    secrets_env: str = "",
) -> dict[str, Any]:
    """Replay an action log on a device and report whether its steps ran.

    Re-runs the successful, state-changing actions of a nerve_actionlog_start
    JSONL log (taps, long presses, swipes, typing, keys, launches) in order.
    A tap or long press with a recorded target element waits (up to
    step_timeout_s) for that element — matched by resource_id+text, then
    resource_id, text or content description — and taps its centre, so the
    replay survives layout shifts; if a text/resource_id target never shows
    up the run diverges there instead of tapping stale coordinates.

    USE FOR: reproducing a bug from a recorded run, re-running an agent's
        exploratory session N times to check flakiness, regression-checking
        a recorded flow, minimising a repro (edit the log, replay again).
    NOT FOR: deciding whether the bug happened (your oracle decides; nerve
        only reports completed vs diverged), recording a log
        (nerve_actionlog_start), single taps (nerve_tap).
    TIP: secret nerve_type steps are redacted in the log — pass
        secrets_env="<seq>=NERVE_SECRET_<NAME>,..." naming environment
        variables of the nerve process that hold them (only NERVE_SECRET_*
        names are accepted); a missing one diverges with "missing secret".
        Only taps, long presses, swipes, typing, keys and launches are
        replayed. Steps run through the normal device tools, so an active
        action log records each replayed step (not the replay call) and any
        device-tool guard applies. Gaps between steps are replayed, capped
        at 3 s. A whole run is capped at 75 s — inside the 90 s MCP call
        timeout — and then diverges with "replay exceeded its 75 s budget",
        so no step runs after the call returns.

    Args:
        device: Device serial.
        log_path: Host path of the action-log JSONL file to replay (must end in
            .jsonl, under $LINC_HOME/nerve/actionlogs or a NERVE_ACTIONLOG_ROOTS
            directory; a relative path is taken under the first root).
        step_timeout_s: Seconds to wait for each located element (0 < s <= 120).
        secrets_env: Comma list of seq=NERVE_SECRET_* pairs for secret type steps.

    Returns:
        Dict with outcome ("completed" | "diverged"), steps_total, steps_done,
        diverged_at (step index or null), reason and duration_s, or
        {"error": ...} (validation_error true for a bad log/argument).
    """
    inputs = _replay_inputs(log_path, step_timeout_s, secrets_env)
    if isinstance(inputs, dict):
        return inputs
    steps, secrets = inputs
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}
    guard = _core._check_guard(device)
    if guard:
        return guard
    result = replay.replay(
        _core._get_controller(platform),
        device,
        steps,
        step_timeout_s=step_timeout_s,
        secrets=secrets,
        clock=replay._clock,
        sleep=replay._sleep,
        dispatch=_tool_dispatch(device),
    )
    return dict(result)
