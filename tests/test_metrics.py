"""Tests for command metrics collector."""

from __future__ import annotations

import threading
import time

from linc_nerve.core.metrics import CommandMetric, MetricsCollector


class TestCommandMetric:
    def test_defaults(self) -> None:
        m = CommandMetric(command="input", serial="ABC", duration_ms=5.0, success=True)
        assert m.command == "input"
        assert m.error is None
        assert m.timestamp > 0

    def test_with_error(self) -> None:
        m = CommandMetric(
            command="screenshot",
            serial="ABC",
            duration_ms=15000.0,
            success=False,
            error="timeout",
        )
        assert m.success is False
        assert m.error == "timeout"


class TestMetricsCollector:
    def test_record_and_count(self) -> None:
        mc = MetricsCollector()
        assert mc.count == 0

        mc.record(CommandMetric(command="input", serial="A", duration_ms=10.0, success=True))
        assert mc.count == 1

    def test_ring_buffer_maxlen(self) -> None:
        mc = MetricsCollector(maxlen=5)
        for i in range(10):
            mc.record(
                CommandMetric(command="input", serial="A", duration_ms=float(i), success=True)
            )
        assert mc.count == 5
        # Oldest entries evicted — recent should have durations 5-9
        recent = mc.recent(n=5)
        durations = [r["duration_ms"] for r in recent]
        assert durations == [5.0, 6.0, 7.0, 8.0, 9.0]

    def test_stats_single_command(self) -> None:
        mc = MetricsCollector()
        for d in [10.0, 20.0, 30.0, 40.0, 50.0]:
            mc.record(CommandMetric(command="input", serial="A", duration_ms=d, success=True))

        stats = mc.stats()
        assert "input" in stats
        s = stats["input"]
        assert s["count"] == 5
        assert s["p50"] == 30.0
        assert s["max"] == 50.0
        assert s["error_rate"] == 0.0

    def test_stats_filter_by_command(self) -> None:
        mc = MetricsCollector()
        mc.record(CommandMetric(command="input", serial="A", duration_ms=10.0, success=True))
        mc.record(CommandMetric(command="screenshot", serial="A", duration_ms=100.0, success=True))

        stats = mc.stats(command="screenshot")
        assert "screenshot" in stats
        assert "input" not in stats

    def test_stats_filter_by_serial(self) -> None:
        mc = MetricsCollector()
        mc.record(CommandMetric(command="input", serial="A", duration_ms=10.0, success=True))
        mc.record(CommandMetric(command="screenshot", serial="B", duration_ms=100.0, success=True))

        stats = mc.stats(serial="A")
        assert "input" in stats
        assert "screenshot" not in stats

    def test_slow_commands_filter_by_serial(self) -> None:
        mc = MetricsCollector()
        mc.record(
            CommandMetric(command="screenshot", serial="A", duration_ms=3000.0, success=True)
        )
        mc.record(
            CommandMetric(command="elements", serial="B", duration_ms=5000.0, success=False)
        )

        slow = mc.slow_commands(threshold_ms=2000, serial="B")
        assert len(slow) == 1
        assert slow[0]["serial"] == "B"

    def test_recent_filter_by_serial(self) -> None:
        mc = MetricsCollector()
        mc.record(CommandMetric(command="input", serial="A", duration_ms=10.0, success=True))
        mc.record(CommandMetric(command="screenshot", serial="B", duration_ms=20.0, success=True))
        mc.record(CommandMetric(command="elements", serial="A", duration_ms=30.0, success=True))

        recent = mc.recent(n=20, serial="A")
        assert len(recent) == 2
        assert all(r["serial"] == "A" for r in recent)

    def test_stats_error_rate(self) -> None:
        mc = MetricsCollector()
        mc.record(CommandMetric(command="input", serial="A", duration_ms=10.0, success=True))
        mc.record(
            CommandMetric(
                command="input",
                serial="A",
                duration_ms=10.0,
                success=False,
                error="timeout",
            )
        )

        stats = mc.stats()
        assert stats["input"]["error_rate"] == 0.5

    def test_stats_empty(self) -> None:
        mc = MetricsCollector()
        assert mc.stats() == {}

    def test_slow_commands(self) -> None:
        mc = MetricsCollector()
        mc.record(CommandMetric(command="input", serial="A", duration_ms=100.0, success=True))
        mc.record(
            CommandMetric(command="screenshot", serial="A", duration_ms=3000.0, success=True)
        )
        mc.record(CommandMetric(command="elements", serial="A", duration_ms=5000.0, success=False))

        slow = mc.slow_commands(threshold_ms=2000)
        assert len(slow) == 2
        assert slow[0]["command"] == "screenshot"
        assert slow[1]["command"] == "elements"

    def test_slow_commands_empty(self) -> None:
        mc = MetricsCollector()
        mc.record(CommandMetric(command="input", serial="A", duration_ms=100.0, success=True))
        assert mc.slow_commands(threshold_ms=2000) == []

    def test_recent(self) -> None:
        mc = MetricsCollector()
        for i in range(5):
            mc.record(
                CommandMetric(command=f"cmd{i}", serial="A", duration_ms=float(i), success=True)
            )

        recent = mc.recent(n=3)
        assert len(recent) == 3
        assert recent[0]["command"] == "cmd2"
        assert recent[2]["command"] == "cmd4"

    def test_thread_safety(self) -> None:
        """Multiple threads recording concurrently should not corrupt state."""
        mc = MetricsCollector(maxlen=500)
        errors: list[Exception] = []

        def record_batch(start: int) -> None:
            try:
                for i in range(100):
                    mc.record(
                        CommandMetric(
                            command="input",
                            serial=f"D{start}",
                            duration_ms=float(i),
                            success=True,
                        )
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=record_batch, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert mc.count == 500  # 5 threads * 100 = 500 = maxlen

    def test_percentile_single_value(self) -> None:
        mc = MetricsCollector()
        mc.record(CommandMetric(command="input", serial="A", duration_ms=42.0, success=True))
        stats = mc.stats()
        assert stats["input"]["p50"] == 42.0
        assert stats["input"]["p99"] == 42.0


class TestToolMetrics:
    def test_record_tool_call_increments(self) -> None:
        mc = MetricsCollector()
        mc.record_tool_call("nerve_screenshot", True)
        mc.record_tool_call("nerve_screenshot", True)
        mc.record_tool_call("nerve_screenshot", True)
        mc.record_tool_call("nerve_screenshot", False)

        stats = mc.tool_stats()
        assert stats["nerve_screenshot"]["success"] == 3
        assert stats["nerve_screenshot"]["fail"] == 1
        assert stats["nerve_screenshot"]["error_rate"] == 0.25

    def test_tool_stats_empty(self) -> None:
        mc = MetricsCollector()
        assert mc.tool_stats() == {}

    def test_record_reserve_release_duration(self) -> None:
        mc = MetricsCollector()
        mc.record_reserve("ABC123")
        time.sleep(0.01)
        duration = mc.record_release("ABC123")
        assert duration is not None
        assert duration > 0
        assert len(mc._reservation_durations) == 1

    def test_reservation_warnings_threshold(self) -> None:
        mc = MetricsCollector()
        # Inject a reservation that started 60s ago
        with mc._lock:
            mc._reservation_starts["OLD_DEV"] = time.time() - 60
        warnings = mc.reservation_warnings(threshold_s=10)
        assert len(warnings) == 1
        assert warnings[0]["serial"] == "OLD_DEV"
        assert warnings[0]["held_seconds"] >= 50

    def test_reservation_warnings_none_below_threshold(self) -> None:
        mc = MetricsCollector()
        mc.record_reserve("FRESH_DEV")
        warnings = mc.reservation_warnings(threshold_s=1800)
        assert warnings == []

    def test_launch_to_screenshot_latency(self) -> None:
        mc = MetricsCollector()
        mc.record_launch("ABC123")
        time.sleep(0.01)
        latency = mc.record_screenshot("ABC123")
        assert latency is not None
        assert latency > 0
        # Second call returns None (cleared)
        assert mc.record_screenshot("ABC123") is None

    def test_launch_latency_stats(self) -> None:
        mc = MetricsCollector()
        for _ in range(5):
            mc.record_launch("DEV")
            time.sleep(0.005)
            mc.record_screenshot("DEV")

        stats = mc.launch_latency_stats()
        assert stats["count"] == 5
        assert stats["p50"] > 0
        assert len(stats["recent"]) == 5
