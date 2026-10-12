"""Tests for inspect_helpers._detect_crashes — 13 tests, TDD-first."""

from linc_nerve.core.inspect_helpers import (
    _detect_crashes,
    _parse_activity_state,
    _parse_meminfo,
    _parse_pids,
)


class TestDetectCrashesAndroid:
    def test_fatal_exception(self):
        lines = [
            "I/System: some normal log",
            "E/AndroidRuntime: FATAL EXCEPTION: main",
            "E/AndroidRuntime: Process: com.example.app, PID: 12345",
            "E/AndroidRuntime: java.lang.NullPointerException: Attempt to invoke",
        ]
        crashes = _detect_crashes(lines, "android")
        assert len(crashes) >= 1
        fatal = next((c for c in crashes if c["type"] == "FATAL_EXCEPTION"), None)
        assert fatal is not None
        assert fatal["line"] == lines[1]
        assert "context" in fatal
        # context should include surrounding lines
        assert lines[0] in fatal["context"] or lines[2] in fatal["context"]

    def test_anr(self):
        lines = [
            "I/ActivityManager: ANR in com.example.app (com.example.app/.MainActivity)",
            "I/ActivityManager: PID: 9876",
            "I/ActivityManager: Reason: Input dispatching timed out",
        ]
        crashes = _detect_crashes(lines, "android")
        anr = next((c for c in crashes if c["type"] == "ANR"), None)
        assert anr is not None
        assert "ANR in" in anr["line"]

    def test_native_signal(self):
        lines = [
            "F/libc: Fatal signal 11 (SIGSEGV), code 1",
            "F/DEBUG: *** *** *** *** *** *** *** ***",
        ]
        crashes = _detect_crashes(lines, "android")
        sig = next((c for c in crashes if c["type"] == "NATIVE_SIGNAL"), None)
        assert sig is not None

    def test_process_died(self):
        lines = [
            "W/ActivityManager: Process com.example.app (pid 4321) has died.",
        ]
        crashes = _detect_crashes(lines, "android")
        pd = next((c for c in crashes if c["type"] == "PROCESS_DIED"), None)
        assert pd is not None
        assert "has died" in pd["line"]

    def test_caused_by_chain(self):
        lines = [
            "E/AndroidRuntime: java.lang.RuntimeException: Unable to start activity",
            "E/AndroidRuntime: \tat android.app.ActivityThread.performLaunchActivity",
            "E/AndroidRuntime: Caused by: java.lang.NullPointerException",
            "E/AndroidRuntime: \tat com.example.app.MainActivity.onCreate",
        ]
        crashes = _detect_crashes(lines, "android")
        cb = next((c for c in crashes if c["type"] == "CAUSED_BY"), None)
        assert cb is not None
        assert "Caused by:" in cb["line"]

    def test_no_crashes_clean_log(self):
        lines = [
            "I/System: App started",
            "D/MainActivity: onCreate called",
            "V/ViewRoot: Finishing draw",
            "I/Choreographer: Skipped 2 frames",
        ]
        crashes = _detect_crashes(lines, "android")
        assert crashes == []

    def test_java_exception(self):
        lines = [
            "E/SomeTag: java.lang.IllegalArgumentException: bad argument",
            "E/SomeTag: \tat com.example.Foo.bar(Foo.java:42)",
        ]
        crashes = _detect_crashes(lines, "android")
        je = next((c for c in crashes if c["type"] == "JAVA_EXCEPTION"), None)
        assert je is not None
        assert "java.lang." in je["line"]

    def test_kotlin_exception(self):
        lines = [
            "E/SomeTag: kotlin.UninitializedPropertyAccessException: lateinit not initialized",
        ]
        crashes = _detect_crashes(lines, "android")
        ke = next((c for c in crashes if c["type"] == "KOTLIN_EXCEPTION"), None)
        assert ke is not None
        assert "kotlin." in ke["line"]

    def test_segfault(self):
        lines = [
            "F/DEBUG: signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0",
        ]
        crashes = _detect_crashes(lines, "android")
        assert any(c["type"] == "SEGFAULT" for c in crashes)

    def test_android_runtime_prefix(self):
        lines = [
            "E/AndroidRuntime: shutting down VM",
        ]
        crashes = _detect_crashes(lines, "android")
        assert any(c["type"] == "ANDROID_RUNTIME" for c in crashes)


class TestDetectCrashesIOS:
    def test_exc_bad_access(self):
        lines = [
            "Exception Type: EXC_BAD_ACCESS (SIGSEGV)",
            "Exception Subtype: KERN_INVALID_ADDRESS at 0x0000000000000000",
        ]
        crashes = _detect_crashes(lines, "ios")
        eba = next((c for c in crashes if c["type"] == "EXC_BAD_ACCESS"), None)
        assert eba is not None
        assert "EXC_BAD_ACCESS" in eba["line"]

    def test_watchdog_kill(self):
        lines = [
            "Exception Code: 0x8badf00d",
            "Termination Reason: Namespace SPRINGBOARD, Code 0x8badf00d",
        ]
        crashes = _detect_crashes(lines, "ios")
        wk = next((c for c in crashes if c["type"] == "WATCHDOG_KILL"), None)
        assert wk is not None
        assert "0x8badf00d" in wk["line"]

    def test_fatal_error(self):
        lines = [
            "Fatal error: Unexpectedly found nil while unwrapping an Optional value",
            "Current stack trace:",
        ]
        crashes = _detect_crashes(lines, "ios")
        fe = next((c for c in crashes if c["type"] == "FATAL_ERROR"), None)
        assert fe is not None
        assert "Fatal error" in fe["line"]

    def test_no_crashes_clean_log(self):
        lines = [
            "App launched",
            "viewDidLoad called",
            "Network request succeeded",
            "Memory warning received",
        ]
        crashes = _detect_crashes(lines, "ios")
        assert crashes == []


class TestDetectCrashesContext:
    def test_context_includes_surrounding_lines(self):
        # Build a 15-line log with a crash at line index 7
        prefix = [f"INFO line {i}" for i in range(7)]
        crash_line = "FATAL EXCEPTION: main"
        suffix = [f"POST line {i}" for i in range(5)]
        lines = [*prefix, crash_line, *suffix]

        crashes = _detect_crashes(lines, "android")
        assert len(crashes) >= 1

        fatal = next((c for c in crashes if c["type"] == "FATAL_EXCEPTION"), None)
        assert fatal is not None

        ctx = fatal["context"]
        # 5 lines before the crash line
        for before_line in prefix[-5:]:
            assert before_line in ctx, f"Expected '{before_line}' in context"

        # 2 lines after the crash line
        for after_line in suffix[:2]:
            assert after_line in ctx, f"Expected '{after_line}' in context"

        # The crash line itself should NOT be in context (it's in "line")
        # or it may be — spec says context = surrounding lines. Either is fine
        # as long as the 5 before + 2 after are present.


class TestParseActivityState:
    def test_resumed_activity(self):
        output = (
            "  mResumedActivity: ActivityRecord{abc com.example/.MainActivity t1}\n"
            "  mLastPausedActivity: ActivityRecord{def com.example/.OtherActivity t2}\n"
        )
        assert _parse_activity_state(output) == "RESUMED"

    def test_no_foreground_activity(self):
        output = (
            "  mLastPausedActivity: ActivityRecord{def com.example/.OtherActivity t2}\n"
            "  mFocusedStack=null\n"
        )
        assert _parse_activity_state(output) is None

    def test_api34_last_resumed(self):
        output = "  mLastResumedActivity: ActivityRecord{abc com.example/.MainActivity t1}\n"
        assert _parse_activity_state(output) == "RESUMED"

    def test_api35_top_resumed(self):
        """API 35 uses topResumedActivity= instead of mResumedActivity:"""
        output = (
            "    topResumedActivity=ActivityRecord"
            "{244109325 u0 com.lincmobile.level/.MainActivity t8414}\n"
        )
        assert _parse_activity_state(output) == "RESUMED"

    def test_api35_resumed_activity_no_m_prefix(self):
        """API 35 also shows ResumedActivity: without the m prefix."""
        output = (
            "  ResumedActivity: ActivityRecord"
            "{244109325 u0 com.lincmobile.level/.MainActivity t8414}\n"
        )
        assert _parse_activity_state(output) == "RESUMED"

    def test_malformed_input(self):
        output = "not valid dumpsys output at all !!@#$%"
        assert _parse_activity_state(output) is None

    def test_empty_input(self):
        assert _parse_activity_state("") is None


class TestParseMeminfo:
    def test_standard_aosp(self):
        output = (
            "App Summary:\n"
            "                       Pss(KB)                        Rss(KB)\n"
            "                        ------                         ------\n"
            "           Java Heap:     8192                          12288\n"
            "         Native Heap:     4096                           6144\n"
            "              Graphics:    2048                           2048\n"
            "         TOTAL PSS:    14336    TOTAL RSS:    20480\n"
        )
        result = _parse_meminfo(output)
        assert result["total_pss_kb"] == 14336
        assert result["java_heap_kb"] == 8192
        assert result["native_heap_kb"] == 4096
        assert result["graphics_kb"] == 2048

    def test_empty_output(self):
        assert _parse_meminfo("") == {}

    def test_partial_output(self):
        output = "         TOTAL PSS:     9999    TOTAL RSS:    15000\n"
        result = _parse_meminfo(output)
        assert result["total_pss_kb"] == 9999
        assert "java_heap_kb" not in result
        assert "native_heap_kb" not in result
        assert "graphics_kb" not in result


class TestParsePids:
    def test_single_process(self):
        ps_output = "  PID NAME\n  101 com.example.app\n  202 com.other.app\n"
        result = _parse_pids(ps_output, "com.example.app")
        assert result == [101]

    def test_multi_process(self):
        ps_output = (
            "  PID NAME\n"
            "  101 com.example.app\n"
            "  102 com.example.app:remote\n"
            "  103 com.example.app:service\n"
            "  204 com.other.app\n"
        )
        result = _parse_pids(ps_output, "com.example.app")
        assert sorted(result) == [101, 102, 103]

    def test_no_match(self):
        ps_output = "  PID NAME\n  201 com.other.app\n  202 com.another.app\n"
        result = _parse_pids(ps_output, "com.example.app")
        assert result == []

    def test_empty_output(self):
        result = _parse_pids("", "com.example.app")
        assert result == []

    def test_prefix_collision(self):
        ps_output = (
            "  PID NAME\n"
            "  101 com.example.app\n"
            "  102 com.example.app:remote\n"
            "  301 com.example.application\n"
        )
        result = _parse_pids(ps_output, "com.example.app")
        assert sorted(result) == [101, 102]

    def test_invalid_pid_value_error(self):
        ps_output = "  PID NAME\n  abc com.example.app\n  102 com.example.app:remote\n"
        result = _parse_pids(ps_output, "com.example.app")
        assert result == [102]
