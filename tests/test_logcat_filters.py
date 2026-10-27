"""Tests for nerve_logcat package=, grep=, and zero-line tag fallback (linc-nerve-jdt9)."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.ios import IOSController
from linc_nerve.mcp import server as mcp_mod


class _RecordingDevice:
    """Fake adbutils device that records every shell command and returns scripted output."""

    def __init__(self, handler: Any) -> None:
        self.commands: list[str] = []
        self._handler = handler

    def shell(self, cmd: str, timeout: int = 15) -> str:
        self.commands.append(cmd)
        if callable(self._handler):
            return str(self._handler(cmd))
        return str(self._handler.get(cmd, ""))


class _RecordingClient:
    def __init__(self, device: _RecordingDevice) -> None:
        self._device = device

    def device(self, serial: str) -> _RecordingDevice:
        # Every device command must target the requested serial (a None/other serial fails).
        if serial != "PIXEL8PRO":
            raise AssertionError(f"adb command sent to unexpected serial {serial!r}")
        return self._device


def _reset_singletons() -> None:
    if mcp_mod._device_mgr is not None:
        mcp_mod._device_mgr.close()
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = []
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    mcp_mod._android = mock_android
    mcp_mod._ios = mock_ios
    mcp_mod._state_mgr = None
    mcp_mod._metrics = None
    mcp_mod._device_mgr = None
    mcp_mod._emulator = None
    mcp_mod._cached_devices = []
    mcp_mod._cache_timestamp = 0.0


@pytest.fixture(autouse=True)
def _cleanup_singletons() -> Any:
    _reset_singletons()
    yield
    _reset_singletons()


def _setup_fake_android(handler: Any) -> list[str]:
    """Wire a real AndroidController backed by a command-recording fake adb shell."""
    _reset_singletons()

    rec_dev = _RecordingDevice(handler)
    ctrl = AndroidController()
    ctrl._scrcpy_available = False
    ctrl._client = _RecordingClient(rec_dev)

    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []

    mcp_mod._android = ctrl
    mcp_mod._ios = mock_ios
    mcp_mod._cached_devices = [
        NerveDevice(
            serial="PIXEL8PRO",
            platform=Platform.ANDROID,
            device_type=DeviceType.PHONE,
            model="Pixel 8 Pro",
            os_version="14",
        )
    ]
    mcp_mod._cache_timestamp = time.monotonic()
    return rec_dev.commands


def _setup_fake_ios() -> MagicMock:
    """Wire a mock IOSController with a cached simulator."""
    mcp_mod._android = MagicMock(spec=AndroidController)
    mcp_mod._android.discover.return_value = []
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    mcp_mod._ios = mock_ios
    mcp_mod._cached_devices = [
        NerveDevice(
            serial="UUID-123",
            platform=Platform.IOS,
            device_type=DeviceType.SIMULATOR,
            model="iPhone 15 Pro",
            os_version="17.5",
        )
    ]
    mcp_mod._cache_timestamp = time.monotonic()
    return mock_ios


def test_package_resolves_pid_and_passes_pid_flag() -> None:
    commands = _setup_fake_android(
        {
            "pidof -s dev.example.app": "4321\n",
            "logcat -d -t 50 --pid=4321": (
                "10-14 07:00:01.000  4321  4321 I ExampleApp: started\n"
                "10-14 07:00:02.000  4321  4321 D ExampleApp: ready\n"
            ),
        }
    )

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", package="dev.example.app")
    assert result["count"] == 2
    assert result["matched_via"] == "package"
    assert "hint" not in result
    assert result["lines"] == [
        "10-14 07:00:01.000  4321  4321 I ExampleApp: started",
        "10-14 07:00:02.000  4321  4321 D ExampleApp: ready",
    ]
    assert commands == [
        "pidof -s dev.example.app",
        "logcat -d -t 50 --pid=4321",
    ]


def test_package_not_running_returns_validation_error() -> None:
    commands = _setup_fake_android(
        {
            "pidof -s dev.example.app": "",
            "ps -A -o PID,NAME": "  1 init\n",
        }
    )

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", package="dev.example.app")
    assert result == {
        "error": (
            "package 'dev.example.app' is not running (no pid); launch it first or omit package"
        ),
        "validation_error": True,
    }
    assert not any("logcat" in cmd for cmd in commands)


def test_package_with_tag_filters_pid_then_exact_tag() -> None:
    commands = _setup_fake_android(
        {
            "pidof -s dev.example.app": "4321\n",
            "logcat -d -t 2000 --pid=4321": (
                "10-14 07:00:01.000  4321  4321 I OtherTag: ignore me\n"
                "10-14 07:00:02.000  4321  4321 I TargetTag: keep me\n"
            ),
        }
    )

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", package="dev.example.app", tag="TargetTag")
    assert result["count"] == 1
    assert result["matched_via"] == "package"
    assert result["lines"] == ["10-14 07:00:02.000  4321  4321 I TargetTag: keep me"]
    assert commands == [
        "pidof -s dev.example.app",
        "logcat -d -t 2000 --pid=4321",
    ]


@pytest.mark.parametrize("bad_pkg", ["bad;pkg", "-n com.evil", "com.app\n"])
def test_package_invalid_identifier_rejected_before_shell(bad_pkg: str) -> None:
    commands = _setup_fake_android({})
    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", package=bad_pkg)
    assert result.get("validation_error") is True
    assert "error" in result
    assert commands == []


def test_package_with_invalid_tag_rejected_before_shell() -> None:
    commands = _setup_fake_android({})
    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", package="dev.example.app", tag="bad;tag")
    assert result.get("validation_error") is True
    assert "error" in result
    assert commands == []


def test_ios_package_returns_validation_error() -> None:
    mock_ios = _setup_fake_ios()
    result = mcp_mod._nerve_logcat_sync("UUID-123", package="dev.example.app")
    assert result.get("validation_error") is True
    assert "android" in result["error"].lower()
    mock_ios.get_logs.assert_not_called()


@pytest.mark.parametrize(
    ("lines", "expected_window"),
    [
        (50, 2000),
        (150, 3000),
        (1500, 20000),
    ],
)
def test_grep_case_insensitive_and_window_math(lines: int, expected_window: int) -> None:
    log_body = "\n".join(
        [
            "10-14 07:00:01.000  100  100 I Noise: nothing here",
            "10-14 07:00:02.000  100  100 I App: Token=Alpha-1",
            "10-14 07:00:03.000  100  100 I App: TOKEN=ALPHA-2 [literal]",
            "10-14 07:00:04.000  100  100 I App: token=alpha-3",
        ]
    )
    commands = _setup_fake_android(lambda cmd: log_body)

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", lines=lines, grep="token=alpha")
    assert commands == [f"logcat -d -t {expected_window}"]
    assert result["matched_via"] == "grep"
    assert result["count"] == 3
    assert result["lines"] == [
        "10-14 07:00:02.000  100  100 I App: Token=Alpha-1",
        "10-14 07:00:03.000  100  100 I App: TOKEN=ALPHA-2 [literal]",
        "10-14 07:00:04.000  100  100 I App: token=alpha-3",
    ]


def test_grep_keeps_last_n_matches_and_is_literal_substring() -> None:
    log_body = "\n".join(f"line {i} [a-z]+ match" for i in range(10))
    _setup_fake_android(lambda cmd: log_body)

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", lines=3, grep="[A-Z]+")
    assert result["matched_via"] == "grep"
    assert result["count"] == 3
    assert result["lines"] == [
        "line 7 [a-z]+ match",
        "line 8 [a-z]+ match",
        "line 9 [a-z]+ match",
    ]


def test_grep_never_appears_in_device_commands() -> None:
    needle = "SuperSecretNeedle_42"
    commands = _setup_fake_android(
        {
            "pidof -s dev.example.app": "4321\n",
            "logcat -d -t 2000": "10-14 07:00:01.000  4321  4321 I App: supersecretneedle_42\n",
            "logcat -d -t 2000 --pid=4321": (
                "10-14 07:00:01.000  4321  4321 I App: supersecretneedle_42\n"
            ),
            "logcat -d -s MyTag": "10-14 07:00:01.000  4321  4321 I MyTag: supersecretneedle_42\n",
        }
    )

    r1 = mcp_mod._nerve_logcat_sync("PIXEL8PRO", grep=needle)
    r2 = mcp_mod._nerve_logcat_sync("PIXEL8PRO", package="dev.example.app", grep=needle)
    r3 = mcp_mod._nerve_logcat_sync("PIXEL8PRO", tag="MyTag", grep=needle)

    assert r1["count"] == 1
    assert r2["count"] == 1
    assert r3["count"] == 1
    assert commands
    for cmd in commands:
        assert needle.lower() not in cmd.lower(), f"grep leaked into command: {cmd!r}"


def test_ios_grep_filters_in_python_with_wider_window() -> None:
    mock_ios = _setup_fake_ios()
    mock_ios.get_logs.return_value = "ios noise\nios BenchTarget hit\n"

    result = mcp_mod._nerve_logcat_sync("UUID-123", lines=25, grep="benchtarget")
    mock_ios.get_logs.assert_called_once_with("UUID-123", lines=2000, filter="")
    assert result["matched_via"] == "grep"
    assert result["lines"] == ["ios BenchTarget hit"]


@pytest.mark.parametrize("bad_grep", ["has\nnewline", "has\x00nul", "has\ttab", "x" * 201])
def test_grep_rejects_control_chars_and_overlong_input(bad_grep: str) -> None:
    commands = _setup_fake_android({})
    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", grep=bad_grep)
    assert result.get("validation_error") is True
    assert "error" in result
    assert commands == []


@pytest.mark.parametrize("pkg_tag", ["dev.example.app", "dev.lincforge.benchtarget"])
def test_tag_zero_exact_lines_falls_back_to_package_pid(pkg_tag: str) -> None:
    commands = _setup_fake_android(
        {
            f"logcat -d -s {pkg_tag}": "",
            f"pidof -s {pkg_tag}": "4321\n",
            "logcat -d -t 50 --pid=4321": (
                "10-14 07:00:01.000  4321  4321 I BenchTarget: token=alpha-42\n"
            ),
        }
    )

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", tag=pkg_tag)
    assert result["matched_via"] == "package_fallback"
    assert result["count"] == 1
    assert result["lines"] == ["10-14 07:00:01.000  4321  4321 I BenchTarget: token=alpha-42"]
    assert "hint" in result
    assert "package_fallback" in result["hint"] or pkg_tag in result["hint"]
    assert commands == [
        f"logcat -d -s {pkg_tag}",
        f"pidof -s {pkg_tag}",
        "logcat -d -t 50 --pid=4321",
    ]


def test_tag_zero_exact_lines_falls_back_to_substring_match() -> None:
    commands = _setup_fake_android(
        {
            "logcat -d -s benchtarget": "",
            "logcat -d -t 5000": (
                "10-14 07:00:00.500  1000  1000 I ActivityManager: proc start\n"
                "10-14 07:00:01.000  4321  4321 I BenchTarget: token=alpha-42\n"
            ),
        }
    )

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", tag="benchtarget")
    assert result["matched_via"] == "substring_fallback"
    assert result["count"] == 1
    assert result["lines"] == ["10-14 07:00:01.000  4321  4321 I BenchTarget: token=alpha-42"]
    assert "hint" in result
    assert "substring_fallback" in result["hint"] or "substring" in result["hint"].lower()
    assert commands == [
        "logcat -d -s benchtarget",
        "logcat -d -t 5000",
    ]


def test_exact_tag_hit_issues_no_fallback_command() -> None:
    commands = _setup_fake_android(
        {
            "logcat -d -s BenchTarget": (
                "10-14 07:00:01.000  4321  4321 I BenchTarget: exact hit\n"
            ),
        }
    )

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", tag="BenchTarget")
    assert result["matched_via"] == "tag"
    assert result["count"] == 1
    assert "hint" not in result
    assert commands == ["logcat -d -s BenchTarget"]


def test_unfiltered_call_omits_matched_via() -> None:
    _setup_fake_android({"logcat -d -t 50": "line1\nline2\n"})
    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO")
    assert result["count"] == 2
    assert "matched_via" not in result
    assert "hint" not in result


def test_large_fallback_output_preserves_matched_via_and_hint() -> None:
    many_lines = "\n".join(
        f"10-14 07:00:01.{i:03d}  4321  4321 I BenchTarget: line {i}" for i in range(260)
    )
    _setup_fake_android(
        {
            "logcat -d -s benchtarget": "",
            "logcat -d -t 5000": many_lines,
        }
    )

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", lines=260, tag="benchtarget")
    assert result["count"] == 260
    assert result["matched_via"] == "substring_fallback"
    assert "logcat" in result
    assert "Full output (260 lines) saved to" in result["hint"]
    assert "benchtarget" in result["hint"]


def test_line_has_tag_formats() -> None:
    from linc_nerve.mcp.diagnostic_tools import _line_has_tag

    assert _line_has_tag("10-14 07:00:01.000  4321  4321 I BenchTarget: msg", "BenchTarget")
    assert _line_has_tag("I/BenchTarget( 4321): msg", "BenchTarget")
    assert _line_has_tag("E BenchTarget: msg", "BenchTarget")
    assert _line_has_tag("BenchTarget: msg", "BenchTarget")
    assert not _line_has_tag("--------- beginning of main", "BenchTarget")
    assert not _line_has_tag(
        "10-14 07:00:01.000  4321  4321 I Other: BenchTarget: msg", "BenchTarget"
    )


def test_package_shaped_tag_not_running_falls_through_to_substring() -> None:
    """A package-shaped tag whose app is not running must not report a package error."""
    commands = _setup_fake_android(
        {
            "logcat -d -s dev.gone.app": "",
            "pidof -s dev.gone.app": "",
            "ps -A -o PID,NAME": "  1 init\n",
            "logcat -d -t 5000": "10-14 07:00:01.000  4321  4321 I Probe: dev.gone.app crashed\n",
        }
    )

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", tag="dev.gone.app")
    assert "error" not in result
    assert result["matched_via"] == "substring_fallback"
    assert result["lines"] == ["10-14 07:00:01.000  4321  4321 I Probe: dev.gone.app crashed"]
    assert commands[0] == "logcat -d -s dev.gone.app"
    assert commands[-1] == "logcat -d -t 5000"
    assert not any("--pid=" in cmd for cmd in commands)


@pytest.mark.parametrize("bad_lines", [0, -5])
def test_lines_below_one_rejected_before_shell(bad_lines: int) -> None:
    commands = _setup_fake_android({})

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", lines=bad_lines, grep="x")
    assert result == {"error": "lines must be >= 1", "validation_error": True}
    assert commands == []


_PID_LINE = "10-14 07:00:01.000  4321  4321 I BenchTarget: token=alpha-42"


@pytest.mark.parametrize(
    ("kwargs", "handler", "matched_via"),
    [
        (
            {"package": "dev.example.app"},
            {"pidof -s dev.example.app": "4321\n", "logcat -d -t 50 --pid=4321": _PID_LINE},
            "package",
        ),
        (
            {"tag": "dev.example.app"},
            {
                "logcat -d -s dev.example.app": "",
                "pidof -s dev.example.app": "4321\n",
                "logcat -d -t 50 --pid=4321": _PID_LINE,
            },
            "package_fallback",
        ),
        (
            {"tag": "benchtarget"},
            {"logcat -d -s benchtarget": "", "logcat -d -t 5000": _PID_LINE},
            "substring_fallback",
        ),
        ({"tag": "BenchTarget"}, {"logcat -d -s BenchTarget": _PID_LINE}, "tag"),
        ({"grep": "alpha-42"}, {"logcat -d -t 2000": _PID_LINE}, "grep"),
    ],
)
def test_android_results_name_device_platform_and_match(
    kwargs: dict[str, str], handler: dict[str, str], matched_via: str
) -> None:
    _setup_fake_android(handler)

    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", **kwargs)
    assert result["device"] == "PIXEL8PRO"
    assert result["platform"] == "android"
    assert result["matched_via"] == matched_via
    assert result["lines"] == [_PID_LINE]
    assert ("hint" in result) is matched_via.endswith("_fallback")


def test_ios_tag_result_names_device_platform_and_tag_match() -> None:
    mock_ios = _setup_fake_ios()
    mock_ios.get_logs.return_value = "ios BenchTarget hit\n"

    result = mcp_mod._nerve_logcat_sync("UUID-123", lines=5, tag="BenchTarget")
    mock_ios.get_logs.assert_called_once_with("UUID-123", lines=5, filter="BenchTarget")
    assert result == {
        "lines": ["ios BenchTarget hit"],
        "count": 1,
        "device": "UUID-123",
        "platform": "ios",
        "matched_via": "tag",
    }


def test_ios_package_error_is_exact() -> None:
    _setup_fake_ios()
    result = mcp_mod._nerve_logcat_sync("UUID-123", package="dev.example.app")
    assert result == {
        "error": "package filter is Android-only; on iOS pass the bundle ID via tag=",
        "validation_error": True,
    }


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ValueError("bad filter"), {"error": "bad filter"}),
        (RuntimeError("boom"), {"error": "iOS log retrieval failed: boom"}),
    ],
)
def test_ios_log_errors_are_reported(exc: Exception, expected: dict[str, str]) -> None:
    mock_ios = _setup_fake_ios()
    mock_ios.get_logs.side_effect = exc
    assert mcp_mod._nerve_logcat_sync("UUID-123", tag="X") == expected


def test_invalid_package_error_names_the_package_field() -> None:
    _setup_fake_android({})
    result = mcp_mod._nerve_logcat_sync("PIXEL8PRO", package="bad;pkg")
    assert result["validation_error"] is True
    assert "package" in result["error"]
    assert "bad;pkg" in result["error"]


def test_read_pid_logcat_targets_serial_with_timeout() -> None:
    from linc_nerve.mcp.diagnostic_tools import _read_pid_logcat

    android = MagicMock()
    android._shell.return_value = "a\nb\n"
    assert _read_pid_logcat(android, "S1", 42, 7) == ["a", "b"]
    android._shell.assert_called_once_with("S1", "logcat -d -t 7 --pid=42", timeout=10)


def test_get_android_logs_pid_and_plain_paths() -> None:
    from linc_nerve.mcp.diagnostic_tools import _get_android_logs

    android = MagicMock()
    android._shell.return_value = "1\n2\n3\n"
    assert _get_android_logs(android, "S1", 42, 2) == ["2", "3"]
    android._shell.assert_called_once_with("S1", "logcat -d -t 2 --pid=42", timeout=10)

    android = MagicMock()
    android.logcat.return_value = "x\ny\nz\n"
    assert _get_android_logs(android, "S1", None, 2) == ["y", "z"]
    android.logcat.assert_called_once_with("S1", lines=2)

    android = MagicMock()
    android.logcat.side_effect = RuntimeError("adb gone")
    assert _get_android_logs(android, "S1", None, 2) == []


def test_filter_constants_drive_limits_and_windows() -> None:
    from linc_nerve.mcp import diagnostic_tools as dt

    assert dt._validate_grep("x" * dt._GREP_MAX_LEN) is None
    assert dt._validate_grep("x" * (dt._GREP_MAX_LEN + 1)) == {
        "error": (
            f"Invalid grep filter: must be printable characters and <= {dt._GREP_MAX_LEN} chars"
        ),
        "validation_error": True,
    }
    assert dt._read_window(10, "g") == dt._GREP_WINDOW_MIN
    assert dt._read_window(500, "g") == 500 * dt._GREP_WINDOW_FACTOR
    assert dt._read_window(10**6, "g") == dt._GREP_WINDOW_MAX
    assert dt._read_window(7, "") == 7
    assert dt._SUBSTRING_FALLBACK_WINDOW == 5000


@pytest.mark.parametrize(
    ("tag", "is_package"),
    [("dev.example.app", True), ("a.b_c.D9", True), ("benchtarget", False), ("1dev.app", False)],
)
def test_package_like_pattern(tag: str, is_package: bool) -> None:
    from linc_nerve.mcp.diagnostic_tools import _PACKAGE_LIKE_RE

    assert bool(_PACKAGE_LIKE_RE.fullmatch(tag)) is is_package


def test_line_tag_pattern_tolerates_leading_whitespace() -> None:
    from linc_nerve.mcp.diagnostic_tools import _LINE_TAG_RE, _line_has_tag

    assert _line_has_tag("   I/BenchTarget( 4321): ready", "BenchTarget") is True
    assert _line_has_tag("   I/BenchTarget( 4321): ready", "Other") is False
    assert _LINE_TAG_RE.match("I/BenchTarget( 4321): ready") is not None
