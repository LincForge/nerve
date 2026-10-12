"""Operation metrics collector for linc-nerve.

Tracks timing, success/failure, and error rates for ADB commands,
per-tool success/fail counters, reservation leak detection, and
launch-to-first-screenshot latency.
Thread-safe ring buffer with statistical aggregation.
"""

from __future__ import annotations

import enum
import threading
import time
from collections import deque
from dataclasses import dataclass, field


class ErrorCategory(enum.StrEnum):
    """Structured error categories for ADB command failures."""

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    PERMISSION = "permission"
    DEVICE_NOT_FOUND = "device_not_found"
    UNKNOWN = "unknown"


def classify_error(exc: BaseException) -> ErrorCategory:
    """Classify an exception into a structured error category."""
    msg = str(exc).lower()

    if isinstance(exc, TimeoutError):
        return ErrorCategory.TIMEOUT
    if isinstance(exc, ConnectionResetError):
        return ErrorCategory.CONNECTION
    if isinstance(exc, OSError):
        if "connection" in msg or "broken pipe" in msg or "reset" in msg:
            return ErrorCategory.CONNECTION
        if "timed out" in msg or "timeout" in msg:
            return ErrorCategory.TIMEOUT
        if "permission" in msg:
            return ErrorCategory.PERMISSION
        return ErrorCategory.CONNECTION  # Most OSErrors are connection-related
    if "not found" in msg or "no device" in msg or "offline" in msg:
        return ErrorCategory.DEVICE_NOT_FOUND
    if "permission" in msg or "unauthorized" in msg:
        return ErrorCategory.PERMISSION
    return ErrorCategory.UNKNOWN


def _filter_by_serial(
    metrics: list[CommandMetric], serial: str | None
) -> list[CommandMetric]:
    """Restrict metrics to a single device serial; falsy serial = no filter.

    Extracted as a free function so the aggregation methods (stats/slow_commands/
    recent) gain device scoping via a single call instead of an inline branch —
    keeping each within its complexity budget (the serial branch lives here).
    """
    if not serial:
        return list(metrics)
    return [m for m in metrics if m.serial == serial]


@dataclass
class CommandMetric:
    """A single command execution metric."""

    command: str
    serial: str
    duration_ms: float
    success: bool
    timestamp: float = field(default_factory=time.time)
    error: str | None = None
    error_category: ErrorCategory | None = None


class MetricsCollector:
    """Thread-safe ring buffer of command metrics with aggregation.

    Stores the last ``maxlen`` metrics (default 500) and provides
    percentile statistics, error rates, and slow-command detection.
    """

    def __init__(self, maxlen: int = 500) -> None:
        self._buffer: deque[CommandMetric] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

        # Per-tool success/fail counters
        self._tool_counts: dict[str, dict[str, int]] = {}

        # Reservation tracking
        self._reservation_starts: dict[str, float] = {}
        self._reservation_durations: deque[dict] = deque(maxlen=100)

        # Launch-to-screenshot latency
        self._launch_timestamps: dict[str, float] = {}
        self._launch_latencies: deque[float] = deque(maxlen=100)

    def record(self, metric: CommandMetric) -> None:
        """Append a metric to the ring buffer."""
        with self._lock:
            self._buffer.append(metric)

    @property
    def count(self) -> int:
        """Total metrics in the buffer."""
        with self._lock:
            return len(self._buffer)

    def stats(self, command: str | None = None, serial: str | None = None) -> dict:
        """Compute aggregate statistics, optionally filtered by command/device.

        Returns a dict keyed by command name with p50, p95, p99, max,
        count, and error_rate for each command type. Pass ``serial`` to
        restrict the aggregation to a single device.
        """
        with self._lock:
            metrics = _filter_by_serial(list(self._buffer), serial)

        # Group by command
        groups: dict[str, list[CommandMetric]] = {}
        for m in metrics:
            if command and m.command != command:
                continue
            groups.setdefault(m.command, []).append(m)

        result: dict[str, dict] = {}
        for cmd, items in groups.items():
            durations = [m.duration_ms for m in items]
            errors = sum(1 for m in items if not m.success)
            sorted_d = sorted(durations)
            n = len(sorted_d)

            # Error breakdown by category
            error_breakdown: dict[str, int] = {}
            for m in items:
                if m.error_category is not None:
                    cat = m.error_category.value
                    error_breakdown[cat] = error_breakdown.get(cat, 0) + 1

            entry: dict = {
                "count": n,
                "p50": round(self._percentile(sorted_d, 0.50), 1),
                "p95": round(self._percentile(sorted_d, 0.95), 1),
                "p99": round(self._percentile(sorted_d, 0.99), 1),
                "max": round(max(sorted_d), 1),
                "error_rate": round(errors / n, 3) if n else 0.0,
            }
            if error_breakdown:
                entry["errors_by_category"] = error_breakdown
            result[cmd] = entry

        return result

    def slow_commands(
        self, threshold_ms: float = 2000, serial: str | None = None
    ) -> list[dict]:
        """Return metrics for commands slower than the threshold.

        Pass ``serial`` to restrict results to a single device.
        """
        with self._lock:
            metrics = _filter_by_serial(list(self._buffer), serial)

        return [
            {
                "command": m.command,
                "serial": m.serial,
                "duration_ms": m.duration_ms,
                "success": m.success,
                "error": m.error,
                "timestamp": m.timestamp,
            }
            for m in metrics
            if m.duration_ms > threshold_ms
        ]

    def recent(self, n: int = 20, serial: str | None = None) -> list[dict]:
        """Return the last n metrics, optionally restricted to one device."""
        with self._lock:
            items = _filter_by_serial(list(self._buffer), serial)[-n:]

        return [
            {
                "command": m.command,
                "serial": m.serial,
                "duration_ms": m.duration_ms,
                "success": m.success,
                "error": m.error,
            }
            for m in items
        ]

    # --- Tool-level counters ---

    def record_tool_call(self, tool: str, success: bool) -> None:
        """Increment success or fail counter for a tool."""
        with self._lock:
            if tool not in self._tool_counts:
                self._tool_counts[tool] = {"success": 0, "fail": 0}
            key = "success" if success else "fail"
            self._tool_counts[tool][key] += 1

    def tool_stats(self) -> dict[str, dict]:
        """Return per-tool counts with computed error_rate."""
        with self._lock:
            result = {}
            for tool, counts in self._tool_counts.items():
                total = counts["success"] + counts["fail"]
                result[tool] = {
                    "success": counts["success"],
                    "fail": counts["fail"],
                    "error_rate": round(counts["fail"] / total, 3) if total else 0.0,
                }
            return result

    # --- Reservation tracking ---

    def record_reserve(self, serial: str) -> None:
        """Record a device reservation start time."""
        with self._lock:
            self._reservation_starts[serial] = time.time()

    def record_release(self, serial: str) -> float | None:
        """Record a device release; return duration in seconds or None."""
        with self._lock:
            start = self._reservation_starts.pop(serial, None)
            if start is None:
                return None
            duration = time.time() - start
            self._reservation_durations.append(
                {"serial": serial, "duration_s": round(duration, 1)}
            )
            return duration

    def reservation_warnings(self, threshold_s: float = 1800.0) -> list[dict]:
        """Return reservations held longer than threshold (passive query)."""
        now = time.time()
        with self._lock:
            return [
                {"serial": s, "held_seconds": round(now - t, 1)}
                for s, t in self._reservation_starts.items()
                if now - t > threshold_s
            ]

    # --- Launch-to-screenshot latency ---

    def record_launch(self, serial: str) -> None:
        """Record a launch timestamp for latency tracking."""
        with self._lock:
            self._launch_timestamps[serial] = time.time()

    def record_screenshot(self, serial: str) -> float | None:
        """Record first screenshot after launch; return latency in ms or None."""
        with self._lock:
            start = self._launch_timestamps.pop(serial, None)
            if start is None:
                return None
            latency_ms = (time.time() - start) * 1000
            self._launch_latencies.append(latency_ms)
            return latency_ms

    def launch_latency_stats(self) -> dict:
        """Return launch-to-screenshot latency stats."""
        with self._lock:
            latencies = sorted(self._launch_latencies)
        if not latencies:
            return {"count": 0}
        return {
            "count": len(latencies),
            "p50": round(self._percentile(latencies, 0.50), 1),
            "p95": round(self._percentile(latencies, 0.95), 1),
            "recent": [round(v, 1) for v in list(self._launch_latencies)[-5:]],
        }

    @staticmethod
    def _percentile(sorted_data: list[float], p: float) -> float:
        """Compute a percentile from sorted data."""
        if not sorted_data:
            return 0.0
        n = len(sorted_data)
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * frac
