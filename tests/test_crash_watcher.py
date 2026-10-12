"""Tests for crash_watcher — log-line crash classification + iOS report scan."""

from __future__ import annotations

from linc_nerve.core.crash_watcher import (
    classify_log_line_crash,
    scan_ios_crash_reports,
)


class TestClassifyLogLineCrash:
    def test_fatal_exception_is_classified(self) -> None:
        line = "E AndroidRuntime: FATAL EXCEPTION: main"
        event = classify_log_line_crash(line, "android")
        assert event is not None
        assert event["event"] == "app_crashed"
        assert event["signature"] == "FATAL_EXCEPTION"
        assert event["platform"] == "android"
        assert event["message"] == line

    def test_anr_extracts_package(self) -> None:
        event = classify_log_line_crash("ANR in com.sniperpulse.app (reason)", "android")
        assert event is not None
        assert event["signature"] == "ANR"
        assert event["package"] == "com.sniperpulse.app"

    def test_process_died_extracts_package(self) -> None:
        line = "I ActivityManager: Process com.example.app (pid 123) has died"
        event = classify_log_line_crash(line, "android")
        assert event is not None
        assert event["package"] == "com.example.app"

    def test_explicit_package_overrides_extraction(self) -> None:
        event = classify_log_line_crash(
            "E AndroidRuntime: FATAL EXCEPTION: main", "android", package="com.under.test"
        )
        assert event is not None
        assert event["package"] == "com.under.test"

    def test_ios_uncaught_exception(self) -> None:
        event = classify_log_line_crash("Uncaught exception NSInvalidArgument", "ios")
        assert event is not None
        assert event["signature"] == "UNCAUGHT_EXCEPTION"
        assert event["platform"] == "ios"

    def test_non_crash_line_returns_none(self) -> None:
        assert classify_log_line_crash("D MyApp: just a debug line", "android") is None

    def test_no_package_key_when_unknown(self) -> None:
        event = classify_log_line_crash("E AndroidRuntime: FATAL EXCEPTION: main", "android")
        assert event is not None
        assert "package" not in event


class TestScanIosCrashReports:
    def test_finds_new_report_for_bundle(self, tmp_path) -> None:
        report = tmp_path / "MyApp-2026-06-12.ips"
        report.write_text("Incident ... com.example.myapp ... EXC_BAD_ACCESS")
        events = scan_ios_crash_reports(tmp_path, "com.example.myapp")
        assert len(events) == 1
        assert events[0]["event"] == "app_crashed"
        assert events[0]["platform"] == "ios"
        assert events[0]["bundle_id"] == "com.example.myapp"
        assert events[0]["report"] == "MyApp-2026-06-12.ips"

    def test_matches_bundle_in_filename(self, tmp_path) -> None:
        (tmp_path / "com.example.myapp_crash.crash").write_text("unrelated body")
        events = scan_ios_crash_reports(tmp_path, "com.example.myapp")
        assert len(events) == 1

    def test_ignores_other_bundles(self, tmp_path) -> None:
        (tmp_path / "Other.ips").write_text("com.other.app crash")
        assert scan_ios_crash_reports(tmp_path, "com.example.myapp") == []

    def test_ignores_non_report_files(self, tmp_path) -> None:
        (tmp_path / "notes.txt").write_text("com.example.myapp")
        assert scan_ios_crash_reports(tmp_path, "com.example.myapp") == []

    def test_seen_set_suppresses_repeat(self, tmp_path) -> None:
        (tmp_path / "MyApp.ips").write_text("com.example.myapp EXC_CRASH")
        seen: set[str] = set()
        first = scan_ios_crash_reports(tmp_path, "com.example.myapp", seen)
        second = scan_ios_crash_reports(tmp_path, "com.example.myapp", seen)
        assert len(first) == 1
        assert second == []
        assert "MyApp.ips" in seen

    def test_missing_dir_returns_empty(self, tmp_path) -> None:
        assert scan_ios_crash_reports(tmp_path / "nope", "com.x") == []
