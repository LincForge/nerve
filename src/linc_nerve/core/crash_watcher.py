"""Crash classification for the live log stream and iOS crash-report dirs.

Turns raw log lines / crash-report files into structured ``app_crashed`` events
so the SSE stream and the BVT runner learn about a mid-test crash immediately —
with root cause attached — instead of a misleading downstream assertion failure.

Pure functions (no daemon state); the SSE handler and crash watcher call in.
"""

from __future__ import annotations

import re
from pathlib import Path

from linc_nerve.core.inspect_helpers import _detect_crashes

# Packages named in Android crash lines we can attribute precisely.
_ANR_PACKAGE_RE = re.compile(r"ANR in ([\w.]+)")
_PROCESS_DIED_RE = re.compile(r"Process ([\w.]+) .*has died")

_IOS_REPORT_SUFFIXES = (".ips", ".crash")


def _extract_package(line: str) -> str | None:
    """Best-effort package name from an Android crash line (ANR / process death)."""
    for pattern in (_ANR_PACKAGE_RE, _PROCESS_DIED_RE):
        m = pattern.search(line)
        if m:
            return m.group(1)
    return None


def classify_log_line_crash(
    line: str,
    platform: str,
    package: str | None = None,
) -> dict[str, str] | None:
    """Classify a single log line as a crash, or return ``None``.

    Reuses the nerve_inspect crash-signature patterns. On a match, returns a
    structured event ``{"event": "app_crashed", "platform", "signature",
    "message"}`` — plus ``"package"`` when supplied or extractable from the line
    (ANR / process-death markers carry the package).
    """
    crashes = _detect_crashes([line], platform)
    if not crashes:
        return None
    crash = crashes[0]
    event: dict[str, str] = {
        "event": "app_crashed",
        "platform": platform,
        "signature": str(crash["type"]),
        "message": str(crash["message"]),
    }
    pkg = package or _extract_package(line)
    if pkg:
        event["package"] = pkg
    return event


def scan_ios_crash_reports(
    crash_dir: str | Path,
    bundle_id: str,
    seen: set[str] | None = None,
) -> list[dict[str, str]]:
    """Scan a simulator/device crash-report dir for new reports for ``bundle_id``.

    Returns one ``app_crashed`` event per previously-unseen ``.ips``/``.crash``
    file whose name or contents mention ``bundle_id``. ``seen`` (filenames) is
    updated in place so a polling watcher only reports each report once. A
    missing directory yields no events.
    """
    seen = seen if seen is not None else set()
    events: list[dict[str, str]] = []
    directory = Path(crash_dir)
    if not directory.is_dir():
        return events

    for report in sorted(directory.iterdir()):
        if report.suffix not in _IOS_REPORT_SUFFIXES or report.name in seen:
            continue
        try:
            text = report.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if bundle_id in report.name or bundle_id in text:
            seen.add(report.name)
            events.append(
                {
                    "event": "app_crashed",
                    "platform": "ios",
                    "signature": "IOS_CRASH_REPORT",
                    "bundle_id": bundle_id,
                    "report": report.name,
                }
            )
    return events
