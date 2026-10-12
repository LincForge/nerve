"""Pure helper functions for nerve_inspect MCP tool.

These functions have zero I/O — they only parse strings.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Android crash patterns
# ---------------------------------------------------------------------------
_ANDROID_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("FATAL_EXCEPTION", re.compile(r"FATAL EXCEPTION")),
    ("ANR", re.compile(r"ANR in")),
    ("NATIVE_SIGNAL", re.compile(r"Signal \d+|libc.*Fatal signal")),
    ("SEGFAULT", re.compile(r"SEGV_")),
    ("PROCESS_DIED", re.compile(r"ActivityManager: Process .* has died")),
    ("CAUSED_BY", re.compile(r"Caused by:")),
    ("JAVA_EXCEPTION", re.compile(r"java\.lang\.\w+Exception")),
    ("KOTLIN_EXCEPTION", re.compile(r"kotlin\.\w+Exception")),
    ("ANDROID_RUNTIME", re.compile(r"E/AndroidRuntime:")),
]

# ---------------------------------------------------------------------------
# iOS crash patterns
# ---------------------------------------------------------------------------
_IOS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("UNCAUGHT_EXCEPTION", re.compile(r"Uncaught exception")),
    ("FATAL_ERROR", re.compile(r"Fatal error")),
    ("EXC_BAD_ACCESS", re.compile(r"EXC_BAD_ACCESS")),
    ("EXC_CRASH", re.compile(r"EXC_CRASH")),
    ("ASSERTION_FAILURE", re.compile(r"Assertion failure")),
    ("WATCHDOG_KILL", re.compile(r"0x8badf00d")),
]

_CONTEXT_BEFORE = 5
_CONTEXT_AFTER = 2


def _detect_crashes(
    log_lines: list[str],
    platform: str,
) -> list[dict[str, str | list[str]]]:
    """Scan *log_lines* for crash signatures and return structured crash records.

    Args:
        log_lines: Raw log lines (no newlines required).
        platform: ``"android"`` or ``"ios"``.

    Returns:
        A list of dicts, one per matched line, each containing:
        - ``type`` (str): pattern name, e.g. ``"FATAL_EXCEPTION"``
        - ``message`` (str): the matched log line
        - ``line`` (str): same as ``message`` (kept for symmetry)
        - ``context`` (list[str]): up to 5 lines before + 2 lines after the match

        If a single line matches multiple patterns, only the first pattern wins
        (deduplication by line index).
    """
    if platform == "android":
        patterns = _ANDROID_PATTERNS
    elif platform == "ios":
        patterns = _IOS_PATTERNS
    else:
        patterns = _ANDROID_PATTERNS + _IOS_PATTERNS

    matched_indices: set[int] = set()
    crashes: list[dict[str, str | list[str]]] = []

    for idx, line in enumerate(log_lines):
        if idx in matched_indices:
            continue
        for crash_type, pattern in patterns:
            if pattern.search(line):
                matched_indices.add(idx)
                start = max(0, idx - _CONTEXT_BEFORE)
                end = min(len(log_lines), idx + _CONTEXT_AFTER + 1)
                context = log_lines[start:idx] + log_lines[idx + 1 : end]
                crashes.append(
                    {
                        "type": crash_type,
                        "message": line,
                        "line": line,
                        "context": context,
                    }
                )
                break  # first matching pattern wins

    return crashes


# ---------------------------------------------------------------------------
# First-class crash signature (linc-nerve#98)
# ---------------------------------------------------------------------------
#
# `_detect_crashes` returns one record per matched log LINE. A real Android
# crash spreads its useful facts across several lines — the "FATAL EXCEPTION:
# main" marker line, then a "java.lang.XException: <message>" line, then "at
# ..." stack frames below it — so each lands as its OWN record. A caller
# previously had to read every record's `message`/`context` and hand-mine the
# exception class themselves (mobile-agent-bench b5 evidence: agents drove
# raw `nerve_logcat` instead of the already-existing `nerve_inspect` crash
# detection, because it never surfaced ONE named answer to "what crashed and
# why"). `build_crash_signature` closes that gap without a parallel
# crash-retrieval tool: it consolidates the existing `_detect_crashes` matches
# into a single structure.
_EXCEPTION_CLASS_RE = re.compile(r"\b([\w.]+Exception)\b")


def _extract_exception_class(text: str) -> str | None:
    """First ``<pkg>.<Name>Exception``-shaped token in *text*, or ``None``."""
    match = _EXCEPTION_CLASS_RE.search(text)
    return match.group(1) if match else None


def _extract_first_app_frame(lines: list[str], package: str) -> str | None:
    """First ``at ...`` stack-trace frame, preferring one that names *package*.

    Falls back to the first ``at ...`` frame at all when none mention the
    package (e.g. the crash unwound entirely inside framework code).
    """
    fallback: str | None = None
    for line in lines:
        marker = line.find("at ")
        if marker == -1:
            continue
        frame = line[marker + 3 :].strip()
        if fallback is None:
            fallback = frame
        if package and package in frame:
            return frame
    return fallback


def build_crash_signature(
    crashes: list[dict[str, str | list[str]]],
    package: str = "",
) -> dict[str, Any]:
    """Consolidate raw ``_detect_crashes()`` matches into ONE named signature.

    Returns ``{"status": "no_crash_found"}`` — an explicit, literal status,
    never an error and never an empty structure — when *crashes* is empty.

    Otherwise returns a dict naming the crash outright:
        - ``exception_class``: e.g. ``"java.lang.IllegalStateException"``,
          extracted from whichever matched record (message or context) first
          contains an ``...Exception`` token. Falls back to the raw pattern
          name (e.g. ``"FATAL_EXCEPTION"``, ``"ANR"``) when no line names an
          explicit exception class (native crashes, ANRs, process deaths).
        - ``message``: the full log line the exception class was found on.
        - ``package``: echoes the caller-supplied package/bundle ID.
        - ``first_app_frame``: the first stack frame naming *package*, or the
          first frame at all if none do.
        - ``type``: the underlying ``_detect_crashes`` pattern name of the
          first match, for callers that want the raw classification too.
    """
    if not crashes:
        return {"status": "no_crash_found"}

    all_lines: list[str] = []
    for crash in crashes:
        all_lines.append(str(crash["message"]))
        all_lines.extend(str(line) for line in crash.get("context", []) or [])

    exception_class: str | None = None
    message = str(crashes[0]["message"])
    for line in all_lines:
        found = _extract_exception_class(line)
        if found:
            exception_class = found
            message = line
            break
    if exception_class is None:
        exception_class = str(crashes[0]["type"])

    return {
        "exception_class": exception_class,
        "message": message,
        "package": package,
        "first_app_frame": _extract_first_app_frame(all_lines, package),
        "type": str(crashes[0]["type"]),
    }


# ---------------------------------------------------------------------------
# Activity state parsing
# ---------------------------------------------------------------------------
_RESUMED_PATTERN = re.compile(
    r"(?:m(?:Last)?ResumedActivity:|topResumedActivity=|ResumedActivity:)"
)


def _parse_activity_state(dumpsys_output: str) -> str | None:
    """Parse foreground activity state from dumpsys activity activities output.

    Returns "RESUMED" if mResumedActivity or mLastResumedActivity found,
    None otherwise.
    """
    if not dumpsys_output:
        return None
    if _RESUMED_PATTERN.search(dumpsys_output):
        return "RESUMED"
    return None


# ---------------------------------------------------------------------------
# Memory info parsing
# ---------------------------------------------------------------------------
_MEMINFO_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("total_pss_kb", re.compile(r"TOTAL PSS:\s+(\d+)")),
    ("java_heap_kb", re.compile(r"Java Heap:\s+(\d+)")),
    ("native_heap_kb", re.compile(r"Native Heap:\s+(\d+)")),
    ("graphics_kb", re.compile(r"Graphics:\s+(\d+)")),
]
_TOTAL_FALLBACK_PATTERN = re.compile(r"TOTAL:\s+(\d+)")


def _parse_meminfo(dumpsys_output: str) -> dict[str, int]:
    """Parse memory stats from dumpsys meminfo output. Label-anchored regex.

    Returns a dict with only the fields that were found. Empty dict for
    unparseable input.
    """
    if not dumpsys_output:
        return {}

    result: dict[str, int] = {}
    for field, pattern in _MEMINFO_PATTERNS:
        m = pattern.search(dumpsys_output)
        if m:
            result[field] = int(m.group(1))

    # Fallback for total_pss_kb if primary pattern not found
    if "total_pss_kb" not in result:
        m = _TOTAL_FALLBACK_PATTERN.search(dumpsys_output)
        if m:
            result["total_pss_kb"] = int(m.group(1))

    return result


# ---------------------------------------------------------------------------
# Process ID parsing
# ---------------------------------------------------------------------------


def _parse_pids(ps_output: str, package: str) -> list[int]:
    """Parse PIDs from 'ps -A -o PID,NAME' output.

    Matches both main process and sub-processes (package:remote, package:service).
    Returns list of PIDs; empty list if no match.
    """
    if not ps_output:
        return []

    pids: list[int] = []
    for line in ps_output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        pid_str, name = parts[0], parts[1]
        if name.split(":", 1)[0] == package:
            try:
                pids.append(int(pid_str))
            except ValueError:
                continue

    return pids
