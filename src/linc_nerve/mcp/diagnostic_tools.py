"""Diagnostic tools — logcat, metrics, session stats, inspect."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from datetime import UTC, datetime
from typing import Any

from linc_nerve.core.android import AndroidController
from linc_nerve.core.android_apps import _validate_shell_identifier
from linc_nerve.core.contracts import require
from linc_nerve.core.inspect_helpers import (
    _detect_crashes,
    _parse_activity_state,
    _parse_meminfo,
    _parse_pids,
    build_crash_signature,
)
from linc_nerve.mcp import _core
from linc_nerve.mcp._core import device_tool, global_tool

# Max logcat lines to return inline.  Larger dumps go to a file.
_LOGCAT_INLINE_MAX = 100


def _process_log_lines(device: str, platform: str, log_lines: list[str]) -> dict[str, Any]:
    if len(log_lines) > _LOGCAT_INLINE_MAX:
        path = _core._next_output_path(device, "logcat", ext="txt")
        with open(path, "w") as f:
            f.write("\n".join(log_lines))
        return {
            "logcat": path,
            "count": len(log_lines),
            "device": device,
            "platform": platform,
            "hint": "Large output saved to file. Use Read tool to view.",
        }
    return {
        "lines": log_lines,
        "count": len(log_lines),
        "device": device,
        "platform": platform,
    }


@device_tool
def _nerve_logcat_sync(device: str, lines: int = 50, tag: str = "") -> dict[str, Any]:
    """Tail the last N lines from a device's logcat buffer or iOS system log.

    USE FOR: Tailing recent log lines, runtime warnings, errors, and exceptions
        emitted by apps. Returns a static one-shot snapshot of the most recent
        log entries from the device's main logcat buffer. Tail logs, dump
        recent log output, read logcat messages from the buffer.
    NOT FOR: pushing logs through a subscription channel (nerve_stream_logs).
        Reading a process's PID, memory map, or crash signature (nerve_inspect).
        Stepping through code with breakpoints (spectra_attach).

    TIP: returns ~50-5000 tokens (varies by line count). Large dumps are saved to a file.

    Args:
        device: Device serial (Android or iOS).
        lines: Number of recent lines to return (default 50).
        tag: Optional logcat tag (Android) or process name / bundle ID (iOS) filter.

    Returns:
        A dictionary containing the log lines from the device's buffer.
    """
    android, _ = _core._ensure_initialized()
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    if platform != "android":
        # iOS log support via simctl/devicectl
        _, ios = _core._ensure_initialized()
        try:
            output = ios.get_logs(device, lines=lines, filter=tag)
            log_lines = output.strip().splitlines()[-lines:] if output.strip() else []
            return _process_log_lines(device, "ios", log_lines)
        except ValueError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"iOS log retrieval failed: {exc}"}

    try:
        output = android.logcat(device, lines=lines, tag=tag)
        log_lines = output.strip().splitlines()[-lines:] if output.strip() else []
        return _process_log_lines(device, "android", log_lines)
    except Exception as exc:
        return {"error": f"Logcat failed: {exc}"}


@device_tool
def _nerve_stream_logs_sync(device: str, filter_str: str = "") -> dict[str, Any]:
    """Get the SSE subscription URL for continuous real-time log streaming.

    USE FOR: Continuous monitoring of app logs during interactions (OODA Observe step).
    NOT FOR: Static log dumps (use nerve_logcat instead).
    TIP: Connect an HTTP client to the returned URL to receive Server-Sent Events (SSE)
    such as '{"event": "log_error", "data": "Exception in MainActivity"}'.

    Args:
        device: Device serial (Android or iOS).
        filter_str: Optional logcat tag (Android) or process name / bundle ID (iOS) filter.
    """
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    import urllib.parse

    from linc_nerve.mcp.sse_server import SSE_PORT

    query = {"device": device}
    if filter_str:
        query["filter"] = filter_str

    url = f"http://127.0.0.1:{SSE_PORT}/stream?{urllib.parse.urlencode(query)}"

    return {
        "url": url,
        "protocol": "Server-Sent Events (SSE)",
        "hint": "Connect via HTTP GET to receive continuous log pushes. "
        "Disconnect to stop the stream.",
    }


@global_tool
def _nerve_session_stats_sync() -> dict[str, Any]:
    """Get state tracking statistics for all devices.

    USE FOR: Monitoring delta detection efficiency, seeing token savings.
    NOT FOR: Device discovery (use nerve_discover).
    TIP: Shows total screenshots vs unchanged count per device.
    """
    if _core._state_mgr is None:
        return {"sessions": {}, "count": 0}
    sessions = _core._state_mgr.active_sessions
    return {"sessions": sessions, "count": len(sessions)}


@global_tool
def _nerve_metrics_sync(
    command: str = "", last_n: int = 20, device: str = "", package: str = ""
) -> dict[str, Any]:
    """Performance metrics for nerve commands.

    USE FOR: Profiling command latency, identifying slow operations, debugging
    ADB issues.
    NOT FOR: Device discovery or control.
    TIP: Call with no args for overall stats. Filter by command name for
    specifics, or by device serial to scope to a single device.

    Args:
        command: Optional command name filter (e.g., "screenshot", "input").
        last_n: Number of recent metrics to include (default 20).
        device: Optional device serial to scope command-level metrics to a
            single device. Tool/reservation/launch counters remain global.
        package: Optional app package name (forward-compatibility for callers
            scoping metrics to an app session).
    """
    if _core._metrics is None:
        _core._ensure_initialized()
    metrics = require(_core._metrics, "metrics collector")

    # Empty device string = no filter (handled by the metrics layer), so pass it
    # straight through — no None-coercion branch needed here.
    stats = metrics.stats(command=command or None, serial=device)
    slow = metrics.slow_commands(serial=device)
    recent = metrics.recent(n=last_n, serial=device)

    return {
        "device": device,
        "stats": stats,
        "slow_commands": slow[-last_n:],
        "recent": recent,
        "total_recorded": metrics.count,
        "tool_stats": metrics.tool_stats(),
        "reservation_warnings": metrics.reservation_warnings(),
        "launch_to_screenshot": metrics.launch_latency_stats(),
    }


@global_tool
def _nerve_usage_stats_sync(
    server: str = "",
    since_hours: float = 0.0,
    include_rotated: bool = False,
    top_n: int = 50,
) -> dict[str, Any]:
    """Usage analytics over the shared MCP call log (~/.linc/mcp-calls.jsonl).

    The analysis layer the JSONL writer always lacked: per-tool call counts,
    success rate, and p50/p95 latency, plus the dormant (registered-but-unused)
    nerve tools and any test-fixture junk that leaked into the prod log.

    USE FOR: auditing which MCP tools are hot/cold/broken, spotting 0%-success
        tools (signature bugs), finding shelf-ware, measuring latency.
    NOT FOR: live device metrics (use nerve_metrics) or device discovery.
    TIP: the log is shared across nerve/crucible/spectra — omit ``server`` for
        the whole-host picture, or pass e.g. "linc-nerve" to scope per-tool
        stats. Dormancy is always computed against nerve's own tool registry.

    Args:
        server: Optional server filter (e.g. "linc-nerve"); empty = all servers.
        since_hours: Only count calls newer than this many hours; 0 = all history.
        include_rotated: Also read rotated mcp-calls.<epoch>.jsonl siblings.
        top_n: Cap the per_tool list to the N busiest tools (default 50).
    """
    from linc_nerve.core.usage_stats import compute_usage_stats

    registered = [t.name for t in _core.server._tool_manager.list_tools()]
    return compute_usage_stats(
        server=server or None,
        registered_tools=registered,
        since_hours=since_hours or None,
        include_rotated=include_rotated,
        top_n=top_n,
    )


@device_tool
def _nerve_inspect_sync(
    device: str,
    package: str,
    lines: int = 50,
) -> dict[str, Any]:
    """Non-invasive post-mortem snapshot of a process — pid, RSS, crash signature.

    Read pid / memory / foreground state of a running process without
    attaching a debugger — no DAP setup; works on a just-crashed process too.

    USE FOR: post-mortem inspection after a crash, non-invasive process
        state reads (pid/RSS/memory/foreground), THE FIRST-CLASS ANSWER to
        "did the app crash, and why" — ``crash_signature`` names exception,
        log line, first app frame, package (no nerve_logcat mining).
    NOT FOR: live breakpoint debugging via DAP (spectra_attach); stepping
        (spectra_step); ending a debug session (spectra_detach); streaming
        logs (nerve_logcat).

    Args:
        device: Device serial (from nerve_discover).
        package: Android package name or iOS bundle ID.
        lines: Log lines to include (default 50).

    Returns:
        inspect dict with pid, memory, crashes, and ``crash_signature``.
    """
    android, ios_ctrl = _core._ensure_initialized()
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    guard = _core._check_guard(device)
    if guard:
        return guard

    result: dict[str, Any] = {
        "device": device,
        "platform": platform,
        "timestamp": datetime.now(UTC).isoformat(),
        "package": package,
    }

    if platform == "android":
        _inspect_android(android, device, package, lines, result)
    else:
        _inspect_ios(ios_ctrl, device, package, lines, result)

    return result


def _get_android_pid(android: Any, serial: str, package: str) -> int | None:
    try:
        pidof_out = android._shell(serial, f"pidof -s {shlex.quote(package)}", timeout=5)
        if pidof_out.strip():
            return int(pidof_out.strip().split()[0])
    except Exception:
        pass

    try:
        ps_out = android._shell(serial, "ps -A -o PID,NAME", timeout=5)
        pids = _parse_pids(ps_out, package)
        if pids:
            return pids[0]
    except Exception:
        pass
    return None


def _get_android_logs(android: Any, serial: str, pid: int | None, lines: int) -> list[str]:
    try:
        if pid is not None:
            log_out = android._shell(serial, f"logcat -d -t {lines} --pid={pid}", timeout=10)
        else:
            log_out = android.logcat(serial, lines=lines)
        return log_out.strip().splitlines()[-lines:] if log_out.strip() else []
    except Exception:
        return []


def _inspect_android(
    android: AndroidController | Any, serial: str, package: str, lines: int, result: dict[str, Any]
) -> None:
    """Gather Android diagnostics via AndroidController._shell()."""
    _validate_shell_identifier(package, "package")
    pid = _get_android_pid(android, serial, package)
    result["process"] = {"pid": pid, "running": pid is not None, "package": package}

    try:
        activity_out = android._shell(
            serial,
            "dumpsys activity activities | grep -iE 'ResumedActivity'",
            timeout=5,
        )
        state = _parse_activity_state(activity_out)
        if state is not None:
            result["activity_state"] = state
    except Exception:
        pass

    if pid is not None:
        try:
            meminfo_out = android._shell(
                serial, f"dumpsys meminfo {shlex.quote(package)}", timeout=10
            )
            mem = _parse_meminfo(meminfo_out)
            if mem:
                result["memory"] = mem
        except Exception:
            pass

    log_lines = _get_android_logs(android, serial, pid, lines)
    result["recent_logs"] = log_lines
    result["crashes"] = _detect_crashes(log_lines, "android")
    result["crash_signature"] = build_crash_signature(result["crashes"], package)


def _is_physical_ios(serial: str) -> bool:
    """Check if serial is a physical iOS device (not a simulator UUID)."""
    return len(serial) <= 30 or not (len(serial) == 36 and serial.count("-") == 4)


def _devicectl_json(serial: str, info: str) -> dict[str, Any]:
    """Run ``devicectl device info <info>`` and return its parsed JSON output.

    The ``--json-output`` temp file is always removed, even when devicectl or
    the parse fails (security review, Info: leaked ``delete=False`` files).
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        subprocess.run(
            [
                "xcrun",
                "devicectl",
                "device",
                "info",
                info,
                "--device",
                serial,
                "--json-output",
                path,
            ],
            capture_output=True,
            timeout=15,
        )
        with open(path) as fh:
            return json.load(fh)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _devicectl_pid(serial: str, package: str) -> int | None:
    """Get PID for a bundle ID on a physical iOS device via devicectl."""
    app_url: str | None = None
    try:
        data = _devicectl_json(serial, "apps")
        for app in data.get("result", {}).get("apps", []):
            if app.get("bundleIdentifier") == package:
                app_url = app.get("url", "")
                break
    except Exception:
        return None

    if not app_url:
        return None

    try:
        data = _devicectl_json(serial, "processes")
        url_prefix = app_url.rstrip("/")
        for proc in data.get("result", {}).get("runningProcesses", []):
            exe = proc.get("executable", "")
            if exe.startswith(url_prefix):
                return proc.get("processIdentifier")
    except Exception:
        return None

    return None


def _get_ios_simulator_pid(serial: str, package: str) -> int | None:
    try:
        proc_out = subprocess.run(
            ["xcrun", "simctl", "spawn", serial, "launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in proc_out.stdout.splitlines():
            if package in line:
                parts = line.strip().split("\t")
                if len(parts) >= 3 and parts[0].strip().isdigit():
                    return int(parts[0].strip())
                match = re.search(r'"pid"\s*=\s*(\d+)', line)
                if match:
                    return int(match.group(1))
                break
    except Exception:
        pass
    return None


def _inspect_ios(
    ios_ctrl: Any, serial: str, package: str, lines: int, result: dict[str, Any]
) -> None:
    """Gather iOS diagnostics via IOSController and subprocess."""
    physical = _is_physical_ios(serial)
    pid = _devicectl_pid(serial, package) if physical else _get_ios_simulator_pid(serial, package)

    result["process"] = {"pid": pid, "running": pid is not None, "package": package}

    if pid is not None:
        try:
            ps_result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "rss=,vsz="],
                capture_output=True,
                text=True,
                timeout=5,
            )
            parts = ps_result.stdout.strip().split()
            if len(parts) >= 2:
                result["memory"] = {"rss_kb": int(parts[0]), "vsz_kb": int(parts[1])}
        except Exception:
            pass

    log_lines: list[str] = []
    try:
        log_out = ios_ctrl.get_logs(serial, lines=lines, filter=package)
        log_lines = log_out.strip().splitlines()[-lines:] if log_out.strip() else []
    except Exception:
        pass

    result["recent_logs"] = log_lines
    result["crashes"] = _detect_crashes(log_lines, "ios")
    result["crash_signature"] = build_crash_signature(result["crashes"], package)
