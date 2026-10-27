"""Action-log tools + the device_tool logging hook (linc-nerve-7ea).

A fake controller stands in for adb/WDA (``patch.object(_core, ...)``); the log
file lives under ``tmp_path``. No test spawns adb or talks to :8111.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from linc_nerve.core import action_log
from linc_nerve.mcp import _core, input_tools, recording_tools
from linc_nerve.mcp.actionlog_tools import (
    _nerve_actionlog_start_sync,
    _nerve_actionlog_stop_sync,
)


class FakeController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.elements_calls = 0
        self.elements_error: Exception | None = None
        self.tap_error: Exception | None = None

    def elements(self, serial: str) -> list[dict[str, Any]]:
        self.elements_calls += 1
        if self.elements_error is not None:
            raise self.elements_error
        return [
            {"class": "android.widget.FrameLayout", "bounds": "[0,0][1080,2400]"},
            {"class": "android.widget.Button", "text": "Next", "bounds": "[100,100][300,200]"},
        ]

    def tap(self, serial: str, x: int, y: int) -> None:
        self.calls.append(("tap", (serial, x, y)))
        if self.tap_error is not None:
            raise self.tap_error

    def long_press(self, serial: str, x: int, y: int, duration_ms: int) -> None:
        self.calls.append(("long_press", (serial, x, y, duration_ms)))

    def type_text(self, serial: str, text: str) -> None:
        self.calls.append(("type_text", (serial, text)))

    def press_key(self, serial: str, key: str) -> None:
        self.calls.append(("press_key", (serial, key)))

    def get_density(self, serial: str) -> dict[str, float]:
        return {"density_dpi": 480.0, "density_factor": 3.0}


@pytest.fixture
def ctrl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    fake = FakeController()
    action_log.REGISTRY.reset()
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(tmp_path))
    with (
        patch.object(_core, "_resolve_device", return_value=(object(), "android")),
        patch.object(_core, "_get_controller", return_value=fake),
        patch.object(_core, "_check_guard", return_value=None),
    ):
        yield fake
    action_log.REGISTRY.reset()


def _entries(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if json.loads(line)["type"] == "action"]


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


def test_tap_is_logged_with_target(ctrl: FakeController, tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    started = _nerve_actionlog_start_sync("S", str(path))
    assert started["success"] is True

    result = input_tools._nerve_tap_sync("S", 150, 150)
    assert result == {"success": True, "action": "tap", "x": 150, "y": 150}

    (entry,) = _entries(path)
    assert entry["tool"] == "nerve_tap"
    assert entry["args"] == {"x": 150, "y": 150}
    assert entry["ok"] is True
    assert entry["error"] is None
    assert entry["target"]["text"] == "Next"
    assert entry["seq"] == 1
    assert isinstance(entry["duration_ms"], float)
    assert "readonly" not in entry


def test_long_press_records_target(ctrl: FakeController, tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _nerve_actionlog_start_sync("S", str(path))
    input_tools._nerve_long_press_sync("S", 150, 150, duration_ms=1200)
    (entry,) = _entries(path)
    assert entry["args"] == {"x": 150, "y": 150, "duration_ms": 1200}
    assert entry["target"]["class_name"] == "android.widget.Button"


def test_secret_text_never_reaches_the_log(ctrl: FakeController, tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _nerve_actionlog_start_sync("S", str(path))

    result = input_tools._nerve_type_sync("S", "hunter2", secret=True)

    assert ("type_text", ("S", "hunter2")) in ctrl.calls
    assert "hunter2" not in json.dumps(result)
    assert "hunter2" not in path.read_text(encoding="utf-8")
    (entry,) = _entries(path)
    assert entry["args"] == {"text": "<redacted>", "secret": True, "length": 7}


def test_type_default_behaviour_unchanged(ctrl: FakeController) -> None:
    assert input_tools._nerve_type_sync("S", "hello") == {
        "success": True,
        "action": "type",
        "text": "hello",
    }
    assert inspect.signature(input_tools._nerve_type_sync).parameters["secret"].default is False


def test_no_session_is_a_passthrough(ctrl: FakeController) -> None:
    result = input_tools._nerve_tap_sync("S", 1, 2)
    assert result == {"success": True, "action": "tap", "x": 1, "y": 2}
    assert ctrl.elements_calls == 0


def test_controller_exception_logged_and_returned_unchanged(
    ctrl: FakeController, tmp_path: Path
) -> None:
    path = tmp_path / "a.jsonl"
    _nerve_actionlog_start_sync("S", str(path))
    ctrl.tap_error = RuntimeError("boom")

    result = input_tools._nerve_tap_sync("S", 150, 150)

    assert result == {"error": "Tap failed: boom"}
    (entry,) = _entries(path)
    assert entry["ok"] is False
    assert entry["error"] == "Tap failed: boom"


def test_raised_exception_is_logged_and_reraised(ctrl: FakeController, tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _nerve_actionlog_start_sync("S", str(path))
    with (
        patch.object(recording_tools, "get_device_sync", side_effect=ValueError("no device")),
        pytest.raises(ValueError, match="no device"),
    ):
        recording_tools._nerve_record_start_sync("S")
    (entry,) = _entries(path)
    assert entry["tool"] == "nerve_record_start"
    assert entry["ok"] is False
    assert entry["error"] == "no device"


def test_elements_failure_records_null_target(ctrl: FakeController, tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _nerve_actionlog_start_sync("S", str(path))
    ctrl.elements_error = RuntimeError("uiautomator killed")

    result = input_tools._nerve_tap_sync("S", 150, 150)

    assert result["success"] is True
    (entry,) = _entries(path)
    assert "target" in entry and entry["target"] is None


def test_unknown_device_records_null_target(ctrl: FakeController, tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _nerve_actionlog_start_sync("S", str(path))
    with patch.object(_core, "_resolve_device", return_value=(None, "Device 'S' not found")):
        result = input_tools._nerve_tap_sync("S", 150, 150)
    assert result == {"error": "Device 'S' not found"}
    (entry,) = _entries(path)
    assert entry["target"] is None
    assert entry["ok"] is False
    assert ctrl.elements_calls == 0


def test_readonly_tool_flagged(ctrl: FakeController, tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _nerve_actionlog_start_sync("S", str(path))
    input_tools._nerve_key_sync(device="S", key="BACK")
    input_tools._nerve_density_sync("S")
    key_entry, density_entry = _entries(path)
    assert key_entry["args"] == {"key": "BACK"}
    assert "readonly" not in key_entry
    assert density_entry["tool"] == "nerve_density"
    assert density_entry["readonly"] is True
    assert "target" not in density_entry


def test_bad_call_still_raises_type_error(ctrl: FakeController, tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _nerve_actionlog_start_sync("S", str(path))
    with pytest.raises(TypeError):
        input_tools._nerve_key_sync("S", "BACK", "extra", "args")
    (entry,) = _entries(path)
    assert entry["ok"] is False


def test_recording_marks_give_rec_offsets(
    ctrl: FakeController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = {"t": 10.0}
    monkeypatch.setattr(action_log, "_clock", lambda: now["t"])
    path = tmp_path / "a.jsonl"
    _nerve_actionlog_start_sync("S", str(path))

    class Dev:
        def start_recording(self, serial: str) -> None: ...

        def stop_recording(self, serial: str) -> str:
            return "/tmp/r.mp4"

    with patch.object(recording_tools, "get_device_sync", return_value=Dev()):
        recording_tools._nerve_record_start_sync("S")  # rec starts at t=10
        now["t"] = 12.0
        input_tools._nerve_tap_sync("S", 150, 150)
        now["t"] = 15.0
        input_tools._nerve_tap_sync("S", 150, 150)
        recording_tools._nerve_record_stop_sync("S")
        input_tools._nerve_tap_sync("S", 150, 150)

    entries = _entries(path)
    assert [e["tool"] for e in entries] == [
        "nerve_record_start",
        "nerve_tap",
        "nerve_tap",
        "nerve_record_stop",
        "nerve_tap",
    ]
    # offsets use the recording in effect when each action STARTED: record_start
    # began before the recording existed, record_stop began while it was live
    assert entries[0]["t_rec_s"] is None
    assert entries[1]["t_rec_s"] == 2.0
    assert entries[2]["t_rec_s"] == 5.0
    assert entries[3]["t_rec_s"] == 5.0
    assert entries[4]["t_rec_s"] is None


def test_record_fixed_marks_start_and_stop(ctrl: FakeController) -> None:
    class Dev:
        def start_recording(self, serial: str) -> None:
            pass

        def stop_recording(self, serial: str) -> str:
            assert action_log.REGISTRY.recording_offset("S", action_log._clock()) is not None
            return "/tmp/r.mp4"

    with (
        patch.object(recording_tools, "get_device_sync", return_value=Dev()),
        patch("time.sleep"),
    ):
        recording_tools._nerve_record_fixed_sync("S", 1)
    assert action_log.REGISTRY.recording_offset("S", action_log._clock()) is None


# ---------------------------------------------------------------------------
# Session tools
# ---------------------------------------------------------------------------


def test_start_twice_refuses(ctrl: FakeController, tmp_path: Path) -> None:
    _nerve_actionlog_start_sync("S", str(tmp_path / "a.jsonl"))
    second = _nerve_actionlog_start_sync("S", str(tmp_path / "b.jsonl"))
    assert "already active" in second["error"]
    assert not (tmp_path / "b.jsonl").exists()


def test_stop_without_session_refuses(ctrl: FakeController) -> None:
    assert "no action log" in _nerve_actionlog_stop_sync("S")["error"]


def test_start_and_stop_round_trip(ctrl: FakeController, tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    started = _nerve_actionlog_start_sync(
        "S", str(path), target_package="com.x", meta_json='{"run": 3}'
    )
    assert started["path"] == str(path)
    input_tools._nerve_tap_sync("S", 150, 150)

    stopped = _nerve_actionlog_stop_sync("S")

    assert stopped == {
        "success": True,
        "action": "actionlog_stop",
        "serial": "S",
        "path": str(path),
        "entries": 1,
        # target_package makes the session guarded (linc-nerve-2wh)
        "guard_refusals": 0,
    }
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert header["meta"] == {"run": 3, "target_package": "com.x"}
    # the actionlog tools themselves never appear in the log
    assert [e["tool"] for e in _entries(path)] == ["nerve_tap"]


@pytest.mark.parametrize("meta_json", ["not json", "[1, 2]"])
def test_start_rejects_bad_meta(ctrl: FakeController, tmp_path: Path, meta_json: str) -> None:
    result = _nerve_actionlog_start_sync("S", str(tmp_path / "a.jsonl"), meta_json=meta_json)
    assert result["validation_error"] is True
    assert "meta_json" in result["error"]
    assert action_log.REGISTRY.active("S") is None


@pytest.mark.parametrize("bad_path", ["", "/tmp/notes.txt"])
def test_start_rejects_non_jsonl_path(ctrl: FakeController, bad_path: str) -> None:
    result = _nerve_actionlog_start_sync("S", bad_path)
    assert result["validation_error"] is True
    assert ".jsonl" in result["error"]


def test_start_reports_unwritable_path(ctrl: FakeController, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    result = _nerve_actionlog_start_sync("S", str(blocker / "sub" / "a.jsonl"))
    assert "Cannot open action log" in result["error"]
    assert action_log.REGISTRY.active("S") is None


# ---------------------------------------------------------------------------
# One wrapper, every transport
# ---------------------------------------------------------------------------


def test_registry_maps_tap_to_logging_wrapper(ctrl: FakeController, tmp_path: Path) -> None:
    from linc_nerve.mcp import sse_server

    registry = sse_server._get_tool_registry()
    assert registry["nerve_tap"] is input_tools._nerve_tap_sync
    assert "nerve_actionlog_start" in registry
    assert "nerve_actionlog_stop" in registry

    path = tmp_path / "a.jsonl"
    registry["nerve_actionlog_start"](device="S", path=str(path))
    registry["nerve_tap"](device="S", x=150, y=150)
    assert len(_entries(path)) == 1


def test_mcp_wrapper_logs_exactly_once(ctrl: FakeController, tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _nerve_actionlog_start_sync("S", str(path))

    asyncio.run(_core.server.call_tool("nerve_tap", {"device": "S", "x": 150, "y": 150}))

    assert len(_entries(path)) == 1


def test_wrapper_preserves_name_and_signature() -> None:
    fn = input_tools._nerve_tap_sync
    assert fn.__name__ == "_nerve_tap_sync"
    assert list(inspect.signature(fn).parameters) == ["device", "x", "y", "prompt"]
    assert "Tap a single point" in (fn.__doc__ or "")


def test_actionlog_tools_are_not_wrapped() -> None:
    assert not hasattr(_nerve_actionlog_start_sync, "__wrapped__")
    assert not hasattr(_nerve_actionlog_stop_sync, "__wrapped__")


# ---------------------------------------------------------------------------
# Path confinement (linc-nerve-yhjn)
# ---------------------------------------------------------------------------


def test_start_outside_roots_refused_and_creates_no_file(
    ctrl: FakeController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NERVE_ACTIONLOG_ROOTS", raising=False)
    monkeypatch.setenv("LINC_HOME", str(tmp_path))
    target = tmp_path / "elsewhere" / "x.jsonl"
    result = _nerve_actionlog_start_sync("S", str(target))
    assert result.get("validation_error") is True
    assert str(tmp_path / "nerve" / "actionlogs") in result.get("error", "")
    assert not target.exists()


def test_start_relative_path_creates_under_default_root_mode_0700(
    ctrl: FakeController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NERVE_ACTIONLOG_ROOTS", raising=False)
    monkeypatch.setenv("LINC_HOME", str(tmp_path))
    result = _nerve_actionlog_start_sync("S", "run1.jsonl")
    assert result.get("success") is True
    created = tmp_path / "nerve" / "actionlogs" / "run1.jsonl"
    assert created.exists()
    root_dir = tmp_path / "nerve" / "actionlogs"
    assert root_dir.stat().st_mode & 0o777 == 0o700


def test_start_path_containing_null_byte_returns_validation_error(ctrl: FakeController) -> None:
    result = _nerve_actionlog_start_sync("S", "run\x001.jsonl")
    assert result.get("validation_error") is True


def test_symlink_refused_by_start_and_replay(
    ctrl: FakeController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(tmp_path))
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"type":"header"}\n', encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(outside)
    before_stat = outside.stat()
    result = _nerve_actionlog_start_sync("S", str(link))
    assert "error" in result
    assert result.get("validation_error") is not True
    assert outside.stat().st_mtime == before_stat.st_mtime

    from linc_nerve.mcp.actionlog_tools import _nerve_replay_sync

    replay_result = _nerve_replay_sync("S", str(link))
    assert replay_result.get("validation_error") is True


def test_symlinked_parent_dir_pointing_outside_refused(
    ctrl: FakeController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link_dir = root / "link_dir"
    link_dir.symlink_to(outside)
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(root))
    result = _nerve_actionlog_start_sync("S", str(link_dir / "test.jsonl"))
    assert result.get("validation_error") is True


def test_escape_parent_and_bad_suffix_refused(
    ctrl: FakeController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(root))
    escape_result = _nerve_actionlog_start_sync("S", str(root / ".." / "escape.jsonl"))
    assert escape_result.get("validation_error") is True
    suffix_result = _nerve_actionlog_start_sync("S", str(root / "bad.json"))
    assert suffix_result.get("validation_error") is True


def test_multiple_roots_accept_any_root_and_relative_lands_in_first(
    ctrl: FakeController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "root_a"
    b = tmp_path / "root_b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", f"{a}:{b}")
    path_b = b / "b.jsonl"
    res_b = _nerve_actionlog_start_sync("S", str(path_b))
    assert res_b.get("success") is True
    action_log.REGISTRY.stop("S")

    from linc_nerve.mcp.actionlog_tools import _nerve_replay_sync

    res_replay_b = _nerve_replay_sync("S", str(path_b))
    assert res_replay_b.get("validation_error") is not True

    neither = tmp_path / "neither.jsonl"
    res_neither = _nerve_actionlog_start_sync("S", str(neither))
    assert res_neither.get("validation_error") is True

    res_rel = _nerve_actionlog_start_sync("S", "rel.jsonl")
    assert res_rel.get("success") is True
    action_log.REGISTRY.stop("S")
    assert (a / "rel.jsonl").exists()


def test_replay_outside_roots_validation_error_before_read(
    ctrl: FakeController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(tmp_path / "root"))
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"type":"header"}\n', encoding="utf-8")
    with patch("linc_nerve.mcp.actionlog_tools._read_log_bytes") as mock_read:
        from linc_nerve.mcp.actionlog_tools import _nerve_replay_sync

        result = _nerve_replay_sync("S", str(outside))
        assert result.get("validation_error") is True
        mock_read.assert_not_called()


def test_load_log_reads_roots_from_the_daemon_environ_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from linc_nerve.mcp import actionlog_tools

    root = tmp_path / "root"
    root.mkdir()
    log = root / "run.jsonl"
    log.write_text('{"type": "header"}\n', encoding="utf-8")
    monkeypatch.setenv("LINC_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("NERVE_ACTIONLOG_ROOTS", raising=False)

    # Roots configured only on the _environ seam are honoured ...
    with patch.object(actionlog_tools, "_environ", {"NERVE_ACTIONLOG_ROOTS": str(root)}):
        assert actionlog_tools._load_log(str(log)) == [(1, {"type": "header"})]

    # ... and roots in the process environment are ignored when the seam says otherwise.
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(root))
    with patch.object(actionlog_tools, "_environ", {}):
        refused = actionlog_tools._load_log(str(log))
    assert refused == (
        "path must be under an allowed action-log root "
        f"({tmp_path / 'home' / 'nerve' / 'actionlogs'}), got {str(log)!r}"
    )


def test_replay_relative_log_path_resolves_under_first_root(
    ctrl: FakeController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from linc_nerve.mcp import actionlog_tools

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "rel.jsonl").write_text('{"type": "header"}\n', encoding="utf-8")
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", f"{a}:{b}")
    assert actionlog_tools._load_log("rel.jsonl") == [(1, {"type": "header"})]


def test_start_already_active_different_path_includes_stop_first_hint(
    ctrl: FakeController, tmp_path: Path
) -> None:
    path_a = tmp_path / "a.jsonl"
    path_b = tmp_path / "b.jsonl"
    _nerve_actionlog_start_sync("S", str(path_a), target_package="com.example.app")

    second = _nerve_actionlog_start_sync("S", str(path_b), target_package="com.example.app")
    assert "action log already active on S" in second["error"]
    assert "nerve_actionlog_stop" in second["hint"]


def test_start_rearm_returns_rearmed_and_updated_guard_info(
    ctrl: FakeController, tmp_path: Path
) -> None:
    path = tmp_path / "a.jsonl"
    first = _nerve_actionlog_start_sync("S", str(path), target_package="com.example.app")
    assert first["success"] is True
    assert "rearmed" not in first

    rearmed = _nerve_actionlog_start_sync(
        "S",
        str(path),
        target_package="com.example.app",
        allow_packages="com.example.auth",
        deny_words="delete account",
    )
    assert rearmed["success"] is True
    assert rearmed["action"] == "actionlog_start"
    assert rearmed["rearmed"] is True
    assert rearmed["guarded"] is True
    assert "com.example.auth" in rearmed["allowed_packages"]
    assert rearmed["deny_words"] == ["delete account"]


def test_start_and_rearm_results_are_exact(
    ctrl: FakeController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    monkeypatch.setattr(
        action_log, "_wallclock", lambda: datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
    )
    first = _nerve_actionlog_start_sync(
        "S", str(tmp_path / "a.jsonl"), target_package="com.example.app"
    )
    resolved = first["path"]
    builtin = action_log.BUILTIN_ALLOWED_PACKAGES
    assert first == {
        "success": True,
        "action": "actionlog_start",
        "serial": "S",
        "path": resolved,
        "started_at": "2026-10-03T12:00:00+00:00",
        "guarded": True,
        "target_package": "com.example.app",
        "allowed_packages": sorted({"com.example.app"} | builtin),
        "deny_words": list(action_log.DEFAULT_DENY_WORDS),
    }

    rearmed = _nerve_actionlog_start_sync(
        "S",
        resolved,
        target_package="com.example.app",
        allow_packages="com.example.auth",
        deny_words="delete account",
    )
    assert rearmed == {
        "success": True,
        "action": "actionlog_start",
        "serial": "S",
        "path": resolved,
        "started_at": "2026-10-03T12:00:00+00:00",
        "rearmed": True,
        "guarded": True,
        "target_package": "com.example.app",
        "allowed_packages": sorted({"com.example.app", "com.example.auth"} | builtin),
        "deny_words": ["delete account"],
    }


def test_already_active_error_is_exact_with_stop_first_hint(
    ctrl: FakeController, tmp_path: Path
) -> None:
    assert action_log.ALREADY_ACTIVE_HINT == (
        "Call nerve_actionlog_stop first to start a new log, or call "
        "nerve_actionlog_start with the active path and target_package to "
        "re-arm the guard in place."
    )
    first = _nerve_actionlog_start_sync(
        "S", str(tmp_path / "a.jsonl"), target_package="com.example.app"
    )
    second = _nerve_actionlog_start_sync(
        "S", str(tmp_path / "b.jsonl"), target_package="com.example.app"
    )
    assert second == {
        "error": f"action log already active on S: {first['path']}",
        "hint": action_log.ALREADY_ACTIVE_HINT,
    }
